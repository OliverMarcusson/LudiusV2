from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.client import ScheduleActionStartWorkflow, ScheduleOverlapPolicy
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes
from test_day import FakeRunner, FakeSession, note

from ludius import durable, scheduled
from ludius.cli import load_templates
from ludius.day import DayRunner
from ludius.planner import DayPlan


def test_schedule_is_paused_bounded_and_never_overlaps() -> None:
    s = durable.schedule_for("aberg", "08:15", "Europe/Stockholm", canary=True)
    assert s.state.paused and s.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert s.policy.catchup_window == dt.timedelta(hours=1)
    assert s.spec.time_zone_name == "Europe/Stockholm"
    assert s.spec.calendars[0].hour[0].start == 8 and s.spec.calendars[0].minute[0].start == 15
    assert isinstance(s.action, ScheduleActionStartWorkflow)
    assert s.action.workflow == "ScheduledDay" and s.action.task_queue == "ludius-day-aberg"
    assert s.action.args == [durable.ScheduledInput(persona="aberg", tz="Europe/Stockholm", canary=True)]
    assert not durable.schedule_for("aberg", "08:15", "UTC", paused=False).state.paused
    for at in ("08:15:12", "8:15", "24:00"):
        with pytest.raises(ValueError):
            durable.schedule_for("aberg", at, "UTC")
    with pytest.raises(ValueError, match="not a path"):
        durable.schedule_for("../aberg", "08:15", "UTC")


def test_occurrence_date_and_plan_identity_are_explicit(tmp_path: Path) -> None:
    # Midnight and the repeated Stockholm autumn hour use the intended occurrence, not worker wall time.
    assert durable.plan_date(dt.datetime(2026, 10, 26, 23, 30, tzinfo=dt.UTC), "Europe/Stockholm") == "2026-10-27"
    for hour in (0, 1):
        assert durable.plan_date(dt.datetime(2026, 10, 25, hour, 30, tzinfo=dt.UTC), "Europe/Stockholm") == "2026-10-25"
    with pytest.raises(ValueError, match="aware"):
        durable.plan_date(dt.datetime(2026, 10, 27), "UTC")
    with pytest.raises(FileNotFoundError, match="no plan"):
        durable.scheduled_plan(tmp_path, "aberg", "2026-10-27")
    folder = tmp_path / "plans"
    folder.mkdir()
    path = folder / "2026-10-27-aberg.yaml"
    path.write_text("persona: lnord\ndate: '2026-10-27'\nitems: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not aberg"):
        durable.scheduled_plan(tmp_path, "aberg", "2026-10-27")
    path.write_text("persona: aberg\ndate: '2026-10-27'\nitems: []\n", encoding="utf-8")
    assert durable.scheduled_plan(tmp_path, "aberg", "2026-10-27").persona == "aberg"
    with pytest.raises(ValueError):
        durable.scheduled_plan(tmp_path, "aberg", "../../outside")


def test_intended_occurrence_drives_child_id_even_after_midnight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at = dt.datetime(2026, 10, 26, 22, 45, tzinfo=dt.UTC)  # local23:45, not delayed-next-day wall time
    attrs = TypedSearchAttributes(
        [SearchAttributePair(SearchAttributeKey.for_datetime("TemporalScheduledStartTime"), at)]
    )
    assert scheduled.occurrence_time(attrs) == at
    with pytest.raises(ValueError, match="refusing to guess"):
        scheduled.occurrence_time(TypedSearchAttributes([]))
    folder = tmp_path / "plans"
    folder.mkdir()
    (folder / "2026-10-26-aberg.yaml").write_text("persona: aberg\ndate: '2026-10-26'\nitems: []\n", encoding="utf-8")
    inp = durable.ScheduledInput(persona="aberg", tz="Europe/Stockholm")
    calls: list[str] = []

    async def load(name: str, fire: scheduled.Fire, **kw: Any) -> durable.DayInput:
        assert name == "load_scheduled_day" and fire.occurrence == at
        return scheduled.load_day(tmp_path, "aberg", fire)

    async def child(fn: Any, day: durable.DayInput, **kw: Any) -> list[durable.Done]:
        calls.append(kw["id"])
        assert day.plan.date == "2026-10-26" and day.utc_offset_s == 3600
        return []

    monkeypatch.setattr(scheduled.workflow, "info", lambda: SimpleNamespace(typed_search_attributes=attrs))
    monkeypatch.setattr(scheduled.workflow, "execute_activity", load)
    monkeypatch.setattr(scheduled.workflow, "execute_child_workflow", child)
    assert asyncio.run(scheduled.ScheduledDay().run(inp)) == []
    assert calls == ["day-aberg-2026-10-26"]


