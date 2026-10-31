//! `ludius range warp|unwarp|sweep` (port of cli.py _range_warp, _range_unwarp, _range_sweep): plays N simulated
//! weekdays on every persona as durable days, jumping the range clocks under the clock lease, and puts the clocks
//! back afterwards (or, for a warp that died, the sweeper does once the lease goes stale).
use std::collections::BTreeMap;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Datelike, NaiveDate, NaiveTime, TimeZone, Utc, Weekday};
use chrono_tz::Tz;
use serde_json::{Value, json};

use crate::clocklease::{self, Lease};
use crate::durable::{DayOwner, workflow_id};
use crate::planner::{DayPlan, World};
use crate::rangeclock::RangeClock;
use crate::rangerun::{Supervisor, vm_of};

pub fn lease_path(runs: &Path) -> PathBuf {
    runs.join("locks").join("clock-range.json")
}

fn offsets(got: &BTreeMap<u32, f64>) -> Value {
    let r = |v: f64| (v * 10.0).round() / 10.0;
    json!(got.iter().map(|(k, v)| (k.to_string(), json!(r(*v)))).collect::<serde_json::Map<_, _>>())
}

fn tail(s: &str, n: usize) -> String {
    let c: Vec<char> = s.chars().collect();
    c[c.len().saturating_sub(n)..].iter().collect()
}

/// Reset the range clocks now and drop the lease; 3 when any VM failed (the lease stays for the sweeper).
pub fn unwarp(clock: impl FnOnce() -> Result<RangeClock>, lease: &Path) -> i32 {
    match clock().and_then(|c| c.reset()) {
        Err(e) => {
            eprintln!("unwarp FAILED, range clocks may still be jumped: {e:#}");
            3
        }
        Ok(got) => {
            let _ = std::fs::remove_file(lease);
            println!("{}", json!({"offsets_s": offsets(&got)}));
            0
        }
    }
}

/// The sweeper (systemd timer): reset the clocks of a warp that stopped renewing its lease.
pub fn sweep(clock: impl FnOnce() -> Result<RangeClock>, lease: &Path, now: Option<DateTime<Utc>>) -> Result<i32> {
    // a warp's lease-and-jump waits for this sweep, so the lease is still the one checked
    clocklease::exclusive(lease, || {
        let Some(rec) = clocklease::orphan(lease, now) else { return 0 };
        match clock().and_then(|c| c.reset()) {
            Err(e) => {
                eprintln!("sweep: range clock lease orphaned (pid {}), reset FAILED, retrying: {e:#}", rec["pid"]);
                3
            }
            Ok(got) => {
                let _ = std::fs::remove_file(lease);
                println!("{}", json!({"swept": rec, "offsets_s": offsets(&got)}));
                0
            }
        }
    })
}

/// The weekdays a warp plays, or why the arguments are refused.
pub fn dates(start: Option<&str>, days: usize, today: NaiveDate) -> std::result::Result<Vec<NaiveDate>, String> {
    let bad = || "warp needs --start YYYY-MM-DD (a future date) and --days 1-7".to_string();
    let start = NaiveDate::parse_from_str(start.ok_or_else(bad)?, "%Y-%m-%d").map_err(|_| bad())?;
    if !(1..=7).contains(&days) {
        return Err(bad());
    }
    if start <= today {
        return Err("warp is forward only: --start must be after today".into());
    }
    Ok(start.iter_days().filter(|d| !matches!(d.weekday(), Weekday::Sat | Weekday::Sun)).take(days).collect())
}

/// Planning and playing days: Temporal and the planner live, fakes in tests.
pub trait Days {
    /// Every persona's plan for `date`, or why it has none.
    fn plan(&self, date: NaiveDate) -> BTreeMap<String, Result<DayPlan>>;
    /// Play the days to their end (the supervisor kept stepping); None when `stop` was raised first.
    fn play(&self, plans: &BTreeMap<String, DayPlan>, canary: bool, sup: &mut Supervisor, stop: &AtomicBool) -> Result<Option<BTreeMap<String, Value>>>;
    /// Terminate the day workflows; the ones not confirmed closed.
    fn stop(&self, wids: &[String]) -> Vec<String>;
}

