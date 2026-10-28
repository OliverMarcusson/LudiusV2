"""Decisions state and option set, built in code from one settled snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ludius.decisions import MAX_OPTIONS
from ludius.policy import SHELL_PROCESSES, Policy, alt_f4_safe
from ludius.tasks import (
    LAUNCH_TEXT,
    Subgoal,
    Task,
    declared_commits,
    element_matches,
    pixels_region,
    pixels_window,
)
from ludius.v1 import observation_pb2 as obs

CLICKABLE = {
    "button", "split button", "menu item", "list item", "tab item", "hyperlink", "check box", "radio button",
    "combo box", "tree item", "data item", "edit", "document", "header item", "spinner", "slider",
}  # fmt: skip
EDITABLE = {"edit", "document", "combo box"}
OPENABLE = {"list item", "data item"}  # double-click opens files and shell items
BASE_KEYS = ["Enter", "Esc", "Tab", "Shift+Tab", "Up", "Down", "Left", "Right", "Alt+F4"]
MAX_ELEMENT_LINES = 120
COPY_SOURCES = {"text", "hyperlink", "list item", "data item", "heading", "tree item"}
MAX_COPY = 12
LAUNCH_PROCESSES = {"searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe"}  # Start search, not Explorer
_ASCII = re.compile(r"^[\x20-\x7e]{3,200}$")  # the virtual keyboard types ASCII only


@dataclass
class Option:
    key: str  # Decisions choice key: o1, o2, ...
    kind: str  # click | double_click | type | key | scroll | rescan
    text: str  # what Decisions reads
    element: obs.Element | None = None
    keys: str = ""
    content: str = ""  # content key for type
    literal: str = ""  # type:copy text read from an on-screen element, or the subgoal's template `search` text
    notches: int = 0


@dataclass
class StepView:
    """What the controller knows about the screen for one step."""

    snapshot: obs.Snapshot
    allowed: list[obs.Element] = field(default_factory=list[obs.Element])
    windows: dict[int, obs.Window] = field(default_factory=dict[int, obs.Window])


def describe(e: obs.Element) -> str:
    s = f"[{e.id}] {e.role} '{e.name}'"
    if e.value:
        s += f" value='{e.value[:80]}'"
    flags = [f for f, on in (("focused", e.focused), ("selected", e.selected), ("expanded", e.expanded),
                             ("read-only", e.read_only), ("disabled", not e.enabled)) if on]  # fmt: skip
    if flags:
        s += " " + ",".join(flags)
    if e.framework_id == "visual":  # pixels have only a place to be known by: unnamed icons look alike without it
        s += f" at {e.bounds.x},{e.bounds.y} {e.bounds.width}x{e.bounds.height}"
    if e.hints:
        s += " hint=" + "; ".join(e.hints)
    return s


def window_line(w: obs.Window) -> str:
    return f"{w.title or w.class_name} ({w.process_name})"


def view(snap: obs.Snapshot, task: Task, sg: Subgoal, policy: Policy) -> StepView:
    """Elements the task may touch: its apps' windows, the shell when launching, never refused elements.

    A window read from pixels has no process to match: it is in scope only while the template's pixels title and
    structural anchors are visible in it (tasks.Pixels), never as the shell, and then only its rows inside the
    title-to-taskbar region (tasks.pixels_region).
    """
    v = StepView(snapshot=snap)
    region: set[int] = set()
    apps = {p.lower() for p in task.template.processes}
    for i, w in enumerate(snap.windows):
        if pixels_window(w):
            if rows := pixels_region(snap, i, task):
                v.windows[i] = w
                region.update(e.id for e in rows)
            continue
        proc = w.process_name.lower()
        dialog_of_app = w.kind in (obs.Window.OWNED, obs.Window.POPUP)  # common dialogs and menus of the app
        shell = w.kind == obs.Window.SHELL or proc in SHELL_PROCESSES
        if proc in apps or (dialog_of_app and _owner_is_app(snap, apps)) or (sg.launch and shell):
            v.windows[i] = w
    for e in snap.elements:
        in_region = e.window in v.windows and (e.id in region or not pixels_window(v.windows[e.window]))
        if in_region and policy.element_allowed(e, sg.allow_destructive, snap) is None:
            v.allowed.append(e)
    return v


def _owner_is_app(snap: obs.Snapshot, apps: set[str]) -> bool:
    fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
    return fg is not None and fg.process_name.lower() in apps


def build_state(v: StepView, task: Task, sg: Subgoal, history: list[str], last_result: str) -> dict[str, Any]:
    snap = v.snapshot
    fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
    focused = next((e for e in snap.elements if e.focused), None)
    on_pixels = fg is not None and pixels_window(fg)
    content = task.text(sg)
    state: dict[str, Any] = {
        "goal": f"{sg.goal} {task.fmt(sg.pixels_hint)}" if on_pixels and sg.pixels_hint else sg.goal,
        "task_context": task.inst.context or task.fmt(task.template.task.replace("_", " ")),
        "window": window_line(fg) if fg else "desktop",
        "other_windows": [window_line(w) for w in snap.windows if w.kind in (obs.Window.OWNED, obs.Window.POPUP)],
        # Pixels carry no focus: "none" would assert what nobody observed.
        "focused": "unknown (read from pixels)" if on_pixels else f"[{focused.id}]" if focused else "none",
        "history": history[-8:],
        "last_result": last_result,
        "elements": [describe(e) for e in v.allowed[:MAX_ELEMENT_LINES]],
        "context": list(snap.context[:30]),
    }
    if content is not None:
        state["text_to_enter"] = content if len(content) <= 160 else content[:160] + "…"
    if snap.busy_cursor or snap.indeterminate_progress:
        state["busy"] = True
    return state


def search_target(e: obs.Element | None, snap: obs.Snapshot, task: Task) -> bool:
    """Where a subgoal's template `search` text may go: an enabled, writable edit field in the foreground
    window of one of the task's own apps, never the keyboard focus unseen (it could be a password field, a reserved
    field or another app). The element's policy (password, reserved) is checked by the caller as for planner text."""
    if (
        e is None
        or e.role != "edit"
        or e.read_only
        or not e.enabled
        or e.password
        or not 0 <= e.window < len(snap.windows)
    ):
        return False
    w = snap.windows[e.window]
    apps = {p.lower() for p in task.template.processes}
    return w.kind == obs.Window.FOREGROUND and w.process_name.lower() in apps and not pixels_window(w)


