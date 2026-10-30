from __future__ import annotations

import asyncio
import datetime as dt
import importlib.metadata
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from temporalio.client import ScheduleActionExecutionStartWorkflow, WorkflowExecutionStatus
from temporalio.common import SearchAttributeKey
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from test_day import FakeRunner, FakeSession, note

from ludius import durable, scheduled
from ludius.cli import load_templates
from ludius.day import DayRunner
from ludius.planner import DayPlan

CLI = Path(tempfile.gettempdir()) / f"temporal-sdk-python-{importlib.metadata.version('temporalio')}.exe"
TZ = "Europe/Stockholm"
STARTED = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
HEADROOM = dt.timedelta(seconds=25)


@pytest.mark.service
def test_calendar_fires_one_scheduled_day_at_its_intended_occurrence(tmp_path: Path) -> None:
    if not CLI.exists():
        pytest.skip(f"no cached Temporal CLI at {CLI}; not downloading one")
    now = dt.datetime.now(ZoneInfo(TZ))
    target = (now + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    if target - now < HEADROOM:
        target += dt.timedelta(minutes=1)
    if target.date() != now.date():
        pytest.skip("occurrence would land after midnight, on the next plan date")
    asyncio.run(_calendar_fire(tmp_path, target))


async def _calendar_fire(tmp_path: Path, target: dt.datetime) -> None:
    runs, desk, calls = tmp_path / "runs", tmp_path / "desk" / "vm.lock", list[str]()
    date = target.date().isoformat()
    plan = runs / "plans" / f"{date}-aberg.yaml"
    plan.parent.mkdir(parents=True)
    plan.write_text(DayPlan(persona="aberg", date=date, items=[note("09:00")]).model_dump_json(), encoding="utf-8")

    @contextmanager
    def factory(day: durable.DayInput) -> Generator[DayRunner]:
        calls.append("open " + day.plan.date)
        log = durable.HeartbeatLog(tmp_path / f"{day.plan.date}.jsonl")
        try:
            yield DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, day.clock, 0,  # type: ignore[arg-type]
                            sleep=lambda s: None)  # fmt: skip
        finally:
            calls.append("close " + day.plan.date)
            log.close()

    def reservation() -> str | None:
        rec = scheduled.DayOwner(desk).read()
        return rec and rec.owner

    async with await WorkflowEnvironment.start_local(  # pyright: ignore[reportUnknownMemberType]
        data_converter=pydantic_data_converter, ui=False, ip="127.0.0.1",
        dev_server_existing_path=str(CLI), dev_server_database_filename=str(tmp_path / "temporal.db"),
    ) as env:  # fmt: skip
        client = env.client
        sched = await client.create_schedule(
            durable.schedule_id("aberg"), durable.schedule_for("aberg", target.strftime("%H:%M"), TZ, "compressed")
        )
        worker = asyncio.create_task(scheduled.serve(client, runs, "aberg", desk, factory, lambda d, a: {}))
        try:
            await asyncio.sleep(3)
            desc = await sched.describe()
            assert desc.schedule.state.paused and desc.info.num_actions == 0 and calls == []
            assert dt.datetime.now(ZoneInfo(TZ)) < target - dt.timedelta(seconds=10), "too late to unpause safely"
            await sched.unpause()
            assert not (await sched.describe()).schedule.state.paused

            action = None
            deadline = target + dt.timedelta(seconds=90)
            while dt.datetime.now(ZoneInfo(TZ)) < deadline:
                acts = (await sched.describe()).info.recent_actions
                if acts:
                    action = acts[0]
                    break
                await asyncio.sleep(1)
            assert action is not None, f"the calendar never fired the {target:%H:%M} occurrence"
            started = action.action
            assert isinstance(started, ScheduleActionExecutionStartWorkflow)
            assert action.scheduled_at == target
            assert started.workflow_id.startswith(durable.schedule_id("aberg")), started.workflow_id

            run = client.get_workflow_handle(
                started.workflow_id, run_id=started.first_execution_run_id, result_type=list[durable.Done]
            )
            assert [d.status for d in await asyncio.wait_for(run.result(), 120)] == ["done"]
            attrs = (await run.describe()).typed_search_attributes
            assert attrs.get(STARTED) == target
            child = await client.get_workflow_handle(f"day-aberg-{date}").describe()
            assert child.status == WorkflowExecutionStatus.COMPLETED
            assert calls == [f"open {date}", "normalize", "run note 0900", f"close {date}"]
            assert reservation() is None
            assert (await sched.describe()).info.num_actions == 1
            print("target", target.isoformat(), "scheduled_at", action.scheduled_at.isoformat(),
                  "actual", action.started_at.isoformat(), "workflow", started.workflow_id,
                  "search attr", attrs.get(STARTED), "child", child.id, child.run_id, "calls", calls)  # fmt: skip
        finally:
            await sched.pause(note="calendar test done")
            await sched.delete()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