def test_worker_reservation_survives_shutdown_and_rejects_competing_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    desk = tmp_path / "desk.lock"
    plan = DayPlan(persona="aberg", date="2026-10-27", items=[note("09:00")])
    inp = durable.DayInput(plan=plan, clock="time-jump", utc_offset_s=3600)
    info = SimpleNamespace(
        workflow_namespace="default", workflow_id=durable.workflow_id(plan), attempt=1, activity_type="slot"
    )
    monkeypatch.setattr(scheduled.activity, "info", lambda: info)
    monkeypatch.setattr(scheduled.activity, "in_activity", lambda: False)

    @contextmanager
    def factory(day: durable.DayInput) -> Generator[DayRunner]:
        calls.append("open " + day.plan.date)
        log = durable.HeartbeatLog(tmp_path / f"{day.plan.date}.jsonl")
        try:
            yield DayRunner(
                FakeRunner(log, calls),  # type: ignore[arg-type]
                FakeSession(calls),  # type: ignore[arg-type]
                load_templates(),
                log,
                day.clock,
                0,
                set_clock=lambda t: calls.append(f"clock {t:%H:%M}" if t else "clock reset"),
            )  # type: ignore[arg-type]
        finally:
            calls.append("close")
            log.close()

    def canary(day: DayRunner, apps: set[str]) -> dict[str, str]:
        return {}

    acts = scheduled.WorkerActivities(tmp_path, "aberg", desk, factory, canary)
    acts.prepare(inp)
    acts.close()  # process shutdown: close resources, keep workflow reservation
    assert scheduled.DayOwner(desk).read() is not None
    original = list(calls)
    other = inp.model_copy(update={"plan": plan.model_copy(update={"date": "2026-10-28"})})
    info.workflow_id = durable.workflow_id(other.plan)
    with pytest.raises(ValueError, match="reserved"):
        acts.prepare(other)
    acts.abort()  # a failed competing prepare cannot reset/release the original day
    assert calls == original and scheduled.DayOwner(desk).read() is not None

    info.workflow_id = durable.workflow_id(plan)
    resumed = scheduled.WorkerActivities(tmp_path, "aberg", desk, factory, canary)
    # A restarted worker loads its frozen input when the next slot arrives, without replaying day_start.
    assert resumed.slot(durable.SlotInput(date=plan.date, item=plan.items[0]))[0].status == "done"
    resumed.finish(
        durable.DayEnd(plan=plan, done=[durable.Done(at="09:00", template="notepad_write_note", status="done")])
    )
    assert not scheduled.DayOwner(desk).path.exists()
    assert calls.count("open 2026-10-27") == 2 and calls[-2:] == ["clock reset", "close"]


def test_changed_or_malformed_reservations_fail_closed(tmp_path: Path) -> None:
    owner = scheduled.DayOwner(tmp_path / "desk.lock")
    inp = durable.DayInput(
        plan=DayPlan(persona="aberg", date="2026-10-27", items=[]), clock="compressed", utc_offset_s=0
    )
    owner.claim("default/day-aberg-2026-10-27", inp)
    with pytest.raises(ValueError, match="input changed"):
        owner.claim("default/day-aberg-2026-10-27", inp.model_copy(update={"clock": "realtime"}))
    owner.path.write_text("{partial", encoding="utf-8")
    with pytest.raises(ValueError):
        owner.check("default/day-aberg-2026-10-27")


