from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from ludius.cli import load_templates
from ludius.planner import DayPlan, PlanItem, World, validate
from ludius.policy import Policy


def test_handoff_creates_thread_only_when_done(tmp_path: Path) -> None:
    item = PlanItem(at="09:00", template="explorer_new_folder", params={"folder_name": "budget review for lisa"},
                    context="Sets up a folder Lisa asked for.", handoff="lnord")  # fmt: skip
    failed = PlanItem(at="10:00", template="explorer_new_folder", params={"folder_name": "vendor list draft"},
                      handoff="lnord")  # fmt: skip
    templates = load_templates()
    assert validate([item], templates, [], Policy(), {"lnord"}) == []
    assert "not a colleague" in validate([item], templates, [], Policy(), {"other"})[0][1]

    world = World(tmp_path)
    world.save_plan(DayPlan(persona="aberg", date="2026-10-27", items=[item, failed]), templates)
    log = world.day_log("aberg", "2026-10-27")
    log.parent.mkdir(parents=True)
    ends = [{"event": "item_end", "at": a, "template": "explorer_new_folder", "status": s}
            for a, s in (("09:00", "done"), ("10:00", "abandoned"))]  # fmt: skip
    log.write_text("\n".join(map(json.dumps, ends)))
    threads = world.threads("lnord", dt.date(2026, 10, 28))
    assert len(threads) == 1 and "from aberg" in threads[0] and "budget review" in threads[0]
    assert "Threads colleagues handed" in world.summary("lnord", dt.date(2026, 10, 28))
    assert world.threads("aberg", dt.date(2026, 10, 28)) == []  # own handoffs are not inbound
    assert world.threads("lnord", dt.date(2026, 11, 5)) == []  # older than the window