def launch_target(e: obs.Element | None, snap: obs.Snapshot) -> bool:
    """Where the launch query (LAUNCH_TEXT) may go: the shell's Start search box, an enabled writable edit of the
    Start/search process, or the Run dialog's Open box; never an app's field (a draft's Subject in front)."""
    if e is None or e.role != "edit" or e.read_only or not e.enabled or e.password:
        return False
    if not 0 <= e.window < len(snap.windows) or pixels_window(w := snap.windows[e.window]):
        return False
    # or the Run dialog's "Open:" box (Win+R; Notepad, WordPad, Excel and PowerShell launch from it).
    run = w.process_name.lower() == "explorer.exe" and w.class_name == "#32770" and w.title == "Run"
    return w.process_name.lower() in LAUNCH_PROCESSES or (run and e.name == "Open:")


def build_options(v: StepView, task: Task, sg: Subgoal, policy: Policy) -> list[Option]:
    opts: list[Option] = []

    def add(kind: str, text: str, **kw: Any) -> None:
        opts.append(Option(key=f"o{len(opts) + 1}", kind=kind, text=text, **kw))

    content = task.text(sg)
    literal = "" if sg.text else sg.search
    for e in v.allowed:
        if any(element_matches(e, c, task) for c in sg.avoid):
            continue
        # Nothing to type: Decisions never writes text, so an edit field (a search box) leads nowhere (Excel run2: 12
        # clicks on Microsoft search over F12). No template subgoal without text needs one focused by a click.
        if content is None and not sg.copy_text and e.role == "edit":
            continue
        # Words read from pixels (visual fallback) carry no role: clicking them is the only way in.
        if (e.role in CLICKABLE or (e.role == "text" and e.framework_id == "visual")) and e.bounds.width > 0:
            add("click", f"click {describe(e)}", element=e)
            if e.role in OPENABLE:
                add("double_click", f"double-click to open [{e.id}] {e.role} '{e.name}'", element=e)
        if (
            content is not None
            and e.role in EDITABLE
            and not e.read_only
            and e.enabled
            and policy.text_allowed(content, e) is None
            and (sg.text or search_target(e, v.snapshot, task))
            and (sg.text != LAUNCH_TEXT or launch_target(e, v.snapshot))
        ):
            text = f"type the text to enter into [{e.id}] {e.role} '{e.name}'"
            add("type", text, element=e, content=sg.text or "", literal=literal)
    elements = len(opts)
    focused = next((e for e in v.snapshot.elements if e.focused), None)
    if (sg.text and content is not None and policy.text_allowed(content, None) is None
            and (sg.text != LAUNCH_TEXT or launch_target(focused, v.snapshot))):  # fmt: skip
        # Where focus already is (Start search, a field without a usable tree).
        add("type", "type the text to enter at the keyboard focus", content=sg.text or "", literal=literal)
    focus = next((e for e in v.allowed if e.focused and e.role in EDITABLE and not e.read_only), None)
    if sg.copy_text and focus is not None:
        # Visible text: element names, and the observer's non-actionable text lines ("role: text").
        sources = [(f"[{e.id}] ", e.name) for e in v.allowed if e.role in COPY_SOURCES and e.key != focus.key]
        sources += [("", line.split(": ", 1)[-1]) for line in v.snapshot.context]
        seen: set[str] = set()
        for ref, text in sources:
            if len(seen) >= MAX_COPY or text in seen or not _ASCII.match(text) or policy.text_allowed(text, focus):
                continue
            seen.add(text)
            add("type", f"type:copy {ref}'{text[:60]}' into [{focus.id}]", element=focus, literal=text)
    fg = next((w for w in v.snapshot.windows if w.kind == obs.Window.FOREGROUND), None)
    for k in [*BASE_KEYS, *sg.keys]:
        if (k == "Win" and not sg.launch) or (k == "Alt+F4" and not alt_f4_safe(fg)):
            continue
        if policy.key_allowed(k, v.snapshot, declared_commits(task, sg)):
            continue
        add("key", f"press {k}", keys=k)
    if any("list continues" in h for e in v.allowed for h in e.hints) or any(e.role == "document" for e in v.allowed):
        add("scroll", "scroll down", notches=-3)
        add("scroll", "scroll up", notches=3)
    add("rescan", "rescan: wait for the screen to finish loading and look again")
    if len(opts) > MAX_OPTIONS:
        # Over the decision model's limit, element options give way: cutting the tail lost the keys, F12 among them,
        # behind Excel's ~250 ribbon controls (runs 2 and 4 never saw the subgoal's own key).
        opts = opts[: elements - (len(opts) - MAX_OPTIONS)] + opts[elements:]
        for i, o in enumerate(opts, start=1):
            o.key = f"o{i}"
    return opts
