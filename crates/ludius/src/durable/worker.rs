//! The persona's day worker (port of scheduled.WorkerActivities and serve): one thread owns the desktop
//! (host agent, runner, session, DayRunner) for the reserved day; each activity hands it one job and
//! heartbeats from the day's ground-truth writes. The desk lock is held for the worker's life.
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use anyhow::{Result, anyhow, bail};
use chrono::NaiveDate;
use serde_json::json;
use sha2::{Digest, Sha256};
use temporalio_common::ActivityError;
use temporalio_macros::activities;
use temporalio_sdk::ApplicationFailure;
use temporalio_sdk::activities::ActivityContext;

use super::{DayEnd, DayInput, DayOwner, Done, Fire, Reservation, SlotInput, load_day, workflow_id};
use crate::calls::CapReached;
use crate::day::{DayRunner, Desk, ItemResult, Live};
use crate::nightly::worker::{Final, is_final};
use crate::planner::{DayPlan, World};
use crate::runner::{Clock, GroundTruth, now_ms};
use crate::tasks::Persona;

type Beat = tokio::sync::mpsc::UnboundedSender<()>;
type Job = Box<dyn FnOnce(&mut DayRunner<'_>, &mut dyn Desk) + Send>;

struct DayThread {
    key: String,
    jobs: mpsc::Sender<(Beat, Job)>,
    thread: JoinHandle<()>,
}

/// What the worker drives: one persona on one desktop.
pub struct Config {
    pub runs: PathBuf,
    pub persona: Persona,
    pub vm: String,
    pub agent: String,
    pub key: String,
    pub model: String,
}

#[derive(Clone)]
pub struct DayActivities {
    cfg: Arc<Config>,
    owner: DayOwner,
    day: Arc<Mutex<Option<DayThread>>>,
}

#[derive(Clone)]
struct Info {
    key: String,
    workflow_id: String,
    attempt: u32,
    activity: String,
}

fn info(ctx: &ActivityContext) -> Info {
    let i = ctx.info();
    let wid = i.workflow_id.clone().unwrap_or_default();
    Info { key: format!("{}/{wid}", i.namespace), workflow_id: wid, attempt: i.attempt, activity: i.activity_type.clone() }
}

fn failure(e: anyhow::Error) -> ActivityError {
    let kind = if e.is::<CapReached>() { "CapReached" } else if is_final(&e) { "ValueError" } else { "RuntimeError" };
    ActivityError::application(ApplicationFailure::builder(format!("{e:#}")).type_name(kind.to_string()).non_retryable(is_final(&e)).build())
}

fn done(r: &ItemResult) -> Done {
    Done { at: r.item.at.clone(), template: r.item.template.clone(), status: r.status.clone() }
}

impl DayActivities {
    pub fn new(cfg: Config) -> Self {
        let owner = DayOwner::new(&desk(&cfg.runs, &cfg.vm));
        Self { cfg: Arc::new(cfg), owner, day: Arc::default() }
    }

    /// Run `f` off the async runtime, recording a heartbeat for every ground-truth write it causes.
    async fn blocking<T: Send + 'static>(self: Arc<Self>, ctx: &ActivityContext, f: impl FnOnce(&Self, &Info, Beat) -> Result<T> + Send + 'static) -> Result<T, ActivityError> {
        let i = info(ctx);
        let (beat, mut beats) = tokio::sync::mpsc::unbounded_channel();
        let mut work = tokio::task::spawn_blocking(move || f(&self, &i, beat));
        loop {
            tokio::select! {
                r = &mut work => return r.map_err(anyhow::Error::from).and_then(|r| r).map_err(failure),
                Some(()) = beats.recv() => { let _ = ctx.record_heartbeat(()).await; }
            }
        }
    }

    fn reserved(&self, key: &str) -> Result<Option<Reservation>> {
        Ok(self.owner.read()?.filter(|r| r.owner == key))
    }

    /// Hand `f` to the day thread of `key`'s reserved day (started on first use) and wait for it.
    fn on_day<T: Send + 'static>(&self, key: &str, beat: Beat, f: impl FnOnce(&mut DayRunner<'_>, &mut dyn Desk) -> Result<T> + Send + 'static) -> Result<T> {
        let rec = self.owner.check(key)?.ok_or_else(|| Final("day not prepared; refusing to guess runner state".into()))?;
        let mut slot = self.day.lock().unwrap();
        if slot.as_ref().is_some_and(|d| d.key != key) {
            bail!(Final("another day still owns the local context".into()));
        }
        if slot.is_none() {
            *slot = Some(self.spawn(key, rec.day)?);
        }
        let (tx, rx) = mpsc::channel();
        let job: Job = Box::new(move |day, desk| _ = tx.send(f(day, desk)));
        slot.as_ref().unwrap().jobs.send((beat, job)).map_err(|_| anyhow!("the day thread stopped"))?;
        rx.recv().map_err(|_| anyhow!("the day thread stopped mid-activity"))?
    }

    fn spawn(&self, key: &str, inp: DayInput) -> Result<DayThread> {
        let (jobs, rx) = mpsc::channel();
        let (ready_tx, ready) = mpsc::channel();
        let cfg = self.cfg.clone();
        let thread = std::thread::Builder::new().name("day".into()).spawn(move || day_thread(&cfg, &inp, &ready_tx, rx))?;
        match ready.recv() {
            Ok(Ok(())) => Ok(DayThread { key: key.into(), jobs, thread }),
            Ok(Err(e)) => Err(e),
            Err(_) => Err(anyhow!("the day thread failed to start")),
        }
    }

    /// Stop the day thread (it closes the agent and the log) and drop the reservation of `release`.
    pub fn close(&self, release: Option<&str>) -> Result<()> {
        if let Some(d) = self.day.lock().unwrap().take() {
            drop(d.jobs);
            let _ = d.thread.join();
        }
        match release {
            Some(owner) => self.owner.release(owner),
            None => Ok(()),
        }
    }

    fn attempt(day: &DayRunner, i: &Info, s: &SlotInput) -> Result<NaiveDate> {
        if i.attempt > 1 {
            day.log.write(json!({"event": "item_retry", "host_ms": now_ms(), "at": s.item.at, "template": s.item.template, "attempt": i.attempt, "activity": i.activity}))?;
        }
        Ok(NaiveDate::parse_from_str(&s.date, "%Y-%m-%d")?)
    }
}

