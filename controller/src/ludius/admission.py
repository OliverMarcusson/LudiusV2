"""Template admission (spec sections 6 and 8): run every variant in compressed mode from a clean checkpoint.

A variant is admitted when it completes in at least two thirds of its runs; a template is admitted with its
reliable variants only. Proposed templates (data/templates/proposed/) move into the catalog on admission.

A run that fails on infrastructure (Jev or LLM contract/HTTP errors, an RPC to the host agent or observer, an
observer that never comes back) is recorded as "error": a failed attempt, never a pass. The next run starts
only after a successful restore; a failed restore stops admission, keeping the runs done so far.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc
import httpx
import yaml

from ludius.day import run_task
from ludius.jev import JevError
from ludius.llm import LlmError
from ludius.loop import Runner
from ludius.session import Session
from ludius.tasks import Task, TaskInstance, Template

# Operational failures isolated per run. Anything else is a bug and still raises.
OPERATIONAL = (JevError, LlmError, grpc.RpcError, httpx.HTTPError, TimeoutError)


def reliable(passes: int, runs: int) -> bool:
    return runs > 0 and passes >= math.ceil(runs * 2 / 3)


def validate(
    template: Template,
    runs: int,
    restore: Callable[[], None],
    runner: Runner,
    session: Session,
    report: Callable[[str], None] = print,
) -> tuple[dict[int, list[str]], str]:
    """Statuses per variant, and why admission stopped early ("" when every run happened).

    `restore` resets the VM to the clean checkpoint before each run. Each run is logged as an
    `admission_run` event (template, variant, run, status, steps, reason).
    """
    inst = TaskInstance(template=template.task, params=template.example, context="admission test run")
    results: dict[int, list[str]] = {}
    for v in range(len(template.variants)):
        for n in range(runs):
            try:
                restore()
            except OPERATIONAL as e:
                why = f"restore failed before variant {v} run {n + 1}: {type(e).__name__}: {e}"
                runner.log.write(event="admission_stopped", template=template.task, reason=why)
                report(f"  {why}")
                return results, why
            task = Task(template, inst.model_copy(update={"variant": v}), random.Random(n))
            try:
                out = run_task(runner, session, task)
                status, steps, reason = out.status, out.steps, out.reason
            except OPERATIONAL as e:
                status, steps, reason = "error", 0, f"{type(e).__name__}: {str(e)[:300]}"
            results.setdefault(v, []).append(status)
            runner.log.write(event="admission_run", template=template.task, variant=v, run=n + 1,
                             status=status, steps=steps, reason=reason)  # fmt: skip
            report(f"  variant {v} run {n + 1}: {status} in {steps} steps" + (f" - {reason}" if reason else ""))
    return results, ""


def admit(template: Template, results: dict[int, list[str]], source: Path, catalog: Path, record: Path) -> bool:
    """Record the outcome; write the admitted template (reliable variants only) into the catalog."""
    keep = [v for v, st in results.items() if reliable(st.count("done"), len(st))]
    data: dict[str, Any] = yaml.safe_load(record.read_text(encoding="utf-8")) if record.exists() else {}
    data[template.task] = {
        "date": dt.date.today().isoformat(),
        "variants": {v: f"{st.count('done')}/{len(st)}" for v, st in results.items()},
        "admitted_variants": keep,
        "admitted": bool(keep),
    }
    record.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    if not keep:
        return False
    target = catalog / f"{template.task}.yaml"
    if source.resolve() == target.resolve() and len(keep) == len(template.variants):
        return True  # already in the catalog, nothing dropped
    raw: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["variants"] = [template.variants[v] for v in keep]
    if template.weights:
        raw["weights"] = [template.weights[v] for v in keep]
    used = {s for v in raw["variants"] for s in v}
    raw["subgoals"] = {k: s for k, s in raw["subgoals"].items() if k in used}
    target.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    if source.resolve() != target.resolve():
        source.unlink()
    return True
