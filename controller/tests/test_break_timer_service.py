"""A real durable BREAK timer across a worker shutdown/restart, on one private loopback Temporal dev server.

Uses only the Temporal CLI the SDK already cached; never downloads one. Fake desktop, temporary SQLite.
The break is already late (00:00), so its away timer (a real minute) is the only timer the day waits on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.metadata
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from test_day import FakeRunner, FakeSession, note

from ludius import durable, scheduled
from ludius.cli import load_templates
from ludius.day import DayRunner
from ludius.planner import DayPlan, PlanItem

CLI = Path(tempfile.gettempdir()) / f"temporal-sdk-python-{importlib.metadata.version('temporalio')}.exe"
TZ = "Europe/Stockholm"


class Session(FakeSession):
    """Records when the screen is unlocked; `unlock_ok=False` is a failed unlock."""

    def __init__(self, calls: list[str], unlocks: list[dt.datetime], unlock_ok: bool) -> None:
        super().__init__(calls)
        self.unlocks, self.unlock_ok = unlocks, unlock_ok

    def unlock(self) -> bool:
        self.unlocks.append(dt.datetime.now(dt.UTC))
        super().unlock()
        return self.unlock_ok


@pytest.mark.service
@pytest.mark.parametrize("unlock_ok", [True, False], ids=["unlocked", "unlock-fails"])
def test_break_away_timer_survives_a_worker_restart(tmp_path: Path, unlock_ok: bool) -> None:
    if not CLI.exists():
        pytest.skip(f"no cached Temporal CLI at {CLI}; not downloading one")
    now = dt.datetime.now(ZoneInfo(TZ))
    if now.hour == 23 and now.minute >= 55 or now.hour == 0 and now.minute < 5:
        pytest.skip("00:00 slots would not be late, or the day would cross midnight")
    asyncio.run(_break_across_restart(tmp_path, unlock_ok))


async def _break_across_restart(tmp_path: Path, unlock_ok: bool) -> None:
    runs, desk, calls = tmp_path / "runs", tmp_path / "desk" / "vm.lock", list[str]()
    unlocks = list[dt.datetime]()
    today = durable.plan_date(dt.datetime.now(dt.UTC), TZ)
    plan = runs / "plans" / f"{today}-aberg.yaml"
    plan.parent.mkdir(parents=True)
    items = [PlanItem(at="00:00", template="break", minutes=1), note("00:01")]
    plan.write_text(DayPlan(persona="aberg", date=today, items=items).model_dump_json(), encoding="utf-8")

    @contextmanager
    def factory(day: durable.DayInput) -> Generator[DayRunner]:
        calls.append("open")
        log = durable.HeartbeatLog(tmp_path / "day.jsonl")
        try:
            yield DayRunner(FakeRunner(log, calls), Session(calls, unlocks, unlock_ok), load_templates(), log,  # type: ignore[arg-type]
                            day.clock, 0, sleep=lambda s: None)  # fmt: skip
        finally:
            calls.append("close")
            log.close()

    async def stop(task: asyncio.Task[None]) -> None:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def reservation() -> str | None:
        rec = scheduled.DayOwner(desk).read()
        return rec and rec.owner

    owner = f"default/day-aberg-{today}"
    live: list[asyncio.Task[None]] = []
    async with await WorkflowEnvironment.start_local(  # pyright: ignore[reportUnknownMemberType]
        data_converter=pydantic_data_converter, ui=False, ip="127.0.0.1",
        dev_server_existing_path=str(CLI), dev_server_database_filename=str(tmp_path / "temporal.db"),
    ) as env:  # fmt: skip
        client = env.client
        sched = await client.create_schedule(
            durable.schedule_id("aberg"), durable.schedule_for("aberg", "09:00", TZ, "realtime")
        )
        try:
            live.append(asyncio.create_task(scheduled.serve(client, runs, "aberg", desk, factory, lambda d, a: {})))
            await sched.trigger()
            child = client.get_workflow_handle(f"day-aberg-{today}", result_type=list[durable.Done])

            async def events() -> list[Any]:
                return [e async for e in child.fetch_history_events()]

            started: list[Any] = []
            for _ in range(300):  # break_start finished (screen locked) and the away timer is pending
                try:
                    started = [e for e in await events() if e.HasField("timer_started_event_attributes")]
                except Exception:  # noqa: BLE001 - the child does not exist until the schedule fires
                    started = []
                if started:
                    break
                await asyncio.sleep(0.1)
            assert started, "the break never reached its away timer"
            timer = started[0].timer_started_event_attributes
            fire_at = started[0].event_time.ToDatetime(dt.UTC) + timer.start_to_fire_timeout.ToTimedelta()
            assert timer.start_to_fire_timeout.ToTimedelta() == dt.timedelta(minutes=1)
            assert dt.datetime.now(dt.UTC) < fire_at - dt.timedelta(seconds=10), "too close to due to prove a restart"
            assert calls == ["open", "normalize", "lock"] and reservation() == owner

            await stop(live[0])  # worker shutdown only: the workflow is never cancelled
            assert (await child.describe()).status == WorkflowExecutionStatus.RUNNING
            assert calls == ["open", "normalize", "lock", "close"] and reservation() == owner and unlocks == []
            live.append(asyncio.create_task(scheduled.serve(client, runs, "aberg", desk, factory, lambda d, a: {})))
            await asyncio.sleep(3)  # worker back and replaying with the away timer still pending
            assert (await child.describe()).status == WorkflowExecutionStatus.RUNNING
            assert calls == ["open", "normalize", "lock", "close"] and unlocks == [] and reservation() == owner
            assert dt.datetime.now(dt.UTC) < fire_at, "restart took longer than the away timer"

            if unlock_ok:
                done = await asyncio.wait_for(child.result(), 120)
                assert [(d.template, d.status) for d in done] == [("break", "done"), ("notepad_write_note", "done")]
                # One lock (before the stop), one unlock (after the timer fired), then the later task, then release.
                after = ["unlock", "normalize", "run note 0001", "close"]
            else:  # a failed unlock stops the day: no later task, the abort cleans up and releases, the day fails
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(child.result(), 120)
                after = ["unlock", "close"]
            assert calls == ["open", "normalize", "lock", "close", "open", *after]
            assert len(unlocks) == 1 and unlocks[0] >= fire_at - dt.timedelta(seconds=1), (unlocks, fire_at)
            assert reservation() is None

            seq: list[str] = []
            for e in await events():
                if e.HasField("timer_started_event_attributes"):
                    seq.append("timer_started")
                elif e.HasField("timer_fired_event_attributes"):
                    seq.append("timer_fired")
                elif e.HasField("activity_task_scheduled_event_attributes"):
                    seq.append(e.activity_task_scheduled_event_attributes.activity_type.name)
            assert seq == [
                "day_prepare",
                "break_start",
                "timer_started",
                "timer_fired",
                "break_end",
                *(["slot", "day_end"] if unlock_ok else ["day_abort"]),
            ], seq
            print("calls", calls, "events", seq, "timer fires", fire_at.isoformat(), "unlocked", unlocks[0].isoformat())
            await stop(live[-1])
            await sched.delete()
        finally:
            for t in live:
                t.cancel()
            await asyncio.gather(*live, return_exceptions=True)
