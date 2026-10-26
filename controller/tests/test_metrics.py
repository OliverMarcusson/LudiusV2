from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ludius import metrics


def _log(path: Path, gap_ms: int, launch: str, n: int) -> Path:
    t = 0
    lines: list[dict[str, Any]] = [{"event": "step", "host_ms": 1, "persona": "p"}]  # orphan
    for k in range(n):
        lines.append({"event": "task_start", "host_ms": t + 1, "persona": "p", "task": "tpl", "subgoals": [launch]})
        for _ in range(3):
            t += gap_ms + (k * 7 if launch == "human" else 0)
            lines.append({"event": "step", "host_ms": t, "action": "click", "process": "x.exe"})
        lines.append({"event": "task_end", "host_ms": t + 1, "task": "tpl", "status": "done" if k else "abandoned"})
    path.write_text("\n".join(json.dumps(r) for r in lines), encoding="utf-8")
    return path


def test_range_metrics_and_filterability(tmp_path: Path) -> None:
    noise = _log(tmp_path / "n.jsonl", 1000, "open_from_start", 4)
    human = _log(tmp_path / "h.jsonl", 1000, "human", 4)
    r = metrics.report([noise], [human])
    assert r["coherence"]["completion_by_template"] == {"tpl": 0.75}
    assert r["coherence"]["orphan_steps"] == 1
    assert r["guardrails"]["failure_rate_by_app"] == {"x.exe": 0.25}
    assert r["repetition"]["sequence_repeat_by_template"] == {"tpl": 0.75}
    f = r["filterability"]
    assert f["best"][0] == 1.0 and not f["pass"]  # launch method alone separates them
    assert f["rules"]["steps"][0] == 0.5  # identical step counts carry no signal


def _logs(tmp_path: Path, prefix: str, launch: str, n: int) -> list[Path]:
    """n independent source logs (runs), each a separate group for the held-out split."""
    return [_log(tmp_path / f"{prefix}{k}.jsonl", 1000 + 50 * k, launch, 4) for k in range(n)]


def test_held_out_splits_whole_source_logs_and_fits_on_train_only(tmp_path: Path) -> None:
    noise, human = _logs(tmp_path, "n", "open_from_start", 4), _logs(tmp_path, "h", "human", 3)
    r = metrics.report(noise, human, tmp_path / "features.csv")
    h = r["filterability"]["held_out"]
    assert h["available"] and h["grouping"] == "source log" and h["salt"] == metrics.HOLDOUT_SALT
    assert "near-duplicate" in h["limits"] and "genuine humans" in h["limits"]
    train, test = set(h["train"]["sources"]), set(h["test"]["sources"])
    assert not train & test and train | test == {p.name for p in noise + human}  # no run on both sides
    assert h["test"]["noise"] and h["test"]["human"] and h["train"]["noise"] and h["train"]["human"]
    assert h["held_out_score"] == 1.0 and not h["pass"]  # the launch method still separates them
    assert metrics.report(noise, human)["filterability"]["held_out"] == h  # reproducible
    d = r["dataset"]
    assert d["human_baseline"] is False and d["labels"].startswith("stand-in")
    assert [x["source"] for x in d["noise"]] == ["n0.jsonl", "n1.jsonl", "n2.jsonl", "n3.jsonl"]
    assert all(len(x["sha256"]) == 64 for x in d["noise"] + d["comparison"])
    with (tmp_path / "features.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == d["features_csv"]["rows"] == 28
    assert {r["split"] for r in rows} == {"train", "test"} and rows[0]["line"] == "2"
    assert {(r["source"], r["split"]) for r in rows} == {(s, "train") for s in train} | {(s, "test") for s in test}


def test_provenance_never_becomes_a_feature(tmp_path: Path) -> None:
    # Same content under different names and labels: only provenance differs, so nothing beats chance.
    noise = [_log(tmp_path / f"n{k}.jsonl", 1000, "open_from_start", 4) for k in range(3)]
    human = [_log(tmp_path / f"h{k}.jsonl", 1000, "open_from_start", 4) for k in range(3)]
    i = metrics.load(noise).instances[0]
    assert not set(metrics.features(i)) & {"source", "line", "persona", "label", "split"}
    h = metrics.report(noise, human)["filterability"]["held_out"]
    assert h == h | {"available": False, "reason": "no training rule beats chance"}


def test_held_out_is_unavailable_rather_than_optimistic(tmp_path: Path) -> None:
    one = metrics.report(_logs(tmp_path, "n", "open_from_start", 3), _logs(tmp_path, "h", "human", 1))
    h = one["filterability"]["held_out"]
    assert not h["available"] and "1 independent human source" in h["reason"]
    assert one["filterability"]["best"][0] == 1.0  # the in-sample number alone would have looked decisive
    shared = metrics.load(_logs(tmp_path, "s", "human", 2)).instances
    assert "both labels" in metrics.held_out(shared, shared)["reason"]
