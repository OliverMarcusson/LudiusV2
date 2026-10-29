"""Live activity lines for the operator monitor, one per persona (`ludius range summaries`).

Each persona's newest ground-truth day log (runs/days/<date>-<user>.jsonl) is read every few seconds and turned into
one line by rule, without a model: the running task and its current subgoal, or "idle" or "unknown" (a quiet log or
one that can't be read); nothing is invented. Output: runs/monitor-summaries.json beside openrouter-calls.json; the
host agent serves it read-only at /api/summaries. (An LLM sentence per step was retired after warp6: not useful.)

Each pass also judges every persona's health by rule (ludius.health: ok, warn or fail with a reason, in the same
file). A change into warn or fail, and the recovery from one, is one journal line and one line in
runs/monitor-alerts.jsonl (persona, health, reason, rules, host time and the persona's logical time) for an
orchestrator to tail.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from ludius import calls, health, interactions
from ludius.rangerun import personas, vm_of

OUT = calls.PATH.parent / "monitor-summaries.json"
QUIET_MS = 10 * 60_000  # a running task that logs nothing this long: the worker may be gone, so "unknown"
TAIL = 512 << 10  # bytes read from the end of a day log
STEPS = 5
PROGRESS = {"day_start", "item_start", "task_start", "step", "subgoal_done", "task_end", "item_end", "break",
            "day_end", "day_aborted", "interrupt", "resume"}  # fmt: skip


@dataclass
class Activity:
    state: Literal["active", "idle", "unknown"]
    text: str  # the line shown


def newest_log(days: Path, user: str) -> Path | None:
    """The persona's most recently written day log (a warp's simulated days sort after today's); OSError if the
    directory can't be read."""
    logs = [(p.stat().st_mtime, p) for p in days.glob(f"????-??-??-{user}.jsonl")]
    return max(logs)[1] if logs else None


def activity(days: Path, user: str, now_ms: int) -> Activity:
    """What the persona's newest day log says is happening now."""
    try:
        log = newest_log(days, user)
    except OSError:
        return Activity("unknown", "unknown: day log unreadable")
    if log is None:
        return Activity("idle", "idle: no day log yet")
    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return Activity("unknown", "unknown: day log unreadable")

    # One frame per item being played, an interruption on top of the item it cut into:
    # {"item": its item_start or None, "task": its task_start or None, "steps": [...]}.
    stack: list[dict[str, Any]] = []
    ended = False
    last: dict[str, Any] | None = None
    last_ms = 0

    def frame(item: dict[str, Any] | None, ms: int) -> dict[str, Any]:
        return {"item": item, "task": None, "steps": []}

    for text in lines:
        try:
            parsed: Any = json.loads(text)
        except ValueError:
            continue  # the cut at the tail's start, or a line still being written
        if not isinstance(parsed, dict):
            continue
        e = cast(dict[str, Any], parsed)
        if e.get("event") not in PROGRESS:
            continue
        last, ev = e, e["event"]
        last_ms = int(e.get("host_ms") or last_ms)
        if ev == "day_start":
            stack, ended = [], False
        elif ev == "item_start":
            stack = [*stack, frame(e, last_ms)] if e.get("interrupt") else [frame(e, last_ms)]
        elif ev == "resume" and not stack:  # the interrupted item's start was cut off: its name is in the resume
            stack = [frame({"template": e.get("task")}, last_ms)]
        elif ev in ("task_start", "step"):
            if not stack:  # a task whose start is not in the part of the log read: active, task unknown
                stack = [frame(None, last_ms)]
            top = stack[-1]
            if ev == "task_start":
                top.update(task=e, steps=[])
            else:
                top["steps"] = [*top["steps"], e][-STEPS:]
        elif ev == "task_end" and stack:
            stack[-1]["task"] = None
        elif ev == "item_end" and stack:
            stack = stack[:-1]
        elif ev in ("day_end", "day_aborted"):  # an aborted day is over too: nothing runs after it
            stack, ended = [], True
    if last is None:
        return Activity("idle", "idle: day not started")
    if ended:
        return Activity("idle", "idle: day ended")
    if not stack:
        return Activity("idle", "idle: between tasks")
    top = stack[-1]
    item: dict[str, Any] | None = top["item"]
    task: dict[str, Any] | None = top["task"]
    steps: list[dict[str, Any]] = top["steps"]
    if item and item.get("template") == "break":
        return Activity("idle", f"away from the desk (screen locked), break from {item.get('at')}")
    if now_ms - last_ms > QUIET_MS:
        return Activity("unknown", f"unknown: no activity recorded for {(now_ms - last_ms) // 60_000} min")
    template = str((task or {}).get("task") or (item or {}).get("template") or "") or "an unknown task"
    subgoal = str(steps[-1].get("subgoal")) if steps else ""
    return Activity("active", f"working on {template}" + (f": {subgoal}" if subgoal else ""))


