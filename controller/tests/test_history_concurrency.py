from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, nightly, planner
from ludius.planner import DayPlan, HistoryBusy, World

DAY = "2026-10-28"
QUERIES = ("ifrs 16 lease accounting", "supplier payment terms netherlands")
PLAN = DayPlan.model_validate({
    "persona": "aberg", "date": DAY,
    "items": [{"at": at, "template": "edge_web_lookup", "params": {"search_query": q},
               "context": "Checks the lease rules before the close."}
              for at, q in zip(("09:00", "09:30"), QUERIES, strict=True)],
})  # fmt: skip


def row(date: str, persona: str, text: str) -> str:
    return json.dumps({"date": date, "persona": persona, "text": text}) + "\n"


def no_llm() -> Any:
    raise AssertionError("an existing plan must never reach the model")


class TornWriter:
    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world, self.mid, self.go, self.error = world, threading.Event(), threading.Event(), None
        self.waiting = threading.Event()
        real = World._append  # pyright: ignore[reportPrivateUsage]
        once: list[int] = []

        def append(w: World, plan: DayPlan, values: list[str]) -> None:
            if once:
                return real(w, plan, values)
            once.append(1)
            data = "".join(row(plan.date, plan.persona, v) for v in values).encode()
            w.history_path.parent.mkdir(parents=True, exist_ok=True)
            with w.history_path.open("ab") as f:
                f.write(data[:20])
                f.flush()
                self.mid.set()
                assert self.go.wait(10)
                f.write(data[20:])

        def nap() -> None:
            self.waiting.set()
            self.go.set()

        monkeypatch.setattr(World, "_append", append)
        monkeypatch.setattr(planner, "_nap", nap)

    def start(self, templates: Any) -> threading.Thread:
        def run() -> None:
            try:
                self.world.save_plan(PLAN, templates)
            except BaseException as e:  # noqa: BLE001 - surfaced by the test
                self.error = e

        t = threading.Thread(target=run)
        t.start()
        assert self.mid.wait(10)
        return t


def _run_beside(fn: Callable[[], Any]) -> tuple[list[Any], threading.Thread]:
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as e:  # noqa: BLE001
            out.append(e)

    t = threading.Thread(target=run)
    t.start()
    return out, t


def test_reader_waits_out_an_in_flight_append_instead_of_calling_it_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = cli.plan_sources()
    world = World(tmp_path)
    torn = TornWriter(world, monkeypatch)
    writer = torn.start(src.templates)
    out, reader = _run_beside(lambda: world.history("aberg"))
    assert torn.waiting.wait(10)
    reader.join(10)
    writer.join(10)
    assert torn.error is None and out == [list(QUERIES)]


def test_repair_waits_out_a_manual_save_and_neither_conflicts_nor_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = cli.plan_sources()
    world = World(tmp_path)
    torn = TornWriter(world, monkeypatch)
    writer = torn.start(src.templates)
    out, repair = _run_beside(lambda: world.repair_history(PLAN, src.templates))
    assert torn.waiting.wait(10)
    repair.join(10)
    writer.join(10)
    assert torn.error is None and out == [0]
    assert world.history("aberg") == list(QUERIES)


def test_a_busy_journal_is_retryable_not_a_corrupt_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = cli.plan_sources()
    world = World(tmp_path)
    world.save_narrative(_narrative())
    world.lock_wait = 0
    world.save_plan(PLAN, {}, new=True)
    torn = TornWriter(world, monkeypatch)
    writer = torn.start(src.templates)
    before = world.history_path.read_bytes()
    with pytest.raises(HistoryBusy) as e:
        world.history("aberg")
    assert not isinstance(e.value, ValueError)
    acts = nightly.PlanActivities(tmp_path, cli.plan_sources, no_llm)
    acts.world.lock_wait = 0
    fire = nightly.PlanFire(persona="aberg", tz="UTC", occurrence=_utc(2026, 10, 27, 21))
    with pytest.raises(HistoryBusy):
        acts.plan(fire)
    assert world.history_path.read_bytes() == before
    torn.go.set()
    writer.join(10)
    assert torn.error is None
    world.lock_wait = 10
    assert world.history("aberg") == list(QUERIES)


def test_a_held_lock_is_not_reentered_by_repair_or_save(tmp_path: Path) -> None:
    src = cli.plan_sources()
    world = World(tmp_path)
    world.lock_wait = 0
    assert world.repair_history(PLAN, src.templates) == 2
    assert world.repair_history(PLAN, src.templates) == 0
    world.save_plan(PLAN, src.templates)
    assert world.history("aberg") == [*QUERIES, *QUERIES]


def test_repair_then_manual_save_keeps_both_personas_and_past_rows_and_may_duplicate_on_purpose(
    tmp_path: Path,
) -> None:
    src = cli.plan_sources()
    world = World(tmp_path)
    world.history_path.parent.mkdir(parents=True)
    old = row("2026-10-27", "aberg", "an earlier day") + row(DAY, "lnord", QUERIES[1]) + row(DAY, "aberg", "old manual")
    world.history_path.write_text(old, encoding="utf-8", newline="")
    world.repair_history(PLAN, src.templates)
    world.save_plan(PLAN, src.templates)
    assert world.history_path.read_text(encoding="utf-8").startswith(old)
    assert world.history("aberg") == ["an earlier day", "old manual", *QUERIES, *QUERIES]
    assert world.history("lnord") == [QUERIES[1]]


@pytest.mark.parametrize("tail", ['{"date": "2026-10-28", "pers', "[1, 2]"])
def test_a_true_corrupt_tail_still_fails_closed_and_releases_the_lock(tmp_path: Path, tail: str) -> None:
    src = cli.plan_sources()
    world = World(tmp_path)
    world.lock_wait = 0
    world.history_path.parent.mkdir(parents=True)
    old = row("2026-10-27", "aberg", "an earlier day") + tail
    world.history_path.write_text(old, encoding="utf-8", newline="")
    for _ in range(2):
        with pytest.raises(ValueError, match="line 2 is not a history row"):
            world.history("aberg")
        with pytest.raises(ValueError, match="line 2 is not a history row"):
            world.repair_history(PLAN, src.templates)
    assert world.history_path.read_text(encoding="utf-8") == old


HOLDER = """
import sys
from pathlib import Path
from ludius.planner import World
with World(Path(sys.argv[1]))._journal():
    print("held", flush=True)
    sys.stdin.read()
"""


def test_another_process_excludes_this_one_and_a_killed_holder_releases(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.lock_wait = 0
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert p.stdout is not None and p.stdout.readline().strip() == b"held"
        with pytest.raises(HistoryBusy):
            world.history("aberg")
        with pytest.raises(HistoryBusy):
            world.save_plan(PLAN, {})
        assert not world.history_path.exists()
        p.kill()
        p.wait(10)
        world.lock_wait = 10
        assert world.history("aberg") == []
    finally:
        p.kill()
        p.wait(10)


def _narrative() -> planner.Narrative:
    people = [planner.Person(name="Anna Berg", role="Finance controller", department="Finance", user="aberg")]
    return planner.Narrative(company="Harbourline", city="Rotterdam", summary="Packaging.", people=people,
                             projects=[], events=[])  # fmt: skip


def _utc(*a: int) -> Any:
    import datetime as dt

    return dt.datetime(*a, tzinfo=dt.UTC)
