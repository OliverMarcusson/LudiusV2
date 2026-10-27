"""The real ScheduledDay worker (scheduled.serve) against one private loopback Temporal dev server.

Uses only the Temporal CLI the SDK already cached; never downloads one. Fake desktop, temporary SQLite.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.metadata
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from temporalio.client import (
    Client,
    ScheduleActionExecutionStartWorkflow,
    ScheduleBackfill,
    ScheduleOverlapPolicy,
    WorkflowExecutionStatus,
    WorkflowFailureError,
)
from temporalio.common import SearchAttributeKey
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from test_day import FakeRunner, FakeSession, note

from ludius import durable, scheduled
from ludius.cli import load_templates
from ludius.day import DayRunner
from ludius.loop import Outcome
from ludius.planner import DayPlan
from ludius.tasks import Task

CLI = Path(tempfile.gettempdir()) / f"temporal-sdk-python-{importlib.metadata.version('temporalio')}.exe"
TZ = "Europe/Stockholm"
STARTED = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")


class GateRunner(FakeRunner):
    """The 09:30 slot blocks on its first attempt until the worker shuts down under it."""

    blocked = threading.Event()

    def run(self, task: Task, between: Any = None) -> Outcome:
        if task.inst.params["file_name"] == "note 0930" and not self.blocked.is_set():
            self.calls.append("blocked")
            self.blocked.set()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:  # the SDK raises its cancellation into this thread
                time.sleep(0.05)
            raise AssertionError("worker shutdown never cancelled the slot")
        return super().run(task, between)


@pytest.mark.service
def test_scheduled_worker_runs_triggered_days_once_and_keeps_a_pending_day_across_restart(tmp_path: Path) -> None:
    if not CLI.exists():
        pytest.skip(f"no cached Temporal CLI at {CLI}; not downloading one")
    asyncio.run(_scheduled_worker(tmp_path))


async def _scheduled_worker(tmp_path: Path) -> None:
    runs, desk, calls = tmp_path / "runs", tmp_path / "desk" / "vm.lock", list[str]()
    today = durable.plan_date(dt.datetime.now(dt.UTC), TZ)
    d2, d3 = "2026-10-21", "2026-10-22"
    for date, items in {today: [note("09:00")], d2: [note("09:00"), note("09:30")], d3: [note("09:00")]}.items():
        path = runs / "plans" / f"{date}-aberg.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DayPlan(persona="aberg", date=date, items=items).model_dump_json(), encoding="utf-8")

    @contextmanager
    def factory(day: durable.DayInput) -> Generator[DayRunner]:
        calls.append("open " + day.plan.date)
        log = durable.HeartbeatLog(tmp_path / f"{day.plan.date}.jsonl")
        try:
            yield DayRunner(GateRunner(log, calls), FakeSession(calls), load_templates(), log, day.clock, 0,  # type: ignore[arg-type]
                            sleep=lambda s: None)  # fmt: skip
        finally:
            calls.append("close " + day.plan.date)
            log.close()

    live: list[asyncio.Task[None]] = []

    def worker(client: Client) -> asyncio.Task[None]:
        live.append(asyncio.create_task(scheduled.serve(client, runs, "aberg", desk, factory, lambda d, a: {})))
        return live[-1]

    async def stop(task: asyncio.Task[None]) -> None:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def fired(n: int) -> tuple[str, str]:
        for _ in range(200):
            acts = (await sched.describe()).info.recent_actions
            if len(acts) >= n:
                started = acts[n - 1].action
                assert isinstance(started, ScheduleActionExecutionStartWorkflow)
                return started.workflow_id, started.first_execution_run_id
            await asyncio.sleep(0.1)
        raise AssertionError(f"schedule action {n} never started")

    async def result(run: tuple[str, str]) -> list[durable.Done]:
        handle = client.get_workflow_handle(run[0], run_id=run[1], result_type=list[durable.Done])
        return await asyncio.wait_for(handle.result(), 120)

    async def failure(run: tuple[str, str]) -> str:
        with pytest.raises(WorkflowFailureError) as e:
            await result(run)
        err: BaseException | None = e.value
        chain: list[str] = []
        while err is not None:
            chain.append(f"{type(err).__name__}: {err}")
            err = err.__cause__
        return " <- ".join(chain)

    def reservation() -> str | None:
        rec = scheduled.DayOwner(desk).read()
        return rec and rec.owner

    def at_nine(date: str) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(date), dt.time(9), ZoneInfo(TZ))

    def window(date: str, overlap: ScheduleOverlapPolicy | None = None) -> ScheduleBackfill:
        return ScheduleBackfill(
            at_nine(date) - dt.timedelta(minutes=1), at_nine(date) + dt.timedelta(minutes=1), overlap
        )

    async with await WorkflowEnvironment.start_local(  # pyright: ignore[reportUnknownMemberType]
        data_converter=pydantic_data_converter, ui=False, ip="127.0.0.1",
        dev_server_existing_path=str(CLI), dev_server_database_filename=str(tmp_path / "temporal.db"),
    ) as env:  # fmt: skip
        client = env.client
        sched = await client.create_schedule(
            durable.schedule_id("aberg"), durable.schedule_for("aberg", "09:00", TZ, "compressed")
        )
        try:
            task = worker(client)
            await asyncio.sleep(2)
            desc = await sched.describe()
            assert desc.schedule.state.paused and desc.info.num_actions == 0 and calls == []

            # Explicit trigger: the intended time is now, so today's existing plan runs as day-aberg-<today>.
            await sched.trigger()
            first = await fired(1)
            assert [d.status for d in await result(first)] == ["done"]
            child = await client.get_workflow_handle(f"day-aberg-{today}").describe()
            assert child.status == WorkflowExecutionStatus.COMPLETED
            assert calls == [f"open {today}", "normalize", "run note 0900", f"close {today}"]
            assert reservation() is None
            print("trigger", first, "->", child.id, child.run_id)

            # A repeat fire for the same date fails on the child id and never touches the desktop again.
            await sched.trigger()
            again = await fired(2)
            assert again[1] != first[1]
            why = await failure(again)
            assert "already" in why.lower(), why
            assert (await client.get_workflow_handle(child.id).describe()).run_id == child.run_id
            assert calls.count("run note 0900") == 1
            print("repeat", again, "rejected:", why)

            # Backfill emits a past intended timestamp: that date's plan is chosen, not today's.
            await sched.backfill(window(d2))
            pending = await fired(3)
            info = await client.get_workflow_handle(pending[0], run_id=pending[1]).describe()
            assert info.typed_search_attributes.get(STARTED) == at_nine(d2)
            await asyncio.to_thread(GateRunner.blocked.wait, 60)
            await stop(task)  # worker shutdown mid-slot: local resources close, the day keeps its desktop
            assert calls[-2:] == ["blocked", f"close {d2}"]
            assert reservation() == f"default/day-aberg-{d2}"

            # A different date fired (overlap forced) while d2 is pending cannot take the reserved desktop.
            await sched.backfill(window(d3, ScheduleOverlapPolicy.ALLOW_ALL))
            competing = await fired(4)
            task = worker(client)  # restart: reloads d2's frozen input from the reservation
            why = await failure(competing)
            assert f"desktop reserved by default/day-aberg-{d2}" in why, why
            assert [d.status for d in await result(pending)] == ["done", "done"]
            assert reservation() is None and f"open {d3}" not in calls
            assert calls.count(f"open {d2}") == 2 and calls.count("run note 0930") == 1
            assert '"item_retry"' in (tmp_path / f"{d2}.jsonl").read_text(encoding="utf-8")
            print("pending", pending, "competing", competing, "rejected:", why, "calls", calls)
            await stop(task)
            await sched.delete()
        finally:  # a failed assertion must not leave a worker polling a stopped server
            for t in live:
                t.cancel()
            await asyncio.gather(*live, return_exceptions=True)
