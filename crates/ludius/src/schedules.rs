//! Temporal schedules (port of durable/nightly schedule_for, cmd_schedule, _range_schedules) and the
//! closed-day reservation reconcile (scheduled.reconcile).

use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use temporalio_client::schedules::{CreateScheduleOptions, ScheduleAction, ScheduleCalendarSpec, ScheduleOverlapPolicy, ScheduleSpec};
use temporalio_client::{Client, WorkflowExecutionStatus};

use crate::durable::{DayInput, DayOwner, Done, PersonaDay, ScheduledDay, ScheduledInput, desk_lock, task_queue, workflow_id};
use crate::planner::DayPlan;
use crate::nightly::worker::{NightlyInput, NightlyPlan, TASK_QUEUE, persona_name};
use crate::runner::Clock;

pub async fn connect(address: &str, namespace: &str) -> Result<Client> {
    use temporalio_client::{ClientOptions, Connection, ConnectionOptions};
    let url = if address.contains("://") { address.to_string() } else { format!("http://{address}") };
    let connection = Connection::connect(ConnectionOptions::new(url::Url::parse(&url)?).build()).await?;
    Ok(Client::new(connection, ClientOptions::new(namespace).build())?)
}

/// A schedule to create: id, definition and its catch-up window (set right after create; the create
/// options have no field for it).
pub struct Def {
    pub id: String,
    pub opts: CreateScheduleOptions,
    pub catchup: Duration,
}

fn spec(at: &str, tz: &str) -> Result<ScheduleSpec> {
    tz.parse::<chrono_tz::Tz>().map_err(|_| anyhow::anyhow!("unknown time zone {tz:?}"))?;
    let t = chrono::NaiveTime::parse_from_str(at, "%H:%M").ok().filter(|_| at.len() == 5).context("schedule time must be HH:MM")?;
    use chrono::Timelike as _;
    let cal = ScheduleCalendarSpec::builder().hour(t.hour().to_string()).minute(t.minute().to_string()).build();
    Ok(ScheduleSpec::builder().calendars(vec![cal]).timezone_name(tz).build())
}

fn opts(action: ScheduleAction, spec: ScheduleSpec, paused: bool, note: String) -> CreateScheduleOptions {
    CreateScheduleOptions::builder().action(action).spec(spec).overlap_policy(ScheduleOverlapPolicy::Skip).paused(paused).note(note).build()
}

pub fn day_id(persona: &str) -> Result<String> {
    persona_name(persona)?;
    Ok(format!("day-{persona}"))
}

pub fn plan_id(persona: &str) -> Result<String> {
    persona_name(persona)?;
    Ok(format!("plan-{persona}"))
}

/// durable.schedule_for: the persona's day playback, daily at `at` in `tz`.
pub fn day(persona: &str, at: &str, tz: &str, clock: Clock, paused: bool, canary: bool, day_offset: i64) -> Result<Def> {
    let id = day_id(persona)?;
    if day_offset != 0 {
        // ponytail: day_offset needs the time-jump clock, which the Rust controller does not have
        bail!("--day-offset needs --clock time-jump, which is not ported");
    }
    let inp = ScheduledInput { persona: persona.into(), tz: tz.into(), clock, canary, day_offset };
    let action = ScheduleAction::start_workflow(ScheduledDay::run, inp, task_queue(persona), &id);
    let note = format!("{persona} daily at {at} {tz}, {}", json!(clock).as_str().unwrap_or_default());
    Ok(Def { opts: opts(action, spec(at, tz)?, paused, note), id, catchup: Duration::from_secs(3600) })
}

/// nightly.schedule_for: plan the persona's next day, daily at `at` in `tz`.
pub fn plan(persona: &str, at: &str, tz: &str, paused: bool) -> Result<Def> {
    let id = plan_id(persona)?;
    let action = ScheduleAction::start_workflow(NightlyPlan::run, NightlyInput { persona: persona.into(), tz: tz.into() }, TASK_QUEUE, &id);
    let note = format!("plan {persona}'s next day at {at} {tz}");
    Ok(Def { opts: opts(action, spec(at, tz)?, paused, note), id, catchup: Duration::from_secs(8 * 3600) })
}

