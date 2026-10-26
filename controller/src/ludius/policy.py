"""Code-enforced guardrails. Jev never sees an option this module would refuse."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


def alt_f4_safe(fg: obs.Window | None) -> bool:
    return fg is not None and fg.class_name not in DESKTOP_CLASSES


def shell_dialog(fg: obs.Window | None) -> bool:
    """A dialog of the shell itself (Shut Down Windows, Run): leave it with Esc, never click its buttons."""
    return fg is not None and fg.class_name == "#32770" and fg.process_name.lower() in SHELL_PROCESSES


@dataclass
class Policy:
    reserved_paths: list[str] = field(default_factory=list[str])  # scenario-reserved, regexes

    def element_allowed(self, e: obs.Element, allow_destructive: list[str]) -> str | None:
        """None if the element may be acted on, else the reason."""
        if e.password:
            return "password field"
        if NEVER.search(e.name):
            return "never-list"
        if "destructive" in e.hints and not any(
            re.fullmatch(p, e.name.replace("&", ""), re.I) for p in allow_destructive
        ):
            return "destructive, not allowlisted"
        return None

    def text_allowed(self, text: str, target: obs.Element | None) -> str | None:
        if target is not None and target.password:
            return "never type into password fields"
        for p in self.reserved_paths:
            if re.search(p, text, re.I):
                return "scenario-reserved path"
        return None
