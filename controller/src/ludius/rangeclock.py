"""Range clock on a Proxmox range (spec section 7, time-jump): every range VM on one logical time, DC first.

dc01 is the range's time source (w32time NoSync, announced reliable PDC; roles/dc01) and every member follows the
domain hierarchy (NT5DS), so a member set to the DC's time stays there: KVM has no Hyper-V-style integration time
sync, and nothing pushes host time into a guest unless asked (`guest-set-time`, which this never calls). Each VM is
set through the QEMU guest agent (`qm guest exec`, the controller runs on the box as root): dc01 first, then ex01 and
the personas in parallel, each to the DC's instant at that moment. A set is verified: every VM within MAX_SKEW_S of
the DC, else it raises (the caller resets). Per-persona jumps can't exist on this range: 8 personas share one DC
and a member more than 5 min off it loses Kerberos.

Forward only: a set behind the DC's clock is refused (AD and Exchange don't go back in time). reset() does go back,
to the box's UTC; after a warp the range is rolled back to its pre-warp snapshot anyway.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

MAX_SKEW_S = 60.0  # spec of the warp test; Kerberos breaks at 300
POOL = "ludius"  # the range pool (host agent Proxmox__Pool)
Exec = Callable[[int, str], str]  # (vmid, PowerShell) -> stdout; raises on failure


def qm_exec(vmid: int, script: str) -> str:
    """PowerShell in a guest over the QEMU guest agent; a nonzero exit or a hung agent raises. `qm` itself fails now
    and then (exit 255) with the agent fine a moment later: three attempts. A retried set lands a few seconds
    late, inside MAX_SKEW_S, and the member's w32time pulls it to the DC."""
    argv = ["qm", "guest", "exec", str(vmid), "--timeout", "60", "--", "powershell", "-NoProfile", "-NonInteractive",
            "-Command", script]  # fmt: skip
    out = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    for _ in range(2):
        if out.returncode == 0:
            break
        time.sleep(3)
        out = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    if out.returncode:
        raise RuntimeError(f"VM {vmid}: qm guest exec exit {out.returncode}: {out.stderr.strip()[-300:]}")
    res = json.loads(out.stdout)
    if res.get("exitcode") != 0 or not res.get("exited", 1):
        raise RuntimeError(f"VM {vmid}: exit {res.get('exitcode')}: {str(res.get('err-data', ''))[-300:]}")
    return str(res.get("out-data", ""))


# Epoch milliseconds in and out: no agreement on time zones needed. The guest prints its UTC after setting. The time
# PowerShell took to start (seconds on a busy persona) is added, so a member lands on the DC's instant. It is measured
# on the guest's wall clock, so a set from an earlier call still running in the guest (qm gave up waiting, the guest
# process did not) can make it days long or negative: outside 0-30 s it counts as 0 (live: a reset landed a VM 4.3
# days in the past).
_SET = ("$e = (Get-Date) - [Diagnostics.Process]::GetCurrentProcess().StartTime; "
        "if ($e.TotalSeconds -lt 0 -or $e.TotalSeconds -gt 30) {{ $e = [TimeSpan]::Zero }}; "
        "Set-Date ([DateTimeOffset]::FromUnixTimeMilliseconds({ms}).LocalDateTime + $e) | Out-Null; ")  # fmt: skip
_NOW = "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()"


@dataclass
class RangeClock:
    dc: int
    members: list[int]  # ex01 and the personas
    run: Exec = qm_exec
    now: Callable[[], float] = time.time
    # VMIDs that are not range VMs right now -> why (violations() over a fresh pvesh read); checked before every
    # set and reset, not once: a VMID can leave the pool or be renamed during a multi-hour warp.
    verify: Callable[[], dict[int, str]] = lambda: {}

    def offsets(self) -> dict[int, float]:
        """Each VM's clock minus the box's, in seconds."""
        vms = [self.dc, *self.members]
        with ThreadPoolExecutor(len(vms)) as pool:
            return dict(zip(vms, pool.map(self._offset, vms), strict=True))

    def _offset(self, vmid: int) -> float:
        t0 = self.now()
        guest = int(self.run(vmid, _NOW).strip()) / 1000
        return guest - (t0 + self.now()) / 2  # measured at the midpoint of the call

    def set(self, at: dt.datetime) -> dict[int, float]:
        """Every VM to `at` (aware), DC first; returns the verified offsets. Refuses to go back in time."""
        if at.tzinfo is None:
            raise ValueError("range clock needs an aware datetime")
        if bad := self.verify():
            raise ValueError("range clock refused, not range VMs: " + "; ".join(f"{k}: {v}" for k, v in bad.items()))
        target = at.timestamp()
        dc_now = self.now() + self._offset(self.dc)
        if target <= dc_now:
            raise ValueError(f"range clock is forward only: DC at {_iso(dc_now)}, asked {_iso(target)}")
        return self._apply(target - self.now())

    def reset(self) -> dict[int, float]:
        """Every VM back to the box's UTC, DC first (the members follow it again). Tries every VM whatever fails
        (a DC whose agent is down must not keep the members jumped) and raises all errors together at the end.
        A VM that fails the range check is skipped and reported; if the check itself fails, nothing is touched."""
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

        # Each VM to box time + offset at the moment its command is built: the members land on the DC's instant.
        def set_one(vmid: int) -> str:
            return self.run(vmid, _SET.format(ms=int((self.now() + offset) * 1000)) + _NOW)

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
    """The VMIDs that are not a VM (not a template) in the range pool on this node with their expected name, and why.
    `resources` is `pvesh get /cluster/resources --type vm`."""
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
    """Every VMID a range VM (violations()), else refuse before anything is touched."""
    if bad := violations(expected, resources, node, pool):
        raise ValueError("range clock refused, not range VMs: " + "; ".join(f"{k}: {v}" for k, v in bad.items()))


def resources() -> list[dict[str, object]]:
    out = subprocess.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
                         check=True, capture_output=True, text=True, timeout=60)  # fmt: skip
    return json.loads(out.stdout)


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")
