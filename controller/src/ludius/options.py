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
    guard_matches,
    pixels_region,
    pixels_window,
)
from ludius.v1 import observation_pb2 as obs

CLICKABLE = {
    "button", "split button", "menu item", "list item", "tab item", "hyperlink", "check box", "radio button",
    "combo box", "tree item", "data item", "edit", "document", "header item", "spinner", "slider",
}  # fmt: skip
EDITABLE = {"edit", "document", "combo box"}
OPENABLE = {"list item", "data item"}
BASE_KEYS = ["Enter", "Esc", "Tab", "Shift+Tab", "Up", "Down", "Left", "Right", "Alt+F4"]
MAX_ELEMENT_LINES = 120
COPY_SOURCES = {"text", "hyperlink", "list item", "data item", "heading", "tree item"}
MAX_COPY = 12
LAUNCH_PROCESSES = {"searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe"}
_ASCII = re.compile(r"^[\x20-\x7e]{3,200}$")


@dataclass
class Option:
    key: str
    kind: str
    text: str
    element: obs.Element | None = None
    keys: str = ""
    content: str = ""
    literal: str = ""
    notches: int = 0


@dataclass
class StepView:
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
    if e.framework_id == "visual":
        s += f" at {e.bounds.x},{e.bounds.y} {e.bounds.width}x{e.bounds.height}"
    if e.hints:
        s += " hint=" + "; ".join(e.hints)
    return s


def window_line(w: obs.Window) -> str:
    return f"{w.title or w.class_name} ({w.process_name})"


def view(snap: obs.Snapshot, task: Task, sg: Subgoal, policy: Policy) -> StepView:
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
        dialog_of_app = w.kind in (obs.Window.OWNED, obs.Window.POPUP)
        shell = w.kind == obs.Window.SHELL or proc in SHELL_PROCESSES
        if proc in apps or (dialog_of_app and _owner_is_app(snap, apps)) or (sg.launch and shell):
            v.windows[i] = w
    fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
    hidden = set[str]()
    if not sg.launch and (fg is None or fg.process_name.lower() in SHELL_PROCESSES):
        hidden = {_plain(a.title.split(" - ")[-1]) for a in snap.apps if a.process_name.lower() in apps} - {""}
    scope = dict(v.windows)
    for e in snap.elements:
        in_region = e.window in scope and (e.id in region or not pixels_window(v.windows[e.window]))
        w = snap.windows[e.window] if 0 <= e.window < len(snap.windows) else None
        button = (hidden and w is not None and e.role == "button"
                  and w.class_name in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
                  and any(h in _plain(e.name) for h in hidden))  # fmt: skip
        if (in_region or button) and policy.element_allowed(e, sg.allow_destructive, snap) is None:
            if button and w is not None:
                v.windows[e.window] = w
            v.allowed.append(e)
    return v


def _plain(s: str) -> str:
    return s.replace("\u200b", "").strip().lower()


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
    if e is None or e.role != "edit" or e.read_only or not e.enabled or e.password:
        return False
    if not 0 <= e.window < len(snap.windows) or pixels_window(w := snap.windows[e.window]):
        return False
    run = w.process_name.lower() == "explorer.exe" and w.class_name == "#32770" and w.title == "Run"
    return w.process_name.lower() in LAUNCH_PROCESSES or (run and e.name == "Open:")


_OFF_RANGE_RESULT = re.compile(r"\bstore\b|\bsearch the web\b|\bweb results?\b", re.I)


def off_range_result(e: obs.Element, snap: obs.Snapshot) -> bool:
    """A Start search result that leads to the Store or the web: off the range, never the app asked for."""
    w = snap.windows[e.window] if 0 <= e.window < len(snap.windows) else None
    return w is not None and w.process_name.lower() in LAUNCH_PROCESSES and bool(_OFF_RANGE_RESULT.search(e.name))


def field_of_another_step(e: obs.Element | None, task: Task, sg: Subgoal) -> bool:
    if e is None or (sg.field and re.search(task.fmt_re(sg.field), e.name)):
        return False
    return any(s.field and re.search(task.fmt_re(s.field), e.name) for s in task.template.subgoals.values())


REASON_OTHER_FIELD = "that field belongs to another step of this task (e.g. the address bar): click into the page first"


def commits_unguarded(k: str, focused: obs.Element | None, task: Task, sg: Subgoal) -> bool:
    commits = any(
        c.keys.lower() == k.lower() and (focused is None or element_matches(focused, c.focus, task))
        for c in task.template.key_commits
    )
    return commits and (
        not sg.require or focused is None or not any(guard_matches(focused, c, task) for c in sg.require)
    )


def build_options(v: StepView, task: Task, sg: Subgoal, policy: Policy) -> list[Option]:
    opts: list[Option] = []

    def add(kind: str, text: str, **kw: Any) -> None:
        opts.append(Option(key=f"o{len(opts) + 1}", kind=kind, text=text, **kw))

    content = task.text(sg)
    literal = "" if sg.text else sg.search
    for e in v.allowed:
        if any(element_matches(e, c, task) for c in sg.avoid) or off_range_result(e, v.snapshot):
            continue
        if content is None and not sg.copy_text and e.role == "edit":
            continue
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
            and not field_of_another_step(e, task, sg)
        ):
            text = f"type the text to enter into [{e.id}] {e.role} '{e.name}'"
            add("type", text, element=e, content=sg.text or "", literal=literal)
    elements = len(opts)
    focused = next((e for e in v.snapshot.elements if e.focused), None)
    if (sg.text and content is not None and policy.text_allowed(content, None) is None
            and not field_of_another_step(focused, task, sg)
            and (sg.text != LAUNCH_TEXT or launch_target(focused, v.snapshot))):  # fmt: skip
        add("type", "type the text to enter at the keyboard focus", content=sg.text or "", literal=literal)
    focus = next((e for e in v.allowed if e.focused and e.role in EDITABLE and not e.read_only), None)
    if sg.copy_text and focus is not None:
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
        if commits_unguarded(k, focused, task, sg):
            continue
        add("key", f"press {k}", keys=k)
    if any("list continues" in h for e in v.allowed for h in e.hints) or any(e.role == "document" for e in v.allowed):
        add("scroll", "scroll down", notches=-3)
        add("scroll", "scroll up", notches=3)
    add("rescan", "rescan: wait for the screen to finish loading and look again")
    if len(opts) > MAX_OPTIONS:
        opts = opts[: elements - (len(opts) - MAX_OPTIONS)] + opts[elements:]
        for i, o in enumerate(opts, start=1):
            o.key = f"o{i}"
    return opts