/// Create `d`; Ok(false) when a schedule with that id exists (kept as it is).
pub async fn create(client: &Client, d: Def) -> Result<bool> {
    use temporalio_client::schedules::ScheduleError;
    let h = match client.create_schedule(&d.id, d.opts).await {
        Ok(h) => h,
        Err(ScheduleError::Rpc(s)) if s.code() == tonic::Code::AlreadyExists => return Ok(false),
        Err(e) => return Err(e.into()),
    };
    let catchup = d.catchup;
    h.update(move |u| { u.set_catchup_window(catchup); }, Default::default()).await.with_context(|| format!("{}: created, but setting its catch-up window failed", d.id))?;
    Ok(true)
}

/// The schedule's id, paused flag and next fire times (cmd_schedule's describe output).
pub async fn describe(client: &Client, id: &str) -> Result<Value> {
    let d = client.get_schedule_handle(id).describe(Default::default()).await?;
    let next: Vec<String> = d.future_action_times().into_iter().map(|t| chrono::DateTime::<chrono::Utc>::from(t).to_rfc3339()).collect();
    Ok(json!({"id": id, "paused": d.paused(), "next": next}))
}

/// Play `plan` as a PersonaDay on its persona's worker and wait (an existing run of that day is joined, a
/// closed one started again). Inner Err: the workflow failed, was terminated, cancelled or timed out.
pub async fn play(client: &Client, plan: DayPlan, tz: chrono_tz::Tz, clock: Clock, canary: bool) -> Result<Result<Vec<Done>, String>> {
    use chrono::{Offset as _, TimeZone as _};
    use temporalio_client::errors::WorkflowGetResultError as E;
    use temporalio_client::{WorkflowIdConflictPolicy, WorkflowIdReusePolicy, WorkflowStartOptions};
    let day = chrono::NaiveDate::parse_from_str(&plan.date, "%Y-%m-%d")?;
    let noon = tz.from_local_datetime(&day.and_hms_opt(12, 0, 0).unwrap()).earliest().context("noon")?;
    let utc_offset_s = i64::from(noon.offset().fix().local_minus_utc());
    let opts = WorkflowStartOptions::new(task_queue(&plan.persona), workflow_id(&plan))
        .id_reuse_policy(WorkflowIdReusePolicy::AllowDuplicate)
        .id_conflict_policy(WorkflowIdConflictPolicy::UseExisting)
        .build();
    let inp = DayInput { plan, clock, utc_offset_s, heartbeat_s: 300, retry_s: 30, canary };
    let h = client.start_workflow(PersonaDay::run, inp, opts).await?;
    match h.get_result(Default::default()).await {
        Ok(done) => Ok(Ok(done)),
        Err(e @ (E::Failed(_) | E::Terminated { .. } | E::Cancelled { .. } | E::TimedOut)) => Ok(Err(e.to_string())),
        Err(e) => Err(e.into()),
    }
}

fn closed(s: WorkflowExecutionStatus) -> bool {
    use WorkflowExecutionStatus as S;
    matches!(s, S::Completed | S::Failed | S::Canceled | S::Terminated | S::TimedOut)
}

