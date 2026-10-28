"""Durable persona days on Temporal (spec section 9): the day is a workflow, each slot an activity.

The workflow holds the day's timeline: durable timers wait for each slot's time (realtime) and for the time
away on a break, so a controller crash or restart resumes after the last finished slot instead of replaying
the day. The GUI work stays in DayRunner (day.py), called from activities on one worker per desktop.

Delivery is at-least-once, not exactly-once: a slot whose controller died, hung or lost its connection is
run again from the start (after session hygiene), so its GUI effects may happen twice, as when a person
retries. Each retry is logged (item_retry) so ground truth shows it. Two guards keep a retry from typing
into the desktop while an earlier attempt still does:
- one controller per desktop: play() holds an OS file lock for the desktop while its worker runs, and the
  worker runs one activity at a time;
- a hung attempt stops heartbeating (heartbeats come from ground-truth writes, i.e. steps, not from a
  timer), so Temporal times it out; the SDK then raises cancellation inside the activity's thread, which
  stops it at its next Python statement (a call blocked inside the agent RPC returns first), and the
  retry only starts in this worker once that thread has returned.
Needs the `durable` dependency group and a Temporal server (`ludius day --temporal host:port`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import sys
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    WorkflowFailureError,
)
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from ludius.day import DayRunner, ItemResult, back_at, slots
    from ludius.loop import Clock, GroundTruth
    from ludius.planner import BREAK, DayPlan, PlanItem, World, load_plan

ITEM_TIMEOUT = dt.timedelta(hours=2)
QUICK = dt.timedelta(minutes=5)
# Template or plan errors do not get better by retrying; nor does a spent budget (calls.CapReached): the day stops.
NOT_RETRYABLE = ["KeyError", "ValueError", "CapReached"]


class DayInput(BaseModel):
    plan: DayPlan
    clock: Clock  # realtime | compressed | time-jump
    utc_offset_s: int  # the plan's times are wall-clock times in this offset
    heartbeat_s: int = 300  # longest a slot may go without a ground-truth write before it counts as hung
    retry_s: int = 30  # first retry backoff; at most 3 attempts per slot
    canary: bool = False  # run the day's apps' drift canaries first and skip the items of drifted apps


class ScheduledInput(BaseModel):
    """What a recurring schedule starts each day: the day's plan is chosen when it fires, never generated."""

    persona: str
    tz: str  # IANA zone the persona's day (and the plan's date) lives in
    clock: Clock = "realtime"
    canary: bool = False
    day_offset: int = 0  # play the plan this many days after the fire's local date (time warp with time-jump)


class SlotInput(BaseModel):
    date: str
    item: PlanItem
    interrupts: list[PlanItem] = []
    jump_at: str | None = None  # time-jump: logical HH:MM to set first; None means the item's own time


class Done(BaseModel):
    at: str
    template: str
    status: str


class DayEnd(BaseModel):
    plan: DayPlan
    done: list[Done]


def _done(r: ItemResult) -> Done:
    return Done(at=r.item.at, template=r.item.template, status=r.status)


class HeartbeatLog(GroundTruth):
    """Ground truth whose every write inside an activity is a heartbeat: a slot is alive while it makes steps."""

    def write(self, **rec: Any) -> None:
        super().write(**rec)
        if activity.in_activity():
            activity.heartbeat()


