from __future__ import annotations

import asyncio
import datetime as dt
import re
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing, contextmanager, nullcontext
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
    import ludius.calls as calls
    import ludius.durable as durable
    import ludius.mail as mail
    import ludius.planner as planner
    import ludius.reply as reply
    import ludius.requests as requests
    import ludius.revision as revision
    import ludius.world_state as world_state
    from ludius.llm import LlmClient
    from ludius.planner import DayPlan, World
    from ludius.policy import Policy
    from ludius.scheduled import occurrence_time
    from ludius.tasks import Persona, Template

NIGHTLY_PLAN = "NightlyPlan"
TASK_QUEUE = "ludius-plan"
CATCHUP = dt.timedelta(hours=8)
TIMEOUT = dt.timedelta(minutes=30)
PARALLEL = 8


class NightlyInput(BaseModel):
    persona: str
    tz: str


class PlanFire(BaseModel):
    persona: str
    tz: str
    occurrence: dt.datetime


class Planned(BaseModel):
    persona: str
    date: str
    path: str
    generated: bool
    items: int
    rejects: list[str] = []
    note: str = ""


class PlanConflict(ValueError):
    pass


class Sources(NamedTuple):
    personas: list[Persona]
    templates: dict[str, Template]
    policy: Policy
    inbox: Callable[[str], list[mail.Message] | None] = lambda _: []
    decider: Callable[[], Decider] | None = None


class Generated(NamedTuple):
    plan: DayPlan
    rejects: list[str]
    auto: revision.AutoRevision


def schedule_id(persona: str) -> str:
    durable.schedule_id(persona)
    return f"plan-{persona}"


def schedule_for(persona: str, at: str, tz: str, *, paused: bool = True) -> Schedule:
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
    return dt.date.fromisoformat(durable.plan_date(occurrence, tz)) + dt.timedelta(days=1)


