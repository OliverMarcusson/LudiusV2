"""Nightly day-plan generation: the planning lane the playback schedule (`ScheduledDay`) only reads from.

A separate paused-by-default schedule (`plan-<persona>`) starts `NightlyPlan` on the host-side `ludius-plan`
queue. Its explicitly started worker never touches a desktop or VM: it runs the same generation as
`ludius plan day` for the local day after the intended occurrence. An existing plan file for that day is kept
(retries are idempotent) and never overwritten; one that is malformed, misnamed or fails validation stops the
run without retries for an operator to resolve.

Weekends: every calendar day gets a plan, matching the daily playback schedule; the prompt names the weekday, so a
weekend day's content is the planner's choice. Restricting both schedules to workdays is a later, joint change.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from ludius import durable, planner, revision, world_state
    from ludius.llm import LlmClient
    from ludius.planner import DayPlan, World
    from ludius.policy import Policy
    from ludius.scheduled import occurrence_time
    from ludius.tasks import Persona, Template

NIGHTLY_PLAN = "NightlyPlan"
TASK_QUEUE = "ludius-plan"  # host-side: no desktop, so one queue serves every persona's planning schedule
CATCHUP = dt.timedelta(hours=8)  # a late night fire still plans the right day (from its occurrence) before morning
TIMEOUT = dt.timedelta(minutes=30)  # weekly revision plus up to two planner calls


class NightlyInput(BaseModel):
    persona: str
    tz: str  # IANA zone of the persona's day


class PlanFire(BaseModel):
    persona: str
    tz: str
    occurrence: dt.datetime


class Planned(BaseModel):
    persona: str
    date: str
    path: str
    generated: bool  # False: an existing valid plan was kept
    items: int
    rejects: list[str] = []
    note: str = ""  # what the weekly revision did


class PlanConflict(ValueError):
    """An existing plan file is not a valid plan of this persona and date. Never overwritten; fix it by hand."""


class Sources(NamedTuple):
    """What `ludius plan day` loads: every persona, the admitted templates and the scenario policy."""

    personas: list[Persona]
    templates: dict[str, Template]
    policy: Policy


class Generated(NamedTuple):
    plan: DayPlan
    rejects: list[str]
    auto: revision.AutoRevision


def schedule_id(persona: str) -> str:
    durable.schedule_id(persona)  # same account-name check as the day schedule
    return f"plan-{persona}"


def schedule_for(persona: str, at: str, tz: str, *, paused: bool = True) -> Schedule:
    """Every evening at `at` (HH:MM) in `tz`, plan that persona's next local day. Paused unless asked; SKIP overlap."""
    sid = schedule_id(persona)
    ZoneInfo(tz)
    if not re.fullmatch(r"\d{2}:\d{2}", at):
        raise ValueError("schedule time must be HH:MM")
    t = dt.time.fromisoformat(at)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            NIGHTLY_PLAN, NightlyInput(persona=persona, tz=tz), id=sid, task_queue=TASK_QUEUE
        ),
        spec=ScheduleSpec(
            calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(t.hour)], minute=[ScheduleRange(t.minute)])],
            time_zone_name=tz,
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=CATCHUP),
        state=ScheduleState(paused=paused, note=f"plan {persona}'s next day at {at} {tz}"),
    )


def planning_date(occurrence: dt.datetime, tz: str) -> dt.date:
    """The persona's local day after the intended occurrence (never worker wall time); schedule it in the evening."""
    return dt.date.fromisoformat(durable.plan_date(occurrence, tz)) + dt.timedelta(days=1)


def generate_day(
    llm: LlmClient,
    world: World,
    persona: Persona,
    src: Sources,
    date: dt.date,
    scenario: planner.Scenario | None = None,
) -> Generated:
    """`ludius plan day`: due weekly revision first (raises RevisionBusy), world state, then the validated plan."""
    auto = revision.revise_if_due(llm, world, src.personas, date)
    state = world_state.build(world, auto.narrative, src.personas, date)
    world_state.save(state, world)
    summary = world.summary(persona.user, date)
    context = "\n".join(x for x in (summary, world_state.brief(state, persona.user, date)) if x)
    plan, rejects = planner.plan_day(
        llm, auto.narrative, persona, date, src.templates, context, world.history(persona.user),
        policy=src.policy, scenario=scenario, existing=world_state.existing_files(world, persona.user),
        followups={f.id: f.about for f in world_state.due(state, persona.user, date)},
        mail_threads=world_state.mail_threads(state, persona.user),
    )  # fmt: skip
    return Generated(plan, rejects, auto)


