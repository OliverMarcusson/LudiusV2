import datetime as dt
import re
import time

import pytest

from ludius.rangeclock import RangeClock, confine


class Range:
    """Fake guests: a set moves that VM's offset; order of sets is recorded."""

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

    monkeypatch.setattr(cli, "_range_clock", lambda: RangeClock(110, [111], down, time.time))
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
    monkeypatch.setattr(rangeclock, "resources", lambda: RES)
    with pytest.raises(ValueError, match="not range VMs"):
        cli._range_clock()  # pyright: ignore[reportPrivateUsage]
