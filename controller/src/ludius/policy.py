"""Code-enforced guardrails. Jev never sees an option this module would refuse."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ludius.tasks import Strict, pixels_window
from ludius.v1 import observation_pb2 as obs

# Actions noise must never take, whatever the task says (spec section 5).
NEVER = re.compile(
    r"clear (browsing )?(history|cache|data)|empty recycle bin|disk clean-?up|uninstall|clear (the )?log|"
    r"clear (all )?events|log size|format (disk|drive)|delete browsing",
    re.I,
)
# Apps that are never noise targets even when visible.
SHELL_PROCESSES = {
    "explorer.exe",
    "searchapp.exe",
    "searchui.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
}
# Alt+F4 on the desktop or taskbar opens Shut Down Windows; one Enter or OK there powers the VM off.
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd"}
# Shortcuts that open, rename or delete whatever is selected or focused; navigation and Esc are never in here.
ACTIVATION_KEYS = {"enter", "space", "ctrl+o", "alt+enter", "ctrl+enter", "shift+enter", "f2", "delete", "shift+delete"}
EDITABLE_ROLES = {"edit", "document", "combo box"}


def alt_f4_safe(fg: obs.Window | None) -> bool:
    # Read from pixels, the window could be the desktop itself: no Alt+F4 without a known class and process.
    if fg is None or pixels_window(fg) or fg.process_name == "":
        return False
    return fg.class_name not in DESKTOP_CLASSES


def shell_dialog(fg: obs.Window | None) -> bool:
    """A dialog of the shell itself (Shut Down Windows, Run): leave it with Esc, never click its buttons."""
    return fg is not None and fg.class_name == "#32770" and fg.process_name.lower() in SHELL_PROCESSES


class ReservedPath(Strict):
    path: str  # Windows path glob, case-insensitive: * and ? stay within one path component
    red_herring: bool = False  # the scenario wants noise to overlap here


class Scenario(Strict):
    """Scenario file (LUDIUS_SCENARIO): what noise must not touch (spec section 5, policy)."""

    reserved: list[ReservedPath] = []
    mailboxes: list[str] = []  # addresses or globs noise must not write to


def _glob(g: str) -> str:
    out = ""
    for c in g:
        out += r"[^\\/]*" if c == "*" else r"[^\\/]" if c == "?" else re.escape(c)
    return out


@dataclass
class Policy:
    reserved_paths: list[str] = field(default_factory=list[str])  # regexes searched in typed text and values
    reserved_names: list[str] = field(default_factory=list[str])  # regexes an element's whole name must not match

    @classmethod
    def load(cls, path: Path) -> Policy:
        s = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        paths: list[str] = []
        names: list[str] = []
        for r in s.reserved:
            if not r.path.strip():
                raise ValueError(f"{path}: empty reserved path")
            if r.red_herring:
                continue
            g = r.path.strip().rstrip("\\/")
            paths.append(_glob(g))
            names.append(_glob(re.split(r"[\\/]", g)[-1]))
        for m in s.mailboxes:
            paths.append(_glob(m.strip()))
            names.append(_glob(m.strip()))
        return cls(reserved_paths=paths, reserved_names=names)

    def reserved(self, text: str) -> bool:
        return any(re.search(p, text, re.I) for p in self.reserved_paths)

    def reserved_element(self, e: obs.Element) -> bool:
        return any(re.fullmatch(p, e.name.strip(), re.I) for p in self.reserved_names) or self.reserved(e.value)

    def key_allowed(self, keys: str, snap: obs.Snapshot) -> str | None:
        """Keyboard activation acts on the selection or focus, which click filtering never sees.

        Refused while a visible reserved element is selected or focused in the window that receives keys (a modal
        dialog, else the foreground window and any open menu), unless a non-reserved text field has the focus.
        Pixels rows carry no selection or focus, so nothing is claimed about screens read from pixels.
        """
        if not (self.reserved_paths or self.reserved_names) or keys.lower() not in ACTIVATION_KEYS:
            return None
        owned = any(w.kind == obs.Window.OWNED for w in snap.windows)
        live = {
            i for i, w in enumerate(snap.windows)
            if w.kind == obs.Window.POPUP or w.kind == (obs.Window.OWNED if owned else obs.Window.FOREGROUND)
        }  # fmt: skip
        here = [e for e in snap.elements if e.window in live and e.bounds.width > 0 and e.bounds.height > 0]
        if any(e.focused and e.role in EDITABLE_ROLES and not self.reserved_element(e) for e in here):
            return None
        if any((e.selected or e.focused) and self.reserved_element(e) for e in here):
            return "scenario-reserved path"
        return None

    def element_allowed(self, e: obs.Element, allow_destructive: list[str]) -> str | None:
        """None if the element may be acted on, else the reason."""
        if e.password:
            return "password field"
        if NEVER.search(e.name):
            return "never-list"
        if self.reserved_element(e):
            return "scenario-reserved path"
        if "destructive" in e.hints and not any(
            re.fullmatch(p, e.name.replace("&", ""), re.I) for p in allow_destructive
        ):
            return "destructive, not allowlisted"
        return None

    def text_allowed(self, text: str, target: obs.Element | None) -> str | None:
        if target is not None and target.password:
            return "never type into password fields"
        if self.reserved(text) or any(re.fullmatch(p, text.strip(), re.I) for p in self.reserved_names):
            return "scenario-reserved path"
        return None
