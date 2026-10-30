from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

COLUMNS = ["time_utc", "clock", "source", "persona", "vm", "task", "subgoal", "event", "action", "target", "window",
           "process", "result"]  # fmt: skip
EVENTS = {"day_start", "day_end", "item_start", "item_end", "task_start", "task_end", "subgoal_done", "step",
          "ladder", "interrupt", "resume", "break", "unlock", "unlocked", "normalized", "normalize_failed",
          "side_effects", "blocked"}  # fmt: skip


def rows(paths: list[Path]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for path in paths:
        persona = vm = task = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            r: dict[str, Any] = json.loads(line)
            ev = r.get("event", "")
            if ev not in EVENTS:
                continue
            persona = r.get("persona") or persona
            vm = r.get("vm") or vm
            task = r.get("task") or r.get("template") or task
            guest = r.get("guest_ms")
            ms = guest or r.get("host_ms") or 0
            el: dict[str, Any] = r.get("element") or {}
            out.append({
                "time_utc": dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat(timespec="milliseconds"),
                "clock": "guest" if guest else "host",
                "source": "noise",
                "persona": persona,
                "vm": vm,
                "task": task,
                "subgoal": r.get("subgoal", ""),
                "event": ev,
                "action": r.get("action") or r.get("status") or r.get("by") or "",
                "target": f"{el.get('role', '')} '{el.get('name', '')}'" if el else "",
                "window": r.get("window", ""),
                "process": r.get("process", ""),
                "result": _result(r),
            })  # fmt: skip
    out.sort(key=lambda row: row["time_utc"])
    return out


def _result(r: dict[str, Any]) -> str:
    if r.get("event") == "side_effects":
        opened: list[str] = r.get("opened") or []
        return "opened " + "; ".join(opened) if opened else ""
    parts = [str(r[k]) for k in ("result", "reason", "context") if r.get(k)]
    if r.get("event") == "break":
        parts.append(f"away {r.get('minutes')} min")
    return " | ".join(parts)


def export(paths: list[Path], out: Path) -> int:
    data = rows(paths)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, COLUMNS)
        w.writeheader()
        w.writerows(data)
    return len(data)
