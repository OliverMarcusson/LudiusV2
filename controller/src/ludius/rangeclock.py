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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

MAX_SKEW_S = 60.0  # spec of the warp test; Kerberos breaks at 300
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
# PowerShell took to start (seconds on a busy persona) is added, so a member lands on the DC's instant.
_SET = ("Set-Date ([DateTimeOffset]::FromUnixTimeMilliseconds({ms}).LocalDateTime + ((Get-Date) - "
        "[Diagnostics.Process]::GetCurrentProcess().StartTime)) | Out-Null; ")  # fmt: skip
_NOW = "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()"


@dataclass
class RangeClock:
    dc: int
    members: list[int]  # ex01 and the personas
    run: Exec = qm_exec
    now: Callable[[], float] = time.time

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
        target = at.timestamp()
        dc_now = self.now() + self._offset(self.dc)
        if target < dc_now - MAX_SKEW_S:
            raise ValueError(f"range clock is forward only: DC at {_iso(dc_now)}, asked {_iso(target)}")
        return self._apply(target - self.now())

    def reset(self) -> dict[int, float]:
        """Every VM back to the box's UTC, DC first (the members follow it again)."""
        return self._apply(0.0)

    def _apply(self, offset: float) -> dict[int, float]:
        # Each VM to box time + offset at the moment its command is built: the members land on the DC's instant.
        def one(vmid: int) -> str:
            return self.run(vmid, _SET.format(ms=int((self.now() + offset) * 1000)) + _NOW)

        one(self.dc)
        with ThreadPoolExecutor(max(1, len(self.members))) as pool:
            list(pool.map(one, self.members))
        got = self.offsets()
        if (skew := max(got.values()) - min(got.values())) > MAX_SKEW_S or abs(got[self.dc] - offset) > MAX_SKEW_S:
            raise RuntimeError(f"range clock not converged (skew {skew:.1f} s): {got}")
        return got


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")