fn desk(runs: &std::path::Path, vm: &str) -> PathBuf {
    runs.join("locks").join(format!("{vm}.lock"))
}

/// The day thread: build the desktop for `inp`, report ready, then run jobs until the worker closes it.
fn day_thread(cfg: &Config, inp: &DayInput, ready: &mpsc::Sender<Result<()>>, jobs: mpsc::Receiver<(Beat, Job)>) {
    use crate::assist::Assist;
    use crate::decisions::DecisionsClient;
    use crate::hostagent::HostAgent;
    use crate::llm::{DEFAULT_MODEL, LlmClient};
    use crate::reply::Composer;
    use crate::runner::Runner;
    use crate::session::Session;
    let env = |k: &str| std::env::var(k).unwrap_or_default();
    let user = cfg.persona.user.clone();
    let agent = match (|| -> Result<HostAgent> {
        if inp.plan.persona != user {
            bail!(Final("worker cannot drive another persona's plan".into()));
        }
        // ponytail: the Hyper-V clock jump is not ported; time-jump days stay on the Python worker
        if inp.clock == Clock::TimeJump {
            bail!(Final("time-jump days need the Python worker (the clock jump is not in Rust yet)".into()));
        }
        let agent = HostAgent::new(&cfg.agent, &cfg.vm)?;
        let hello = agent.observe(false, 0, 0)?.hello.map(|h| h.user).unwrap_or_default();
        if !hello.rsplit('\\').next().unwrap_or("").eq_ignore_ascii_case(&user) {
            bail!(Final("worker requires the configured persona to be logged on; unknown identity fails closed".into()));
        }
        Ok(agent)
    })() {
        Ok(a) => a,
        Err(e) => {
            let _ = ready.send(Err(e));
            return;
        }
    };
    let built = (|| -> Result<_> {
        let decider = DecisionsClient::new(&cfg.key, Some(cfg.model.clone()), None)?;
        let log = GroundTruth::open(&World::new(&cfg.runs).day_log(&user, &inp.plan.date))?;
        let llm = |model_var: &str| LlmClient::new(&cfg.key, Some(std::env::var(model_var).as_deref().unwrap_or(DEFAULT_MODEL)));
        let planner = |on: bool| if on && !cfg.key.is_empty() { llm("LUDIUS_PLANNER_MODEL").map(Some) } else { Ok(None) };
        let planner = (planner(env("LUDIUS_ASSIST") != "0")?, planner(true)?);
        let react = if cfg.key.is_empty() || env("LUDIUS_CHAT_HOST").is_empty() { None } else { Some((llm("LUDIUS_REACT_MODEL")?, DecisionsClient::new(&cfg.key, None, None)?)) };
        let digest = Sha256::digest(format!("{}/{user}/{}", cfg.vm, inp.plan.date));
        Ok((decider, log, planner, react, u32::from_be_bytes(digest[..4].try_into()?) as u64, crate::planner::templates()?, crate::policy::Policy::from_env()?))
    })();
    let (decider, log, planner, react, seed, templates, policy) = match built {
        Ok(b) => b,
        Err(e) => {
            let _ = ready.send(Err(e));
            return;
        }
    };
    let password = std::env::var(format!("LUDIUS_PERSONA_PASSWORD_{}", user.to_uppercase())).ok().filter(|p| !p.is_empty()).unwrap_or_else(|| env("LUDIUS_PERSONA_PASSWORD"));
    let lifecycle = |op| agent.lifecycle(op, "", "");
    let session = Session::new(&agent, &log, password, Some(&lifecycle));
    let assist = planner.0.map(|llm| Assist { llm });
    let composer = planner.1.map(|llm| Composer { llm, narrative: cfg.runs.join("world").join("narrative.yaml"), confirm: true });
    let pacing = if inp.clock == Clock::TimeJump { Clock::Compressed } else { inp.clock };
    let mut runner = Runner::new(&agent, &decider, cfg.persona.clone(), &log, pacing, policy.clone(), Some(seed), assist, composer);
    let world = World::new(&cfg.runs);
    let mut reactor = react.as_ref().map(|(llm, d)| {
        let mut r = crate::react::Reactor::new(cfg.persona.clone(), templates.clone(), policy.clone(), llm, cfg.runs.join("world").join("narrative.yaml"), &log).with_decider(d);
        r.existing = Box::new(|| crate::world_state::existing_files(&world, &user));
        r
    });
    let mut day = DayRunner::new(templates, &log, inp.clock, seed);
    day.reactor = reactor.as_mut().map(|r| r as &mut dyn crate::day::React);
    let _ = ready.send(Ok(()));
    let mut desk = Live { runner: &mut runner, session: &session };
    for (beat, job) in jobs {
        *log.beat.lock().unwrap() = Some(Box::new(move || _ = beat.send(())));
        job(&mut day, &mut desk);
        *log.beat.lock().unwrap() = None;
    }
}

