"""A persona's health from its ground-truth day log, by rule (no LLM), for the monitor (ludius.summaries).

ok, warn or fail, with a short reason. Rules, thresholds from the environment:
- stalled (fail): a day is running (day_start without day_end) and nothing was logged for LUDIUS_HEALTH_STALL_S
  (300 s). A realtime day logs nothing while it waits for the next item's time, so between items it is not judged;
  inside an item it is, a break allowed its minutes first.
- looping (warn): LUDIUS_HEALTH_NORMALIZE_FAILS (2) normalize_failed in a row without a normalized between them, or
  an item_retry at attempt LUDIUS_HEALTH_RETRY_ATTEMPT (3) or more for the item still open.
- failing (fail): more than LUDIUS_HEALTH_ABANDONED (6) items ended other than done today (a full day on the range
  abandons 4 to 6 of 8 to 10 now), or LUDIUS_HEALTH_ERRORS (3) errors: decision_error and budget_spent in the day log
  plus the worker's exits and failed starts in runs/range/supervisor.jsonl since the day started.
A finished day (day_end) is judged on its counts only. A log that can't be read is warn: health unknown.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

Health = Literal["ok", "warn", "fail"]
MAX_READ = 8 << 20  # day logs run to about 120 KB
ERRORS = ("decision_error", "budget_spent")


def _env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Rules:
    stall_s: int = field(default_factory=lambda: _env("LUDIUS_HEALTH_STALL_S", 300))
    normalize_fails: int = field(default_factory=lambda: _env("LUDIUS_HEALTH_NORMALIZE_FAILS", 2))
    retry_attempt: int = field(default_factory=lambda: _env("LUDIUS_HEALTH_RETRY_ATTEMPT", 3))
    abandoned: int = field(default_factory=lambda: _env("LUDIUS_HEALTH_ABANDONED", 6))
    errors: int = field(default_factory=lambda: _env("LUDIUS_HEALTH_ERRORS", 3))


@dataclass
class Verdict:
    health: Health
    reason: str  # shown on the tile; "" when ok
    rules: tuple[str, ...] = ()  # which rules fired: an alert is due when these or the health change
    logical: str | None = None  # the persona's own clock at the last event (guest time, else the item's slot)


def worker_exits(supervisor: Path, worker: str) -> list[int]:
    """Host ms of the worker's exits and failed starts in the supervisor's event log (local-time stamps)."""
    out: list[int] = []
    try:
        lines = supervisor.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            r = cast(dict[str, Any], e)
            if r.get("worker") == worker and r.get("event") in ("exit", "spawn_failed"):
                try:
                    out.append(int(dt.datetime.fromisoformat(str(r.get("at"))).timestamp() * 1000))
                except ValueError:
                    continue
    return out


def classify(log: Path | None, now_ms: int, rules: Rules, exits: list[int] | None = None) -> Verdict:
    if log is None:
        return Verdict("ok", "")
    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - MAX_READ))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return Verdict("warn", "health unknown: day log unreadable", ("unreadable",))

    running = False
    clock: Any = None
    last_ms = start_ms = 0
    item: dict[str, Any] | None = None  # the open item (the innermost, while interrupted)
    open_items = 0
    normalize_streak = retry = abandoned = errors = 0
    retry_template = ""
    guest_ms: int | None = None
    for text in lines:
        try:
            parsed: Any = json.loads(text)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        e = cast(dict[str, Any], parsed)
        ev = e.get("event")
        last_ms = int(e.get("host_ms") or last_ms)
        if isinstance(e.get("guest_ms"), int):
            guest_ms = e["guest_ms"]
        if ev == "day_start":  # a restarted day starts its counts again
            running, clock, start_ms, item, open_items = True, e.get("clock"), last_ms, None, 0
            normalize_streak = retry = abandoned = errors = 0
        elif ev == "day_end":
            running, item, open_items = False, None, 0
        elif ev == "item_start":
            item, open_items = e, (open_items + 1 if e.get("interrupt") else 1)
        elif ev == "item_end":
            open_items = max(0, open_items - 1)
            item = item if open_items else None
            abandoned += e.get("status") != "done"
            retry = 0
        elif ev == "normalize_failed":
            normalize_streak += 1
        elif ev == "normalized":
            normalize_streak = 0
        elif ev == "item_retry":
            retry, retry_template = int(e.get("attempt") or 0), str(e.get("template") or "")
        elif ev in ERRORS:
            errors += 1
    if not last_ms:
        return Verdict("ok", "")
    errors += sum(t >= start_ms for t in exits or [])

    found: list[tuple[str, Health, str]] = []
    quiet_s = (now_ms - last_ms) // 1000
    if running and quiet_s >= rules.stall_s and (open_items or clock != "realtime"):
        away_s = int(item.get("minutes") or 0) * 60 if item and item.get("template") == "break" else 0
        if quiet_s >= rules.stall_s + away_s:
            found.append(("stalled", "fail", f"stalled: nothing logged for {quiet_s // 60} min"))
    if running and normalize_streak >= rules.normalize_fails:
        found.append(("looping", "warn", f"looping: normalize failed {normalize_streak}x in a row"))
    if running and retry >= rules.retry_attempt:
        found.append(("retrying", "warn", f"looping: {retry_template or 'item'} retry attempt {retry}"))
    if abandoned > rules.abandoned:
        found.append(("abandoning", "fail", f"failing: {abandoned} items not done"))
    if errors >= rules.errors:
        found.append(("errors", "fail", f"failing: {errors} errors"))
    logical = (
        dt.datetime.fromtimestamp(guest_ms / 1000, dt.UTC).isoformat(timespec="seconds")
        if guest_ms
        else (str(item.get("logical") or "") or None)
        if item
        else None
    )
    if not found:
        return Verdict("ok", "", (), logical)
    health: Health = "fail" if any(h == "fail" for _, h, _ in found) else "warn"
    return Verdict(health, "; ".join(r for _, _, r in found), tuple(n for n, _, _ in found), logical)
