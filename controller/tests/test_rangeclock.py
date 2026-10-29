import datetime as dt
import re

import pytest

from ludius.rangeclock import RangeClock


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
