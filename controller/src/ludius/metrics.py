"""Range test metrics and filterability check (spec section 8), from ground-truth logs.

Covers what the step logs can answer: coherence, guardrails, volume, repetition and timing. Artifact
coverage and survival need a collected super-timeline (Velociraptor/KAPE -> Plaso) and are not computed here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Balanced accuracy a single trainee rule may reach before the noise counts as filterable.
CHANCE_MARGIN = 0.6
# Held-out evaluation: whole source logs (one run or session each) go to one side, never rows of the same
# run on both sides; the order within each label is a salted hash, so the split is reproducible.
HOLDOUT_FRACTION = 0.3
HOLDOUT_SALT = "ludius-holdout-v1"
# What the labels are. Nothing here verifies that the comparison logs are human sessions.
LABELS = (
    "stand-in: positive = logs given as noise, negative = logs given with --human; operator-supplied, "
    "not verified as genuine human sessions"
)


@dataclass
class Instance:
    """One task attempt: task_start to task_end (or end of log)."""

    persona: str
    template: str
    launch: str
    status: str = "open"
    steps: list[dict[str, Any]] = field(default_factory=lambda: [])
    source: str = ""  # log path relative to the dataset root, forward slashes (provenance, never a feature)
    line: int = 0  # 1-based line of its task_start


@dataclass
class Logs:
    instances: list[Instance] = field(default_factory=lambda: [])
    orphans: int = 0
    blocked: Counter[str] = field(default_factory=lambda: Counter[str]())
    span_ms: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _ms(r: dict[str, Any]) -> int:
    return int(r.get("guest_ms") or r.get("host_ms") or 0)


def root_of(paths: list[Path]) -> Path:
    """The dataset root: the deepest directory holding every log, so source paths don't depend on the cwd."""
    return Path(os.path.commonpath([p.resolve().parent for p in paths])) if paths else Path()


def load(paths: list[Path], root: Path | None = None) -> Logs:
    logs = Logs()
    root = root or root_of(paths)
    for path in paths:
        persona, cur, first, last = "", None, 0, 0
        source = path.resolve().relative_to(root).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            r: dict[str, Any] = json.loads(line)
            ev, ms = r.get("event"), int(r.get("host_ms") or 0)
            if ms:
                first, last = first or ms, ms
            persona = r.get("persona") or persona
            if ev == "task_start":
                subgoals: list[str] = r.get("subgoals") or [""]
                cur = Instance(persona, r.get("task", ""), subgoals[0], source=source, line=n)
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


Rule = tuple[str, str, float | str, bool]  # feature, op ("==" or "<="), value, predicts noise


def _balanced(pred: list[bool], noise: list[bool]) -> float:
    pos = [p for p, n in zip(pred, noise, strict=True) if n]
    neg = [p for p, n in zip(pred, noise, strict=True) if not n]
    return (sum(pos) / len(pos) + sum(not p for p in neg) / len(neg)) / 2


def _hits(x: float | str, op: str, v: float | str) -> bool:
    return x == v if op == "==" else float(x) <= float(v)


def _text(rule: Rule | None) -> str:
    if rule is None:
        return ""
    name, op, v, noise = rule
    return f"{'noise' if noise else 'human'} if {name} {op} {v!r}"


def _fit(rows: list[dict[str, float | str]], label: list[bool]) -> dict[str, tuple[float, Rule | None]]:
    """Best single-feature rule ("noise if x <= t", "noise if x == c", or their inverse) per feature."""
    # ponytail: decision stumps stand in for the spec's LightGBM + rule miner; swap in a real model once
    # a human baseline of several days exists and multi-feature interactions matter.
    rules: dict[str, tuple[float, Rule | None]] = {}
    names: list[str] = list(rows[0]) if rows and any(label) and not all(label) else []
    for name in names:
        vals = [r[name] for r in rows]
        best: tuple[float, Rule | None] = (0.5, None)
        for v in set(vals):
            op = "==" if isinstance(v, str) else "<="
            acc = _balanced([_hits(x, op, v) for x in vals], label)
            for score, noise in ((acc, True), (1 - acc, False)):
                if score > best[0]:
                    best = (round(score, 3), (name, op, v, noise))
        rules[name] = best
    return rules


def _score(rule: Rule, rows: list[dict[str, float | str]], label: list[bool]) -> float:
    name, op, v, noise = rule
    return _balanced([_hits(r[name], op, v) == noise for r in rows], label)


