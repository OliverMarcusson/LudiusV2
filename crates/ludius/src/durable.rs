//! Durable persona days on Temporal (port of durable.py and scheduled.py): the PersonaDay workflow plays a
//! plan slot by slot through activities on the persona's worker; ScheduledDay loads the day a schedule fires
//! and starts it as a child. Same names, task queues and JSON payloads as the Python worker.
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use serde::{Deserialize, Serialize};
use temporalio_common::RetryPolicy;
use temporalio_common::search_attributes::SearchAttributeKey;
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityCancellationType, ActivityOptions, ApplicationFailure, ChildWorkflowOptions, WorkflowCancellationToken, WorkflowContext, WorkflowIdReusePolicy, WorkflowResult,
    WorkflowTermination,
};

use crate::day::{back_at, slots};
use crate::nightly::worker::{Final, persona_name};
use crate::planner::{BREAK, DayPlan, PlanItem, World, load_plan};
use crate::runner::Clock;

pub mod worker;

pub const ITEM_TIMEOUT: Duration = Duration::from_secs(2 * 3600);
pub const QUICK: Duration = Duration::from_secs(5 * 60);
/// Python's NOT_RETRYABLE: our Final errors carry the type ValueError.
const NOT_RETRYABLE: [&str; 3] = ["KeyError", "ValueError", "CapReached"];

fn d300() -> u64 {
    300
}

fn d30() -> u64 {
    30
}

