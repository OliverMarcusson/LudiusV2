from __future__ import annotations

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
