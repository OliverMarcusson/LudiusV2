from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, clocklease


def test_lease_is_renewed_until_released(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "clock-ludius-x.json"
    lease = clocklease.Lease(path, "ludius-x", renew_s=0.05)
    lease.hold()
    first = path.stat().st_mtime_ns
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["vm"] == "ludius-x" and dt.datetime.fromisoformat(data["renewed"]).tzinfo is not None
    time.sleep(0.3)
    assert path.stat().st_mtime_ns > first  # the heartbeat thread rewrote it
    lease.release()
    assert not path.exists()
    time.sleep(0.15)
    assert not path.exists()  # the stopped thread doesn't bring it back


def test_set_clock_keeps_the_lease_when_the_reset_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDIUS_VM", "ludius-x")
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]
    lease = tmp_path / "locks" / "clock-ludius-x.json"
    fail_reset = False

    def fake_run(cmd: list[str], **_: Any) -> None:
        assert lease.exists()  # held before any jump
        if "-Reset" in cmd and fail_reset:
            raise subprocess.CalledProcessError(1, cmd, stderr="guest unreachable")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.set_clock(dt.datetime(2026, 10, 28, 8, 20))
    fail_reset = True
    with pytest.raises(subprocess.CalledProcessError):
        cli.set_clock(None)
    assert lease.exists()  # left for the host agent's sweeper
    fail_reset = False
    cli.set_clock(None)
    assert not lease.exists()
    cli._clock_lease().release()  # pyright: ignore[reportPrivateUsage]
    cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]