class DayActivities:
    """The desktop side: one DayRunner, one activity at a time (there is one keyboard and mouse)."""

    def __init__(
        self, day: DayRunner, canary: Callable[[set[str]], dict[str, str]] | None = None, *, plan: DayPlan | None = None
    ) -> None:
        self._fixed_day = day
        self._expected_plan = plan
        self.canary_run = canary  # apps -> state (ok, drift, unknown): canary.run bound to this desktop

    def _owns_activity(self) -> bool:
        return (
            self._expected_plan is None
            or not activity.in_activity()
            or activity.info().workflow_id == workflow_id(self._expected_plan)
        )

    @property
    def day(self) -> DayRunner:
        if not self._owns_activity():
            raise ValueError("one-shot worker cannot drive another dated workflow on this persona queue")
        return self._fixed_day

    @activity.defn(name="canary")
    def canary(self, plan: DayPlan) -> list[Done]:
        """Drift canaries for the plan's apps on this desktop; the items of drifted apps are skipped, and logged."""
        if self.canary_run is None:
            raise ValueError("this worker has no canary runner")
        apps = {self.day.templates[i.template].app for i in plan.items if i.template in self.day.templates}
        drift = {app for app, state in self.canary_run(apps).items() if state == "drift"}
        skipped = [
            i for i in plan.items if i.template in self.day.templates and self.day.templates[i.template].app in drift
        ]
        for i in skipped:
            self.day.log.write(event="skipped_drift", host_ms=_now(), at=i.at, task=i.template,
                               app=self.day.templates[i.template].app)  # fmt: skip
        return [Done(at=i.at, template=i.template, status="skipped_drift") for i in skipped]

    def _attempt(self, s: SlotInput) -> dt.date:
        info = activity.info()
        if info.attempt > 1:
            self.day.log.write(event="item_retry", host_ms=_now(), at=s.item.at, template=s.item.template,
                               attempt=info.attempt, activity=info.activity_type)  # fmt: skip
        return dt.date.fromisoformat(s.date)

    @activity.defn(name="day_start")
    def start(self, plan: DayPlan) -> None:
        if self._expected_plan is not None and plan != self._expected_plan:
            raise ValueError("workflow plan differs from this one-shot worker")
        self.day.start(plan)

    @activity.defn(name="day_prepare")
    def prepare(self, inp: DayInput) -> None:
        if self.day.clock != inp.clock:
            raise ValueError("workflow clock differs from this worker's day")
        self.start(inp.plan)

    @activity.defn(name="day_abort")
    def abort(self) -> None:
        if self._owns_activity():
            self.clock_reset()

    @activity.defn(name="day_end")
    def finish(self, e: DayEnd) -> None:
        items = {(i.at, i.template): i for i in e.plan.items}
        self.day.finish(e.plan, [ItemResult(items[d.at, d.template], d.status) for d in e.done])

    @activity.defn(name="clock_reset")
    def clock_reset(self) -> None:
        """The day failed or was cancelled before day_end: put the range back on host time (time-jump only)."""
        if self.day.clock == "time-jump" and self.day.set_clock:
            self.day.set_clock(None)
            self.day.log.write(event="clock_reset", host_ms=_now(), reason="day stopped before its end")

    @activity.defn(name="slot")
    def slot(self, s: SlotInput) -> list[Done]:
        return [_done(r) for r in self.day.slot(self._attempt(s), s.item, s.interrupts, jump_at=s.jump_at)]

    @activity.defn(name="break_start")
    def break_start(self, s: SlotInput) -> None:
        self.day.begin_break(self._attempt(s), s.item)

    @activity.defn(name="break_end")
    def break_end(self, s: SlotInput) -> Done:
        return _done(self.day.end_break(self._attempt(s), s.item))

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.start,
            self.prepare,
            self.abort,
            self.finish,
            self.clock_reset,
            self.canary,
            self.slot,
            self.break_start,
            self.break_end,
        ]