pub struct Warp<'a> {
    pub runs: PathBuf,
    pub users: Vec<String>,
    pub tz: Tz,
    pub plan_at: NaiveTime,
    pub clock: RangeClock,
    pub lease: Lease,
    pub sup: Supervisor,
    pub days: &'a dyn Days,
    /// Raised by SIGTERM or Ctrl-C.
    pub stop: &'a AtomicBool,
}

#[derive(Debug)]
struct Interrupted;

impl std::fmt::Display for Interrupted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("interrupted")
    }
}

impl std::error::Error for Interrupted {}

#[derive(Default)]
struct State {
    failed: usize,
    playing: Vec<String>,
    probed: bool,
}

impl Warp<'_> {
    fn journal(&self) -> PathBuf {
        self.runs.join("range").join("warp.jsonl")
    }

    fn note(&self, rec: Value) {
        let mut line = json!({"at": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()});
        line.as_object_mut().unwrap().extend(rec.as_object().cloned().unwrap_or_default());
        let j = self.journal();
        let _ = std::fs::create_dir_all(j.parent().unwrap());
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&j) {
            let _ = writeln!(f, "{line}");
        }
        println!("{line}");
    }

    fn local(&self, d: NaiveDate, t: NaiveTime) -> Result<DateTime<Tz>> {
        self.tz.from_local_datetime(&d.and_time(t)).earliest().with_context(|| format!("{d} {t} does not exist in {}", self.tz))
    }

    fn jump(&mut self, at: DateTime<Tz>) -> Result<()> {
        let path = self.lease.path.clone();
        clocklease::exclusive(&path, || -> Result<()> {
            self.lease.hold()?;
            let got = self.clock.set(at.to_utc())?;
            let (lo, hi) = got.values().fold((f64::MAX, f64::MIN), |(lo, hi), &v| (lo.min(v), hi.max(v)));
            let logical = at.to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
            self.note(json!({"event": "clock_set", "logical": logical, "skew_s": ((hi - lo) * 10.0).round() / 10.0, "offsets_s": offsets(&got)}));
            Ok(())
        })?
    }

    fn halt(&self) -> Result<()> {
        if self.stop.load(Ordering::SeqCst) { Err(anyhow!(Interrupted)) } else { Ok(()) }
    }

    // ponytail: a stop is seen between steps and while days play, not inside a jump or the planner's LLM calls.
    fn days(&mut self, dates: &[NaiveDate], st: &mut State) -> Result<()> {
        let narrative = World::new(&self.runs).narrative_path;
        if !narrative.exists() {
            bail!("no company narrative at {}; run `ludius plan narrative --date {}`", narrative.display(), dates[0]);
        }
        let mut held = vec![];
        for u in &self.users {
            if let Some(r) = DayOwner::new(&self.runs.join("locks").join(format!("{}.lock", vm_of(u)))).read()? {
                held.push(format!("{u} by {}", r.owner));
            }
        }
        if !held.is_empty() {
            // a day left from an interrupted run would fail that persona's first warp day
            bail!("desktops still reserved: {}; inspect, clear with `ludius reservation`", held.join("; "));
        }
        self.sup.step()?;
        for &d in dates {
            self.halt()?;
            self.jump(self.local(d.pred_opt().context("date")?, self.plan_at)?)?;
            let mut plans = BTreeMap::new();
            let mut why = vec![];
            for (u, got) in self.days.plan(d) {
                match got {
                    Ok(p) => drop(plans.insert(u, p)),
                    Err(e) => {
                        let e = format!("{e:#}").chars().take(300).collect::<String>();
                        why.push(format!("{u}: {e}"));
                        self.note(json!({"event": "no_plan", "persona": u, "date": d, "error": e}));
                    }
                }
            }
            let items: serde_json::Map<_, _> = plans.iter().map(|(u, p): (&String, &DayPlan)| (u.clone(), json!(p.items.len()))).collect();
            self.note(json!({"event": "planned", "date": d, "items": items}));
            if plans.is_empty() {
                bail!("no persona got a plan for {d}: {}", why.first().map_or("see no_plan", String::as_str));
            }
            st.failed += self.users.len().saturating_sub(plans.len());
            plans.retain(|_, p| !p.items.is_empty());
            let Some(first) = plans.values().flat_map(|p| &p.items).map(|i| NaiveTime::parse_from_str(&i.at, "%H:%M")).collect::<Result<Vec<_>, _>>()?.into_iter().min() else {
                continue;
            };
            self.halt()?;
            self.jump(self.local(d, first)? - chrono::Duration::minutes(10))?;
            st.playing = plans.values().map(workflow_id).collect();
            // the first played day probes each app first (runs/health.json); a drifted app's items are skipped
            let Some(results) = self.days.play(&plans, !st.probed, &mut self.sup, self.stop)? else { bail!(Interrupted) };
            st.probed = true;
            st.playing.clear();
            for (u, res) in results {
                st.failed += usize::from(res.as_str().is_some_and(|s| s.starts_with("failed")));
                self.note(json!({"event": "day", "persona": u, "date": d, "result": res}));
            }
        }
        Ok(())
    }

    /// The warp: 0 all days played, 1 a persona day without a plan or failed, 3 clock reset failed, 4 days not
    /// stopped; an error when the warp itself failed (after the clocks were put back).
    pub fn run(mut self, dates: &[NaiveDate]) -> Result<i32> {
        let mut st = State::default();
        let failure = match self.days(dates, &mut st) {
            Ok(()) => None,
            Err(e) if e.is::<Interrupted>() => {
                self.note(json!({"event": "interrupted"}));
                None
            }
            Err(e) => {
                self.note(json!({"event": "warp_failed", "error": tail(&format!("{e:#}"), 1500)}));
                Some(e)
            }
        };
        let open = if st.playing.is_empty() { vec![] } else { self.days.stop(&st.playing) };
        if !open.is_empty() {
            self.note(json!({"event": "days_not_stopped", "workflows": open, "fix": "temporal workflow terminate --workflow-id"}));
        }
        self.sup.stop();
        let mut reset_failed = false;
        if self.lease.held() {
            match self.clock.reset() {
                Ok(got) => {
                    self.lease.release();
                    let (lo, hi) = got.values().fold((f64::MAX, f64::MIN), |(lo, hi), &v| (lo.min(v), hi.max(v)));
                    self.note(json!({"event": "clock_reset", "skew_s": ((hi - lo) * 10.0).round() / 10.0, "offsets_s": offsets(&got)}));
                }
                Err(e) => {
                    reset_failed = true;
                    self.note(json!({"event": "clock_reset_failed", "error": tail(&format!("{e:#}"), 1500), "fix": "ludius range unwarp"}));
                }
            }
        }
        if reset_failed {
            eprintln!("warp: clock reset FAILED, run `ludius range unwarp`; warp error: {}", failure.map_or("None".into(), |e| format!("{e:#}")));
            return Ok(3);
        }
        if !open.is_empty() {
            eprintln!("warp: day workflows not confirmed stopped, terminate them: {open:?}");
            return Ok(4);
        }
        if let Some(e) = failure {
            return Err(e);
        }
        if st.failed > 0 {
            eprintln!("warp: {} persona day(s) without a plan or failed, see {}", st.failed, self.journal().display());
        }
        Ok(i32::from(st.failed > 0))
    }
}