@pytest.mark.parametrize("fail", ["day_prepare", "canary", "day_end"])
def test_new_workflow_failure_boundaries_always_abort(fail: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def execute(name: str, *args: Any, **kw: Any) -> list[durable.Done]:
        calls.append(name)
        if name == fail:
            raise RuntimeError(fail)
        return []

    monkeypatch.setattr(durable.workflow, "execute_activity", execute)
    monkeypatch.setattr(durable.workflow, "random", lambda: random.Random(0))

    def patched(marker: str) -> bool:
        return True

    monkeypatch.setattr(durable.workflow, "patched", patched)
    inp = durable.DayInput(
        plan=DayPlan(persona="aberg", date="2026-10-27", items=[]), clock="compressed", utc_offset_s=0, canary=True
    )
    with pytest.raises(RuntimeError, match=fail):
        asyncio.run(durable.PersonaDay().run(inp))
    assert calls[-1] == "day_abort"  # compressed days also release their active context, not only jumped clocks


def test_one_shot_worker_refuses_another_dates_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    plan = DayPlan(persona="aberg", date="2026-10-27", items=[note("09:00")])
    log = durable.HeartbeatLog(tmp_path / "day.jsonl")
    try:
        day = DayRunner(
            FakeRunner(log, calls),  # type: ignore[arg-type]
            FakeSession(calls),  # type: ignore[arg-type]
            load_templates(),
            log,
            "time-jump",
            0,
            set_clock=lambda t: calls.append("clock"),
        )  # type: ignore[arg-type]
        acts = durable.DayActivities(day, plan=plan)
        monkeypatch.setattr(durable.activity, "in_activity", lambda: True)
        monkeypatch.setattr(durable.activity, "info", lambda: SimpleNamespace(workflow_id="day-aberg-2026-10-28"))
        with pytest.raises(ValueError, match="another dated workflow"):
            acts.slot(durable.SlotInput(date="2026-10-28", item=plan.items[0]))
        acts.abort()
        assert calls == []  # rejected work must neither touch the desktop nor reset its clock
    finally:
        log.close()


def test_unopenable_runner_still_releases_a_day_without_a_jumped_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prepare claims, then the runner cannot open (wrong logon, agent down). Abort must not need that runner
    # for a realtime day, or every retry fails and the desktop stays reserved by a failed workflow forever.
    plan = DayPlan(persona="aberg", date="2026-10-27", items=[note("09:00")])
    info = SimpleNamespace(workflow_namespace="default", workflow_id=durable.workflow_id(plan))
    monkeypatch.setattr(scheduled.activity, "info", lambda: info)
    opened: list[str] = []

    @contextmanager
    def factory(day: durable.DayInput) -> Generator[DayRunner]:
        opened.append(day.clock)
        raise RuntimeError("persona not logged on")
        yield  # type: ignore[unreachable]

    acts = scheduled.WorkerActivities(tmp_path, "aberg", tmp_path / "desk.lock", factory, lambda d, a: {})
    for clock in ("realtime", "compressed"):
        with pytest.raises(RuntimeError, match="logged on"):
            acts.prepare(durable.DayInput(plan=plan, clock=clock, utc_offset_s=0))
        acts.abort()
        assert not acts.owner.path.exists()
    assert opened == ["realtime", "compressed"]  # abort never reopened the runner
    # A time-jump day may have a jumped clock this process cannot reset: fail closed, keep the reservation.
    with pytest.raises(RuntimeError):
        acts.prepare(durable.DayInput(plan=plan, clock="time-jump", utc_offset_s=0))
    with pytest.raises(RuntimeError):
        acts.abort()
    assert acts.owner.read() is not None


def test_retried_day_end_after_release_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # day_end released the reservation but its completion was lost; the retry must neither fail forever
    # nor finish (reset/log) again, even when the next day already owns the desktop.
    calls: list[str] = []
    plan = DayPlan(persona="aberg", date="2026-10-27", items=[])
    info = SimpleNamespace(workflow_namespace="default", workflow_id=durable.workflow_id(plan))
    monkeypatch.setattr(scheduled.activity, "info", lambda: info)

    @contextmanager
    def factory(day: durable.DayInput) -> Generator[DayRunner]:
        log = durable.HeartbeatLog(tmp_path / f"{day.plan.date}.jsonl")
        try:
            yield DayRunner(
                FakeRunner(log, calls),  # type: ignore[arg-type]
                FakeSession(calls),  # type: ignore[arg-type]
                load_templates(),
                log,
                day.clock,
                0,
                set_clock=lambda t: calls.append("clock reset" if t is None else "clock"),
            )  # type: ignore[arg-type]
        finally:
            log.close()

    acts = scheduled.WorkerActivities(tmp_path, "aberg", tmp_path / "desk.lock", factory, lambda d, a: {})
    acts.prepare(durable.DayInput(plan=plan, clock="time-jump", utc_offset_s=0))
    end = durable.DayEnd(plan=plan, done=[])
    acts.finish(end)
    assert calls.count("clock reset") == 1 and not acts.owner.path.exists()
    acts.finish(end)
    nxt = durable.DayInput(plan=plan.model_copy(update={"date": "2026-10-28"}), clock="time-jump", utc_offset_s=0)
    acts.owner.claim("default/day-aberg-2026-10-28", nxt)
    acts.finish(end)
    acts.abort()
    assert calls.count("clock reset") == 1 and acts.owner.read().owner == "default/day-aberg-2026-10-28"  # type: ignore[union-attr]
