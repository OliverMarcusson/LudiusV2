"""Counts of outgoing OpenRouter requests for the operator monitor, shared by every controller process.

One count per HTTP request actually sent: a retry or a validation re-ask that posts again counts again.
Totals are cumulative in runs/openrouter-calls.json until that file is deleted; the host agent serves it read-only.
A deployment caps spend with LUDIUS_CAP_DECISION / LUDIUS_CAP_LLM: absolute ceilings on these shared totals, so the cap
holds across every controller process and restart (set them to the current total plus the budget).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

PATH = Path(__file__).resolve().parents[3] / "runs" / "openrouter-calls.json"

Kind = Literal["decision", "llm"]


def _lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


class CapReached(RuntimeError):
    """The request would pass LUDIUS_CAP_<KIND>; it is not sent."""


def count(kind: Kind) -> None:
    """Add one request of `kind`, called before sending it. Uncapped it never raises: a monitor counter must not
    break a run. Capped, it raises CapReached at the ceiling, and fails closed when the counter is unusable."""
    cap = os.environ.get(f"LUDIUS_CAP_{kind.upper()}")
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(PATH.with_suffix(".lock"), os.O_RDWR | os.O_CREAT)
        try:
            _lock(fd)  # released when fd closes
            try:
                old: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if cap is not None:  # a lost counter would restart from 0 and grant the whole ceiling again
                    raise
                old = {}
            if "decision" not in old and "jev" in old:  # counter files from before the rename keep their count
                old["decision"] = old.pop("jev")
            if cap is not None:
                total = old.get(kind)
                if type(total) is not int or total < 0:
                    raise ValueError(f"{kind} total {total!r} is not a count")
                if total >= int(cap):
                    raise CapReached(f"{kind} request cap LUDIUS_CAP_{kind.upper()}={cap} reached; nothing sent")
            now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            data = {
                "decision": int(old.get("decision", 0)) + (kind == "decision"),
                "llm": int(old.get("llm", 0)) + (kind == "llm"),
                "since": old.get("since") or now,
                "updated": now,
            }
            tmp = PATH.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            for attempt in range(5):  # Windows refuses the replace while a reader holds the file
                try:
                    os.replace(tmp, PATH)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        if cap is not None:  # an uncounted request could pass the ceiling
            raise e if isinstance(e, CapReached) else CapReached(f"{kind} counter unusable: {e}") from e
