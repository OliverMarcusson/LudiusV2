"""Fixture replay: recorded observations + subgoal -> expected option (offline), expected Decisions choice (live).

Live Decisions replay runs only with LUDIUS_LIVE_DECISION=1 and OPENROUTER_API_KEY set.
"""

from __future__ import annotations

import os
import random
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from google.protobuf import json_format

from ludius.cli import DATA, ROOT, load_env
from ludius.decisions import DecisionsClient
from ludius.options import build_options, build_state, view
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, load_template, load_yaml
from ludius.v1 import observation_pb2 as obs

CASES = sorted(p.parent for p in (ROOT / "fixtures").glob("*/case.yaml"))


def load(case_dir: Path) -> tuple[obs.Snapshot, Task, str, dict[str, Any]]:
    case: dict[str, Any] = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    snap = json_format.Parse((case_dir / "snapshot.json").read_text(encoding="utf-8"), obs.Snapshot())
    inst = load_yaml(TaskInstance, DATA / "instances" / f"{case['instance']}.yaml")
    task = Task(load_template(DATA / "templates" / f"{case['template']}.yaml"), inst, random.Random(0))
    return snap, task, str(case["subgoal"]), case


@pytest.mark.parametrize("case_dir", CASES, ids=[c.name for c in CASES])
def test_expected_option_offered_and_forbidden_absent(case_dir: Path) -> None:
    snap, task, name, case = load(case_dir)
    sg = task.subgoal(name)
    v = view(snap, task, sg, Policy())
    texts = [o.text for o in build_options(v, task, sg, Policy())]
    assert 1 <= len(texts) <= 255
    for pattern in case["expect"]:
        if any(re.search(pattern, t) for t in texts):
            break
    else:
        pytest.fail(f"none of {case['expect']} offered; got {texts[:40]}")
    for bad in case.get("forbid", []):
        assert not any(bad in t for t in texts), f"forbidden option {bad!r} offered"
    # Bounds and sources stay in the controller; Decisions only sees ids, roles, names, values, states, hints.
    state = build_state(v, task, sg, [], "start")
    assert "bounds" not in str(state)


@pytest.mark.skipif(
    os.environ.get("LUDIUS_LIVE_DECISION") != "1", reason="live Decisions replay: set LUDIUS_LIVE_DECISION=1"
)
@pytest.mark.parametrize("case_dir", CASES, ids=[c.name for c in CASES])
def test_decision_choice(case_dir: Path) -> None:
    load_env()
    snap, task, name, case = load(case_dir)
    sg = task.subgoal(name)
    v = view(snap, task, sg, Policy())
    options = build_options(v, task, sg, Policy())
    decider = DecisionsClient(os.environ.get("OPENROUTER_API_KEY", ""))
    try:
        d = decider.decide(build_state(v, task, sg, [], "start of subgoal"), {o.key: o.text for o in options})
    finally:
        decider.close()
    chosen = next(o.text for o in options if o.key == d.choice)
    assert any(re.search(p, chosen) for p in case["expect"]), f"Decisions chose {chosen!r} ({d.confidence:.2f})"