/// The live days: the nightly planner for plans, Temporal PersonaDay workflows on the persona workers.
pub struct Live {
    pub rt: tokio::runtime::Runtime,
    pub temporal: String,
    pub runs: PathBuf,
    pub users: Vec<String>,
    pub tz: Tz,
    pub key: String,
    pub model: Option<String>,
}

/// nightly.ensure_days for `users` on `date` (the plans `range up --today` and the warp need).
pub fn ensure(runs: &Path, users: &[String], key: &str, model: Option<&str>, date: NaiveDate) -> BTreeMap<String, Result<crate::nightly::Planned>> {
    use crate::nightly::{Sources, ensure_days, generator, worker::personas};
    let world = World::new(runs);
    let decider = || crate::decisions::DecisionsClient::new(key, None, None);
    let inbox = |u: &str| crate::mail::newest(u, "inbox");
    let src = (|| -> Result<Sources> {
        Ok(Sources {
            personas: personas()?.into_iter().filter(|p| users.contains(&p.user)).collect(),
            templates: crate::planner::templates()?,
            policy: crate::policy::Policy::from_env()?,
            inbox: &inbox,
            decider: (!key.is_empty()).then_some(&decider as _),
        })
    })();
    let src = match src {
        Ok(s) => s,
        Err(e) => return users.iter().map(|u| (u.clone(), Err(anyhow!("plan sources: {e:#}")))).collect(),
    };
    let generate = generator(key, model, &world, &src);
    ensure_days(&generate, &world, &src.personas, &src, date)
}

