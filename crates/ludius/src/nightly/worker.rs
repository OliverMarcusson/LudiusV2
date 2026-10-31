//! The nightly planner on Temporal (port of nightly.py's workflow and activity): the schedule `plan-<persona>`
//! starts NightlyPlan with {persona, tz}; the workflow hands the scheduled time to the activity, which plans the
//! next local day once. Same names and JSON payloads as the Python worker, so either serves the same schedules.
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Result, bail};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use temporalio_common::search_attributes::SearchAttributeKey;
use temporalio_common::{ActivityError, RetryPolicy};
use temporalio_macros::{activities, workflow, workflow_methods};
use temporalio_sdk::activities::ActivityContext;
use temporalio_sdk::{ActivityOptions, ApplicationFailure, WorkflowContext, WorkflowResult, WorkflowTermination};

use super::{Generate, PlanConflict, Planned, Shared, Sources, ensure_day, generator, planning_date};
use crate::calls::CapReached;
use crate::planner::World;

pub const NIGHTLY_PLAN: &str = "NightlyPlan";
pub const TASK_QUEUE: &str = "ludius-plan";
const TIMEOUT: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NightlyInput {
    pub persona: String,
    pub tz: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanFire {
    pub persona: String,
    pub tz: String,
    pub occurrence: DateTime<Utc>,
}

/// An error that retrying cannot fix (bad input, a conflicting plan, a spent budget).
#[derive(Debug)]
pub struct Final(pub String);

impl std::fmt::Display for Final {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Final {}

pub fn is_final(e: &anyhow::Error) -> bool {
    e.is::<Final>() || e.is::<PlanConflict>() || e.is::<CapReached>()
}

/// durable._persona_name: an account name, never a path.
pub fn persona_name(persona: &str) -> Result<()> {
    if !regex::Regex::new("^[a-zA-Z0-9][a-zA-Z0-9_-]*$").unwrap().is_match(persona) {
        bail!(Final("persona must be an account name, not a path".into()));
    }
    Ok(())
}

/// The activity body: plan `fire.persona`'s next local day once (sources loaded fresh per firing).
pub fn plan(world: &World, shared: &Shared, src: &Sources, generate: Generate, fire: &PlanFire) -> Result<Planned> {
    persona_name(&fire.persona)?;
    let date = planning_date(fire.occurrence, &fire.tz).map_err(|e| Final(format!("{e:#}")))?;
    let Some(persona) = src.personas.iter().find(|p| p.user == fire.persona) else { bail!(Final(format!("no persona {}", fire.persona))) };
    crate::calls::for_persona(&persona.user, || ensure_day(generate, world, persona, src, date, shared))
}

pub struct PlanActivities {
    pub runs: std::path::PathBuf,
    pub shared: Shared,
    pub key: String,
    pub model: Option<String>,
}

impl PlanActivities {
    pub fn new(runs: &Path, key: &str, model: Option<String>) -> Self {
        Self { runs: runs.into(), shared: Shared::new(&World::new(runs)), key: key.into(), model }
    }

    fn fire(&self, fire: &PlanFire) -> Result<Planned> {
        let world = World::new(&self.runs);
        let decider = || crate::decisions::DecisionsClient::new(&self.key, None, None);
        let inbox = |u: &str| crate::mail::newest(u, "inbox");
        let src = Sources {
            personas: personas()?,
            templates: crate::planner::templates()?,
            policy: crate::policy::Policy::from_env()?,
            inbox: &inbox,
            decider: (!self.key.is_empty()).then_some(&decider as _),
        };
        plan(&world, &self.shared, &src, &generator(&self.key, self.model.as_deref(), &world, &src), fire)
    }
}

/// data/personas/*.yaml, sorted by file name (cli.plan_sources).
pub fn personas() -> Result<Vec<crate::tasks::Persona>> {
    let mut paths: Vec<_> = std::fs::read_dir(crate::paths::data().join("personas"))?.map(|e| e.map(|e| e.path())).collect::<std::io::Result<_>>()?;
    paths.retain(|p| p.extension().is_some_and(|x| x == "yaml"));
    paths.sort();
    paths.iter().map(|p| crate::tasks::load_yaml(p)).collect()
}

#[activities]
impl PlanActivities {
    #[activity(name = "nightly_plan")]
    pub async fn nightly_plan(self: Arc<Self>, _ctx: ActivityContext, fire: PlanFire) -> Result<Planned, ActivityError> {
        // blocking work (files, reqwest::blocking) off the async runtime
        let got = tokio::task::spawn_blocking(move || self.fire(&fire)).await.map_err(anyhow::Error::from)?;
        got.map_err(|e| {
            let msg = format!("{e:#}");
            ActivityError::application(ApplicationFailure::builder(msg).non_retryable(is_final(&e)).build())
        })
    }
}

#[workflow]
#[derive(Default)]
pub struct NightlyPlan;

#[workflow_methods]
impl NightlyPlan {
    #[run(name = "NightlyPlan")]
    pub async fn run(ctx: &mut WorkflowContext<Self>, inp: NightlyInput) -> WorkflowResult<Planned> {
        // scheduled.occurrence_time: the intended firing, never the start time (a late start plans the same day)
        let at = ctx.search_attributes().get(&SearchAttributeKey::datetime("TemporalScheduledStartTime"));
        let Some(at) = at.and_then(|t| DateTime::from_timestamp(t.seconds(), t.nanos() as u32)) else {
            let why = "missing TemporalScheduledStartTime; refusing to guess a plan date";
            return Err(WorkflowTermination::failed_application(ApplicationFailure::builder(why).non_retryable(true).build()));
        };
        let opts = ActivityOptions::with_start_to_close_timeout(TIMEOUT).retry_policy(RetryPolicy::builder().initial_interval(Duration::from_secs(60)).maximum_attempts(3).build()).build();
        Ok(ctx.execute_activity(PlanActivities::nightly_plan, PlanFire { persona: inp.persona, tz: inp.tz, occurrence: at }, opts).await?)
    }
}

/// Serve the nightly planner until the process stops.
pub async fn serve(address: &str, namespace: &str, runs: &Path, key: &str, model: Option<String>) -> Result<()> {
    use temporalio_client::{Client, ClientOptions, Connection, ConnectionOptions};
    use temporalio_sdk::{Runtime, Worker, WorkerOptions};
    let runtime = Runtime::from_current_tokio(Default::default())?;
    let url = if address.contains("://") { address.to_string() } else { format!("http://{address}") };
    let connection = Connection::connect(ConnectionOptions::new(url::Url::parse(&url)?).build()).await?;
    let client = Client::new(connection, ClientOptions::new(namespace).build())?;
    let options = WorkerOptions::new(TASK_QUEUE).register_workflow::<NightlyPlan>()?.register_activities(PlanActivities::new(runs, key, model)).build();
    Worker::new(&runtime, client, options)?.run().await?;
    Ok(())
}