#[activities]
impl DayActivities {
    #[activity(name = "load_scheduled_day")]
    pub async fn load_scheduled_day(self: Arc<Self>, ctx: ActivityContext, fire: Fire) -> Result<DayInput, ActivityError> {
        self.blocking(&ctx, move |me, _, _| load_day(&me.cfg.runs, &me.cfg.persona.user, &fire)).await
    }

    #[activity(name = "day_prepare")]
    pub async fn day_prepare(self: Arc<Self>, ctx: ActivityContext, inp: DayInput) -> Result<(), ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            if inp.plan.persona != me.cfg.persona.user {
                bail!(Final("plan persona differs from this worker".into()));
            }
            if i.workflow_id != workflow_id(&inp.plan) {
                bail!(Final("plan date/persona differs from its workflow id".into()));
            }
            me.owner.claim(&i.key, &inp)?;
            me.on_day(&i.key, beat, move |day, _| day.start(&inp.plan))
        })
        .await
    }

    #[activity(name = "day_end")]
    pub async fn day_end(self: Arc<Self>, ctx: ActivityContext, e: DayEnd) -> Result<(), ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            if me.reserved(&i.key)?.is_none() {
                return Ok(());
            }
            me.on_day(&i.key, beat, move |day, _| {
                day.results = e.done.iter().map(|d| {
                    let item = e.plan.items.iter().find(|it| it.at == d.at && it.template == d.template).ok_or_else(|| Final(format!("{} {} is not in the plan", d.at, d.template)))?;
                    Ok(ItemResult { item: item.clone(), status: d.status.clone() })
                }).collect::<Result<_>>()?;
                day.finish(&e.plan)
            })?;
            me.close(Some(&i.key))
        })
        .await
    }

    #[activity(name = "day_abort")]
    pub async fn day_abort(self: Arc<Self>, ctx: ActivityContext) -> Result<(), ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            if me.reserved(&i.key)?.is_none() {
                return Ok(());
            }
            if me.day.lock().unwrap().is_some() {
                // the abort finishes whatever the log does
                let _ = me.on_day(&i.key, beat, |day, _| day.log.write(json!({"event": "day_aborted", "host_ms": now_ms()})));
            }
            me.close(Some(&i.key))
        })
        .await
    }

    #[activity(name = "canary")]
    pub async fn canary(self: Arc<Self>, ctx: ActivityContext, plan: DayPlan) -> Result<Vec<Done>, ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            let runs = me.cfg.runs.clone();
            me.on_day(&i.key, beat, move |day, desk| {
                let app = |t: &str| day.templates.get(t).map(|t| t.app.clone());
                let apps: BTreeSet<String> = plan.items.iter().filter_map(|i| app(&i.template)).collect();
                let admission = crate::paths::data().join("admission.yaml");
                let health = crate::canary::run(&day.templates, &admission, desk, day.log, &runs.join("health.json"), &apps, &mut |s| println!("{s}"))?;
                let mut out = vec![];
                for it in &plan.items {
                    if let Some(a) = app(&it.template).filter(|a| health.get(a).is_some_and(|s| s == "drift")) {
                        day.log.write(json!({"event": "skipped_drift", "host_ms": now_ms(), "at": it.at, "task": it.template, "app": a}))?;
                        out.push(Done { at: it.at.clone(), template: it.template.clone(), status: "skipped_drift".into() });
                    }
                }
                Ok(out)
            })
        })
        .await
    }

    #[activity(name = "slot")]
    pub async fn slot(self: Arc<Self>, ctx: ActivityContext, s: SlotInput) -> Result<Vec<Done>, ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            let i = i.clone();
            me.on_day(&i.key.clone(), beat, move |day, desk| {
                let date = Self::attempt(day, &i, &s)?;
                let mut interrupts = s.interrupts.clone();
                Ok(day.slot(desk, date, &s.item, &mut interrupts, s.jump_at.as_deref())?.iter().map(done).collect())
            })
        })
        .await
    }

    #[activity(name = "break_start")]
    pub async fn break_start(self: Arc<Self>, ctx: ActivityContext, s: SlotInput) -> Result<(), ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            let i = i.clone();
            me.on_day(&i.key.clone(), beat, move |day, desk| {
                let date = Self::attempt(day, &i, &s)?;
                day.begin_break(desk, date, &s.item)
            })
        })
        .await
    }

    #[activity(name = "break_end")]
    pub async fn break_end(self: Arc<Self>, ctx: ActivityContext, s: SlotInput) -> Result<Done, ActivityError> {
        self.blocking(&ctx, move |me, i, beat| {
            let i = i.clone();
            me.on_day(&i.key.clone(), beat, move |day, desk| {
                let date = Self::attempt(day, &i, &s)?;
                Ok(done(&day.end_break(desk, date, &s.item)?))
            })
        })
        .await
    }
}

