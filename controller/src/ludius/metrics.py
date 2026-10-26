"""Range test metrics and filterability check (spec section 8), from ground-truth logs.

Covers what the step logs can answer: coherence, guardrails, volume, repetition and timing. Artifact
coverage and survival need a collected super-timeline (Velociraptor/KAPE -> Plaso) and are not computed here.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Balanced accuracy a single trainee rule may reach before the noise counts as filterable.
CHANCE_MARGIN = 0.6


@dataclass
class Instance:
    """One task attempt: task_start to task_end (or end of log)."""

    persona: str
    template: str
    launch: str
    status: str = "open"
    steps: list[dict[str, Any]] = field(default_factory=lambda: [])


@dataclass
class Logs:
    instances: list[Instance] = field(default_factory=lambda: [])
    orphans: int = 0
    blocked: Counter[str] = field(default_factory=lambda: Counter[str]())
    span_ms: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _ms(r: dict[str, Any]) -> int:
    return int(r.get("guest_ms") or r.get("host_ms") or 0)


def load(paths: list[Path]) -> Logs:
    logs = Logs()
    for path in paths:
        persona, cur, first, last = "", None, 0, 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r: dict[str, Any] = json.loads(line)
            ev, ms = r.get("event"), int(r.get("host_ms") or 0)
            if ms:
                first, last = first or ms, ms
            persona = r.get("persona") or persona
            if ev == "task_start":
                subgoals: list[str] = r.get("subgoals") or [""]
                cur = Instance(persona, r.get("task", ""), subgoals[0])
                logs.instances.append(cur)
            elif ev == "task_end" and cur:
                cur.status, cur = r.get("status", ""), None
            elif ev == "step":
                if cur:
                    cur.steps.append(r)
                else:
                    logs.orphans += 1
            elif ev == "blocked":
                logs.blocked[str(r.get("reason", ""))] += 1
        if persona:
            logs.span_ms[persona] += last - first
    return logs


def _gaps(steps: list[dict[str, Any]]) -> list[float]:
    ts = [_ms(s) for s in steps]
    return [(b - a) / 1000 for a, b in zip(ts, ts[1:], strict=False) if b > a]


def _cv(xs: list[float]) -> float:
    return statistics.pstdev(xs) / statistics.mean(xs) if len(xs) > 1 and statistics.mean(xs) else 0.0


def range_metrics(logs: Logs) -> dict[str, Any]:
    by_tpl: dict[str, list[Instance]] = defaultdict(list)
    by_persona: dict[str, list[Instance]] = defaultdict(list)
    for i in logs.instances:
        by_tpl[i.template].append(i)
        by_persona[i.persona].append(i)
    steps = sum(len(i.steps) for i in logs.instances)

    def failed(xs: list[Instance]) -> int:
        return sum(i.status not in ("done", "open") for i in xs)

    apps: dict[str, list[Instance]] = defaultdict(list)
    for i in logs.instances:
        for proc in {s.get("process") for s in i.steps if s.get("process")}:
            apps[str(proc)].append(i)
    return {
        "coherence": {
            "completion_by_template": {
                t: round(sum(i.status == "done" for i in xs) / len(xs), 3) for t, xs in sorted(by_tpl.items())
            },
            "orphan_steps": logs.orphans,
            "orphan_rate": round(logs.orphans / (steps + logs.orphans), 4) if steps + logs.orphans else 0.0,
        },
        "guardrails": {
            "blocked": dict(logs.blocked),
            "failure_rate_by_app": {a: round(failed(xs) / len(xs), 3) for a, xs in sorted(apps.items())},
        },
        "volume": {
            p: {
                "tasks_per_hour": round(len(xs) / (logs.span_ms[p] / 3.6e6), 1) if logs.span_ms[p] else None,
                "steps_per_hour": round(sum(len(i.steps) for i in xs) / (logs.span_ms[p] / 3.6e6), 1)
                if logs.span_ms[p]
                else None,
            }
            for p, xs in sorted(by_persona.items())
        },
        "repetition": {
            "sequence_repeat_by_template": {t: round(_repeat(xs), 3) for t, xs in sorted(by_tpl.items())},
            "launch_methods": {t: dict(Counter(i.launch for i in xs)) for t, xs in sorted(by_tpl.items())},
            "step_gap_cv_by_persona": {
                p: round(_cv([g for i in xs for g in _gaps(i.steps)]), 3) for p, xs in sorted(by_persona.items())
            },
        },
    }


def _repeat(xs: list[Instance]) -> float:
    """Share of instances whose action sequence exactly repeats an earlier one."""
    sigs = [tuple(f"{s.get('action')}:{_role(s)}" for s in i.steps) for i in xs]
    return 1 - len(set(sigs)) / len(sigs) if sigs else 0.0


def _role(step: dict[str, Any]) -> str:
    el: dict[str, Any] = step.get("element") or {}
    return str(el.get("role", ""))


def features(i: Instance) -> dict[str, float | str]:
    gaps = _gaps(i.steps)
    ts = [_ms(s) for s in i.steps if _ms(s)]
    hour = (ts[0] // 3_600_000) % 24 if ts else -1  # UTC hour of first input
    return {
        "steps": len(i.steps),
        "mean_gap_s": statistics.mean(gaps) if gaps else 0.0,
        "gap_cv": _cv(gaps),
        "hour_utc": hour,
        "launch": i.launch,
        "first_action": str(i.steps[0].get("action")) if i.steps else "",
        "process": str(i.steps[0].get("process")) if i.steps else "",
    }


def _balanced(pred: list[bool], noise: list[bool]) -> float:
    pos = [p for p, n in zip(pred, noise, strict=True) if n]
    neg = [p for p, n in zip(pred, noise, strict=True) if not n]
    return (sum(pos) / len(pos) + sum(not p for p in neg) / len(neg)) / 2


def filterability(noise: list[Instance], human: list[Instance]) -> dict[str, Any]:
    """Best single-feature rule ("noise if x <= t", "noise if x == c", or their inverse) per feature.

    A rule well above chance names a feature where the noise lacks human variety.
    """
    # ponytail: decision stumps stand in for the spec's LightGBM + rule miner; swap in a real model once
    # a human baseline of several days exists and multi-feature interactions matter.
    rows: list[dict[str, float | str]] = [features(i) for i in noise + human]
    label = [True] * len(noise) + [False] * len(human)
    rules: dict[str, tuple[float, str]] = {}
    names: list[str] = list(rows[0]) if noise and human else []
    for name in names:
        vals = [r[name] for r in rows]
        best = (0.5, "")
        for v in set(vals):
            pred = [x == v for x in vals] if isinstance(v, str) else [float(x) <= float(v) for x in vals]
            acc = _balanced(pred, label)
            op = "==" if isinstance(v, str) else "<="
            for score, rule in ((acc, f"noise if {name} {op} {v!r}"), (1 - acc, f"human if {name} {op} {v!r}")):
                if score > best[0]:
                    best = (round(score, 3), rule)
        rules[name] = best
    top = max(rules.values(), default=(0.5, ""))
    return {
        "noise": len(noise),
        "human": len(human),
        "rules": dict(sorted(rules.items(), key=lambda kv: -kv[1][0])),
        "best": top,
        "pass": top[0] <= CHANCE_MARGIN,
    }


def report(noise: list[Path], human: list[Path] | None = None) -> dict[str, Any]:
    logs = load(noise)
    out = range_metrics(logs)
    if human:
        out["filterability"] = filterability(logs.instances, load(human).instances)
    return out
