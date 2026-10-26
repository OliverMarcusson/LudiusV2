from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from ludius.cli import load_templates
from ludius.jev import Decision
from ludius.loop import GroundTruth, Runner
from ludius.tasks import Persona, Task, TaskInstance
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs


class ToggleAgent:
    """Start button toggles the Start menu (open/closed); `grows` makes every click a new screen instead."""

    vm = "t"

    def __init__(self, grows: bool = False) -> None:
        self.open, self.grows, self.clicks = False, grows, 0

    def move(self, x: int, y: int, duration_ms: int = 0) -> int:
        return 0

    def click(self, button: int = 0, count: int = 1) -> int:
        self.clicks += 1
        self.open = not self.open
        return 0

    def chord(self, vks: list[int]) -> int:
        return 0

    def type(self, text: str, mean_ms: int, jitter_ms: int) -> int:
        return 0

    def scroll(self, notches: int) -> int:
        return 0

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        fg = ("Search", "SearchApp.exe") if self.open else ("File Explorer", "explorer.exe")
        box = obs.Rect(x=0, y=1040, width=48, height=40)
        els = [obs.Element(id=4, key="start", role="button", name="Start", bounds=box)]
        if self.grows:  # e.g. a list that gains an item per click: real progress
            els.append(obs.Element(id=9, key="n", role="text", name="count", value=str(self.clicks)))
        s = obs.Snapshot(screen_width=1920, screen_height=1080, dpi=96, settled=True, elements=els,
                         windows=[obs.Window(title=fg[0], process_name=fg[1], kind=obs.Window.FOREGROUND)])  # fmt: skip
        return pb.ObserveResult(snapshot=s)


class PrefersStart:
    """A Jev stand-in that always wants the Start button while it is offered."""

    def decide(self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True) -> Decision:
        key = next((k for k, t in options.items() if "'Start'" in t), next(iter(options)))
        return Decision(key, 0.8, {key: 0.8}, 0.0, 0.0, "fake")


def _run(tmp_path: Path, agent: ToggleAgent) -> list[dict[str, Any]]:
    log = tmp_path / "gt.jsonl"
    runner = Runner(agent, PrefersStart(), Persona(name="t", user="t"), GroundTruth(log), seed=1)  # type: ignore[arg-type]
    tpl = load_templates()["notepad_write_note"]
    task = Task(tpl, TaskInstance(template=tpl.task, params=tpl.example, variant=0), random.Random(0))
    runner._subgoal(task, "open_from_start", task.subgoal("open_from_start"), budget=12, t0=1e18)  # pyright: ignore[reportPrivateUsage]
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_open_close_toggle_is_not_progress(tmp_path: Path) -> None:
    events = _run(tmp_path, ToggleAgent())
    start = [e for e in events if e.get("event") == "step" and "'Start'" in e["option"]]
    assert len(start) <= 3, [e["result"] for e in start]  # was 12: every toggle looked like a change
    assert any(e.get("event") == "ladder" for e in events)


def test_new_screens_still_count_as_progress(tmp_path: Path) -> None:
    events = _run(tmp_path, ToggleAgent(grows=True))
    start = [e for e in events if e.get("event") == "step" and "'Start'" in e["option"]]
    assert len(start) == 12 and not any(e.get("event") == "ladder" for e in events)