@dataclass
class Summariser:
    users: list[str]
    days: Path
    out: Path = OUT
    alerts: Path | None = None  # runs/monitor-alerts.jsonl, default beside `out`
    tracker: interactions.Tracker | None = None  # runs/monitor-interactions.jsonl beside `out`; all range personas
    supervisor: Path | None = None  # runs/range/supervisor.jsonl, default beside days/
    rules: health.Rules = field(default_factory=health.Rules)
    lines: dict[str, Activity] = field(default_factory=dict[str, Activity])
    verdicts: dict[str, health.Verdict] = field(default_factory=dict[str, health.Verdict])
    _seeded: bool = False

    def __post_init__(self) -> None:
        self.alerts = self.alerts or self.out.parent / "monitor-alerts.jsonl"
        self.tracker = self.tracker or interactions.Tracker(
            self.days, personas(), self.out.parent / "monitor-interactions.jsonl"
        )

    def step(self) -> None:
        now = dt.datetime.now(dt.UTC)
        now_ms = int(now.timestamp() * 1000)
        for u in self.users:
            self.check(u, now, now_ms)
            self.lines[u] = activity(self.days, u, now_ms)
        assert self.tracker is not None
        for i in self.tracker.step():
            print(json.dumps({"interaction": asdict(i)}), flush=True)
        self.write(now)

    def check(self, user: str, now: dt.datetime, now_ms: int) -> None:
        """The persona's health by rule; a change into or out of warn/fail is one journal line and one alert."""
        if not self._seeded:  # a restart carries on from the last alerts, so it neither repeats nor loses one
            assert self.alerts is not None
            self.verdicts = seed(self.alerts, self.users) | self.verdicts
            self._seeded = True
        supervisor = self.supervisor or self.days.parent / "range" / "supervisor.jsonl"
        old = self.verdicts.get(user)
        was_running = old is not None and old.running
        try:
            log = newest_log(self.days, user)
        except OSError:
            v = health.Verdict("warn", "unknown: day logs unreadable", ("unreadable",), running=was_running)
        else:
            gone = old is not None and old.log != "" and str(log) != old.log and not Path(old.log).exists()
            if was_running and (log is None or gone):  # the running day's log went away: never ok
                assert old is not None
                v = health.Verdict("warn", "unknown: the running day's log is missing", ("missing",), old.logical,
                                   True, old.log)  # fmt: skip
            elif log is None:
                v = health.Verdict("ok", "")
            else:
                v = health.classify(log, now_ms, self.rules, health.worker_exits(supervisor, user))
                if v.rules == ("unreadable",):
                    v.running, v.logical = was_running, old.logical if old else None
        self.verdicts[user] = v
        if ((old.health, old.rules) if old else ("ok", ())) == (v.health, v.rules):
            return  # also the first ok: only warn/fail, and the recovery from one, are alerts
        alert = {"persona": user, "health": v.health, "reason": v.reason or "recovered", "rules": list(v.rules),
                 "host": _iso(now), "logical": v.logical}  # fmt: skip
        print(json.dumps({"alert": alert}), flush=True)
        assert self.alerts is not None
        self.alerts.parent.mkdir(parents=True, exist_ok=True)
        with self.alerts.open("a", encoding="utf-8") as f:
            f.write(json.dumps(alert) + "\n")

    def write(self, now: dt.datetime) -> None:
        data = {
            "updated": _iso(now),
            "interactions": [asdict(i) for i in self.tracker.recent] if self.tracker else [],
            "personas": {
                u: {
                    "vm": vm_of(u),
                    "state": "ok" if line.state == "active" else line.state,
                    "text": line.text,
                    "at": _iso(now),
                    "health": self.verdicts[u].health if u in self.verdicts else "ok",
                    "reason": self.verdicts[u].reason if u in self.verdicts else "",
                }
                for u, line in self.lines.items()
            },  # fmt: skip
        }
        _write(self.out, data)


def seed(alerts: Path, users: list[str]) -> dict[str, health.Verdict]:
    """Each persona's health as its last alert left it (runs/monitor-alerts.jsonl); a warn or fail is taken as a
    day still running, so a log missing now reads as unknown, not ok."""
    out: dict[str, health.Verdict] = {}
    try:
        lines = alerts.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            a: Any = json.loads(line)
        except ValueError:
            continue
        if not isinstance(a, dict):
            continue
        r = cast(dict[str, Any], a)
        h, rules = r.get("health"), r.get("rules")
        if r.get("persona") in users and h in ("ok", "warn", "fail") and isinstance(rules, list):
            reason = str(r.get("reason") or "") if h != "ok" else ""
            out[str(r["persona"])] = health.Verdict(h, reason,
                                                    tuple(map(str, cast(list[Any], rules))),
                                                    r.get("logical"), running=h != "ok")  # fmt: skip
    return out


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(5):  # Windows refuses the replace while a reader holds the file
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)
