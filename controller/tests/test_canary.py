from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ludius import canary
from ludius.cli import DATA, load_templates
from ludius.jev import JevError
from ludius.loop import GroundTruth, Outcome


def test_canary_states_retry_and_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Per app, scripted runs: notepad passes on the retry, paint fails twice, explorer only loses runs to Jev.
    script: dict[str, list[Any]] = {
        "notepad": ["failed", "done"],
        "paint": ["abandoned", "failed"],
        "explorer": [JevError("down"), JevError("down")],
    }
    seen: list[tuple[str, int | None]] = []

    def fake_run(runner: Any, session: Any, task: Any) -> Outcome:
        seen.append((task.template.task, task.variant))
        r = script[task.template.app].pop(0)
        if isinstance(r, Exception):
            raise r
        return Outcome(r, 3, "")

    monkeypatch.setattr(canary, "run_task", fake_run)
    health = tmp_path / "health.json"
    gt = GroundTruth(tmp_path / "canary.jsonl")
    states = canary.run(load_templates(), DATA / "admission.yaml", SimpleNamespace(log=gt), None,  # type: ignore[arg-type]
                        health, apps={"notepad", "paint", "explorer"}, report=lambda _: None)  # fmt: skip
    gt.close()
    assert states == {"explorer": "unknown", "notepad": "ok", "paint": "drift"}
    assert ("explorer_new_folder", 0) in seen  # first admitted template by name, first admitted variant
    assert all(not v for v in script.values())  # every app ran twice: one retry, no more
    assert canary.drifted(health) == {"paint"}
    assert json.loads(health.read_text(encoding="utf-8"))["notepad"]["runs"] == ["failed", "done"]
