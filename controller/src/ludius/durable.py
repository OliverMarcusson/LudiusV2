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
import sys
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from ludius.day import DayRunner, ItemResult, slots
    from ludius.loop import GroundTruth
    from ludius.planner import BREAK, DayPlan, PlanItem

ITEM_TIMEOUT = dt.timedelta(hours=2)
QUICK = dt.timedelta(minutes=5)
NOT_RETRYABLE = ["KeyError", "ValueError"]  # template or plan errors do not get better by retrying


class DayInput(BaseModel):
    plan: DayPlan
    clock: str  # realtime | compressed | time-jump
    utc_offset_s: int  # the plan's times are wall-clock times in this offset
    heartbeat_s: int = 300  # longest a slot may go without a ground-truth write before it counts as hung
    retry_s: int = 30  # first retry backoff; at most 3 attempts per slot


class SlotInput(BaseModel):
    date: str
    item: PlanItem
    interrupts: list[PlanItem] = []
    jump: bool = True


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

    def __init__(self, day: DayRunner) -> None:
        self.day = day

    def _attempt(self, s: SlotInput) -> dt.date:
        info = activity.info()
        if info.attempt > 1:
            self.day.log.write(event="item_retry", host_ms=_now(), at=s.item.at, template=s.item.template,
                               attempt=info.attempt, activity=info.activity_type)  # fmt: skip
        return dt.date.fromisoformat(s.date)

    @activity.defn(name="day_start")
    def start(self, plan: DayPlan) -> None:
        self.day.start(plan)

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
        return [_done(r) for r in self.day.slot(self._attempt(s), s.item, s.interrupts, jump=s.jump)]

    @activity.defn(name="break_start")
    def break_start(self, s: SlotInput) -> None:
        self.day.begin_break(self._attempt(s), s.item)

    @activity.defn(name="break_end")
    def break_end(self, s: SlotInput) -> Done:
        return _done(self.day.end_break(self._attempt(s), s.item))

    def all(self) -> list[Callable[..., Any]]:
        return [self.start, self.finish, self.clock_reset, self.slot, self.break_start, self.break_end]


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

        await workflow.execute_activity("day_start", plan, start_to_close_timeout=QUICK)
        try:
            for item, interrupts in slots(plan):
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
                    self.done.append(await workflow.execute_activity(
                        "break_end", s, result_type=Done, start_to_close_timeout=QUICK, retry_policy=retry,
                    ))  # fmt: skip
                    for late in interrupts:  # they came due while away: run them on return, no clock set
                        self.done += await slot(SlotInput(date=plan.date, item=late, jump=False))
                else:
                    self.done += await slot(s)
        except (Exception, asyncio.CancelledError):  # failed or cancelled mid-day: do not leave a jumped clock
            if inp.clock == "time-jump":
                await workflow.execute_activity("clock_reset", start_to_close_timeout=QUICK,
                                                retry_policy=RetryPolicy(maximum_attempts=5))  # fmt: skip
            raise
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


async def play(client: Client, day: DayRunner, plan: DayPlan, clock: str, desk: Path, **timing: int) -> list[Done]:
    """Run (or resume, or read back) the persona-day workflow, with this process as its desktop's only worker."""
    if not isinstance(day.log, HeartbeatLog):
        raise ValueError("durable days need a HeartbeatLog: slot heartbeats come from ground-truth writes")
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta()
    inp = DayInput(plan=plan, clock=clock, utc_offset_s=int(offset.total_seconds()), **timing)
    with desk_lock(desk), ThreadPoolExecutor(max_workers=1) as pool:
        async with Worker(client, task_queue=task_queue(plan), workflows=[PersonaDay],
                          activities=DayActivities(day).all(), activity_executor=pool,
                          max_concurrent_activities=1):  # fmt: skip
            try:
                handle = await client.start_workflow(
                    PersonaDay.run, inp, id=workflow_id(plan), task_queue=task_queue(plan),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )  # fmt: skip
            except WorkflowAlreadyStartedError:  # running: this worker picks it up; finished: read its result
                handle = client.get_workflow_handle_for(PersonaDay.run, workflow_id(plan))
            try:
                return await handle.result()
            except WorkflowFailureError as e:
                raise RuntimeError(f"workflow {workflow_id(plan)} failed: {e.cause}") from e


def run(address: str, day: DayRunner, plan: DayPlan, clock: str, desk: Path, **timing: int) -> list[Done]:
    async def main() -> list[Done]:
        return await play(await connect(Target(address)), day, plan, clock, desk, **timing)

    return asyncio.run(main())


def _now() -> int:
    return int(dt.datetime.now().timestamp() * 1000)