impl Days for Live {
    fn plan(&self, date: NaiveDate) -> BTreeMap<String, Result<DayPlan>> {
        ensure(&self.runs, &self.users, &self.key, self.model.as_deref(), date).into_iter().map(|(u, r)| (u, r.and_then(|p| crate::planner::load_plan(Path::new(&p.path), false)))).collect()
    }

    fn play(&self, plans: &BTreeMap<String, DayPlan>, canary: bool, sup: &mut Supervisor, stop: &AtomicBool) -> Result<Option<BTreeMap<String, Value>>> {
        self.rt.block_on(async {
            let client = crate::schedules::connect(&self.temporal, "default").await?;
            let one = |plan: &DayPlan| {
                let client = client.clone();
                let plan = plan.clone();
                async move {
                    Ok::<Value, anyhow::Error>(match crate::schedules::play(&client, plan, self.tz, crate::runner::Clock::Compressed, canary).await? {
                        Ok(done) => json!(done.iter().map(|d| format!("{} {} {}", d.at, d.template, d.status)).collect::<Vec<_>>()),
                        Err(e) => json!(format!("failed: {e}")),
                    })
                }
            };
            let all = futures::future::join_all(plans.values().map(one));
            tokio::pin!(all);
            let mut tick = 0u32;
            loop {
                tokio::select! {
                    r = &mut all => return plans.keys().cloned().zip(r).map(|(u, r)| Ok((u, r?))).collect::<Result<_>>().map(Some),
                    _ = tokio::time::sleep(std::time::Duration::from_secs(1)) => {
                        if stop.load(Ordering::SeqCst) {
                            return Ok(None);
                        }
                        tick += 1;
                        if tick.is_multiple_of(5) {
                            sup.step()?;
                        }
                    }
                }
            }
        })
    }

    fn stop(&self, wids: &[String]) -> Vec<String> {
        use temporalio_client::errors::WorkflowInteractionError;
        use temporalio_client::{UntypedWorkflow, WorkflowExecutionStatus, WorkflowTerminateOptions};
        self.rt.block_on(async {
            let Ok(client) = crate::schedules::connect(&self.temporal, "default").await else { return wids.to_vec() };
            let mut left = vec![];
            for wid in wids {
                let h = client.get_workflow_handle::<UntypedWorkflow>(wid.clone());
                let _ = h.terminate(WorkflowTerminateOptions::builder().reason("warp stopped".to_string()).build()).await;
                match h.describe(Default::default()).await {
                    Ok(d) if d.status() != WorkflowExecutionStatus::Running => {}
                    Err(WorkflowInteractionError::NotFound(_)) => {}
                    _ => left.push(wid.clone()),
                }
            }
            left
        })
    }
}

#[cfg(test)]
mod tests;
