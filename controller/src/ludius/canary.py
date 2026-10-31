from __future__ import annotations

import datetime as dt
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ludius.admission import OPERATIONAL
from ludius.day import run_task
from ludius.loop import Runner
from ludius.session import Session
from ludius.tasks import Task, TaskInstance, Template

RUNS_PER_CANARY = 2


def pick(templates: dict[str, Template], admission: dict[str, Any]) -> dict[str, tuple[Template, int]]:
    out: dict[str, tuple[Template, int]] = {}
    for name in sorted(templates):
        rec: dict[str, Any] = admission.get(name) or {}
        variants: list[int] = rec.get("admitted_variants") or []
        t = templates[name]
        if rec.get("admitted") and variants and t.app not in out:
            out[t.app] = (t, int(variants[0]))
    for name in sorted(templates):  # an app with no admitted task still gets checked that it opens and closes
        if templates[name].app not in out and (p := open_probe(templates[name])) is not None:
            out[p.app] = (p, 0)
    return out


def open_probe(t: Template) -> Template | None:
    """Only t's launch and its app-close subgoal: no writes, no sends. A broken Outlook profile fails the launch."""
    first, last = t.variants[0][0], t.variants[0][-1]
    w = (d := t.subgoals[last].done) and d.window
    if not t.subgoals[first].launch or not w or not w.absent or not w.process or w.class_name:
        return None
    return t.model_copy(update={"task": f"{t.app}_open_probe", "capability": None, "variants": [[first, last]],
                                "weights": None, "subgoals": {first: t.subgoals[first], last: t.subgoals[last]},
                                "writes": [], "removes": [], "key_commits": []})  # fmt: skip


def judge(statuses: list[str]) -> str:
    if "done" in statuses:
        return "ok"
    if all(s == "error" for s in statuses):
        return "unknown"
    return "drift"


def load_health(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def drifted(path: Path) -> set[str]:
    return {app for app, h in load_health(path).items() if h.get("state") == "drift"}


def run(
    templates: dict[str, Template],
    admission_record: Path,
    runner: Runner,
    session: Session,
    health_path: Path,
    apps: set[str] | None = None,
    report: Callable[[str], None] = print,
) -> dict[str, str]:
    admission: dict[str, Any] = yaml.safe_load(admission_record.read_text(encoding="utf-8")) or {}
    health = load_health(health_path)
    states: dict[str, str] = {}
    for app, (t, variant) in sorted(pick(templates, admission).items()):
        if apps and app not in apps:
            continue
        inst = TaskInstance(template=t.task, params=t.example, context="drift canary", variant=variant)
        statuses: list[str] = []
        reason = ""
        for n in range(RUNS_PER_CANARY):
            try:
                out = run_task(runner, session, Task(t, inst, random.Random(n)))
                status, reason = out.status, out.reason
            except OPERATIONAL as e:
                status, reason = "error", f"{type(e).__name__}: {str(e)[:300]}"
            statuses.append(status)
            if status == "done":
                break
        state = judge(statuses)
        states[app] = state
        health[app] = {"state": state, "template": t.task, "variant": variant, "runs": statuses, "reason": reason,
                       "checked": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}  # fmt: skip
        runner.log.write(event="canary", app=app, template=t.task, variant=variant, runs=statuses, state=state,
                         reason=reason)  # fmt: skip
        report(f"  {app}: {state} ({t.task} v{variant}: {' '.join(statuses)})" + (f" - {reason}" if reason else ""))
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(json.dumps(health, indent=2), encoding="utf-8")
    return states