def generate_day(
    llm: LlmClient,
    world: World,
    persona: Persona,
    src: Sources,
    date: dt.date,
    scenario: planner.Scenario | None = None,
    shared: Callable[..., AbstractContextManager[object]] = nullcontext,
) -> Generated:
    with shared():
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
        with shared(boss.user):
            day = requests.ensure(
                llm, world, auto.narrative, {u: world_state.brief(state, u, date) for u in reports}, src.personas,
                src.templates, src.policy, date, boss, {u: src.inbox(u) for u in reports},
                {u: world_state.existing_files(world, u) for u in reports},
            )  # fmt: skip
        notes += [f"requests by {boss.user}: {r}" for r in day.rejects]
        notes += [f"requests by {boss.user}: {r.status} to {r.to} {r.subject!r}" for r in day.requests]
    inbox_mails = src.inbox(persona.user)
    decider = src.decider() if src.decider is not None else None
    if not isinstance(boss, str) and decider is not None:
        with shared():
            mails = requests.pending(world, persona.user, boss, inbox_mails, src.policy, date)
        role = planner.role_templates(src.templates, persona)
        mapped, more = requests.candidates(llm, decider, world, persona, boss, mails, role, date, people)
        notes += more
    asked = {m.subject for m in inbox_mails or [] if m.msgid in {it.request for it in mapped}}
    inbox = reply.hints(inbox_mails, persona.user, people, src.policy, day=date)
    unread = {(m.sender.partition("@")[0], m.subject) for m in inbox_mails or [] if not m.read}
    files = {(m.sender.partition("@")[0], m.subject): m.files for m in inbox_mails or [] if m.files}
    listed = "\n".join(f"- from {u}@{mail.DOMAIN}: {s}" + (" (unread)" if (u, s) in unread else "")
                       + (f" (attached: {', '.join(files[u, s])})" if (u, s) in files else "")
                       for u, s in inbox if s not in asked)  # fmt: skip
    listed = f"Inbox on the mail server (newest first; a reply answers one of these):\n{listed}" if listed else ""
    invites = reply.hints(inbox_mails, persona.user, people, src.policy, mail.MEETING_REQUEST)
    if invites:
        listed += ("\nMeeting invitations waiting for an answer (an answer accepts or declines one):\n"
                   + "\n".join(f"- from {u}@{mail.DOMAIN}: {s}" for u, s in invites))  # fmt: skip
    planned = "\n".join(f"- {it.at} {it.template}: {it.context}" for it in mapped)
    planned = f"Already planned from the manager's requests (keep these times free, do not plan them again):\n{planned}"
    context = "\n".join(x for x in (summary, world_state.brief(state, persona.user, date), listed,
                                    planned if mapped else "") if x)  # fmt: skip
    followups = {f.id: f.about for f in world_state.due(state, persona.user, date)}
    threads = world_state.mail_threads(state, persona.user)
    existing = world_state.existing_files(world, persona.user)
    owned = planner.owned_files(world.root, persona.user, date.isoformat())
    try:
        plan, rejects = planner.plan_day(
            llm, auto.narrative, persona, date, src.templates, context, world.history(persona.user),
            policy=src.policy, scenario=scenario, existing=existing, followups=followups,
            mail_threads=threads, inbox=set(inbox), owned=owned, invites=set(invites), decider=decider,
            unread=unread, taken=[it.at for it in mapped], attached=files,
        )  # fmt: skip
    finally:
        getattr(decider, "close", lambda: None)()
    colleagues = auto.narrative.colleagues(persona.user)
    teams = [c for c, members in auto.narrative.channels.items() if persona.user in members]
    shares = [c for c, members in auto.narrative.shares.items() if persona.user in members]
    role = planner.role_templates(src.templates, persona)
    stale = [
        x
        for x in plan.items
        if (t := role.get(x.template))
        and t.capability == "mail.read"
        and (x.params.get("sender", "").strip().lower().partition("@")[0], x.params.get("subject")) not in unread
    ]
    plan.items = [x for x in plan.items if x not in stale]
    rejects += [f"{x.template} at {x.at}: dropped, the mail is not unread" for x in stale]  # fmt: skip
    for it in mapped:
        dups = requests.duplicates(plan.items, it, world.root)
        rest = [x for x in plan.items if x not in dups]
        while it.at in {x.at for x in rest}:
            it.at = f"{(int(it.at[:2]) * 60 + int(it.at[3:]) + 1) // 60 % 24:02d}:{(int(it.at[3:]) + 1) % 60:02d}"
        planner.fill_recipient_names([it], people)
        why = dict(planner.validate(
            [*rest, it], planner.role_templates(src.templates, persona), world.history(persona.user),
            src.policy, colleagues, None, set(followups), existing, threads, people, persona.user, date.isoformat(),
            set(inbox), teams, owned, shares, set(invites),
        )).get(len(rest)) or requests.planned(it, src.templates, world.root)  # fmt: skip
        if why:
            requests.remember(world.root, persona.user, it.request, f"rejected: {why}")
            rejects.append(f"request {it.template} at {it.at}: {why}")
        else:
            plan.items = sorted([*rest, it], key=lambda x: x.at)
            rejects += [f"{x.template} at {x.at}: dropped, it repeats the request {it.request}" for x in dups]
            if read := requests.read_first(it, inbox_mails, role, plan.items):
                others = [
                    x
                    for x in plan.items
                    if not (x.template == read.template and x.params.get("subject") == read.params["subject"])
                ]
                why = dict(planner.validate(
                    [*others, read], role, [], src.policy, colleagues, None, set(followups), existing, threads,
                    people, persona.user, date.isoformat(), set(inbox),
                )).get(len(others))  # fmt: skip
                if why:
                    rejects.append(f"read before request {it.template} at {it.at}: {why}")
                else:
                    plan.items = sorted([*others, read], key=lambda x: x.at)
            if back := requests.deliver(it, boss, inbox_mails, role, plan.items):
                made = {f"documents\\{back.params['file_name']}".casefold(): date.isoformat()}
                why = dict(planner.validate(
                    [*plan.items, back], role, [], src.policy, colleagues | {boss.user}, None, set(followups),
                    existing, threads, people, persona.user, date.isoformat(), set(inbox), teams, {**owned, **made},
                )).get(len(plan.items))  # fmt: skip
                if why:
                    rejects.append(f"mail back request {it.template} at {it.at}: {why}")
                else:
                    plan.items = sorted([*plan.items, back], key=lambda x: x.at)
    rejects += notes
    return Generated(plan, rejects, auto)


