from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from ludius.calls import CapReached
from ludius.cli import load_templates
from ludius.decisions import Decision
from ludius.loop import GroundTruth, Runner, image_reason
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

    def screenshot(self) -> bytes:
        return b"PNG shot"

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
    """A Decisions stand-in that always wants the Start button while it is offered."""

    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision:
        key = next((k for k, t in options.items() if "'Start'" in t), next(iter(options)))
        return Decision(key, 0.8, {key: 0.8}, 0.0, 0.0, "fake")


class SeesImages(PrefersStart):
    def __init__(self) -> None:
        self.images: list[bytes | None] = []

    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision:
        self.images.append(image)
        d = super().decide(state, options, ask_progress=ask_progress)
        d.image, d.latency_ms = image is not None, 7
        return d


def _run(tmp_path: Path, agent: ToggleAgent, decider: PrefersStart | None = None) -> list[dict[str, Any]]:
    log = tmp_path / "gt.jsonl"
    runner = Runner(agent, decider or PrefersStart(), Persona(name="t", user="t"), GroundTruth(log), seed=1)  # type: ignore[arg-type]
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


class StuckAgent(ToggleAgent):
    """Clicks change nothing on screen."""

    def click(self, button: int = 0, count: int = 1) -> int:
        return 0


class MovesOnShot(ToggleAgent):
    """The foreground changes between the observation and the screenshot."""

    def screenshot(self) -> bytes:
        self.open = not self.open
        return b"PNG shot"


class FailsOnShot(ToggleAgent):
    def screenshot(self) -> bytes:
        raise TimeoutError("deadline exceeded")


def test_screenshot_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reasons(name: str, agent: ToggleAgent) -> list[str | None]:
        decider = SeesImages()
        events = _run(tmp_path / name, agent, decider)
        steps = [e["decision"]["image"] for e in events if e.get("event") == "step"]
        assert [i is not None for i in decider.images] == [r is not None for r in steps]  # logged = sent
        return steps

    # Every step makes progress: text only.
    assert reasons("grows", ToggleAgent(grows=True)) == [None] * 12
    # Nothing changes: every later step follows a "no visible change".
    assert reasons("stuck", StuckAgent()) == [None] + ["no_change"] * 11
    # A foreground window UIA can't read gets it too; LUDIUS_DECISION_IMAGE=1 forces it on every step.
    fg = obs.Window(title="x", process_name="x.exe", kind=obs.Window.FOREGROUND, no_tree=True)
    assert image_reason(None, obs.Snapshot(windows=[fg])) == "unreadable"
    assert image_reason("refused", obs.Snapshot(windows=[fg])) == "refused"
    assert image_reason(None, obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND)])) is None
    monkeypatch.setenv("LUDIUS_DECISION_IMAGE", "1")
    assert reasons("forced", ToggleAgent(grows=True)) == ["forced"] * 12
    # A screenshot taken after the screen moved never goes along with the older state.
    assert reasons("moved", MovesOnShot()) == [None] * 12
    assert reasons("failed", FailsOnShot()) == [None] * 12  # a failed capture: text only, no crash


class Broke(PrefersStart):
    """Decisions whose budget is spent: every request is refused before it is sent."""

    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision:
        raise CapReached("aberg: decision cap reached")


class ThinksDone(PrefersStart):
    def __init__(self, done: float) -> None:
        self.done = done

    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision:
        d = super().decide(state, options, ask_progress=ask_progress)
        d.subgoal_done = self.done
        return d


class BrokeAssist:
    narrative = Path("unused")

    def verify(self, state: dict[str, Any]) -> Any:
        raise CapReached("llm cap reached")

    def next_subgoal(self, state: dict[str, Any]) -> Any:
        raise CapReached("llm cap reached")

    def prepare(self, *_: object) -> Any:
        raise CapReached("llm cap reached")


def open_ended(template: str = "notepad_write_note") -> Task:
    """The template's first subgoal without a code check: only Decisions (and the LLM) can call it done."""
    tpl = load_templates()[template].model_copy(deep=True)
    task = Task(tpl, TaskInstance(template=tpl.task, params=tpl.example, variant=0), random.Random(0))
    tpl.subgoals[task.subgoal_names[0]].done = None
    return task


@pytest.mark.parametrize("site", ["decision", "reply content", "assist subgoal", "assist verify"])
def test_a_spent_budget_abandons_the_task_the_same_way_at_every_site(tmp_path: Path, site: str) -> None:
    log = tmp_path / "gt.jsonl"
    broke: Any = BrokeAssist()
    decider: Any = {"decision": Broke(), "assist subgoal": ThinksDone(0.0), "assist verify": ThinksDone(0.95)}.get(
        site, PrefersStart()
    )
    runner = Runner(ToggleAgent(), decider, Persona(name="t", user="t"), GroundTruth(log), seed=1,  # type: ignore[arg-type]
                    assist=broke if site.startswith("assist") else None,
                    composer=broke if site == "reply content" else None)  # fmt: skip
    if site == "reply content":
        tpl = load_templates()["outlook_reply_mail"]
        task = Task(
            tpl, TaskInstance(template=tpl.task, params={"to": "x@ludius.internal", "to_name": "X"}), random.Random(0)
        )
    else:
        task = open_ended()
    out = runner.run(task)
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert (out.status, out.reason) == ("abandoned", "budget spent")
    spent = next(i for i, e in enumerate(events) if e["event"] == "budget_spent")
    assert any(e["event"] == "ladder" and e["reason"] == "budget spent" for e in events[spent:])  # closed like a person
    assert [e["event"] for e in events].count("task_end") == 1
    assert events[-1]["status"] == "abandoned" and events[-1]["reason"] == "budget spent"
    assert events[-1]["steps"] == out.steps


class Answers(PrefersStart):
    """Says yes to a done question with probability `yes`; steps like PrefersStart otherwise."""

    def __init__(self, yes: float) -> None:
        self.yes, self.asked = yes, 0

    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision:
        if set(options) == {"yes", "no"}:
            self.asked += 1
            assert image == b"PNG shot" and "Notepad" in state["question"]
            probs = {"yes": self.yes, "no": 1 - self.yes}
            return Decision("yes" if self.yes >= 0.5 else "no", 0.9, probs, 0, 0, "fake")
        return super().decide(state, options, ask_progress=ask_progress)


@pytest.mark.parametrize(("yes", "done"), [(0.95, True), (0.6, False)])
def test_a_done_question_the_decider_answers_on_the_screenshot(tmp_path: Path, yes: float, done: bool) -> None:
    log = tmp_path / "gt.jsonl"
    decider = Answers(yes)
    runner = Runner(ToggleAgent(), decider, Persona(name="t", user="t"), GroundTruth(log), seed=1)  # type: ignore[arg-type]
    tpl = load_templates()["notepad_write_note"].model_copy(deep=True)
    task = Task(tpl, TaskInstance(template=tpl.task, params=tpl.example, variant=0), random.Random(0))
    sg = tpl.subgoals["open_from_start"]
    assert sg.done is not None
    sg.done.ask = "Is Notepad open?"  # the code check (a Notepad window) never holds on ToggleAgent
    status, _, _ = runner._subgoal(task, "open_from_start", task.subgoal("open_from_start"), budget=3, t0=1e18)  # pyright: ignore[reportPrivateUsage]
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert (status == "done") is done and decider.asked >= 1
    assert any(e.get("event") == "subgoal_done" and e.get("by") == "decider" for e in events) is done
