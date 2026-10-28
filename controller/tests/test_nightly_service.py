"""A real calendar-fired nightly planning occurrence, then the playback workflow reading its plan, on one private
loopback Temporal dev server.

Nightly side (real): `nightly.schedule_for` is created paused at the next local minute (a morning minute here, to
exercise the date computation; production stays 22:00), unpaused normally, and the server's own clock fires it. No
trigger, backfill, clock change or time skipping. The scripted model and sources factory are injected into the real
`nightly.serve`; the plan is generated for the intended occurrence's local date + 1.

Playback side (SYNTHETIC start): the next day's calendar is not fired. `ScheduledDay` is started directly with an
explicit TemporalScheduledStartTime of occurrence + 1 day, only to prove it reads the plan nightly wrote and runs it
once on a fake desktop.

Uses only the Temporal CLI the SDK already cached; never downloads one. No VM, Decisions, LLM network or paid call.
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
from temporalio.client import ScheduleActionExecutionStartWorkflow, WorkflowExecutionStatus
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from test_day import FakeRunner, FakeSession
from test_nightly import ScriptedLlm
from test_nightly import _world as save_world  # pyright: ignore[reportPrivateUsage]

from ludius import cli, durable, nightly, planner, scheduled
from ludius.cli import load_templates
from ludius.day import DayRunner

CLI = Path(tempfile.gettempdir()) / f"temporal-sdk-python-{importlib.metadata.version('temporalio')}.exe"
TZ = "Europe/Stockholm"
STARTED = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
HEADROOM = dt.timedelta(seconds=25)  # worker start + a paused check + unpause must fit before the occurrence
ITEM = {"at": "09:00", "template": "notepad_write_note",
        "params": {"file_name": "lease memo", "body": "Draft the lease accounting memo."},
        "context": "Writes up the lease rules before the close."}  # fmt: skip


@pytest.mark.service
def test_calendar_fired_nightly_plan_is_read_by_the_scheduled_day(tmp_path: Path) -> None:
    if not CLI.exists():
        pytest.skip(f"no cached Temporal CLI at {CLI}; not downloading one")
    now = dt.datetime.now(ZoneInfo(TZ))
    target = (now + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    if target - now < HEADROOM:
        target += dt.timedelta(minutes=1)
    if target.date() != now.date():
        pytest.skip("occurrence would land after midnight, on a different plan date")
    asyncio.run(_nightly_then_playback(tmp_path, target))


async def _nightly_then_playback(tmp_path: Path, target: dt.datetime) -> None:
    runs, desk, calls = tmp_path / "runs", tmp_path / "desk" / "vm.lock", list[str]()
    save_world(runs)
    day = target.date() + dt.timedelta(days=1)  # the local day after the intended occurrence
    path = runs / "plans" / f"{day}-aberg.yaml"
    llm, made = ScriptedLlm({"items": [ITEM]}), list[str]()

    def make_llm() -> Any:
        made.append("llm")
        if len(made) > 1:
            raise ApplicationError("an existing plan must not reach the model again", non_retryable=True)
        return llm

    @contextmanager
    def factory(d: durable.DayInput) -> Generator[DayRunner]:
        calls.append("open " + d.plan.date)
        log = durable.HeartbeatLog(tmp_path / f"{d.plan.date}.jsonl")
        try:
            yield DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, d.clock, 0,  # type: ignore[arg-type]
                            sleep=lambda s: None)  # fmt: skip
        finally:
            calls.append("close " + d.plan.date)
            log.close()

    def reservation() -> str | None:
        rec = scheduled.DayOwner(desk).read()
        return rec and rec.owner

    workers: list[asyncio.Task[None]] = []
    async with await WorkflowEnvironment.start_local(  # pyright: ignore[reportUnknownMemberType]
        data_converter=pydantic_data_converter, ui=False, ip="127.0.0.1",
        dev_server_existing_path=str(CLI), dev_server_database_filename=str(tmp_path / "temporal.db"),
    ) as env:  # fmt: skip
        client = env.client
        sched = await client.create_schedule(  # paused by default
            nightly.schedule_id("aberg"), nightly.schedule_for("aberg", target.strftime("%H:%M"), TZ)
        )
        try:
            workers.append(asyncio.create_task(nightly.serve(client, runs, cli.plan_sources, make_llm)))
            await asyncio.sleep(3)
            desc = await sched.describe()
            assert desc.schedule.state.paused and desc.info.num_actions == 0 and not path.exists() and made == []
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
            assert started.workflow_id.startswith(nightly.schedule_id("aberg")), started.workflow_id

            run = client.get_workflow_handle(
                started.workflow_id, run_id=started.first_execution_run_id, result_type=nightly.Planned
            )
            planned = await asyncio.wait_for(run.result(), 120)  # pydantic in/out through the real converter
            assert (await run.describe()).typed_search_attributes.get(STARTED) == target
            assert planned == nightly.Planned(
                persona="aberg", date=day.isoformat(), path=str(path), generated=True, items=1,
                rejects=planned.rejects, note=planned.note,
            )  # fmt: skip
            assert planned.date == day.isoformat() != target.date().isoformat()
            plan = planner.load_plan(path)
            assert (plan.persona, plan.date) == ("aberg", day.isoformat())
            assert [(i.at, i.template, i.params) for i in plan.items] == [
                ("09:00", "notepad_write_note", ITEM["params"])
            ]
            assert len(llm.prompts) == 1 and made == ["llm"] and f"{day:%A} {day}" in llm.prompts[0]
            before = path.read_bytes()

            # An unchanged retry (a second workflow with the same intended occurrence) keeps the plan, no model call.
            retry = await client.execute_workflow(
                "NightlyPlan", nightly.NightlyInput(persona="aberg", tz=TZ), id="nightly-retry-aberg",
                task_queue=nightly.TASK_QUEUE, result_type=nightly.Planned,
                search_attributes=TypedSearchAttributes([SearchAttributePair(STARTED, target)]),
            )  # fmt: skip
            assert (retry.generated, retry.path, retry.date, retry.items) == (False, str(path), day.isoformat(), 1)
            assert path.read_bytes() == before and len(llm.prompts) == 1 and made == ["llm"]

            # SYNTHETIC playback start (not a calendar fire): occurrence + 1 day so ScheduledDay reads this plan;
            # compressed pacing, since the plan's 09:00 is tomorrow and a realtime day would sleep until then.
            workers.append(asyncio.create_task(scheduled.serve(client, runs, "aberg", desk, factory, lambda d, a: {})))
            synthetic = target + dt.timedelta(days=1)
            play = await client.start_workflow(
                scheduled.ScheduledDay.run, durable.ScheduledInput(persona="aberg", tz=TZ, clock="compressed"),
                id="synthetic-playback-aberg", task_queue="ludius-day-aberg",
                search_attributes=TypedSearchAttributes([SearchAttributePair(STARTED, synthetic)]),
            )  # fmt: skip
            done = await asyncio.wait_for(play.result(), 120)
            assert [(d.template, d.status) for d in done] == [("notepad_write_note", "done")]
            child = await client.get_workflow_handle(f"day-aberg-{day}").describe()
            assert child.status == WorkflowExecutionStatus.COMPLETED
            assert calls == [f"open {day}", "normalize", "run lease memo", f"close {day}"]
            assert reservation() is None and path.read_bytes() == before and len(llm.prompts) == 1
            print("REAL calendar fire: target", target.isoformat(), "scheduled_at", action.scheduled_at.isoformat(),
                  "actual", action.started_at.isoformat(), "workflow", started.workflow_id, "planned", planned,
                  "prompts", len(llm.prompts), "| retry", retry.generated,
                  "| SYNTHETIC playback", play.id, synthetic.isoformat(), "child", child.id,
                  "calls", calls)  # fmt: skip
        finally:
            await sched.pause(note="nightly test done")
            await sched.delete()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
