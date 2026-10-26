"""Clock lease: a jumped range clock must not outlive the controller that jumped it.

Before a time-jump the controller takes a lease, runs/locks/clock-<vm>.json, and a daemon thread renews it every
RENEW_S. A reset drops it. If the controller is killed the renewals stop; the host agent's sweeper
(infra/hyperv/clock-sweep.ps1) treats a lease not renewed for STALE_S as orphaned, resets the range clocks through
set-clock.ps1 -Reset and only then deletes the lease. Heartbeats rather than pids: a reused pid can't keep a dead
controller's lease alive. Host and LAN time are never touched; the reset is the same one a finishing day runs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

RENEW_S = 30
STALE_S = 120  # the sweeper's threshold (clock-sweep.ps1 -StaleSeconds); four missed renewals


class Lease:
    def __init__(self, path: Path, vm: str, renew_s: float = RENEW_S) -> None:
        self.path, self.vm, self.renew_s = path, vm, renew_s
        self._stop: threading.Event | None = None

    def hold(self) -> None:
        """Write the lease now and keep renewing it until release(); a second hold() only renews."""
        self._write()
        if self._stop is None:
            self._stop = threading.Event()
            threading.Thread(target=self._renew, args=(self._stop,), daemon=True, name="clock-lease").start()

    def release(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._stop = None
        self.path.unlink(missing_ok=True)

    def _renew(self, stop: threading.Event) -> None:
        while not stop.wait(self.renew_s):
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"vm": self.vm, "pid": os.getpid(), "renewed": now}), encoding="utf-8")
        tmp.replace(self.path)  # the sweeper never reads a half-written lease
