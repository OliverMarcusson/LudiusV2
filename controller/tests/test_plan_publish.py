"""World.save_plan(new=True) publishes a complete plan atomically and never replaces an existing one."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from ludius import planner
from ludius.planner import DayPlan, World

PLAN = DayPlan(persona="aberg", date="2026-10-28", items=[])


def _save(world: World, **kw: Any) -> Path:
    return world.save_plan(PLAN, {}, new=True, **kw)


def _no_scratch(world: World) -> None:
    assert [p.name for p in world.plan_path("aberg", "2026-10-28").parent.iterdir() if p.name.endswith(".tmp")] == []


def test_new_plan_is_published_complete(tmp_path: Path) -> None:
    world = World(tmp_path)
    path = _save(world)
    assert planner.load_plan(path) == PLAN
    _no_scratch(world)


def test_serialization_failure_leaves_destination_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(tmp_path)

    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("serialize")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(RuntimeError, match="serialize"):
        _save(world)
    assert not world.plan_path("aberg", "2026-10-28").exists()
    _no_scratch(world)


def test_write_failure_leaves_destination_absent_and_retry_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path)
    real = os.fsync

    def boom(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        _save(world)
    assert not world.plan_path("aberg", "2026-10-28").exists()
    _no_scratch(world)
    monkeypatch.setattr(os, "fsync", real)
    assert planner.load_plan(_save(world)) == PLAN  # the failure left nothing that blocks a retry


def test_publication_failure_leaves_destination_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(tmp_path)

    def no_links(src: Any, dst: Any) -> None:
        raise OSError("not supported")

    monkeypatch.setattr(os, "link", no_links)
    with pytest.raises(OSError, match="hard links unavailable"):
        _save(world)  # fails closed; no replace fallback
    assert not world.plan_path("aberg", "2026-10-28").exists()
    _no_scratch(world)


def test_concurrent_winner_is_never_clobbered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(tmp_path)
    path = world.plan_path("aberg", "2026-10-28")
    winner = "persona: aberg\ndate: '2026-10-28'\nitems: []\n# winner\n"
    real = os.link

    def racing_link(src: Any, dst: Any) -> None:  # the other writer lands between our temp write and publish
        Path(dst).write_text(winner, encoding="utf-8")
        real(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(FileExistsError):
        _save(world)
    assert path.read_text(encoding="utf-8") == winner
    _no_scratch(world)


def test_existing_plan_is_never_replaced(tmp_path: Path) -> None:
    world = World(tmp_path)
    path = _save(world)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        world.save_plan(PLAN.model_copy(update={"persona": "aberg", "date": "2026-10-28"}), {}, new=True)
    assert path.read_bytes() == before
    _no_scratch(world)
