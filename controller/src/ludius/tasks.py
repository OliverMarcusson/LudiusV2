"""Task templates, personas and task instances, plus code-side completion checks.

A template is a skeleton (variants of subgoal sequences); an instance fills its
parameters, from the planner or a hand-written YAML file.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ludius.v1 import observation_pb2 as obs


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowCheck(Strict):
    process: str | None = None  # exact, case-insensitive
    title: str | None = None  # regex, after parameter substitution
    class_name: str | None = None  # exact window class, e.g. CabinetWClass for Explorer folders
    absent: bool = False  # true: no such window may exist


class ElementCheck(Strict):
    role: str | None = None
    name: str | None = None  # regex
    value: str | None = None  # regex


class Done(Strict):
    """All given conditions must hold. `typed` means the content landed in the target element."""

    window: WindowCheck | None = None
    foreground: WindowCheck | None = None
    element: ElementCheck | None = None
    typed: bool = False


class Subgoal(Strict):
    goal: str
    text: str | None = None  # content key whose value may be typed
    keys: list[str] = Field(default_factory=list[str])  # extra shortcuts worth offering
    launch: bool = False  # may use the shell (Start, taskbar, Run) to open apps
    copy_text: bool = False  # may copy text visible on screen into the focused field (type:copy[n])
    allow_destructive: list[str] = Field(default_factory=list[str])  # element names (regex) allowed despite the hint
    done: Done | None = None
    steps: int = 12


class Template(Strict):
    task: str
    app: str
    processes: list[str]  # foreground processes the task may act in (shell is added by `launch`)
    variants: list[list[str]]
    weights: list[float] | None = None
    subgoals: dict[str, Subgoal]
    budget_steps: int = 40
    budget_minutes: float = 8
    on_abandon: str = "close_without_saving"
    params: list[str] = Field(default_factory=list[str])  # the planner fills these
    defaults: dict[str, str] = Field(default_factory=dict[str, str])  # fixed values, e.g. the Start search query
    about: str = ""  # one line for the planner's capability catalog
    example: dict[str, str] = Field(default_factory=dict[str, str])  # parameters for admission test runs
    resembles: str = ""  # signal type this legitimate work looks like (powershell, rdp, macro); spec section 6

    def check(self) -> None:
        for v in self.variants:
            for name in v:
                if name not in self.subgoals:
                    raise ValueError(f"{self.task}: variant uses unknown subgoal {name!r}")


class Persona(Strict):
    name: str
    user: str
    role: str = ""
    department: str = ""
    hours: tuple[str, str] = ("08:30", "17:00")  # usual working day, local time
    style: str = ""  # writing style, file naming and work habits, for the planner
    typing_mean_ms: int = 140
    typing_jitter_ms: int = 60
    think_mean_s: float = 1.6  # pause before each action in realtime mode
    pointer_ms: tuple[int, int] = (180, 520)
    risk_profile: list[str] = Field(default_factory=list[str])  # signal-like templates that fit the role


class TaskInstance(Strict):
    template: str
    params: dict[str, str] = Field(default_factory=dict[str, str])
    content: dict[str, str] = Field(default_factory=dict[str, str])
    variant: int | None = None
    context: str = ""  # one line of narrative for Jev: why the persona is doing this


class Task:
    """A template bound to an instance: formatted subgoals for the chosen variant."""

    def __init__(self, template: Template, inst: TaskInstance, rng: random.Random) -> None:
        have = {**template.defaults, **inst.params, **inst.content}
        missing = [p for p in template.params if not have.get(p)]
        if missing:
            raise ValueError(f"{template.task}: missing parameters {missing}")
        self.template = template
        self.inst = inst
        idx = (
            inst.variant
            if inst.variant is not None
            else rng.choices(range(len(template.variants)), template.weights)[0]
        )
        self.variant = idx
        self.subgoal_names = template.variants[idx]
        self.values = have

    def fmt(self, s: str) -> str:
        return s.format_map(self.values)

    def fmt_re(self, s: str) -> str:
        return s.format_map({k: re.escape(v) for k, v in self.values.items()})

    def subgoal(self, name: str) -> Subgoal:
        sg = self.template.subgoals[name]
        return sg.model_copy(update={"goal": self.fmt(sg.goal)})

    def text(self, sg: Subgoal) -> str | None:
        return self.values.get(sg.text) if sg.text else None


def load_template(path: Path) -> Template:
    t = Template.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    t.check()
    return t


def load_yaml[M: BaseModel](cls: type[M], path: Path) -> M:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return cls.model_validate(data)


# Completion checks read UIA state only ------------------------------------------------


def _window_matches(w: obs.Window, c: WindowCheck, task: Task) -> bool:
    if c.process and w.process_name.lower() != c.process.lower():
        return False
    if c.class_name and w.class_name != c.class_name:
        return False
    return not (c.title and not re.search(task.fmt_re(c.title), w.title, re.I))


def check_done(done: Done | None, snap: obs.Snapshot, task: Task, typed_ok: bool) -> bool | None:
    """True/False when the subgoal has a code check, None when it has none (Jev decides)."""
    if done is None:
        return None
    if done.typed and not typed_ok:
        return False
    if done.window:
        # Foreground set plus every open app window, so a backgrounded window still counts as open.
        hit = any(_window_matches(w, done.window, task) for w in [*snap.windows, *snap.apps])
        if hit == done.window.absent:
            return False
    if done.foreground:
        fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
        hit = fg is not None and _window_matches(fg, done.foreground, task)
        if hit == done.foreground.absent:
            return False
    if done.element:
        c = done.element

        def ok(e: obs.Element) -> bool:
            return (
                (not c.role or e.role == c.role)
                and (not c.name or re.search(task.fmt_re(c.name), e.name, re.I) is not None)
                and (not c.value or re.search(task.fmt_re(c.value), e.value, re.I) is not None)
            )

        if not any(ok(e) for e in snap.elements):
            return False
    return True