@workflow.defn
class PersonaDay:
    def __init__(self) -> None:
        self.done: list[Done] = []

    @workflow.query
    def progress(self) -> list[Done]:
        return self.done

    @workflow.run
    async def run(self, inp: DayInput) -> list[Done]:
        plan, day = inp.plan, dt.date.fromisoformat(inp.plan.date)
        tz = dt.timezone(dt.timedelta(seconds=inp.utc_offset_s))
        retry = RetryPolicy(initial_interval=dt.timedelta(seconds=inp.retry_s), maximum_attempts=3,
                            non_retryable_error_types=NOT_RETRYABLE)  # fmt: skip
        rnd = workflow.random()

        async def slot(s: SlotInput) -> list[Done]:
            return await workflow.execute_activity(
                "slot", s, result_type=list[Done], start_to_close_timeout=ITEM_TIMEOUT,
                heartbeat_timeout=dt.timedelta(seconds=inp.heartbeat_s), retry_policy=retry,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,  # the desktop is free after
            )  # fmt: skip

        # Version markers keep existing histories replayable. New workers need the complete input to bind
        # a lazy runner to this workflow, and cleanup must cover prepare/canary/finish failures too.
        lazy_day = workflow.patched("lazy-day-v1")

        async def prepare() -> DayPlan:
            if lazy_day:
                await workflow.execute_activity(
                    "day_prepare",
                    inp,
                    start_to_close_timeout=QUICK,
                    retry_policy=retry,
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
            else:
                await workflow.execute_activity("day_start", plan, start_to_close_timeout=QUICK)
            if not inp.canary:
                return plan
            skipped = await workflow.execute_activity(
                "canary",
                plan,
                result_type=list[Done],
                start_to_close_timeout=ITEM_TIMEOUT,
                heartbeat_timeout=dt.timedelta(seconds=inp.heartbeat_s),
                retry_policy=retry,
                cancellation_type=(
                    ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                    if lazy_day
                    else ActivityCancellationType.TRY_CANCEL
                ),
            )
            self.done += skipped
            gone = {(d.at, d.template) for d in skipped}
            return plan.model_copy(update={"items": [i for i in plan.items if (i.at, i.template) not in gone]})

        # Preserve the old histories' failure boundaries as well as their activity names.
        todo = plan
        if not lazy_day:
            todo = await prepare()
        try:
            if lazy_day:
                todo = await prepare()
            for item, interrupts in slots(todo):
                if inp.clock == "realtime":
                    # Late slots run at once (a late start); early ones wait, with a minute or two of human slack.
                    due = dt.datetime.combine(day, dt.time.fromisoformat(item.at), tz)
                    wait = (due - workflow.now()).total_seconds() + rnd.uniform(0, 120)
                    if wait > 0:
                        await workflow.sleep(wait)
                s = SlotInput(date=plan.date, item=item, interrupts=interrupts)
                if item.template == BREAK:
                    await workflow.execute_activity("break_start", s, start_to_close_timeout=QUICK, retry_policy=retry)
                    await workflow.sleep(item.minutes * 60 if inp.clock == "realtime" else 5)
                    back_in = await workflow.execute_activity(
                        "break_end", s, result_type=Done, start_to_close_timeout=QUICK, retry_policy=retry,
                    )  # fmt: skip
                    self.done.append(back_in)
                    # A failed unlock stops the day (no later slot acts on a locked desktop). Patched: histories
                    # recorded before this ran on after a failed break_end and still replay that way.
                    if back_in.status != "done" and workflow.patched("break-unlock-fail-closed-v1"):
                        raise ApplicationError(
                            f"break at {item.at}: unlock failed, day stopped", type="BreakUnlockFailed",
                            non_retryable=True,
                        )  # fmt: skip
                    # They came due while away: run them on return, at the break's logical end. The clock is set
                    # again, since a lease sweep may have reset it while this worker was gone.
                    back = back_at(day, item)
                    for late in interrupts:
                        self.done += await slot(SlotInput(date=plan.date, item=late, jump_at=f"{back:%H:%M}"))
                else:
                    self.done += await slot(s)
            if lazy_day:
                await workflow.execute_activity(
                    "day_end", DayEnd(plan=plan, done=self.done), start_to_close_timeout=QUICK
                )
        except (Exception, asyncio.CancelledError):  # do not leave a jumped clock or another day's active context
            if lazy_day or inp.clock == "time-jump":
                await workflow.execute_activity(
                    "day_abort" if lazy_day else "clock_reset",
                    start_to_close_timeout=QUICK,
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            raise
        if not lazy_day:
            await workflow.execute_activity("day_end", DayEnd(plan=plan, done=self.done), start_to_close_timeout=QUICK)
        return self.done


@dataclass
class Target:
    address: str  # host:port of the Temporal frontend
    namespace: str = "default"


def task_queue(plan: DayPlan) -> str:
    return f"ludius-day-{plan.persona}"


def workflow_id(plan: DayPlan) -> str:
    return f"day-{plan.persona}-{plan.date}"


SCHEDULED_DAY = "ScheduledDay"  # registered by the explicitly started long-lived worker
CATCHUP = dt.timedelta(hours=1)  # a fire missed by more than this (server down) is dropped, not replayed late


def schedule_id(persona: str) -> str:
    _persona_name(persona)
    return f"day-{persona}"


def schedule_for(
    persona: str,
    at: str,
    tz: str,
    clock: Clock = "realtime",
    *,
    paused: bool = True,
    canary: bool = False,
    day_offset: int = 0,
) -> Schedule:
    """Every day at `at` (HH:MM) in `tz`, start that persona's day. A day still running when the next fires is
    not doubled (SKIP); created paused unless asked, so defining a schedule never starts a desktop by itself."""
    _persona_name(persona)
    ZoneInfo(tz)  # an unknown zone fails here, not on the server
    if not re.fullmatch(r"\d{2}:\d{2}", at):
        raise ValueError("schedule time must be HH:MM")
    if not 0 <= day_offset <= 7 or (day_offset and clock != "time-jump"):
        raise ValueError("--day-offset is 0-7 and needs --clock time-jump (realtime would wait for that day)")
    t = dt.time.fromisoformat(at)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            SCHEDULED_DAY,
            ScheduledInput(persona=persona, tz=tz, clock=clock, canary=canary, day_offset=day_offset),
            id=schedule_id(persona),
            task_queue=f"ludius-day-{persona}",
        ),  # fmt: skip
        spec=ScheduleSpec(
            calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(t.hour)], minute=[ScheduleRange(t.minute)])],
            time_zone_name=tz,
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=CATCHUP),
        state=ScheduleState(paused=paused, note=f"{persona} daily at {at} {tz}, {clock}"),
    )


