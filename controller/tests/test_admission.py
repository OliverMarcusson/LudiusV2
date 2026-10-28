from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from ludius import admission
from ludius.cli import load_templates
from ludius.decisions import DecisionError
from ludius.loop import GroundTruth, Outcome


def _validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[Any], restores: list[Any]) -> Any:
    """Run admission on explorer_new_folder (3 variants) with scripted run and restore outcomes."""

    def fake_run(runner: Any, session: Any, task: Any) -> Outcome:
        r = runs.pop(0)
        if isinstance(r, Exception):
            raise r
        return Outcome(r, 4, "")

    def fake_restore() -> None:
        r = restores.pop(0) if restores else None
        if isinstance(r, Exception):
            raise r

    monkeypatch.setattr(admission, "run_task", fake_run)
    log = tmp_path / "adm.jsonl"
    gt = GroundTruth(log)
    out = admission.validate(load_templates()["explorer_new_folder"], 2, fake_restore,
                             SimpleNamespace(log=gt), None, report=lambda _: None)  # type: ignore[arg-type]  # fmt: skip
    gt.close()
    return out, [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_operational_error_is_a_failed_run_and_admission_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (results, stopped), events = _validate(
        tmp_path,
        monkeypatch,
        ["done", DecisionError("choice is not the most probable option"), "done", "done", "done", "done"],
        [],
    )
    assert stopped == ""
    assert results == {0: ["done", "error"], 1: ["done", "done"], 2: ["done", "done"]}
    assert not admission.reliable(results[0].count("done"), len(results[0]))  # 1/2: an error never counts as a pass
    err = [e for e in events if e["event"] == "admission_run" and e["status"] == "error"]
    assert err and err[0]["variant"] == 0 and err[0]["run"] == 2 and "DecisionError" in err[0]["reason"]


def test_failed_restore_stops_and_keeps_completed_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Unavailable(grpc.RpcError):
        pass

    (results, stopped), events = _validate(
        tmp_path, monkeypatch, ["done", "done", "failed"], [None, None, None, Unavailable()]
    )
    assert results == {0: ["done", "done"], 1: ["failed"]}  # the fourth run never started
    assert "restore failed before variant 1 run 2" in stopped
    assert events[-1]["event"] == "admission_stopped"


def test_programming_errors_still_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(KeyError):
        _validate(tmp_path, monkeypatch, [KeyError("bug")], [])
