"""Task templates, personas and task instances, plus code-side completion checks.

A template is a skeleton (variants of subgoal sequences); an instance fills its
parameters, from the planner or a hand-written YAML file.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ludius import mail
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


class PixelsCheck(Strict):
    """Completion read from pixels: every `present` text is visible and every `absent` text is gone.

    `present` is required: an empty or failed OCR read must never pass, which an absent-only check would.
    """

    present: list[str] = Field(min_length=1)
    absent: list[str] = Field(default_factory=list[str])


class Pixels(Strict):
    """A template's explicit opt-in for pixels-only mode (spec section 3).

    Regexes are searched case-insensitively in a row's name with all whitespace removed (OCR spacing is
    unreliable: "FileEdit Format View"), so write them without spaces; {param} is the value, escaped and
    compacted the same way. A pixels window is in the task's scope only while `title` and every structural
    anchor are visible in it: a file name alone can't bring the app into scope. No process, class or title
    identity is ever inferred from pixels. Launch subgoals get no shell rows in pixels mode: keys only.
    """

    title: str
    anchors: list[str] = Field(min_length=1)
    done: dict[str, PixelsCheck] = Field(default_factory=dict[str, PixelsCheck])  # subgoal -> visible completion
    # subgoal -> procedural advice added to its goal on pixels snapshots only; never a claim about what is observed
    hints: dict[str, str] = Field(default_factory=dict[str, str])


class MailCheck(Strict):
    """A message is in a mailbox on the range server (ludius.mail), checked there and never in the UI.

    Fields are filled from the task's values: mailbox is an address, subject exact without RE:, body contained.
    """

    mailbox: str
    folder: Literal["inbox", "sent"] = "inbox"
    subject: str = "{subject}"
    body: str = "{body}"


class Done(Strict):
    """All given conditions must hold. `typed` means the content landed in the target element.

    `pixels` applies only to pixels-only snapshots and comes from the template's pixels block. `mail` is read on
    the range server, so it holds the same on any snapshot.
    """

    window: WindowCheck | None = None
    foreground: WindowCheck | None = None
    element: ElementCheck | None = None
    typed: bool = False
    pixels: PixelsCheck | None = None
    mail: MailCheck | None = None


class Subgoal(Strict):
    goal: str
    text: str | None = None  # content key whose value may be typed
    keys: list[str] = Field(default_factory=list[str])  # extra shortcuts worth offering
    launch: bool = False  # may use the shell (Start, taskbar, Run) to open apps
    copy_text: bool = False  # may copy text visible on screen into the focused field (type:copy[n])
    allow_destructive: list[str] = Field(default_factory=list[str])  # element names (regex) allowed despite the hint
    done: Done | None = None
    steps: int = 12
    pixels_hint: str = ""  # from the template's pixels hints


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
    pixels: Pixels | None = None  # pixels-only mode opt-in; without it the task gets no rows in that mode

    @model_validator(mode="after")
    def _pixels_done(self) -> Template:
        """Each pixels completion becomes part of its subgoal's done check (used only on pixels snapshots)."""
        for name, check in (self.pixels.done if self.pixels else {}).items():
            if name not in self.subgoals:
                raise ValueError(f"{self.task}: pixels done for unknown subgoal {name!r}")
            sg = self.subgoals[name]
            sg.done = (sg.done or Done()).model_copy(update={"pixels": check})
        for name, hint in (self.pixels.hints if self.pixels else {}).items():
            if name not in self.subgoals:
                raise ValueError(f"{self.task}: pixels hint for unknown subgoal {name!r}")
            self.subgoals[name].pixels_hint = hint
        return self

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
        # Mistake variants (spec section 6): `{p}_typo` is the value with one stray neighbouring key at the end,
        # typed first and then fixed (Backspace, rename) by a later subgoal.
        self.values = have | {f"{k}_typo": v + _neighbour(v[-1]) for k, v in have.items() if v and "\n" not in v}
        # Pixels read one row per line and a long line is cut at the window edge: `{p}_head` is the first
        # non-blank line, capped, for visible-text checks. A blank value has none, so its check can't match.
        self.values |= {f"{k}_head": next(x.strip() for x in v.splitlines() if x.strip())[:HEAD]
                        for k, v in have.items() if v.strip()}  # fmt: skip

    def fmt(self, s: str) -> str:
        return s.format_map(self.values)

    def fmt_re(self, s: str) -> str:
        return s.format_map({k: re.escape(v) for k, v in self.values.items()})

    def pixels_re(self, s: str) -> str:
        """A pixels regex with {param} as the compacted, escaped value; other braces stay regex syntax."""
        return re.sub(r"\{(\w+)\}", lambda m: re.escape(compact(self.values[m[1]])) if m[1] in self.values else m[0], s)

    def subgoal(self, name: str) -> Subgoal:
        sg = self.template.subgoals[name]
        return sg.model_copy(update={"goal": self.fmt(sg.goal)})

    def text(self, sg: Subgoal) -> str | None:
        return self.values.get(sg.text) if sg.text else None


