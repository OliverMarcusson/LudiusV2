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
from typing import TYPE_CHECKING, NamedTuple
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

if TYPE_CHECKING:
    from ludius.loop import Decider

with workflow.unsafe.imports_passed_through():
    from ludius import durable, mail, planner, reply, requests, revision, world_state
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
    inbox: Callable[[str], list[mail.Message] | None] = lambda _: []  # a persona's Inbox on the range server
    jev: Callable[[], Decider] | None = None  # maps manager requests to task types (ludius.requests); None: not mapped


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
    """`ludius plan day`: due weekly revision first (raises RevisionBusy), world state, the manager's requests (sent
    server-side, ludius.requests), then the validated plan with the persona's new requests mapped into it."""
    auto = revision.revise_if_due(llm, world, src.personas, date)
    state = world_state.build(world, auto.narrative, src.personas, date)
    world_state.save(state, world)
    summary = world.summary(persona.user, date)
    people = {p.user: p.name for p in auto.narrative.people if p.user}
    notes: list[str] = []
    mapped: list[planner.PlanItem] = []
    boss = requests.manager_of(auto.narrative, persona.user, src.personas)
    if isinstance(boss, str):
        notes.append(f"requests: {boss}")
    else:
        reports = [p.user for p in src.personas if p.user == persona.user or requests.manager_of(
            auto.narrative, p.user, src.personas) == boss]  # fmt: skip
        day = requests.ensure(
            llm, world, auto.narrative, {u: world_state.brief(state, u, date) for u in reports}, src.personas,
            src.templates, src.policy, date, boss, {u: src.inbox(u) for u in reports},
            {u: world_state.existing_files(world, u) for u in reports},
        )  # fmt: skip
        notes += [f"requests by {boss.user}: {r}" for r in day.rejects]
        notes += [f"requests by {boss.user}: {r.status} to {r.to} {r.subject!r}" for r in day.requests]
    inbox_mails = src.inbox(persona.user)
    if not isinstance(boss, str) and src.jev is not None:
        jev = src.jev()
        try:
            mails = requests.pending(world, persona.user, boss, inbox_mails, src.policy, date)
            role = planner.role_templates(src.templates, persona)
            mapped, more = requests.candidates(llm, jev, world, persona, boss, mails, role, date, people)
            notes += more
        finally:
            getattr(jev, "close", lambda: None)()
    asked = {m.subject for m in inbox_mails or [] if m.msgid in {it.request for it in mapped}}
    inbox = reply.hints(inbox_mails, persona.user, people, src.policy)
    listed = "\n".join(f"- from {u}@{mail.DOMAIN}: {s}" for u, s in inbox if s not in asked)
    listed = f"Inbox on the mail server (newest first; a reply answers one of these):\n{listed}" if listed else ""
    planned = "\n".join(f"- {it.at} {it.template}: {it.context}" for it in mapped)
    planned = f"Already planned from the manager's requests (keep these times free, do not plan them again):\n{planned}"
    context = "\n".join(x for x in (summary, world_state.brief(state, persona.user, date), listed,
                                    planned if mapped else "") if x)  # fmt: skip
    followups = {f.id: f.about for f in world_state.due(state, persona.user, date)}
    threads = world_state.mail_threads(state, persona.user)
    existing = world_state.existing_files(world, persona.user)
    plan, rejects = planner.plan_day(
        llm, auto.narrative, persona, date, src.templates, context, world.history(persona.user),
        policy=src.policy, scenario=scenario, existing=existing, followups=followups,
        mail_threads=threads, inbox=set(inbox),
    )  # fmt: skip
    colleagues = {p.user for p in auto.narrative.people if p.user and p.user != persona.user}
    for it in mapped:  # the same validation as the planner's own items, after them
        dups = requests.duplicates(plan.items, it, world.root)  # the planner did the request again: the mapped one wins
        rest = [x for x in plan.items if x not in dups]
        while it.at in {x.at for x in rest}:
            it.at = f"{(int(it.at[:2]) * 60 + int(it.at[3:]) + 1) // 60 % 24:02d}:{(int(it.at[3:]) + 1) % 60:02d}"
        planner.fill_recipient_names([it], people)
        why = dict(planner.validate(
            [*rest, it], planner.role_templates(src.templates, persona), world.history(persona.user),
            src.policy, colleagues, None, set(followups), existing, threads, people, persona.user, date.isoformat(),
            set(inbox),
        )).get(len(rest)) or requests.planned(it, src.templates, world.root)  # fmt: skip
        if why:
            requests.remember(world.root, persona.user, it.request, f"rejected: {why}")
            rejects.append(f"request {it.template} at {it.at}: {why}")
        else:
            plan.items = sorted([*rest, it], key=lambda x: x.at)
            rejects += [f"{x.template} at {x.at}: dropped, it repeats the request {it.request}" for x in dups]
    rejects += notes
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
    people = {p.user: p.name for p in narrative.people if p.user}
    inbox = set(reply.hints(src.inbox(persona.user), persona.user, people, src.policy))
    rejects = planner.validate(
        plan.items, planner.role_templates(src.templates, persona), [], src.policy, colleagues,
        followups={i.followup for i in plan.items if i.followup}, mail_threads=threads, inbox=inbox,
        people=people, user=persona.user,
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
