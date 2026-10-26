from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ludius.cli import load_templates
from ludius.day import DayRunner, slots
from ludius.loop import GroundTruth, Outcome
from ludius.planner import DayPlan, PlanItem
from ludius.tasks import Task
from ludius.v1 import observation_pb2 as obs


def note(at: str, interrupt: bool = False) -> PlanItem:
    return PlanItem(at=at, template="notepad_write_note", interrupt=interrupt,
                    params={"file_name": f"note {at.replace(':', '')}", "body": "text"})  # fmt: skip


PLAN = DayPlan(persona="aberg", date="2026-11-02", items=[
    note("10:00"), note("08:55", interrupt=True), note("09:00"), note("09:10", interrupt=True),
    PlanItem(at="12:00", template="break", minutes=30), note("12:05", interrupt=True),
])  # fmt: skip


class Agent:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def chord(self, keys: Any) -> None:
        self.calls.append("lock")


class FakeRunner:
    def __init__(self, log: GroundTruth, calls: list[str]) -> None:
        self.log, self.calls, self.agent = log, calls, Agent(calls)

    def observe(self) -> obs.Snapshot:
        return obs.Snapshot()

    def run(self, task: Task, between: Any = None) -> Outcome:
        self.calls.append(f"run {task.inst.params['file_name']}")
        if between:
            between()
        return Outcome("done", 1)


class FakeSession:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def normalize(self) -> bool:
        self.calls.append("normalize")
        return True

    def escalate(self) -> int:
        return 0

    def unlock(self) -> bool:
        self.calls.append("unlock")
        return False


def test_time_jump_day_keeps_order_breaks_and_interrupts(tmp_path: Path) -> None:
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "time-jump", 0,  # type: ignore[arg-type]
                    sleep=lambda s: calls.append(f"sleep {s:g}"),
                    set_clock=lambda t: calls.append(f"clock {t:%H:%M}" if t else "clock reset"))  # fmt: skip
    results = day.run(PLAN)
    assert [(i.at, [x.at for x in m]) for i, m in slots(PLAN)] == [
        ("08:55", []), ("09:00", ["09:10"]), ("10:00", []), ("12:00", ["12:05"])]  # fmt: skip
    # The 08:55 interrupt has no task to cut into: it runs on its own, first, at its logical time.
    assert calls == [
        "clock 08:55", "normalize", "run note 0855", "clock 09:00", "normalize", "run note 0900", "run note 0910",
        "clock 10:00", "normalize", "run note 1000",
        "clock 12:00", "normalize", "lock", "sleep 5", "clock 12:30", "unlock", "normalize", "run note 1205",
        "clock reset",
    ]  # fmt: skip
    assert [(r.item.at, r.status) for r in results] == [
        ("08:55", "done"), ("09:10", "done"), ("09:00", "done"), ("10:00", "done"), ("12:00", "failed"),
        ("12:05", "done")]  # fmt: skip
    assert day.slot(dt.date(2026, 11, 2), note("13:00"), [])[0].item.at == "13:00"
    # A late interrupt resumed after a break: the clock is set to the break's end, not left to chance.
    calls.clear()
    day.slot(dt.date(2026, 11, 2), note("12:05"), [], jump_at="12:30")
    assert calls[:2] == ["clock 12:30", "normalize"]
    log.close()


def test_break_end_fails_closed_when_the_clock_cannot_be_set(tmp_path: Path) -> None:
    calls: list[str] = []

    def set_clock(t: dt.datetime | None) -> None:
        raise RuntimeError("set-clock failed")

    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "time-jump", 0,  # type: ignore[arg-type]
                    sleep=lambda s: None, set_clock=set_clock)  # fmt: skip
    brk = PlanItem(at="12:00", template="break", minutes=30)
    try:
        day.end_break(dt.date(2026, 11, 2), brk)
        raise AssertionError("end_break acted on an unknown clock")
    except RuntimeError:
        pass
    assert "unlock" not in calls
    log.close()


def test_realtime_day_waits_for_each_slot_in_logical_order(tmp_path: Path) -> None:
    calls: list[str] = []
    t = [dt.datetime(2026, 11, 2, 8, 0)]

    def sleep(s: float) -> None:
        t[0] += dt.timedelta(seconds=s)

    class Timed(FakeRunner):
        def run(self, task: Task, between: Any = None) -> Outcome:
            self.calls.append(f"{t[0]:%H:%M}")
            return super().run(task, between)

    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(Timed(log, calls), FakeSession(calls), load_templates(), log, "realtime", 0,  # type: ignore[arg-type]
                    sleep=sleep, now=lambda: t[0])  # fmt: skip
    plan = DayPlan(persona="aberg", date="2026-11-02", items=[note("09:00"), note("08:55", interrupt=True)])
    day.run(plan)
    log.close()
    runs = [c for c in calls if c != "normalize"]
    assert runs[0] in ("08:55", "08:56", "08:57") and runs[1] == "run note 0855"  # its time, plus human slack
    assert runs[2] >= "09:00" and runs[3] == "run note 0900"
