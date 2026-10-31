from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

MAX_SKEW_S = 60.0
POOL = "ludius"
Exec = Callable[[int, str], str]
RETRIES, RETRY_S = 6, 10.0


class AgentDown(RuntimeError):
    pass


def qm_exec(vmid: int, script: str) -> str:
    argv = ["qm", "guest", "exec", str(vmid), "--timeout", "60", "--", "powershell", "-NoProfile", "-NonInteractive",
            "-Command", script]  # fmt: skip
    out = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    if out.returncode:
        raise AgentDown(f"VM {vmid}: qm guest exec exit {out.returncode}: {out.stderr.strip()[-300:]}")
    res = json.loads(out.stdout)
    if res.get("exitcode") != 0 or not res.get("exited", 1):
        raise RuntimeError(f"VM {vmid}: exit {res.get('exitcode')}: {str(res.get('err-data', ''))[-300:]}")
    return str(res.get("out-data", ""))


_SET = ("$e = (Get-Date) - [Diagnostics.Process]::GetCurrentProcess().StartTime; "
        "if ($e.TotalSeconds -lt 0 -or $e.TotalSeconds -gt 30) {{ $e = [TimeSpan]::Zero }}; "
        "Set-Date ([DateTimeOffset]::FromUnixTimeMilliseconds({ms}).LocalDateTime + $e) | Out-Null; ")  # fmt: skip
_PURGE = "klist -li 0x3e7 purge | Out-Null; klist -li 0x3e4 purge | Out-Null; "
_NOW = "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()"


@dataclass
class RangeClock:
    dc: int
    members: list[int]
    run: Exec = qm_exec
    now: Callable[[], float] = time.time
    verify: Callable[[], dict[int, str]] = lambda: {}
    sleep: Callable[[float], None] = time.sleep

    def _retry[T](self, act: Callable[[], T]) -> T:
        for _ in range(RETRIES):
            try:
                return act()
            except AgentDown:
                self.sleep(RETRY_S)
        return act()

    def offsets(self) -> dict[int, float]:
        vms = [self.dc, *self.members]
        with ThreadPoolExecutor(len(vms)) as pool:
            return dict(zip(vms, pool.map(self._offset, vms), strict=True))

    def _offset(self, vmid: int) -> float:
        def once() -> float:
            t0 = self.now()
            guest = int(self.run(vmid, _NOW).strip()) / 1000
            return guest - (t0 + self.now()) / 2

        return self._retry(once)

    def set(self, at: dt.datetime) -> dict[int, float]:
        if at.tzinfo is None:
            raise ValueError("range clock needs an aware datetime")
        if bad := self.verify():
            raise ValueError("range clock refused, not range VMs: " + "; ".join(f"{k}: {v}" for k, v in bad.items()))
        target = at.timestamp()
        dc_off = self._offset(self.dc)
        dc_now = self.now() + dc_off
        if target <= dc_now:
            raise ValueError(f"range clock is forward only: DC at {_iso(dc_now)}, asked {_iso(target)}")
        return self._apply(target - self.now())

    def reset(self) -> dict[int, float]:
        try:
            bad = self.verify()
        except Exception as e:
            raise RuntimeError(f"range check failed, no clock reset: {e}") from e
        return self._apply(0.0, keep_going=True, skip=bad)

    def _apply(self, offset: float, keep_going: bool = False, skip: dict[int, str] | None = None) -> dict[int, float]:
        skip = skip or {}
        errors = [f"VM {k} skipped, not a range VM: {v}" for k, v in skip.items()]
        got: dict[int, float] = {}
        members = [v for v in self.members if v not in skip]

        def guarded(vmid: int, act: Callable[[int], object]) -> None:
            try:
                act(vmid)
            except Exception as e:
                if not keep_going:
                    raise
                errors.append(f"VM {vmid}: {e}")

        def set_one(vmid: int) -> str:
            return self._retry(
                lambda: self.run(vmid, _SET.format(ms=int((self.now() + offset) * 1000)) + _PURGE + _NOW)
            )

        def measure(vmid: int) -> None:
            got[vmid] = self._offset(vmid)

        def set_guarded(vmid: int) -> None:
            guarded(vmid, set_one)

        def measure_guarded(vmid: int) -> None:
            guarded(vmid, measure)

        dc = [] if self.dc in skip else [self.dc]
        for v in dc:
            set_guarded(v)
        with ThreadPoolExecutor(len(members) + 1) as pool:
            list(pool.map(set_guarded, members))
            list(pool.map(measure_guarded, [*dc, *members]))
        skew = max(got.values()) - min(got.values()) if got else 0.0
        if skew > MAX_SKEW_S or (self.dc in got and abs(got[self.dc] - offset) > MAX_SKEW_S):
            errors.append(f"range clock not converged (skew {skew:.1f} s): {got}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return got


def violations(
    expected: dict[int, str], resources: Sequence[Mapping[str, object]], node: str, pool: str = POOL
) -> dict[int, str]:
    seen = {int(str(r["vmid"])): r for r in resources if "vmid" in r}
    bad: dict[int, str] = {}
    for vmid, name in expected.items():
        r = seen.get(vmid)
        if r is None:
            bad[vmid] = "not found"
        elif (r.get("name"), r.get("pool"), r.get("node"), int(str(r.get("template") or 0))) != (name, pool, node, 0):
            bad[vmid] = (f"{r.get('name')} in pool {r.get('pool')} on {r.get('node')}, expected {name} in {pool} on "
                         f"{node}" + (" (a template)" if r.get("template") else ""))  # fmt: skip
    return bad


def confine(expected: dict[int, str], resources: Sequence[Mapping[str, object]], node: str, pool: str = POOL) -> None:
    if bad := violations(expected, resources, node, pool):
        raise ValueError("range clock refused, not range VMs: " + "; ".join(f"{k}: {v}" for k, v in bad.items()))


def resources() -> list[dict[str, object]]:
    out = subprocess.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
                         check=True, capture_output=True, text=True, timeout=60)  # fmt: skip
    return json.loads(out.stdout)


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")
