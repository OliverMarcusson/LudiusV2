from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

RENEW_S = 30
STALE_S = 120


class Lease:
    def __init__(self, path: Path, vm: str, renew_s: float = RENEW_S) -> None:
        self.path, self.vm, self.renew_s = path, vm, renew_s
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._stop is not None

    def hold(self) -> None:
        with self._lock:
            self._write()
        if self._stop is None:
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._renew, args=(self._stop,), daemon=True, name="clock-lease")
            self._thread.start()

    def release(self) -> None:
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self.path.unlink(missing_ok=True)
        if self._thread is not None:
            self._thread.join()
        self._stop = self._thread = None

    def _renew(self, stop: threading.Event) -> None:
        while not stop.wait(self.renew_s):
            with self._lock:
                if stop.is_set():
                    return
                self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"vm": self.vm, "pid": os.getpid(), "renewed": now}), encoding="utf-8")
        tmp.replace(self.path)
