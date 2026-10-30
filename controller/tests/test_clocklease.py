from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, clocklease


def test_lease_is_renewed_until_released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "locks" / "clock-ludius-x.json"
    lease = clocklease.Lease(path, "ludius-x", renew_s=0.01)
    writes: list[str] = []
    renewed = threading.Event()
    write = lease._write  # pyright: ignore[reportPrivateUsage]

    def counted() -> None:
        write()
        writes.append(threading.current_thread().name)
        if threading.current_thread().name == "clock-lease":
            renewed.set()

    monkeypatch.setattr(lease, "_write", counted)
    lease.hold()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["vm"] == "ludius-x" and dt.datetime.fromisoformat(data["renewed"]).tzinfo is not None
    assert renewed.wait(10)
    lease.release()
    assert not path.exists()
    n = len(writes)
    assert lease._thread is None  # pyright: ignore[reportPrivateUsage]  # release() joined the renewer
    assert len(writes) == n and not path.exists()


def test_set_clock_keeps_the_lease_when_the_reset_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDIUS_VM", "ludius-x")
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]
    lease = tmp_path / "locks" / "clock-ludius-x.json"
    fail_reset = False

    def fake_run(cmd: list[str], **_: Any) -> None:
        assert lease.exists()
        if "-Reset" in cmd and fail_reset:
            raise subprocess.CalledProcessError(1, cmd, stderr="guest unreachable")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.set_clock(dt.datetime(2026, 10, 28, 8, 20))
    fail_reset = True
    with pytest.raises(subprocess.CalledProcessError):
        cli.set_clock(None)
    assert lease.exists()
    fail_reset = False
    cli.set_clock(None)
    assert not lease.exists()
    cli._clock_lease().release()  # pyright: ignore[reportPrivateUsage]
    cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]


def test_explicit_vm_clock_and_lease_target_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDIUS_VM", "ludius-default")
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> None:
        calls.append(cmd)
        assert cmd[-2:] == ["-VM", "ludius-selected"]
        assert (tmp_path / "locks" / "clock-ludius-selected.json").exists()
        assert not (tmp_path / "locks" / "clock-ludius-default.json").exists()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    try:
        cli.set_clock(dt.datetime(2026, 10, 28, 8, 20), vm="ludius-selected")
        assert cli._clock_lease("ludius-selected").held  # pyright: ignore[reportPrivateUsage]
        cli.set_clock(None, vm="ludius-selected")
        assert not cli._clock_lease("ludius-selected").held  # pyright: ignore[reportPrivateUsage]
        assert len(calls) == 2
    finally:
        cli._clock_lease("ludius-selected").release()  # pyright: ignore[reportPrivateUsage]
        cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]
