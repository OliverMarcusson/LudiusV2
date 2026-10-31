from __future__ import annotations

from pathlib import Path

from ludius.cli import load_templates
from ludius.day import DayRunner
from ludius.loop import GroundTruth
from ludius.planner import DayPlan, PlanItem, day_records
from test_day import FakeRunner, FakeSession, note


def test_replayed_day_never_repeats_a_send(tmp_path: Path) -> None:
    templates = load_templates()
    send = PlanItem(at="09:30", template="outlook_send_mail", params=templates["outlook_send_mail"].example)
    plan = DayPlan(persona="aberg", date="2026-11-02", items=[note("09:00"), send])
    calls: list[str] = []
    for _ in range(2):
        log = GroundTruth(tmp_path / "day.jsonl")
        day = DayRunner(FakeRunner(log, calls), FakeSession(calls), templates, log, "compressed", 0,  # type: ignore[arg-type]
                        sleep=lambda s: None)  # fmt: skip
        results = day.run(plan)
        log.close()
    assert [c for c in calls if c.startswith("run")] == ["run note 0900", "run outlook_send_mail", "run note 0900"]
    assert [(r.item.at, r.status) for r in results] == [("09:00", "done"), ("09:30", "skipped")]
    assert [r["event"] for r in day_records(tmp_path / "day.jsonl")].count("item_skipped") == 1