/// scheduled.reconcile: inspect (confirm None) or clear a closed workflow's desktop reservation. `status`
/// describes namespace/workflow-id; an error there is UNKNOWN, never closed.
pub fn reconcile(desk: &Path, status: &dyn Fn(&str, &str) -> Result<WorkflowExecutionStatus>, confirm: Option<(&str, &str)>) -> Result<Value> {
    let vm = desk.file_stem().and_then(|s| s.to_str()).unwrap_or_default().to_string();
    let _lock = desk_lock(desk)?;
    let owner = DayOwner::new(desk);
    let Some(rec) = owner.read()? else {
        if confirm.is_some() {
            bail!("{vm}: no reservation to clear");
        }
        return Ok(json!({"vm": vm, "reservation": null}));
    };
    let st = match rec.owner.split_once('/') {
        Some((ns, wid)) if !ns.is_empty() && !wid.is_empty() => status(ns, wid),
        _ => Err(anyhow::anyhow!("owner is not namespace/workflow-id")),
    };
    let state = match &st {
        Ok(s) => format!("{s:?}").to_uppercase(),
        Err(e) => format!("UNKNOWN ({e:#})"),
    };
    let clearable = st.as_ref().is_ok_and(|s| closed(*s));
    let mut out = json!({"vm": vm, "owner": rec.owner, "persona": rec.day.plan.persona, "date": rec.day.plan.date,
        "clock": rec.day.clock, "workflow": state, "clearable": clearable, "cleared": false});
    let Some(confirm) = confirm else { return Ok(out) };
    if confirm != (vm.as_str(), rec.owner.as_str()) {
        bail!("clear must repeat this VM and the exact reservation owner");
    }
    if !clearable {
        bail!("{} is {state}, not confirmed closed; reservation kept", rec.owner);
    }
    if owner.read()?.as_ref() != Some(&rec) {
        bail!("reservation changed during reconciliation; refusing");
    }
    if rec.day.clock == Clock::TimeJump {
        // ponytail: Python reset the Hyper-V clock here; the Hyper-V clock is not ported, so refuse
        bail!("{} is a time-jump day: reset that VM's clock and remove {} by hand", rec.owner, owner.path.display());
    }
    owner.release(&rec.owner)?;
    out["cleared"] = json!(true);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defs_validate() {
        assert!(day("aberg", "06:30", "Europe/Stockholm", Clock::Realtime, true, false, 0).is_ok());
        assert!(day("../x", "06:30", "Europe/Stockholm", Clock::Realtime, true, false, 0).is_err());
        assert!(day("aberg", "6:30", "Europe/Stockholm", Clock::Realtime, true, false, 0).is_err());
        assert!(day("aberg", "06:30", "Mars/Base", Clock::Realtime, true, false, 0).is_err());
        assert!(day("aberg", "06:30", "Europe/Stockholm", Clock::Realtime, true, false, 1).is_err());
        assert_eq!(plan("aberg", "22:00", "Europe/Stockholm", false).unwrap().id, "plan-aberg");
    }

    #[test]
    fn reconcile_clears_only_closed_confirmed() {
        let dir = std::env::temp_dir().join(format!("ludius-rec-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let desk = dir.join("WS01.lock");
        let none = |_: &str, _: &str| -> Result<WorkflowExecutionStatus> { bail!("down") };
        assert_eq!(reconcile(&desk, &none, None).unwrap()["reservation"], Value::Null);
        let plan: crate::planner::DayPlan = serde_json::from_value(json!({"persona": "aberg", "date": "2026-10-27", "items": []})).unwrap();
        let inp = crate::durable::DayInput { plan, clock: Clock::Compressed, utc_offset_s: 0, heartbeat_s: 300, retry_s: 30, canary: false };
        DayOwner::new(&desk).claim("default/day-aberg-2026-10-27", &inp).unwrap();
        let ok = Some(("WS01", "default/day-aberg-2026-10-27"));
        assert!(reconcile(&desk, &none, ok).is_err(), "unknown is never closed");
        let running = |_: &str, _: &str| Ok(WorkflowExecutionStatus::Running);
        assert!(reconcile(&desk, &running, ok).is_err());
        let done = |_: &str, _: &str| Ok(WorkflowExecutionStatus::Completed);
        assert!(reconcile(&desk, &done, Some(("WS01", "default/other"))).is_err());
        assert_eq!(reconcile(&desk, &done, None).unwrap()["clearable"], true);
        assert_eq!(reconcile(&desk, &done, ok).unwrap()["cleared"], true);
        assert!(DayOwner::new(&desk).read().unwrap().is_none());
    }
}
