import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path

import pytest

from ludius.rangeclock import AgentDown, RangeClock, confine


class Range:
    def __init__(self, stuck: int | None = None) -> None:
        self.t = 1_800_000_000.0
        self.off: dict[int, float] = {}
        self.sets: list[int] = []
        self.stuck = stuck

    def now(self) -> float:
        return self.t

    def run(self, vmid: int, script: str) -> str:
        if m := re.search(r"FromUnixTimeMilliseconds\((\d+)\)", script):
            self.sets.append(vmid)
            if vmid != self.stuck:
                self.off[vmid] = int(m.group(1)) / 1000 - self.t
        return str(int((self.t + self.off.get(vmid, 0.0)) * 1000))


def test_set_dc_first_then_reset() -> None:
    r = Range()
    c = RangeClock(110, [111, 121, 122], r.run, r.now)
    at = dt.datetime.fromtimestamp(r.t, dt.UTC) + dt.timedelta(days=4)
    got = c.set(at)
    assert r.sets[0] == 110 and sorted(r.sets[1:]) == [111, 121, 122]
    assert all(abs(v - 4 * 86400) < 1 for v in got.values())
    with pytest.raises(ValueError, match="forward only"):
        c.set(at - dt.timedelta(hours=1))
    assert all(abs(v) < 1 for v in c.reset().values())


def test_a_guest_agent_that_is_down_for_a_while_is_retried_with_a_fresh_target() -> None:
    r = Range()
    down = {111: 3}

    def run(vmid: int, script: str) -> str:
        if down.get(vmid):
            down[vmid] -= 1
            raise AgentDown("QEMU guest agent is not running")
        return r.run(vmid, script)

    def sleep(s: float) -> None:
        r.t += s

    got = RangeClock(110, [111], run, r.now, sleep=sleep).set(
        dt.datetime.fromtimestamp(r.t, dt.UTC) + dt.timedelta(days=1)
    )
    assert all(abs(v - 86400) < 1 for v in got.values())


def test_forward_only_holds_when_the_dc_check_is_retried() -> None:
    r = Range()
    down = {110: 3}

    def run(vmid: int, script: str) -> str:
        if down.get(vmid):
            down[vmid] -= 1
            raise AgentDown("QEMU guest agent is not running")
        return r.run(vmid, script)

    def sleep(s: float) -> None:
        r.t += s

    with pytest.raises(ValueError, match="forward only"):
        RangeClock(110, [111], run, r.now, sleep=sleep).set(dt.datetime.fromtimestamp(r.t + 5, dt.UTC))
    assert r.sets == []


def test_member_that_does_not_move_fails() -> None:
    r = Range(stuck=122)
    c = RangeClock(110, [111, 122], r.run, r.now)
    with pytest.raises(RuntimeError, match="not converged"):
        c.set(dt.datetime.fromtimestamp(r.t, dt.UTC) + dt.timedelta(days=1))


def test_forward_only_has_no_tolerance() -> None:
    r = Range()
    c = RangeClock(110, [111], r.run, r.now)
    dc_now = dt.datetime.fromtimestamp(r.t, dt.UTC)
    for behind in (dc_now, dc_now - dt.timedelta(seconds=30)):
        with pytest.raises(ValueError, match="forward only"):
            c.set(behind)
    assert r.sets == []


def test_reset_tries_every_vm_when_the_dc_fails() -> None:
    r = Range()
    c = RangeClock(110, [111, 121], r.run, r.now)
    c.set(dt.datetime.fromtimestamp(r.t, dt.UTC) + dt.timedelta(days=1))
    r.sets.clear()
    real = r.run

    def dc_down(vmid: int, script: str) -> str:
        if vmid == 110:
            raise RuntimeError("guest agent not running")
        return real(vmid, script)

    c.run = dc_down
    with pytest.raises(RuntimeError, match="VM 110: guest agent not running"):
        c.reset()
    assert sorted(r.sets) == [111, 121] and abs(r.off[111]) < 1 and abs(r.off[121]) < 1


def test_unwarp_exits_nonzero_when_reset_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from ludius import cli

    def down(vmid: int, script: str) -> str:
        raise RuntimeError("guest agent not running")

    monkeypatch.setattr(cli, "_range_clock", lambda strict=True: RangeClock(110, [111], down, time.time))
    assert cli._range_unwarp() == 3  # pyright: ignore[reportPrivateUsage]


RES = [
    {"vmid": 110, "name": "ludius-dc01", "pool": "ludius", "node": "pve", "template": 0},
    {"vmid": 121, "name": "ludius-aberg", "pool": "ludius", "node": "pve", "template": 0},
]


def test_confine_accepts_range_vms_only() -> None:
    want = {110: "ludius-dc01", 121: "ludius-aberg"}
    confine(want, RES, "pve")
    for bad in (
        [RES[0], {**RES[1], "pool": "other"}],
        [RES[0], {**RES[1], "name": "web01"}],
        [RES[0], {**RES[1], "node": "pve2"}],
        [RES[0], {**RES[1], "template": 1}],
        [RES[0]],
    ):
        with pytest.raises(ValueError, match="121"):
            confine(want, bad, "pve")


def test_range_clock_refuses_before_touching_any_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    from ludius import cli, rangeclock

    monkeypatch.setenv("LUDIUS_MAIL_BACKEND", "exchange")
    monkeypatch.setattr(rangeclock, "resources", lambda: (RES, "pve"))
    with pytest.raises(ValueError, match="not range VMs"):
        cli._range_clock()  # pyright: ignore[reportPrivateUsage]
    assert cli._range_clock(strict=False).dc == 110  # pyright: ignore[reportPrivateUsage]


