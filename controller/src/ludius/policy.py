from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ludius import intranet
from ludius.tasks import COMMITS, Strict, pixels_window, profile
from ludius.v1 import observation_pb2 as obs

NEVER = re.compile(
    r"clear (browsing )?(history|cache|data)|empty recycle bin|disk clean-?up|uninstall|clear (the )?log|"
    r"clear (all )?events|log size|format (disk|drive)|delete browsing",
    re.I,
)
SHELL_PROCESSES = {
    "explorer.exe",
    "searchapp.exe",
    "searchui.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
}
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd"}
ACTIVATION_KEYS = {"enter", "space", "ctrl+o", "alt+enter", "ctrl+enter", "shift+enter", "f2", "delete", "shift+delete"}
FOCUS_KEYS = {"enter", "space", "ctrl+enter", "shift+enter", "alt+enter"}
EDITABLE_ROLES = {"edit", "document", "combo box"}


def commits(e: obs.Element) -> bool:
    name = e.name.replace("&", "").strip()
    return "destructive" in e.hints or any(re.fullmatch(c, name, re.I) for c in COMMITS)


_OW = profile("default").overwrite
OVERWRITE = re.compile(_OW.text, re.I | re.S)
OVERWRITE_TITLES = re.compile(f"(?:{_OW.titles})", re.I)
OVERWRITE_SAFE = re.compile(f"(?:{_OW.safe})", re.I)
OVERWRITE_REASON = "overwrite confirmation: abandon, never replace"


def overwrite_prompt(snap: obs.Snapshot) -> bool:
    return (any(OVERWRITE_TITLES.fullmatch(w.title) for w in snap.windows)
            or any(OVERWRITE.search(e.name) for e in snap.elements)
            or any(OVERWRITE.search(c) for c in snap.context))  # fmt: skip


def alt_f4_safe(fg: obs.Window | None) -> bool:
    if fg is None or pixels_window(fg) or fg.process_name == "":
        return False
    return fg.class_name not in DESKTOP_CLASSES


def shell_dialog(fg: obs.Window | None) -> bool:
    return fg is not None and fg.class_name == "#32770" and fg.process_name.lower() in SHELL_PROCESSES


class ReservedPath(Strict):
    path: str
    red_herring: bool = False


class Scenario(Strict):
    reserved: list[ReservedPath] = []
    mailboxes: list[str] = []
    messages: list[str] = []
    pages: list[str] = []


def _glob(g: str) -> str:
    out = ""
    for c in g:
        out += r"[^\\/]*" if c == "*" else r"[^\\/]" if c == "?" else re.escape(c)
    return out


