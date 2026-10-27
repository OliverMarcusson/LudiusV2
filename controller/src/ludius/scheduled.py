"""Lazy scheduled days on an explicitly started worker. No services or schedules are installed on import.

The desktop's OS lock fences controller processes. A small persistent owner record additionally fences whole
days, including durable timers: one activity thread alone would still interleave two workflows' clocks.
The record freezes the plan/input for crash recovery and is removed only on successful finish/abort. Shutdown
closes local resources and resets the clock but keeps ownership for that workflow's next worker. A terminated
workflow can therefore leave a reservation requiring operator reconciliation (reconcile, `ludius reservation`);
it is never stolen automatically.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import RetryPolicy, SearchAttributeKey, TypedSearchAttributes, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from ludius import durable
    from ludius.clocklease import STALE_S
    from ludius.day import DayRunner
    from ludius.planner import DayPlan


class Reservation(BaseModel):
    owner: str  # namespace/workflow id
    day: durable.DayInput


class DayOwner:
    """Used only while holding the desktop OS lock; malformed reservations fail closed."""

    def __init__(self, desk: Path) -> None:
        self.path = desk.with_suffix(".day.json")

    def read(self) -> Reservation | None:
        return Reservation.model_validate_json(self.path.read_text(encoding="utf-8")) if self.path.exists() else None

    def check(self, owner: str) -> Reservation | None:
        rec = self.read()
        if rec and rec.owner != owner:
            raise ValueError(f"desktop reserved by {rec.owner}; resume or reconcile that day first")
        return rec

    def claim(self, owner: str, inp: durable.DayInput) -> Reservation:
        rec = self.check(owner)
        if rec:
            if rec.day != inp:
                raise ValueError("reserved day input changed; refusing to mix plans or clock modes")
            return rec
        rec = Reservation(owner=owner, day=inp)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8") as f:
            f.write(rec.model_dump_json())
        return rec

    def release(self, owner: str) -> None:
        if self.check(owner):
            self.path.unlink()


class Fire(BaseModel):
    schedule: durable.ScheduledInput
    occurrence: dt.datetime


def occurrence_time(attrs: TypedSearchAttributes) -> dt.datetime:
    """Temporal supplies the intended timestamp, independent of catchup/worker delays. Never use now()."""
    at = attrs.get(SearchAttributeKey.for_datetime("TemporalScheduledStartTime"))
    if not isinstance(at, dt.datetime) or at.tzinfo is None:
        raise ValueError("missing aware TemporalScheduledStartTime; refusing to guess a plan date")
    return at


def load_day(runs: Path, persona: str, fire: Fire) -> durable.DayInput:
    if fire.schedule.persona != persona:
        raise ValueError("schedule persona differs from this worker")
    at = fire.occurrence
    date = durable.plan_date(at, fire.schedule.tz)
    if fire.schedule.day_offset:
        date = (dt.date.fromisoformat(date) + dt.timedelta(days=fire.schedule.day_offset)).isoformat()
    plan = durable.scheduled_plan(runs, persona, date)
    offset = at.astimezone(ZoneInfo(fire.schedule.tz)).utcoffset()
    assert offset is not None
    return durable.DayInput(
        plan=plan, clock=fire.schedule.clock, utc_offset_s=int(offset.total_seconds()), canary=fire.schedule.canary
    )


@workflow.defn(name="ScheduledDay")
class ScheduledDay:
    @workflow.run
    async def run(self, inp: durable.ScheduledInput) -> list[durable.Done]:
        try:
            at = occurrence_time(workflow.info().typed_search_attributes)
        except ValueError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        day = await workflow.execute_activity(
            "load_scheduled_day",
            Fire(schedule=inp, occurrence=at),
            result_type=durable.DayInput,
            start_to_close_timeout=durable.QUICK,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return await workflow.execute_child_workflow(
            durable.PersonaDay.run,
            day,
            id=durable.workflow_id(day.plan),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )


DayFactory = Callable[[durable.DayInput], AbstractContextManager[DayRunner]]
Canary = Callable[[DayRunner, set[str]], dict[str, str]]


class WorkerActivities(durable.DayActivities):
    def __init__(self, runs: Path, persona: str, desk: Path, factory: DayFactory, canary: Canary) -> None:
        self.runs, self.persona, self.owner = runs, persona, DayOwner(desk)
        self.factory, self._canary = factory, canary
        self._context: ExitStack | None = None
        self._day: DayRunner | None = None
        self._bound: str | None = None
        self.canary_run: Callable[[set[str]], dict[str, str]] = self._run_canary

    def _run_canary(self, apps: set[str]) -> dict[str, str]:
        return self._canary(self.day, apps)

    def _key(self) -> str:
        info = activity.info()
        return f"{info.workflow_namespace}/{info.workflow_id}"

    @property
    def day(self) -> DayRunner:
        key = self._key()
        rec = self.owner.check(key)
        if rec is None:
            raise ValueError("day not prepared; refusing to guess runner state")
        if self._bound is not None and self._bound != key:
            raise ValueError("another day still owns the local context")
        if self._day is None:
            stack = ExitStack()
            try:
                self._day = stack.enter_context(self.factory(rec.day))
            except BaseException:
                stack.close()
                raise
            self._context, self._bound = stack, key
        return self._day

    @activity.defn(name="load_scheduled_day")
    def load(self, fire: Fire) -> durable.DayInput:
        return load_day(self.runs, self.persona, fire)

    @activity.defn(name="day_start")
    def start(self, plan: DayPlan) -> None:
        raise ValueError("legacy day history needs the one-shot day worker; it has no frozen clock input")

    @activity.defn(name="day_prepare")
    def prepare(self, inp: durable.DayInput) -> None:
        if inp.plan.persona != self.persona:
            raise ValueError("plan persona differs from this worker")
        if activity.info().workflow_id != durable.workflow_id(inp.plan):
            raise ValueError("plan date/persona differs from its workflow id")
        self.owner.claim(self._key(), inp)
        self.day.start(inp.plan)

    @activity.defn(name="day_end")
    def finish(self, e: durable.DayEnd) -> None:
        if not self._reserved():
            return  # a retry after this day finished and released: never log/reset again or touch the next day
        super().finish(e)
        self.close(release=self._key())

    @activity.defn(name="day_abort")
    def abort(self) -> None:
        # A rejected competing workflow must never reset the active day's clock or release its reservation.
        rec = self._reserved()
        if rec is None:
            return
        if rec.day.clock == "time-jump":
            super().clock_reset()  # an unopenable runner fails closed here: the reservation stays for recovery
        self.close(release=rec.owner)  # other clocks never jumped, so release even if the runner never opened

    def _reserved(self) -> Reservation | None:
        rec = self.owner.read()
        return rec if rec and rec.owner == self._key() else None

    def close(self, *, release: str | None = None) -> None:
        try:
            if self._context:
                self._context.close()  # the factory resets its own clock before releasing resources
        finally:
            self._context = self._day = self._bound = None
        if release:
            self.owner.release(release)

    def all(self) -> list[Callable[..., Any]]:
        return [*super().all(), self.load]


async def serve(client: Client, runs: Path, persona: str, desk: Path, factory: DayFactory, canary: Canary) -> None:
    """Poll only while explicitly running. No VM start/restore, plan generation, or service installation."""
    with durable.desk_lock(desk), ThreadPoolExecutor(max_workers=1) as pool:
        acts = WorkerActivities(runs, persona, desk, factory, canary)
        try:
            async with Worker(
                client,
                task_queue=f"ludius-day-{persona}",
                workflows=[ScheduledDay, durable.PersonaDay],
                activities=acts.all(),
                activity_executor=pool,
                max_concurrent_activities=1,
            ):
                await asyncio.Event().wait()
        finally:
            pool.shutdown(wait=True)  # no cleanup/reset while an activity still touches the desktop
            acts.close()  # retain reservation: shutting down a worker does not finish its server-side day


# Only a server-confirmed closed run may lose its reservation. CONTINUED_AS_NEW has a successor run; RUNNING is live.
CLOSED = frozenset(
    {
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
    }
)
Status = Callable[[str, str], WorkflowExecutionStatus | None]  # (namespace, workflow id) -> server status


def _lease_live(lease: Path) -> bool:
    """A lease renewed within STALE_S belongs to a live controller. A malformed one fails closed as live."""
    if not lease.exists():
        return False
    try:
        renewed = dt.datetime.fromisoformat(json.loads(lease.read_text(encoding="utf-8"))["renewed"])
        return dt.datetime.now(dt.UTC) - renewed < dt.timedelta(seconds=STALE_S)
    except (ValueError, KeyError, TypeError):
        return True


def reconcile(
    desk: Path, lease: Path, status: Status, reset: Callable[[], None], *, confirm: tuple[str, str] | None = None
) -> dict[str, Any]:
    """Operator inspection of a desktop's day reservation; a dry run unless confirm=(vm, owner) repeats both.

    Holds the desktop lock throughout, so it refuses while a worker/controller drives the desktop. A missing,
    unreachable, unknown or open workflow never clears. A time-jump day's clock is reset first; a failed reset
    keeps both reservation and lease for the sweeper/next attempt.
    """
    vm = desk.stem
    with durable.desk_lock(desk):
        owner = DayOwner(desk)
        rec = owner.read()
        if rec is None:
            if confirm is not None:
                raise ValueError(f"{vm}: no reservation to clear")
            return {"vm": vm, "reservation": None}
        st: WorkflowExecutionStatus | None = None
        try:
            ns, _, wid = rec.owner.partition("/")
            if not ns or not wid:
                raise ValueError("owner is not namespace/workflow-id")
            st = status(ns, wid)
            state = st.name if st is not None else "UNKNOWN"
        except Exception as e:  # unreachable server, missing workflow, timeout: all unknown
            state = f"UNKNOWN ({type(e).__name__}: {e})"
        out: dict[str, Any] = {
            "vm": vm,
            "owner": rec.owner,
            "persona": rec.day.plan.persona,
            "date": rec.day.plan.date,
            "clock": rec.day.clock,
            "workflow": state,
            "clearable": st in CLOSED,
            "cleared": False,
        }
        if confirm is None:
            return out
        if confirm != (vm, rec.owner):
            raise ValueError("clear must repeat this VM and the exact reservation owner")
        if st not in CLOSED:
            raise ValueError(f"{rec.owner} is {state}, not confirmed closed; reservation kept")
        if owner.read() != rec:
            raise ValueError("reservation changed during reconciliation; refusing")
        if rec.day.clock == "time-jump":
            if _lease_live(lease):
                raise ValueError("a live controller holds this VM's clock lease; not resetting its clock")
            reset()  # raises on failure: the reservation (and lease) stay
        owner.release(rec.owner)
        out["cleared"] = True
        return out