_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]


def _neighbour(c: str) -> str:
    """The key right of `c` on a QWERTY row (a fat-finger slip); other characters are pressed twice."""
    row = next((r for r in _ROWS if c.lower() in r), None)
    if row is None:
        return c
    n = row[(row.index(c.lower()) + 1) % len(row)]
    return n.upper() if c.isupper() else n


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


def pixels_window(w: obs.Window) -> bool:
    """A window read from pixels only (pixels-only mode): no process or class identity behind it."""
    return w.framework_id == "visual" and w.process_name == ""


def compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


def visible(pattern: str, rows: list[obs.Element], task: Task) -> bool:
    rx = task.pixels_re(pattern)
    return any(re.search(rx, compact(e.name), re.I) for e in rows)


HEAD = 24  # characters of a value's first line that a pixels check may expect on one row
TASKBAR = "ypeheretosearch"  # the taskbar search box; the clock row merges with the date, so it can't mark the bar


def pixels_region(snap: obs.Snapshot, window: int, task: Task) -> list[obs.Element]:
    """The rows of a pixels window the task may click: none unless the title and every structural anchor are
    visible, then those whose centre is right of and below the title row and above the taskbar.

    This is visible geometry, not process identity: it keeps the desktop icons and the taskbar out.
    """
    # ponytail: fixed margins from the 1920x1080 fixtures (a dialog more than 48 px left of the title loses its
    # rows, keys still work); the fallback taskbar height scales with dpi, which pixels mode reports as 96.
    px = task.template.pixels
    if px is None:
        return []
    rows = [e for e in snap.elements if e.window == window]
    if not (visible(px.title, rows, task) and all(visible(a, rows, task) for a in px.anchors)):
        return []
    rx = task.pixels_re(px.title)
    title = min((e for e in rows if re.search(rx, compact(e.name), re.I)), key=lambda e: e.bounds.y)  # topmost
    bar = [e.bounds.y for e in rows if re.search(TASKBAR, compact(e.name), re.I)]
    bottom = min(bar) - 4 if bar else snap.screen_height - 48 * (snap.dpi or 96) // 96
    return [
        e for e in rows
        if e.bounds.x + e.bounds.width / 2 >= title.bounds.x - 48
        and title.bounds.y - 12 <= e.bounds.y + e.bounds.height / 2 < bottom
    ]  # fmt: skip


def check_done(done: Done | None, snap: obs.Snapshot, task: Task, typed_ok: bool) -> bool | None:
    """True/False when the subgoal has a code check, None when it has none (Jev decides)."""
    if done is None:
        return None
    if done.mail and not (done.window or done.foreground or done.element or done.typed or done.pixels):
        return _mail_ok(done.mail, task)
    on_pixels = any(pixels_window(w) for w in snap.windows)
    if done.pixels and not on_pixels and not (done.window or done.foreground or done.element or done.typed):
        return None  # a completion only pixels mode checks: with an accessibility tree, Jev decides as before
    if done.typed and not typed_ok:
        return False
    if on_pixels and not done.pixels:
        return False  # typed/element/window checks can't be seen in pixels: without a visible check, never done
    if done.pixels and on_pixels:
        rows = [e for e in snap.elements if e.window < len(snap.windows) and pixels_window(snap.windows[e.window])]
        return all(visible(p, rows, task) for p in done.pixels.present) and not any(
            visible(p, rows, task) for p in done.pixels.absent
        )
    # Pixels carry no process identity, so a window or foreground check can never pass on them.
    if (done.window or done.foreground) and any(pixels_window(w) for w in snap.windows):
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
            # Edit and RichEdit values end lines with \r or \r\n; multi-line params use \n.
            value = re.sub(r"\r\n?", "\n", e.value)
            return (
                (not c.role or e.role == c.role)
                and (not c.name or re.search(task.fmt_re(c.name), e.name, re.I) is not None)
                and (not c.value or re.search(task.fmt_re(c.value), value, re.I) is not None)
            )

        if not any(ok(e) for e in snap.elements):
            return False
    return done.mail is None or _mail_ok(done.mail, task)


def _mail_ok(c: MailCheck, task: Task) -> bool:
    return mail.delivered(task.fmt(c.mailbox), c.folder, task.fmt(c.subject), task.fmt(c.body))