fn realtime() -> Clock {
    Clock::Realtime
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DayInput {
    pub plan: DayPlan,
    pub clock: Clock,
    pub utc_offset_s: i64,
    #[serde(default = "d300")]
    pub heartbeat_s: u64,
    #[serde(default = "d30")]
    pub retry_s: u64,
    #[serde(default)]
    pub canary: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScheduledInput {
    pub persona: String,
    pub tz: String,
    #[serde(default = "realtime")]
    pub clock: Clock,
    #[serde(default)]
    pub canary: bool,
    #[serde(default)]
    pub day_offset: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SlotInput {
    pub date: String,
    pub item: PlanItem,
    #[serde(default)]
    pub interrupts: Vec<PlanItem>,
    #[serde(default)]
    pub jump_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Done {
    pub at: String,
    pub template: String,
    pub status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DayEnd {
    pub plan: DayPlan,
    pub done: Vec<Done>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fire {
    pub schedule: ScheduledInput,
    pub occurrence: DateTime<Utc>,
}

pub fn workflow_id(plan: &DayPlan) -> String {
    format!("day-{}-{}", plan.persona, plan.date)
}

pub fn task_queue(persona: &str) -> String {
    format!("ludius-day-{persona}")
}

/// The persona's day plan for `date`, which must exist and be that persona's day.
pub fn scheduled_plan(runs: &Path, persona: &str, date: &str) -> Result<DayPlan> {
    persona_name(persona)?;
    if NaiveDate::parse_from_str(date, "%Y-%m-%d").map(|d| d.format("%Y-%m-%d").to_string()).ok().as_deref() != Some(date) {
        bail!(Final("plan date must be YYYY-MM-DD".into()));
    }
    let path = World::new(runs).plan_path(persona, date);
    if !path.exists() {
        bail!("no plan for {persona} on {date}: {} (run `ludius plan` for that date)", path.display());
    }
    let plan = load_plan(&path, false)?;
    if plan.persona != persona || plan.date != date {
        bail!(Final(format!("{} is the plan of {} on {}, not {persona} on {date}", path.display(), plan.persona, plan.date)));
    }
    Ok(plan)
}

/// scheduled.load_day: the plan of the fired day (occurrence in the schedule's zone plus day_offset).
pub fn load_day(runs: &Path, persona: &str, fire: &Fire) -> Result<DayInput> {
    if fire.schedule.persona != persona {
        bail!(Final("schedule persona differs from this worker".into()));
    }
    let tz: chrono_tz::Tz = fire.schedule.tz.parse().map_err(|e| Final(format!("{e}")))?;
    let at = fire.occurrence.with_timezone(&tz) + chrono::Duration::days(fire.schedule.day_offset);
    let plan = scheduled_plan(runs, persona, &at.format("%Y-%m-%d").to_string())?;
    use chrono::Offset as _;
    Ok(DayInput { plan, clock: fire.schedule.clock, utc_offset_s: at.offset().fix().local_minus_utc() as i64, heartbeat_s: 300, retry_s: 30, canary: fire.schedule.canary })
}

/// The scheduled firing time (scheduled.occurrence_time), never the start time: a late start plays the same day.
pub fn scheduled_at<W>(ctx: &WorkflowContext<W>) -> WorkflowResult<DateTime<Utc>> {
    let at = ctx.search_attributes().get(&SearchAttributeKey::datetime("TemporalScheduledStartTime"));
    at.and_then(|t| DateTime::from_timestamp(t.seconds(), t.nanos() as u32)).ok_or_else(|| final_failure("missing TemporalScheduledStartTime; refusing to guess a plan date"))
}

fn final_failure(why: &str) -> WorkflowTermination {
    WorkflowTermination::failed_application(ApplicationFailure::builder(why.to_string()).non_retryable(true).build())
}

/// The desktop reservation of a running Temporal day (`<desk>.day.json`), so nothing else drives that desktop.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Reservation {
    pub owner: String,
    pub day: DayInput,
}

#[derive(Clone)]
pub struct DayOwner {
    pub path: PathBuf,
}

impl DayOwner {
    pub fn new(desk: &Path) -> Self {
        Self { path: desk.with_extension("day.json") }
    }

    /// The reservation; an unreadable one is an error, never "free".
    pub fn read(&self) -> Result<Option<Reservation>> {
        if !self.path.exists() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(&self.path)?;
        Ok(Some(serde_json::from_str(&text).with_context(|| format!("{} is not a readable reservation", self.path.display()))?))
    }

    pub fn check(&self, owner: &str) -> Result<Option<Reservation>> {
        let rec = self.read()?;
        if let Some(r) = &rec
            && r.owner != owner
        {
            bail!(Final(format!("desktop reserved by {}; resume or reconcile that day first", r.owner)));
        }
        Ok(rec)
    }

    pub fn claim(&self, owner: &str, inp: &DayInput) -> Result<Reservation> {
        if let Some(rec) = self.check(owner)? {
            if rec.day != *inp {
                bail!(Final("reserved day input changed; refusing to mix plans or clock modes".into()));
            }
            return Ok(rec);
        }
        let rec = Reservation { owner: owner.into(), day: inp.clone() };
        std::fs::create_dir_all(self.path.parent().unwrap_or(Path::new(".")))?;
        use std::io::Write as _;
        std::fs::OpenOptions::new().write(true).create_new(true).open(&self.path)?.write_all(serde_json::to_string(&rec)?.as_bytes())?;
        Ok(rec)
    }

    pub fn release(&self, owner: &str) -> Result<()> {
        if self.check(owner)?.is_some() {
            std::fs::remove_file(&self.path)?;
        }
        Ok(())
    }
}

/// One controller per desktop: a try-locked file held for the returned handle's life (interoperates with
/// Python's flock/msvcrt lock on the same file).
pub fn desk_lock(path: &Path) -> Result<std::fs::File> {
    std::fs::create_dir_all(path.parent().unwrap_or(Path::new(".")))?;
    let f = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path)?;
    f.try_lock().map_err(|_| anyhow::anyhow!("another controller is driving this desktop ({})", path.display()))?;
    Ok(f)
}

fn opts(timeout: Duration, retry: &RetryPolicy, heartbeat: Option<Duration>) -> ActivityOptions {
    ActivityOptions::with_start_to_close_timeout(timeout)
        .retry_policy(retry.clone())
        .maybe_heartbeat_timeout(heartbeat)
        .cancellation_type(ActivityCancellationType::WaitCancellationCompleted)
        .build()
}

use worker::DayActivities as A;

#[workflow]
#[derive(Default)]
pub struct PersonaDay;

#[workflow_methods]
impl PersonaDay {
    #[run(name = "PersonaDay")]
    pub async fn run(ctx: &mut WorkflowContext<Self>, inp: DayInput) -> WorkflowResult<Vec<Done>> {
        // ponytail: only the lazy-day path; a pre-lazy-day history needs the Python worker that started it
        if !ctx.patched("lazy-day-v1") {
            return Err(final_failure("pre-lazy-day history; resume it on the Python worker"));
        }
        let mut done = vec![];
        let r = play(ctx, &inp, &mut done).await;
        if r.is_err() {
            // also after a cancel: the abort runs on its own token, so the desktop is released either way
            let o = ActivityOptions::with_start_to_close_timeout(QUICK).retry_policy(RetryPolicy::builder().maximum_attempts(5).build()).cancellation_token(WorkflowCancellationToken::new()).build();
            ctx.execute_activity(A::day_abort, (), o).await?;
        }
        r.map(|()| done)
    }
}

async fn play(ctx: &mut WorkflowContext<PersonaDay>, inp: &DayInput, done: &mut Vec<Done>) -> WorkflowResult<()> {
    let plan = &inp.plan;
    let day = NaiveDate::parse_from_str(&plan.date, "%Y-%m-%d").map_err(|e| final_failure(&format!("plan date: {e}")))?;
    let retry = RetryPolicy::builder().initial_interval(Duration::from_secs(inp.retry_s)).maximum_attempts(3).non_retryable_error_types(NOT_RETRYABLE).build();
    let beat = Some(Duration::from_secs(inp.heartbeat_s));
    ctx.execute_activity(A::day_prepare, inp.clone(), opts(QUICK, &retry, None)).await?;
    let mut todo = plan.clone();
    if inp.canary {
        let skipped = ctx.execute_activity(A::canary, plan.clone(), opts(ITEM_TIMEOUT, &retry, beat)).await?;
        todo.items.retain(|i| !skipped.iter().any(|d| d.at == i.at && d.template == i.template));
        done.extend(skipped);
    }
    let slot = |s: SlotInput| ctx.execute_activity(A::slot, s, opts(ITEM_TIMEOUT, &retry, beat));
    for (item, interrupts) in slots(&todo) {
        if inp.clock == Clock::Realtime {
            let due = day.and_time(NaiveTime::parse_from_str(&item.at, "%H:%M").unwrap_or_default()) - chrono::Duration::seconds(inp.utc_offset_s);
            let now: DateTime<Utc> = ctx.workflow_time().map(DateTime::from).unwrap_or_default();
            let wait = (due.and_utc() - now).num_milliseconds() as f64 / 1000.0 + ctx.random::<f64>() * 120.0;
            if wait > 0.0 {
                ctx.timer(Duration::from_secs_f64(wait)).await;
            }
        }
        let s = SlotInput { date: plan.date.clone(), item: item.clone(), interrupts: interrupts.clone(), jump_at: None };
        if item.template != BREAK {
            done.extend(slot(s).await?);
            continue;
        }
        ctx.execute_activity(A::break_start, s.clone(), opts(QUICK, &retry, None)).await?;
        ctx.timer(Duration::from_secs(if inp.clock == Clock::Realtime { item.minutes as u64 * 60 } else { 5 })).await;
        let back_in = ctx.execute_activity(A::break_end, s, opts(QUICK, &retry, None)).await?;
        let ok = back_in.status == "done";
        done.push(back_in);
        if !ok && ctx.patched("break-unlock-fail-closed-v1") {
            let why = format!("break at {}: unlock failed, day stopped", item.at);
            return Err(WorkflowTermination::failed_application(ApplicationFailure::builder(why).type_name("BreakUnlockFailed".to_string()).non_retryable(true).build()));
        }
        let back: NaiveDateTime = back_at(day, &item);
        for late in interrupts {
            done.extend(slot(SlotInput { date: plan.date.clone(), item: late, interrupts: vec![], jump_at: Some(back.format("%H:%M").to_string()) }).await?);
        }
    }
    ctx.execute_activity(A::day_end, DayEnd { plan: plan.clone(), done: done.clone() }, opts(QUICK, &retry, None)).await?;
    Ok(())
}

#[workflow]
#[derive(Default)]
pub struct ScheduledDay;

#[workflow_methods]
impl ScheduledDay {
    #[run(name = "ScheduledDay")]
    pub async fn run(ctx: &mut WorkflowContext<Self>, inp: ScheduledInput) -> WorkflowResult<Vec<Done>> {
        let occurrence = scheduled_at(ctx)?;
        let o = ActivityOptions::with_start_to_close_timeout(QUICK).retry_policy(RetryPolicy::builder().maximum_attempts(1).build()).build();
        let day = ctx.execute_activity(A::load_scheduled_day, Fire { schedule: inp, occurrence }, o).await?;
        let child = ChildWorkflowOptions::builder().workflow_id(workflow_id(&day.plan)).id_reuse_policy(WorkflowIdReusePolicy::RejectDuplicate).build();
        let started = ctx.start_child_workflow(PersonaDay::run, day, child).await.map_err(|e| final_failure(&format!("day did not start: {e}")))?;
        Ok(started.result().await?)
    }
}

#[cfg(test)]
mod tests;