@dataclass
class Policy:
    reserved_paths: list[str] = field(default_factory=list[str])
    reserved_names: list[str] = field(default_factory=list[str])
    reserved_stems: list[str] = field(default_factory=list[str])
    reserved_name_stems: list[str] = field(default_factory=list[str])
    reserved_subjects: list[str] = field(default_factory=list[str])
    reserved_pages: list[str] = field(default_factory=list[str])

    @classmethod
    def load(cls, path: Path) -> Policy:
        s = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        paths: list[str] = []
        names: list[str] = []
        stems: list[str] = []
        name_stems: list[str] = []
        for r in s.reserved:
            if not r.path.strip():
                raise ValueError(f"{path}: empty reserved path")
            if r.red_herring:
                continue
            g = r.path.strip().rstrip("\\/")
            paths.append(_glob(g))
            names.append(_glob(re.split(r"[\\/]", g)[-1]))
            stem = re.sub(r"\.[^.\\/]*$", "", g)
            stems.append(_glob(stem))
            name_stems.append(_glob(re.split(r"[\\/]", stem)[-1]))
        for m in s.mailboxes:
            paths.append(_glob(m.strip()))
            names.append(_glob(m.strip()))
        subjects = [_glob(m.strip()) for m in s.messages if m.strip()]
        pages = [_glob(t.strip()) for t in s.pages if t.strip()]
        pages += [rf"[?&]a={re.escape(p.id)}(?![a-z0-9-])" for p in intranet.load()
                  if any(re.search(g, p.title, re.I) for g in pages)]  # fmt: skip
        return cls(reserved_paths=paths, reserved_names=names, reserved_stems=stems, reserved_name_stems=name_stems,
                   reserved_subjects=subjects, reserved_pages=pages)  # fmt: skip

    @classmethod
    def from_env(cls) -> Policy:
        path = os.environ.get("LUDIUS_SCENARIO")
        return cls.load(Path(path)) if path else cls()

    def path_reserved(self, path: str) -> bool:
        if path.endswith(".*"):
            stem = path[:-2]
            leaf = re.split(r"[\\/]", stem.strip())[-1]
            return any(re.search(p, stem, re.I) for p in [*self.reserved_paths, *self.reserved_stems]) or any(
                re.fullmatch(p, leaf, re.I) for p in [*self.reserved_names, *self.reserved_name_stems]
            )
        leaf = re.split(r"[\\/]", path.strip())[-1]
        return self.reserved(path) or any(re.fullmatch(p, leaf, re.I) for p in self.reserved_names)

    def reserved(self, text: str) -> bool:
        return any(re.search(p, text, re.I) for p in self.reserved_paths)

    def page_reserved(self, text: str) -> bool:
        return any(re.search(p, text, re.I) for p in self.reserved_pages)

    def mail_reserved(self, sender: str, subject: str, body: str) -> bool:
        return any(self.reserved(x) for x in (sender, subject, body)) or any(
            re.fullmatch(p, subject.strip(), re.I) for p in self.reserved_subjects
        )

    def reserved_element(self, e: obs.Element) -> bool:
        name = e.name.strip()
        return (any(re.fullmatch(p, name, re.I) for p in self.reserved_names) or self.reserved(e.value)
                or self.reserved(name) or any(re.search(p, name, re.I) for p in self.reserved_subjects)
                or self.page_reserved(name))  # fmt: skip

    def key_allowed(self, keys: str, snap: obs.Snapshot, declared: list[str] | None = None) -> str | None:
        if overwrite_prompt(snap) and keys.lower() != "esc":
            return OVERWRITE_REASON
        if keys.lower() not in ACTIVATION_KEYS:
            return None
        owned = any(w.kind == obs.Window.OWNED for w in snap.windows)
        live = {
            i for i, w in enumerate(snap.windows)
            if w.kind == obs.Window.POPUP or w.kind == (obs.Window.OWNED if owned else obs.Window.FOREGROUND)
        }  # fmt: skip
        here = [e for e in snap.elements if e.window in live and e.bounds.width > 0 and e.bounds.height > 0]
        typing = any(e.focused and e.role in EDITABLE_ROLES and not self.reserved_element(e) for e in here)
        if not typing and any((e.selected or e.focused) and self.reserved_element(e) for e in here):
            return "scenario-reserved path"
        if keys.lower() in FOCUS_KEYS:
            declared = declared or []
            focus = next((e for e in here if e.focused), None)
            if focus is None and not declared:
                return "the focus is unknown and the task declares no committing effect"
            name = focus.name.replace("&", "").strip() if focus else ""
            if focus is not None and commits(focus) and not any(re.fullmatch(p, name, re.I) for p in declared):
                return f"the focused {focus.role} {name!r} commits or destroys, not declared"
        return None

    def element_allowed(
        self, e: obs.Element, allow_destructive: list[str], snap: obs.Snapshot | None = None
    ) -> str | None:
        if (
            snap is not None
            and overwrite_prompt(snap)
            and not OVERWRITE_SAFE.fullmatch(e.name.replace("&", "").strip())
        ):
            return OVERWRITE_REASON
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
        if self.page_reserved(text):
            return "scenario-reserved intranet page"
        return None