def test_set_and_reset_recheck_the_range() -> None:
    r = Range()
    bad: dict[int, str] = {}
    c = RangeClock(110, [111, 121], r.run, r.now, verify=lambda: dict(bad))
    c.set(dt.datetime.fromtimestamp(r.t, dt.UTC) + dt.timedelta(days=1))
    r.sets.clear()
    bad[121] = "web01 in pool other"
    with pytest.raises(ValueError, match="121: web01"):
        c.set(dt.datetime.fromtimestamp(r.t, dt.UTC) + dt.timedelta(days=2))
    assert r.sets == []
    with pytest.raises(RuntimeError, match="VM 121 skipped"):
        c.reset()
    assert sorted(r.sets) == [110, 111] and abs(r.off[110]) < 1 and abs(r.off[111]) < 1


def test_warp_exits_3_when_reset_fails_after_the_warp_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ludius import cli, rangerun
    from ludius.clocklease import Lease

    class Broken(RangeClock):
        def set(self, at: dt.datetime) -> dict[int, float]:
            raise RuntimeError("guest agent hung mid-jump")

        def reset(self) -> dict[int, float]:
            raise RuntimeError("VM 110: guest agent not running")

    monkeypatch.setattr(cli, "RUNS", tmp_path)
    monkeypatch.setattr(cli, "_range_clock", lambda strict=True: Broken(110, [111]))
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "narrative.yaml").write_text("{}")

    def lease(vm: str | None = None) -> Lease:
        return Lease(tmp_path / f"clock-{vm}.json", vm or "range", renew_s=3600)

    def no_specs(*_: object) -> list[rangerun.Spec]:
        return []

    monkeypatch.setattr(cli, "_clock_lease", lease)
    monkeypatch.setattr(cli, "plan_sources", lambda: None)
    monkeypatch.setattr(rangerun, "specs", no_specs)
    args = argparse.Namespace(start="2099-01-05", days=1, timezone="Europe/Stockholm", plan_at="22:00", agent=None,
                              temporal="127.0.0.1:7233", model="m")  # fmt: skip
    assert cli._range_warp(args, [], dt.date(2099, 1, 1)) == 3  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "reset FAILED" in err and "guest agent hung mid-jump" in err
    assert (tmp_path / "clock-range.json").exists()


def test_warp_without_narrative_fails_visibly_before_any_jump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ludius import cli, rangerun
    from ludius.clocklease import Lease

    class Untouched(RangeClock):
        def set(self, at: dt.datetime) -> dict[int, float]:
            raise AssertionError("clock jumped")

    monkeypatch.setattr(cli, "RUNS", tmp_path)
    monkeypatch.setattr(cli, "_range_clock", lambda strict=True: Untouched(110, [111]))
    monkeypatch.setattr(cli, "_clock_lease", lambda vm=None: Lease(tmp_path / "clock.json", "range", renew_s=3600))
    monkeypatch.setattr(cli, "plan_sources", lambda: None)
    monkeypatch.setattr(rangerun, "specs", lambda *_: [])
    args = argparse.Namespace(start="2099-01-05", days=1, timezone="Europe/Stockholm", plan_at="22:00", agent=None,
                              temporal="127.0.0.1:7233", model="m")  # fmt: skip
    with pytest.raises(RuntimeError, match="plan narrative --date 2099-01-05"):
        cli._range_warp(args, [], dt.date(2099, 1, 1))  # pyright: ignore[reportPrivateUsage]
    events = [json.loads(x)["event"] for x in (tmp_path / "range" / "warp.jsonl").read_text().splitlines()]
    assert events == ["warp_failed"]


def test_warp_refuses_a_reserved_desktop_before_any_jump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ludius import cli, rangerun
    from ludius.clocklease import Lease

    class Untouched(RangeClock):
        def set(self, at: dt.datetime) -> dict[int, float]:
            raise AssertionError("clock jumped")

    monkeypatch.setattr(cli, "RUNS", tmp_path)
    monkeypatch.setattr(cli, "_range_clock", lambda strict=True: Untouched(110, [111]))
    monkeypatch.setattr(cli, "_clock_lease", lambda vm=None: Lease(tmp_path / "clock.json", "range", renew_s=3600))
    monkeypatch.setattr(cli, "plan_sources", lambda: None)
    monkeypatch.setattr(rangerun, "specs", lambda *_: [])
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "narrative.yaml").write_text("{}")
    (tmp_path / "locks").mkdir()
    plan = {"persona": "lnord", "date": "2027-02-03", "items": []}
    day = {"owner": "ludius/day-lnord-2027-02-03", "day": {"plan": plan, "clock": "time-jump", "utc_offset_s": 0}}
    (tmp_path / "locks" / "ludius-lnord.day.json").write_text(json.dumps(day))
    args = argparse.Namespace(start="2099-01-05", days=1, timezone="Europe/Stockholm", plan_at="22:00", agent=None,
                              temporal="127.0.0.1:7233", model="m")  # fmt: skip
    with pytest.raises(RuntimeError, match="lnord by ludius/day-lnord-2027-02-03"):
        cli._range_warp(args, ["aberg", "lnord"], dt.date(2099, 1, 1))  # pyright: ignore[reportPrivateUsage]