def check_existing(world: World, persona: Persona, date: dt.date, src: Sources) -> DayPlan:
    """An existing plan must parse, name this persona and date, and pass item/policy validation as written."""
    path = world.plan_path(persona.user, date.isoformat())
    try:
        plan = planner.load_plan(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise PlanConflict(f"{path} exists but is not a readable plan: {e}") from e
    if (plan.persona, plan.date) != (persona.user, date.isoformat()):
        raise PlanConflict(f"{path} is the plan of {plan.persona} on {plan.date}, not {persona.user} on {date}")
    narrative = world.narrative()
    colleagues = {p.user for p in narrative.people if p.user and p.user != persona.user}
    threads = world_state.mail_threads(world_state.build(world, narrative, src.personas, date), persona.user)
    rejects = planner.validate(
        plan.items, planner.role_templates(src.templates, persona), [], src.policy, colleagues,
        followups={i.followup for i in plan.items if i.followup}, mail_threads=threads,
    )  # fmt: skip  # no content history: this plan's own content is already in it
    if rejects:
        raise PlanConflict(f"{path} fails validation: " + "; ".join(f"item {i}: {why}" for i, why in rejects))
    return plan


def ensure_day(llm: Callable[[], LlmClient], world: World, persona: Persona, src: Sources, date: dt.date) -> Planned:
    """Keep a valid existing plan (no model call, history repaired), refuse an invalid one, else create one."""
    path = world.plan_path(persona.user, date.isoformat())
    if path.exists():
        plan = check_existing(world, persona, date, src)
        try:  # a crash after publishing, before the history append, left its content out of the duplicate check
            world.repair_history(plan, src.templates)
        except ValueError as e:
            raise PlanConflict(f"cannot restore the content history of {path}: {e}") from e
        return Planned(persona=persona.user, date=plan.date, path=str(path), generated=False, items=len(plan.items))
    with closing(llm()) as client:
        g = generate_day(client, world, persona, src, date)
    # A plan written meanwhile (a manual `ludius plan day`) wins: FileExistsError, and the retry keeps it.
    # A crash between this write and the history append is repaired by the retry's existing-plan path.
    # ponytail: manual `ludius plan day` takes no nightly lock and appends unconditionally; the journal lock keeps
    # reads whole, but a manual save after a repair still duplicates (never loses) rows. HistoryBusy is retryable.
    world.save_plan(g.plan, src.templates, new=True)
    return Planned(
        persona=persona.user, date=g.plan.date, path=str(path), generated=True,
        items=len(g.plan.items), rejects=g.rejects, note=g.auto.note,
    )  # fmt: skip


@workflow.defn(name=NIGHTLY_PLAN)
class NightlyPlan:
    @workflow.run
    async def run(self, inp: NightlyInput) -> Planned:
        try:
            at = occurrence_time(workflow.info().typed_search_attributes)
        except ValueError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        return await workflow.execute_activity(
            "nightly_plan",
            PlanFire(persona=inp.persona, tz=inp.tz, occurrence=at),
            result_type=Planned,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RetryPolicy(initial_interval=dt.timedelta(minutes=1), maximum_attempts=3),
        )


class PlanActivities:
    def __init__(self, runs: Path, load: Callable[[], Sources], llm: Callable[[], LlmClient]) -> None:
        self.world, self.load, self.llm = World(runs), load, llm

    @activity.defn(name="nightly_plan")
    def plan(self, fire: PlanFire) -> Planned:
        try:
            schedule_id(fire.persona)
            date = planning_date(fire.occurrence, fire.tz)
            src = self.load()
            persona = next((p for p in src.personas if p.user == fire.persona), None)
            if persona is None:
                raise ValueError(f"no persona {fire.persona}")
        except (ValueError, KeyError) as e:  # bad input or setup does not get better by retrying
            raise ApplicationError(str(e), non_retryable=True) from e
        # Host-side lock: one planner at a time over shared world state, history and plans (no desktop involved).
        try:
            with durable.desk_lock(self.world.root / "locks" / "nightly.lock"):
                return ensure_day(self.llm, self.world, persona, src, date)
        except durable.DeskBusy as e:
            raise RuntimeError("another nightly planner is running; retrying") from e
        except PlanConflict as e:
            raise ApplicationError(str(e), non_retryable=True) from e


async def serve(client: Client, runs: Path, load: Callable[[], Sources], llm: Callable[[], LlmClient]) -> None:
    """Poll only while explicitly running. Generates plans; never starts a desktop, VM or day."""
    acts = PlanActivities(runs, load, llm)
    with ThreadPoolExecutor(max_workers=1) as pool:
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[NightlyPlan],
            activities=[acts.plan],
            activity_executor=pool,
            max_concurrent_activities=1,
        ):
            await asyncio.Event().wait()
