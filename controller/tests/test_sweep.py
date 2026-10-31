from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from ludius import canary, cli, clocklease, planner
from ludius.cli import load_templates

NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)


def _lease(path: Path, age_s: float, pid: int = 999_999_999) -> dict[str, object]:
    rec = {"vm": "range", "pid": pid, "renewed": (NOW - dt.timedelta(seconds=age_s)).isoformat()}
    path.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_orphan_only_when_stale(tmp_path: Path) -> None:
    p = tmp_path / "clock-range.json"
    assert clocklease.orphan(p, NOW) is None
    p.write_text("{", encoding="utf-8")
    assert clocklease.orphan(p, NOW) is None
    _lease(p, 10)
    assert clocklease.orphan(p, NOW) is None
    rec = _lease(p, clocklease.STALE_S + 1)
    assert clocklease.orphan(p, NOW) == rec


class _Clock:
    def __init__(self, fail: bool = False) -> None:
        self.resets, self.fail = 0, fail

    def reset(self) -> dict[int, float]:
        self.resets += 1
        if self.fail:
            raise RuntimeError("guest agent not running")
        return {110: 0.2}


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age_s: float, clock: _Clock) -> Path:
    p = tmp_path / "clock-range.json"
    _lease(p, age_s)
    monkeypatch.setattr(cli, "_clock_lease", lambda vm=None: clocklease.Lease(p, "range"))
    monkeypatch.setattr(cli, "_range_clock", lambda strict=True: clock)
    real = clocklease.orphan
    monkeypatch.setattr(clocklease, "orphan", lambda path, now=None: real(path, NOW))
    return p


def test_sweep_resets_orphan_and_removes_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    p = _setup(tmp_path, monkeypatch, clocklease.STALE_S + 5, clock)
    assert cli._range_sweep() == 0  # pyright: ignore[reportPrivateUsage]
    assert clock.resets == 1 and not p.exists()


def test_sweep_leaves_fresh_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    p = _setup(tmp_path, monkeypatch, 5, clock)
    assert cli._range_sweep() == 0  # pyright: ignore[reportPrivateUsage]
    assert clock.resets == 0 and p.exists()


def test_sweep_keeps_lease_when_reset_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(fail=True)
    p = _setup(tmp_path, monkeypatch, clocklease.STALE_S + 5, clock)
    assert cli._range_sweep() == 3  # pyright: ignore[reportPrivateUsage]
    assert p.exists()


def test_outlook_open_probe_has_no_effects() -> None:
    picked = canary.pick(load_templates(), {})
    t, _ = picked["outlook"]
    assert t.task == "outlook_open_probe" and len(t.variants[0]) == 2 and not t.writes and not t.key_commits


def test_dump_retries_locked_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real, calls = os.replace, [0]

    def flaky(src: str, dst: Path) -> None:
        calls[0] += 1
        if calls[0] < 3:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(planner.os, "replace", flaky)
    planner._dump(tmp_path / "x.yaml", {"a": 1})  # pyright: ignore[reportPrivateUsage]
    assert (tmp_path / "x.yaml").read_text(encoding="utf-8") == "a: 1\n" and calls[0] == 3