def check_existing(world: World, persona: Persona, date: dt.date, src: Sources) -> DayPlan:
    path = world.plan_path(persona.user, date.isoformat())
    try:
        plan = planner.load_plan(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise PlanConflict(f"{path} exists but is not a readable plan: {e}") from e
    if (plan.persona, plan.date) != (persona.user, date.isoformat()):
        raise PlanConflict(f"{path} is the plan of {plan.persona} on {plan.date}, not {persona.user} on {date}")
    narrative = world.narrative()
    colleagues = narrative.colleagues(persona.user)
    threads = world_state.mail_threads(world_state.build(world, narrative, src.personas, date), persona.user)
    people = {p.user: p.name for p in narrative.people if p.user}
    inbox_mails = src.inbox(persona.user)
    inbox = set(reply.hints(inbox_mails, persona.user, people, src.policy, day=date))
    invites = set(reply.hints(inbox_mails, persona.user, people, src.policy, mail.MEETING_REQUEST))
    threads = {f: t for f, t in threads.items() if inbox_mails is None or t in inbox}
    rejects = planner.validate(
        plan.items, planner.role_templates(src.templates, persona), [], src.policy, colleagues,
        followups={i.followup for i in plan.items if i.followup}, mail_threads=threads, inbox=inbox, invites=invites,
        people=people, user=persona.user, day=date.isoformat(),
        teams=[c for c, members in narrative.channels.items() if persona.user in members],
        owned=planner.owned_files(world.root, persona.user, date.isoformat()),
        shares=[c for c, members in narrative.shares.items() if persona.user in members],
    )  # fmt: skip  # no content history: this plan's own content is already in it
    if rejects:
        raise PlanConflict(f"{path} fails validation: " + "; ".join(f"item {i}: {why}" for i, why in rejects))
    return plan


def ensure_day(
    llm: Callable[[], LlmClient],
    world: World,
    persona: Persona,
    src: Sources,
    date: dt.date,
    shared: Callable[..., AbstractContextManager[object]] = nullcontext,
) -> Planned:
    path = world.plan_path(persona.user, date.isoformat())
    if path.exists():
        plan = check_existing(world, persona, date, src)
        try:
            world.repair_history(plan, src.templates)
        except ValueError as e:
            raise PlanConflict(f"cannot restore the content history of {path}: {e}") from e
        return Planned(persona=persona.user, date=plan.date, path=str(path), generated=False, items=len(plan.items))
    with closing(llm()) as client:
        g = generate_day(client, world, persona, src, date, shared=shared)
    world.save_plan(g.plan, src.templates, new=True)
    return Planned(
        persona=persona.user, date=g.plan.date, path=str(path), generated=True,
        items=len(g.plan.items), rejects=g.rejects, note=g.auto.note,
    )  # fmt: skip


def shared_lock(world: World) -> Callable[..., AbstractContextManager[None]]:
    guard, locks = threading.Lock(), dict[str, threading.Lock]()

    @contextmanager
    def shared(key: str = "") -> Generator[None]:
        with guard:
            lock = locks.setdefault(key, threading.Lock())
        with lock, durable.desk_lock(world.root / "locks" / (f"nightly-{key}.lock" if key else "nightly.lock")):
            yield

    return shared


def ensure_days(
    llm: Callable[[], LlmClient], world: World, personas: list[Persona], src: Sources, date: dt.date
) -> dict[str, Planned | Exception]:
    shared = shared_lock(world)

    def one(persona: Persona) -> Planned:
        with calls.for_persona(persona.user):
            return ensure_day(llm, world, persona, src, date, shared=shared)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {p.user: pool.submit(one, p) for p in personas}
    return {u: e if isinstance(e := f.exception(), Exception) else f.result() for u, f in futures.items()}


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
        self._shared = shared_lock(self.world)

    @activity.defn(name="nightly_plan")
    def plan(self, fire: PlanFire) -> Planned:
        try:
            schedule_id(fire.persona)
            date = planning_date(fire.occurrence, fire.tz)
            src = self.load()
            persona = next((p for p in src.personas if p.user == fire.persona), None)
            if persona is None:
                raise ValueError(f"no persona {fire.persona}")
        except (ValueError, KeyError) as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        try:
            with calls.for_persona(fire.persona):
                return ensure_day(self.llm, self.world, persona, src, date, shared=self._shared)
        except durable.DeskBusy as e:
            raise RuntimeError("another nightly planner is running; retrying") from e
        except (PlanConflict, calls.CapReached) as e:
            raise ApplicationError(str(e), non_retryable=True) from e


async def serve(client: Client, runs: Path, load: Callable[[], Sources], llm: Callable[[], LlmClient]) -> None:
    acts = PlanActivities(runs, load, llm)
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[NightlyPlan],
            activities=[acts.plan],
            activity_executor=pool,
            max_concurrent_activities=PARALLEL,
        ):
            await asyncio.Event().wait()