def plan_date(now: dt.datetime, tz: str) -> str:
    """The persona's local date at an aware instant: the date of the plan a fire replays."""
    if now.tzinfo is None:
        raise ValueError("plan_date needs an aware datetime")
    return now.astimezone(ZoneInfo(tz)).date().isoformat()


def scheduled_plan(runs: Path, persona: str, date: str) -> DayPlan:
    """The plan nightly planning wrote for that date. Missing is an error: a schedule replays plans and never
    generates one (no planner, no LLM call)."""
    _persona_name(persona)
    if dt.date.fromisoformat(date).isoformat() != date:
        raise ValueError("plan date must be YYYY-MM-DD")
    path = World(runs).plan_path(persona, date)
    if not path.exists():
        raise FileNotFoundError(f"no plan for {persona} on {date}: {path} (run `ludius plan` for that date)")
    plan = load_plan(path)
    if plan.persona != persona or plan.date != date:
        raise ValueError(f"{path} is the plan of {plan.persona} on {plan.date}, not {persona} on {date}")
    return plan


def _persona_name(persona: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", persona):
        raise ValueError("persona must be an account name, not a path")


async def connect(t: Target) -> Client:
    return await Client.connect(t.address, namespace=t.namespace, data_converter=pydantic_data_converter)


class DeskBusy(RuntimeError):
    pass


@contextmanager
def desk_lock(path: Path) -> Generator[None]:
    """One controller per desktop. The OS drops the lock when the process dies, so a crash never leaves it held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise DeskBusy(f"another controller is driving this desktop ({path})") from e
        yield
    finally:
        os.close(fd)  # closing releases the lock


async def play(
    client: Client, day: DayRunner, plan: DayPlan, clock: Clock, desk: Path,
    *, canary: Callable[[set[str]], dict[str, str]] | None = None, locked: bool = False, **timing: int,
) -> list[Done]:  # fmt: skip
    """Run (or resume, or read back) the persona-day workflow, with this process as its desktop's only worker.

    With `canary` (canary.run bound to this desktop), the day starts with its apps' drift canaries."""
    if not isinstance(day.log, HeartbeatLog):
        raise ValueError("durable days need a HeartbeatLog: slot heartbeats come from ground-truth writes")
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta()
    inp = DayInput(plan=plan, clock=clock, utc_offset_s=int(offset.total_seconds()), canary=canary is not None,
                   **timing)  # fmt: skip
    from ludius.scheduled import DayOwner

    owner = DayOwner(desk)
    with nullcontext() if locked else desk_lock(desk), ThreadPoolExecutor(max_workers=1) as pool:
        rec = owner.check(f"{client.namespace}/{workflow_id(plan)}")
        if rec and (rec.day.plan != plan or rec.day.clock != clock):
            raise ValueError("reserved day plan/clock changed")
        async with Worker(client, task_queue=task_queue(plan), workflows=[PersonaDay],
                          activities=DayActivities(day, canary, plan=plan).all(), activity_executor=pool,
                          max_concurrent_activities=1):  # fmt: skip
            try:
                handle = await client.start_workflow(
                    PersonaDay.run, inp, id=workflow_id(plan), task_queue=task_queue(plan),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )  # fmt: skip
            except WorkflowAlreadyStartedError:  # running: this worker picks it up; finished: read its result
                handle = client.get_workflow_handle_for(PersonaDay.run, workflow_id(plan))
            try:
                result = await handle.result()
                owner.release(f"{client.namespace}/{workflow_id(plan)}")
                return result
            except WorkflowFailureError as e:
                raise RuntimeError(f"workflow {workflow_id(plan)} failed: {e.cause}") from e


def run(
    address: str, day: DayRunner, plan: DayPlan, clock: Clock, desk: Path,
    *, canary: Callable[[set[str]], dict[str, str]] | None = None, locked: bool = False, **timing: int,
) -> list[Done]:  # fmt: skip
    async def main() -> list[Done]:
        return await play(
            await connect(Target(address)), day, plan, clock, desk, canary=canary, locked=locked, **timing
        )

    return asyncio.run(main())


def _now() -> int:
    return int(dt.datetime.now().timestamp() * 1000)