def filterability(noise: list[Instance], human: list[Instance]) -> dict[str, Any]:
    """In-sample: the best rule per feature on all instances. A rule well above chance names a feature where
    the noise lacks human variety. `held_out` is the honest number: fitted on training logs only."""
    rows: list[dict[str, float | str]] = [features(i) for i in noise + human]
    label = [True] * len(noise) + [False] * len(human)
    fitted = _fit(rows, label)
    rules = {name: (score, _text(rule)) for name, (score, rule) in fitted.items()}
    top = max(rules.values(), default=(0.5, ""))
    return {
        "noise": len(noise),
        "human": len(human),
        "rules": dict(sorted(rules.items(), key=lambda kv: -kv[1][0])),
        "best": top,
        "pass": top[0] <= CHANCE_MARGIN,
        "held_out": held_out(noise, human),
    }


def split(noise: list[Instance], human: list[Instance], fraction: float = HOLDOUT_FRACTION,
          salt: str = HOLDOUT_SALT) -> dict[str, str] | str:  # fmt: skip
    """Source log -> "train" or "test", or why no honest split exists. Per label, the sources are ordered by a
    salted hash and the first max(1, round(fraction * n)) are held out, so each side gets every label."""
    groups = {lab: sorted({i.source for i in xs}) for lab, xs in (("noise", noise), ("human", human))}
    if both := set(groups["noise"]) & set(groups["human"]):
        return f"source logs carry both labels: {sorted(both)}"
    out: dict[str, str] = {}
    for lab, gs in groups.items():
        if len(gs) < 2:
            return f"{len(gs)} independent {lab} source log(s); a held-out split needs at least 2 per label"
        ordered = sorted(gs, key=lambda g: hashlib.sha256(f"{salt}:{g}".encode()).hexdigest())
        k = max(1, round(fraction * len(gs)))
        out |= {g: "test" if n < k else "train" for n, g in enumerate(ordered)}
    return out


def held_out(noise: list[Instance], human: list[Instance], fraction: float = HOLDOUT_FRACTION,
             salt: str = HOLDOUT_SALT) -> dict[str, Any]:  # fmt: skip
    """The best stump fitted on the training logs only, scored on the held-out logs only."""
    out: dict[str, Any] = {"grouping": "source log", "fraction": fraction, "salt": salt}
    sides = split(noise, human, fraction, salt)
    if isinstance(sides, str):
        return out | {"available": False, "reason": sides}
    data = [(i, True) for i in noise] + [(i, False) for i in human]
    parts = {s: [(i, lab) for i, lab in data if sides[i.source] == s] for s in ("train", "test")}
    for s, xs in parts.items():
        out[s] = {
            "noise": sum(lab for _, lab in xs),
            "human": sum(not lab for _, lab in xs),
            "sources": sorted({i.source for i, _ in xs}),
        }
    train, test = parts["train"], parts["test"]
    fitted = _fit([features(i) for i, _ in train], [lab for _, lab in train])
    score, rule = max(fitted.values(), key=lambda sr: sr[0], default=(0.5, None))
    if rule is None:
        return out | {"available": False, "reason": "no training rule beats chance"}
    held = round(_score(rule, [features(i) for i, _ in test], [lab for _, lab in test]), 3)
    return out | {"available": True, "rule": _text(rule), "train_score": score, "held_out_score": held,
                  "pass": held <= CHANCE_MARGIN}  # fmt: skip


def provenance(paths: list[Path], root: Path) -> list[dict[str, Any]]:
    return [{"source": p.resolve().relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in paths]  # fmt: skip


def write_features(out: Path, noise: list[Instance], human: list[Instance]) -> int:
    """One CSV row per instance: label, split and provenance columns, then the fitted features (readable as is by
    DuckDB read_csv or polars.read_csv; neither is a dependency here)."""
    sides = split(noise, human)
    rows = [(i, "noise") for i in noise] + [(i, "human") for i in human]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        names = list(features(rows[0][0])) if rows else []
        w.writerow(["label", "split", "source", "line", "persona", "template", "status", *names])
        for i, lab in rows:
            side = sides.get(i.source, "") if isinstance(sides, dict) else ""
            w.writerow([lab, side, i.source, i.line, i.persona, i.template, i.status, *features(i).values()])
    return len(rows)


def report(noise: list[Path], human: list[Path] | None = None, features_csv: Path | None = None) -> dict[str, Any]:
    root = root_of(noise + (human or []))
    logs = load(noise, root)
    out = range_metrics(logs)
    comparison = load(human, root).instances if human else []
    if human:
        out["filterability"] = filterability(logs.instances, comparison)
    dataset: dict[str, Any] = {
        "labels": LABELS,
        "human_baseline": False,  # never claimed: nothing here can verify the comparison logs are human
        "noise": provenance(noise, root),
        "comparison": provenance(human or [], root),
    }
    if features_csv:
        dataset["features_csv"] = {
            "path": str(features_csv),
            "rows": write_features(features_csv, logs.instances, comparison),
        }
    out["dataset"] = dataset
    return out