/// Serve the persona's day queue until the process stops (scheduled.serve): one activity at a time.
pub async fn serve(address: &str, namespace: &str, cfg: Config) -> Result<()> {
    use temporalio_client::{Client, ClientOptions, Connection, ConnectionOptions};
    use temporalio_sdk::runtime::worker_tuner::{FixedSizeSlotSupplier, TunerHolder};
    use temporalio_sdk::{Runtime, Worker, WorkerOptions};
    let _desk = super::desk_lock(&desk(&cfg.runs, &cfg.vm))?;
    let queue = super::task_queue(&cfg.persona.user);
    let runtime = Runtime::from_current_tokio(Default::default())?;
    let url = if address.contains("://") { address.to_string() } else { format!("http://{address}") };
    let connection = Connection::connect(ConnectionOptions::new(url::Url::parse(&url)?).build()).await?;
    let client = Client::new(connection, ClientOptions::new(namespace).build())?;
    let acts = DayActivities::new(cfg);
    let tuner = TunerHolder::builder()
        .workflow_task_slot_supplier(FixedSizeSlotSupplier::new(100))
        .activity_task_slot_supplier(FixedSizeSlotSupplier::new(1))
        .local_activity_task_slot_supplier(FixedSizeSlotSupplier::new(1))
        .nexus_task_slot_supplier(FixedSizeSlotSupplier::new(1))
        .build();
    let options = WorkerOptions::new(queue).tuner(tuner).register_workflow::<super::ScheduledDay>()?.register_workflow::<super::PersonaDay>()?.register_activities(acts.clone()).build();
    let r = Worker::new(&runtime, client, options)?.run().await;
    acts.close(None)?;
    Ok(r?)
}
