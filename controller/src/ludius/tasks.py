from __future__ import annotations

import csv
import datetime as dt
import functools
import hashlib
import io
import os
import random
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ludius import guestfile, intranet, mail, share
from ludius.v1 import observation_pb2 as obs


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowCheck(Strict):
    process: str | None = None
    title: str | None = None
    class_name: str | None = None
    absent: bool = False


class ElementCheck(Strict):
    role: str | None = None
    name: str | None = None
    value: str | None = None
    focused: bool | None = None
    selected: bool | None = None
    at: str | None = None


class AddedCheck(Strict):
    items: ElementCheck
    new: ElementCheck
    mark: str


class PixelsCheck(Strict):
    present: list[str] = Field(min_length=1)
    absent: list[str] = Field(default_factory=list[str])


class Pixels(Strict):
    title: str
    anchors: list[str] = Field(min_length=1)
    done: dict[str, PixelsCheck] = Field(default_factory=dict[str, PixelsCheck])
    hints: dict[str, str] = Field(default_factory=dict[str, str])


class MailCheck(Strict):
    mailbox: str
    folder: Literal["inbox", "sent"] = "inbox"
    subject: str = "{subject}"
    body: str = "{body}"
    above_quote: bool = False
    reply_to: str = ""
    attachment: str | None = None

    @model_validator(mode="after")
    def _own_file(self) -> MailCheck:
        if self.attachment is not None and guestfile.profile_path("u", self.attachment) is None:
            raise ValueError(f"mail check: attachment {self.attachment!r} is a file in a noise folder")
        return self


class InviteCheck(Strict):
    mailbox: str = "{to}"
    subject: str = "{subject}"
    body: str = "{body}"
    date: str = "{date}"
    start: str = "{start}"
    end: str = "{end}"


EDITABLE = (".docx", ".txt", ".xlsx")
ATTACHABLE = (".docx", ".xlsx", ".pptx", ".txt", ".pdf")
ATTACH_MAX_BYTES = 5_000_000


class FileCheck(Strict):
    path: str
    cells: str | None = None
    paragraphs: str | None = None
    slides: list[str] | None = None
    download: str | None = None
    append: str | None = None
    source: str | None = None
    recycled: bool = False
    share: str | None = None
    moved_from: str | None = None
    attachment_of: list[str] | None = None
    export_of: str | None = None
    extract_of: str | None = None
    comment: str | None = None

    @model_validator(mode="after")
    def _noise_folder(self) -> FileCheck:
        need = {".xlsx": "cells", ".docx": "paragraphs", ".pptx": "slides"}
        ext = self.path.lower()[-5:]
        if self.source is not None and (self.append is None or guestfile.profile_path("u", self.source) is None
                                        or self.source.lower()[-5:] != ext or self.source == self.path):  # fmt: skip
            raise ValueError(f"file check {self.path!r}: a source goes with append, another noise file, same type")
        if self.comment is not None:
            if guestfile.profile_path("u", self.path) is None or not self.path.lower().endswith(".docx"):
                raise ValueError(f"file check {self.path!r}: a comment goes on a noise .docx")
            if self.recycled or any(getattr(self, k) is not None for k in (*need.values(), "download", "append",
                                                                            "share", "moved_from", "attachment_of",
                                                                            "export_of", "extract_of")):
                raise ValueError(f"file check {self.path!r}: a comment check stands alone")
            return self
        if self.extract_of is not None:
            paths = (self.path, self.extract_of)
            if any(guestfile.profile_path("u", x) is None for x in paths) or self.extract_of[-4:].lower() != ".zip":
                raise ValueError(f"file check {self.path!r}: an extract goes from a noise .zip to a noise file")
            if self.recycled or any(getattr(self, k) is not None for k in (*need.values(), "download", "append",
                                                                            "share", "moved_from", "attachment_of",
                                                                            "export_of")):  # fmt: skip
                raise ValueError(f"file check {self.path!r}: an extract check stands alone")
            return self
        if self.export_of is not None:
            paths = (self.path, self.export_of)
            kinds, csv_in = (".pdf", ".zip", ".xlsx"), self.export_of.lower().endswith(".csv")
            if any(guestfile.profile_path("u", x) is None for x in paths) or not self.path.lower().endswith(kinds) \
                    or csv_in != self.path.lower().endswith(".xlsx"):
                raise ValueError(f"file check {self.path!r}: an export goes to a noise .pdf, .zip or (from .csv) .xlsx")
            if self.recycled or any(getattr(self, k) is not None for k in (*need.values(), "download", "append",
                                                                            "share", "moved_from", "attachment_of")):
                raise ValueError(f"file check {self.path!r}: an export check stands alone")
            return self
        if self.attachment_of is not None:
            if len(self.attachment_of) != 2 or guestfile.profile_path("u", self.path) is None:
                raise ValueError(f"file check {self.path!r}: an attachment ([sender, subject]) saves to a noise folder")
            others = (*need.values(), "download", "append", "share", "moved_from")
            if self.recycled or any(getattr(self, k) is not None for k in others):
                raise ValueError(f"file check {self.path!r}: a saved attachment check stands alone")
            return self
        if self.recycled or self.share is not None:
            kind = "recycled" if self.recycled else "share"
            if guestfile.profile_path("u", self.path) is None:
                raise ValueError(f"file check {self.path!r}: a {kind} file is in a noise folder")
            others = (*need.values(), "download", "append", "moved_from", *(("share",) if self.recycled else ()))
            if any(getattr(self, k) is not None for k in others):
                raise ValueError(f"file check {self.path!r}: a {kind} check stands alone")
            return self
        if self.moved_from is not None:
            if guestfile.profile_path("u", self.moved_from) is None or guestfile.profile_path("u", self.path) is None:
                raise ValueError(f"file check {self.path!r}: a move goes from one noise folder to another")
            others = (*need.values(), "download", "append")
            if self.moved_from == self.path or any(getattr(self, k) is not None for k in others):
                raise ValueError(f"file check {self.path!r}: a move check stands alone")
            return self
        if self.append is not None:
            if guestfile.profile_path("u", self.path) is None or not self.path.lower().endswith(EDITABLE):
                raise ValueError(f"file check {self.path!r}: append edits a .docx, .txt or .xlsx in a noise folder")
            if any(getattr(self, k) is not None for k in (*need.values(), "download")):
                raise ValueError(f"file check {self.path!r}: an append check stands alone")
            return self
        if self.download is not None and guestfile.profile_path("u", self.path) is not None:
            if any(getattr(self, k) is not None for k in need.values()):
                raise ValueError(f"file check {self.path!r}: a download check stands alone")
            return self
        if guestfile.profile_path("u", self.path) is None or ext not in need:
            raise ValueError(f"file check {self.path!r}: an {'/'.join(need)} in a noise folder")
        if [k for k in need.values() if getattr(self, k) is not None] != [need[ext]]:
            raise ValueError(f"file check {self.path!r}: needs {need[ext]} and no other content check")
        return self


class Done(Strict):
    window: WindowCheck | None = None
    foreground: WindowCheck | None = None
    element: ElementCheck | None = None
    added: AddedCheck | None = None
    typed: bool = False
    pressed: bool = False
    pixels: PixelsCheck | None = None
    mail: MailCheck | None = None
    invite: InviteCheck | None = None
    file: FileCheck | None = None
    read: bool = False
    ask: str | None = None

    @model_validator(mode="after")
    def _ask_alone(self) -> Done:
        if self.ask is not None and self.model_dump(exclude={"ask"}, exclude_defaults=True):
            raise ValueError("done.ask stands alone: the Decider's answer is the whole check")
        return self


class Subgoal(Strict):
    goal: str
    text: str | None = None
    search: str = Field(default="", pattern=r"^[ -~]{0,40}$")
    keys: list[str] = Field(default_factory=list[str], max_length=16)
    launch: bool = False
    copy_text: bool = False
    allow_destructive: list[str] = Field(default_factory=list[str])
    require: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    in_window: WindowCheck | None = None
    breadcrumb: list[str] = Field(default_factory=list[str])
    confirm: str | None = None
    unreadable_ok: bool = False
    needs: str | None = None
    caret: Literal["end", "start", "append"] = "end"
    replace: bool = False
    field: str = ""
    forbid: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    avoid: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    done: Done | None = None
    steps: int = 12
    pixels_hint: str = ""

    @model_validator(mode="after")
    def _one_text(self) -> Subgoal:
        if self.text and self.search:
            raise ValueError("a subgoal types either a content value (text) or template text (search), not both")
        return self


RECIPIENT = "^({to}|{to_name}|{to_name} <{to}>);?$"
Capability = Literal[
    "mail.send",
    "mail.reply",
    "mail.read",
    "mail.save",
    "calendar.invite",
    "file.move",
    "file.write",
    "file.edit",
    "file.reuse",
    "file.export",
    "file.recycle",
    "file.share",
    "file.organize",
    "file.download",
    "chat.post",
    "chat.dm",
    "chat.read",
    "chat.read_channel",
]
SENDS: dict[str, Literal["mail", "meeting"]] = {"mail.send": "mail", "mail.reply": "mail", "calendar.invite": "meeting"}
REFERS_TO_EXISTING: dict[str, set[str]] = {
    "mail.send": {"to", "to_name", "file_name"},
    "mail.reply": {"to", "to_name", "subject"},
    "mail.read": {"sender", "subject"},
    "mail.save": {"sender", "subject"},
    "calendar.invite": {"to", "to_name"},
    "file.move": {"file_name", "folder_name"},
    "file.edit": {"file_name"},
    "file.reuse": {"file_name"},
    "file.export": {"file_name"},
    "file.recycle": {"file_name", "file_ext"},
    "file.share": {"file_name", "file_ext", "share"},
    "file.organize": {"file_name"},
    "chat.post": {"channel"},
    "chat.dm": {"to", "to_name"},
    "chat.read": {"to", "to_name"},
    "chat.read_channel": {"channel"},
}
APPS = Path(__file__).resolve().parents[2] / "data" / "apps"
COMMITS = ["send", "send anyway", "save", "yes", "ok", "replace", "delete"]
LAUNCH_TEXT = "launch_query"


def declared_commits(task: Task, sg: Subgoal) -> list[str]:
    return [*sg.allow_destructive, *(["Save"] if task.template.writes else [])]


class Confirm(Strict):
    prompt: str
    button: str


class Form(Strict):
    recipients: Literal["value"] = "value"
    to: ElementCheck
    empty: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    never_filled: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    avoid: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    confirm: list[Confirm] = Field(default_factory=list[Confirm])

    @model_validator(mode="after")
    def _selectors(self) -> Form:
        if any(c.value is not None or not c.name for c in [self.to, *self.empty, *self.never_filled]):
            raise ValueError("form fields are selectors: a name and no value")
        return self

    def contract(self) -> tuple[list[ElementCheck], list[ElementCheck], list[ElementCheck]]:
        require = [self.to.model_copy(update={"value": RECIPIENT})]
        require += [c.model_copy(update={"value": "^$"}) for c in self.empty]
        forbid = [ElementCheck(name=c.name, value=r"\S") for c in [*self.empty, *self.never_filled]]
        return require, forbid, list(self.avoid)


class Dialogs(Strict):
    discard: list[str]
    choice_ok: str
    leave: list[str]

    @model_validator(mode="after")
    def _no_commit(self) -> Dialogs:
        for w in [*self.discard, *self.leave]:
            if any(re.fullmatch(w, c, re.I) for c in COMMITS):
                raise ValueError(f"dialog word {w!r} names a button that commits")
        if self.choice_ok.lower() != "ok" and any(self.choice_ok.lower() == c for c in COMMITS):
            raise ValueError(f"choice_ok {self.choice_ok!r} names a button that commits")
        return self


class Overwrite(Strict):
    text: str
    titles: str
    safe: str


class Profile(Strict):
    process: str = ""
    launch_query: str = ""
    keys: dict[str, str]
    locale: dict[Literal["date"], str]
    dialogs: Dialogs
    forms: dict[Literal["mail", "meeting"], Form] = Field(default_factory=dict[Literal["mail", "meeting"], Form])
    overwrite: Overwrite

    @model_validator(mode="after")
    def _safe_close(self) -> Profile:
        if self.keys.get("close") not in CLOSE_KEYS:
            raise ValueError(f"close key {self.keys.get('close')!r} is not one of {sorted(CLOSE_KEYS)}")
        return self


CLOSE_KEYS = {"Alt+F4", "Esc", "Ctrl+W"}


def _merge(base: Any, own: Any) -> Any:
    if isinstance(base, dict) and isinstance(own, dict):
        b, o = cast(dict[str, Any], base), cast(dict[str, Any], own)
        return {**b, **{k: _merge(b.get(k), v) for k, v in o.items()}}
    if isinstance(base, list) and isinstance(own, list):
        return [*cast(list[Any], base), *cast(list[Any], own)]
    return own


@functools.cache
def profile(app: str) -> Profile:
    base = yaml.safe_load((APPS / "default.yaml").read_text(encoding="utf-8"))
    path = APPS / f"{app}.yaml"
    mine = app != "default" and re.fullmatch(r"[a-z0-9_]+", app) and path.exists()
    own: Any = yaml.safe_load(path.read_text(encoding="utf-8")) if mine else {}
    if "overwrite" in own:
        raise ValueError(f"{path}: the overwrite guard is range-wide, not an app's")
    return Profile.model_validate(_merge(base, own))


NAME_PARAMS = {"file_name", "folder_name", "new_name"}


SAVE_EXTENSIONS = {"notepad": ".txt", "wordpad": ".rtf", "paint": ".png", "excel": ".xlsx", "word": ".docx",
                   "powerpoint": ".pptx"}  # fmt: skip


class KeyCommit(Strict):
    keys: str
    focus: ElementCheck


class Template(Strict):
    task: str
    app: str
    capability: Capability | None = None
    processes: list[str] = Field(default_factory=list[str])
    variants: list[list[str]]
    weights: list[float] | None = None
    subgoals: dict[str, Subgoal]
    budget_steps: int = 40
    budget_minutes: float = 8
    on_abandon: str = "close_without_saving"
    params: list[str] = Field(default_factory=list[str])
    defaults: dict[str, str] = Field(default_factory=dict[str, str])
    about: str = ""
    example: dict[str, str] = Field(default_factory=dict[str, str])
    resembles: str = ""
    plan_weight: float = Field(default=1.0, gt=0, le=1)
    pixels: Pixels | None = None
    writes: list[str] = Field(default_factory=list[str])
    removes: list[str] = Field(default_factory=list[str])
    fallback_for: str = ""
    key_commits: list[KeyCommit] = Field(default_factory=list[KeyCommit])

    @property
    def attaches(self) -> list[str]:
        return [sg.done.mail.attachment for sg in self.subgoals.values()
                if sg.done and sg.done.mail and sg.done.mail.attachment]  # fmt: skip

    @property
    def sends_mail(self) -> bool:
        return self.capability in SENDS

    @property
    def send_form(self) -> Literal["mail", "meeting"]:
        return SENDS.get(self.capability or "", "mail")

    @property
    def task_content(self) -> Literal["reply"] | None:
        return "reply" if self.capability == "mail.reply" else None

    @model_validator(mode="after")
    def _profile(self) -> Template:
        p = profile(self.app)
        self.processes = self.processes or ([p.process] if p.process else [])
        if not self.processes:
            raise ValueError(f"{self.task}: no processes, and app {self.app!r} has no profile process")
        if p.launch_query:
            self.defaults.setdefault("launch_query", p.launch_query)
        for sg in self.subgoals.values():
            sg.keys = [p.keys.get(k, k) for k in sg.keys]
        return self

    @model_validator(mode="after")
    def _task_content(self) -> Template:
        if self.task_content == "reply":
            checks = [sg.done.mail for sg in self.subgoals.values() if sg.done and sg.done.mail]
            if not checks or any(c.reply_to != "{reply_to}" for c in checks) or "body" in self.params:
                raise ValueError(f"{self.task}: a task-time reply needs mail checks with reply_to and no planned body")
        return self

    @model_validator(mode="after")
    def _read(self) -> Template:
        reads = any(sg.done and sg.done.read for sg in self.subgoals.values())
        if reads != (self.capability == "mail.read") or (reads and not {"sender", "subject"} <= set(self.params)):
            raise ValueError(f"{self.task}: mail.read needs a read done check and params sender, subject (only it)")
        typed = any(sg.text not in (None, LAUNCH_TEXT) or sg.search or sg.copy_text or sg.allow_destructive
                    for sg in self.subgoals.values())  # fmt: skip
        if reads and (self.writes or typed):
            raise ValueError(f"{self.task}: mail.read types only the launch query, no writes or destructive clicks")
        return self

    @model_validator(mode="after")
    def _writes_declared(self) -> Template:
        own = self.attaches or self.capability in ("file.recycle", "file.share")
        if set(self.params) & NAME_PARAMS and not self.writes and not own:
            raise ValueError(f"{self.task}: creates {sorted(set(self.params) & NAME_PARAMS)} but declares no writes")
        for r in self.removes:
            if guestfile.profile_path("u", r.removesuffix(".*")) is None:
                raise ValueError(f"{self.task}: remove {r!r} is not in a noise folder {guestfile.NOISE_FOLDERS}")
        for w in self.writes:
            if guestfile.profile_path("u", w.removesuffix(".*")) is None:
                raise ValueError(f"{self.task}: write {w!r} is not in a noise folder {guestfile.NOISE_FOLDERS}")
            ext = SAVE_EXTENSIONS.get(self.app)
            if ext and "{file_name}" in w and not w.lower().endswith((ext, ".pdf") if self.app == "word" else ext):
                raise ValueError(f"{self.task}: {self.app} saves {ext}, so write {w!r} must end with it")
        marks = {sg.done.added.mark for sg in self.subgoals.values() if sg.done and sg.done.added}
        for name, sg in self.subgoals.items():
            if sg.done and sg.done.element and sg.done.element.at not in (None, *marks):
                raise ValueError(
                    f"{self.task}: {name} checks the item at {sg.done.element.at!r}, which no added check marks"
                )
        for sg in self.subgoals.values():
            if sg.done and sg.done.file and sg.done.file.recycled:
                if sg.done.file.path not in self.removes or self.capability != "file.recycle":
                    raise ValueError(f"{self.task}: recycled check {sg.done.file.path!r} is not a file.recycle remove")
            elif sg.done and sg.done.file and sg.done.file.share is not None:
                if self.capability != "file.share":
                    raise ValueError(f"{self.task}: share check {sg.done.file.path!r} needs capability file.share")
            elif (
                sg.done
                and sg.done.file
                and sg.done.file.moved_from is not None
                and (sg.done.file.moved_from not in self.removes or self.capability != "file.organize")
            ):
                raise ValueError(f"{self.task}: move check {sg.done.file.path!r} needs file.organize removing it")
            elif sg.done and sg.done.file and sg.done.file.path not in self.writes:
                raise ValueError(f"{self.task}: file check {sg.done.file.path!r} is not a declared write")
        appends = [f for sg in self.subgoals.values() if sg.done and (f := sg.done.file) and (f.append or f.comment)]
        kinds = {"file.reuse" if c.source else "file.edit" for c in appends}
        revise = self.capability == "mail.send" and kinds == {"file.edit"}
        if revise and not {c.path for c in appends} <= set(self.attaches):
            raise ValueError(f"{self.task}: a mail.send edits only the file it attaches")
        if not revise and kinds != ({self.capability} & {"file.edit", "file.reuse"}):
            raise ValueError(f"{self.task}: file.edit takes an append or comment check, file.reuse one with a source")
        if any(c.source in self.writes for c in appends):
            raise ValueError(f"{self.task}: a reused source is read, never a write")
        return self

    @model_validator(mode="after")
    def _send_contract(self) -> Template:
        sends = [n for n, sg in self.subgoals.items() if sg.done and (sg.done.mail or sg.done.invite)]
        if sends and not self.sends_mail:
            raise ValueError(f"{self.task}: a mail or meeting done check needs a sending capability {sorted(SENDS)}")
        wrong = "invite" if self.send_form == "mail" else "mail"
        if any(getattr(sg.done, wrong) for sg in self.subgoals.values() if sg.done):
            raise ValueError(f"{self.task}: a {wrong} done check does not fit send_form {self.send_form!r}")
        if self.sends_mail:
            if not sends:
                raise ValueError(f"{self.task}: {self.capability} without a subgoal whose done check is a mail check")
            form = profile(self.app).forms.get(self.send_form)
            if form is None:
                raise ValueError(f"{self.task}: app {self.app!r} has no {self.send_form} form in its profile")
            require, forbid, avoid = form.contract()
            for name, sg in self.subgoals.items():
                sg.avoid = [*avoid, *sg.avoid]
                if name in sends:
                    sg.require = [*require, *sg.require]
                    sg.forbid = [*forbid, *sg.forbid]
        return self

    @model_validator(mode="after")
    def _pixels_done(self) -> Template:
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
    hours: tuple[str, str] = ("08:30", "17:00")
    style: str = ""
    typing_mean_ms: int = 140
    typing_jitter_ms: int = 60
    think_mean_s: float = 1.6
    pointer_ms: tuple[int, int] = (180, 520)
    risk_profile: list[str] = Field(default_factory=list[str])
    tz: str = "Europe/Stockholm"


class TaskInstance(Strict):
    template: str
    params: dict[str, str] = Field(default_factory=dict[str, str])
    content: dict[str, str] = Field(default_factory=dict[str, str])
    variant: int | None = None
    context: str = ""
    request: str = ""


class Task:
    def __init__(self, template: Template, inst: TaskInstance, rng: random.Random) -> None:
        env = {k: os.environ.get(v[1:], "") for k, v in template.defaults.items() if v.startswith("$")}
        if unset := sorted(template.defaults[k] for k, v in env.items() if not v):
            raise ValueError(f"{template.task}: {', '.join(unset)} unset")
        have = {**template.defaults, **env, **inst.params, **inst.content}
        missing = [p for p in template.params if not have.get(p) and p not in template.defaults]
        if missing:
            raise ValueError(f"{template.task}: missing parameters {missing}")
        have |= {k: v.format_map(have) for k, v in template.defaults.items() if "{" in v and k not in inst.params}
        self.template = template
        self.inst = inst
        self.owner = ""
        self.started_ms = 0
        self.tz = "Europe/Stockholm"
        self.mail_before: mail.Seen | None = None
        self.files_absent: frozenset[str] | None = None
        self.files_before: dict[str, list[str]] = {}
        self.sources_before: dict[str, bytes] = {}
        self.comments_before: dict[str, list[str]] = {}
        self.saved_sha: dict[str, str] = {}
        self.files_present: frozenset[str] = frozenset()
        self.read_id = 0
        self.added_base: int | None = None
        self.marks: dict[str, tuple[ElementCheck, int]] = {}
        idx = (
            inst.variant
            if inst.variant is not None
            else rng.choices(range(len(template.variants)), template.weights)[0]
        )
        self.variant = idx
        skip = {
            n
            for n, sg in template.subgoals.items()
            if (k := sg.text or sg.needs) and k in template.defaults and not have[k]
        }
        self.subgoal_names = [n for n in template.variants[idx] if n not in skip]
        self.values = have | {f"{k}_typo": v + _neighbour(v[-1]) for k, v in have.items() if v and "\n" not in v}
        self.values |= {f"{k}_enter": v.rstrip("\n") + "\n" for k, v in have.items() if v}
        self.values |= {f"{k}_line": "\n" + v.lstrip("\n") for k, v in have.items() if v}
        date = profile(template.app).locale["date"]
        self.values |= {f"{k}_ui": date.format(y=v[:4], m=v[5:7], d=v[8:]) for k, v in have.items()
                        if ISO_DATE.fullmatch(v)}  # fmt: skip
        self.values |= {f"{k}_head": next(x.strip() for x in v.splitlines() if x.strip())[:HEAD]
                        for k, v in have.items() if v.strip()}  # fmt: skip

    def fmt(self, s: str) -> str:
        return s.format_map(self.values)

    def fmt_re(self, s: str) -> str:
        return s.format_map({k: re.escape(v) for k, v in self.values.items()})

    def pixels_re(self, s: str) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: re.escape(compact(self.values[m[1]])) if m[1] in self.values else m[0], s)

    def subgoal(self, name: str) -> Subgoal:
        sg = self.template.subgoals[name]
        return sg.model_copy(update={"goal": self.fmt(sg.goal)})

    def text(self, sg: Subgoal) -> str | None:
        return self.values.get(sg.text) if sg.text else sg.search or None


ISO_DATE = re.compile(r"\d{4}-\d\d-\d\d")
_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]


def _neighbour(c: str) -> str:
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


def _window_matches(w: obs.Window, c: WindowCheck, task: Task) -> bool:
    if c.process and w.process_name.lower() != c.process.lower():
        return False
    if c.class_name and w.class_name != c.class_name:
        return False
    return not (c.title and not re.search(task.fmt_re(c.title), w.title, re.I))


def pixels_window(w: obs.Window) -> bool:
    return w.framework_id == "visual" and w.process_name == ""


def compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


def visible(pattern: str, rows: list[obs.Element], task: Task) -> bool:
    rx = task.pixels_re(pattern)
    return any(re.search(rx, compact(e.name), re.I) for e in rows)


HEAD = 24
TASKBAR = "ypeheretosearch"


def pixels_region(snap: obs.Snapshot, window: int, task: Task) -> list[obs.Element]:
    px = task.template.pixels
    if px is None:
        return []
    rows = [e for e in snap.elements if e.window == window]
    if not (visible(px.title, rows, task) and all(visible(a, rows, task) for a in px.anchors)):
        return []
    rx = task.pixels_re(px.title)
    title = min((e for e in rows if re.search(rx, compact(e.name), re.I)), key=lambda e: e.bounds.y)
    bar = [e.bounds.y for e in rows if re.search(TASKBAR, compact(e.name), re.I)]
    bottom = min(bar) - 4 if bar else snap.screen_height - 48 * (snap.dpi or 96) // 96
    return [
        e for e in rows
        if e.bounds.x + e.bounds.width / 2 >= title.bounds.x - 48
        and title.bounds.y - 12 <= e.bounds.y + e.bounds.height / 2 < bottom
    ]  # fmt: skip


def check_done(
    done: Done | None, snap: obs.Snapshot, task: Task, typed_ok: bool, outside: bool = True, pressed_ok: bool = False
) -> bool | None:
    if done is None or done.ask:
        return None
    if (done.mail or done.invite or done.file or done.read) and not (
        done.window or done.foreground or done.element or done.added or done.typed or done.pressed or done.pixels
    ):
        return _outside_ok(done, task) if outside else None
    on_pixels = any(pixels_window(w) for w in snap.windows)
    if (
        done.pixels
        and not on_pixels
        and not (done.window or done.foreground or done.element or done.added or done.typed)
    ):
        return None
    if (done.typed and not typed_ok) or (done.pressed and not pressed_ok):
        return False
    if on_pixels and not done.pixels:
        return False
    if done.pixels and on_pixels:
        rows = [e for e in snap.elements if e.window < len(snap.windows) and pixels_window(snap.windows[e.window])]
        return all(visible(p, rows, task) for p in done.pixels.present) and not any(
            visible(p, rows, task) for p in done.pixels.absent
        )
    if (done.window or done.foreground) and any(pixels_window(w) for w in snap.windows):
        return False
    if done.window:
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
        candidates = snap.elements if c.at is None else _marked(c.at, snap, task)
        if not any(element_matches(e, c, task) for e in candidates):
            return False
    if done.added and not _added_ok(done.added, snap, task):
        return False
    return _outside_ok(done, task) if outside else True


def _items(c: ElementCheck, snap: obs.Snapshot, task: Task) -> list[obs.Element]:
    return sorted((e for e in snap.elements if element_matches(e, c, task)), key=lambda e: (e.bounds.y, e.bounds.x))


def added_baseline(done: Done | None, snap: obs.Snapshot, task: Task) -> None:
    task.added_base = len(_items(done.added.items, snap, task)) if done and done.added else None


def _added_ok(c: AddedCheck, snap: obs.Snapshot, task: Task) -> bool:
    items = _items(c.items, snap, task)
    picked = [i for i, e in enumerate(items) if e.selected]
    if task.added_base is None or len(items) <= task.added_base or len(picked) != 1:
        return False
    if not element_matches(items[picked[0]], c.new, task):
        return False
    task.marks[c.mark] = (c.items, picked[0])
    return True


def _marked(mark: str, snap: obs.Snapshot, task: Task) -> list[obs.Element]:
    if mark not in task.marks:
        return []
    items, at = task.marks[mark]
    found = _items(items, snap, task)
    return found[at : at + 1]


def _outside_ok(done: Done, task: Task) -> bool:
    return ((done.mail is None or _mail_ok(done.mail, task)) and (done.invite is None or _invite_ok(done.invite, task))
            and (done.file is None or _file_ok(done.file, task)) and (not done.read or _read_ok(task)))  # fmt: skip


def _read_ok(task: Task) -> bool:
    return task.read_id > 0 and bool(task.owner) and mail.read_state(task.owner, task.read_id) is True


def element_matches(e: obs.Element, c: ElementCheck, task: Task, flags: int = re.I) -> bool:
    value = re.sub(r"\r\n?", "\n", e.value)
    return (
        (not c.role or e.role == c.role)
        and (not c.name or re.search(task.fmt_re(c.name), e.name, flags) is not None)
        and (not c.value or re.search(task.fmt_re(c.value), value, flags) is not None)
        and (c.focused is None or e.focused == c.focused)
        and (c.selected is None or e.selected == c.selected)
    )


def guard_matches(e: obs.Element, c: ElementCheck, task: Task) -> bool:
    return "value" in e.actions and e.value.isascii() and element_matches(e, c, task, re.I | re.A)


def preconditions(sg: Subgoal, snap: obs.Snapshot, task: Task, target: obs.Element | None = None) -> str | None:
    fg = next((i for i, w in enumerate(snap.windows) if w.kind == obs.Window.FOREGROUND), None)
    if (sg.require or sg.in_window) and target is not None and target.window != fg:
        return "precondition: the target is not in the foreground window"
    if sg.in_window and (fg is None or not _window_matches(snap.windows[fg], sg.in_window, task)):
        want = sg.in_window.title or sg.in_window.process or ""
        return f"precondition: the foreground window is not {task.fmt(want)!r}"
    if sg.breadcrumb and (got := explorer_crumbs(snap, fg)) != sg.breadcrumb:
        return f"precondition: the folder is {' > '.join(got or ['unknown'])!r}, not {' > '.join(sg.breadcrumb)!r}"
    for c in sg.require:
        ok = guard_matches if c.value is not None else element_matches
        if not any(e.window == fg and ok(e, c, task) for e in snap.elements):
            seen = [
                e.value[:80]
                for e in snap.elements
                if e.window == fg and element_matches(e, c.model_copy(update={"value": None}), task)
            ]
            w = snap.windows[fg] if fg is not None else None
            where = f"{w.title!r} {w.class_name} no_tree={w.no_tree}" if w else "none"
            return (f"precondition: no readable {task.fmt(c.name or c.role or '')!r} field with value"
                    f" {task.fmt(c.value or '')!r} (read: {seen!r}; foreground: {where})")  # fmt: skip
    for c in sg.forbid:
        if any(element_matches(e, c, task) for e in snap.elements):
            return f"precondition: {c.name or c.role!r} must not match {task.fmt(c.value or '')!r}"
    return None


def explorer_crumbs(snap: obs.Snapshot, fg: int | None) -> list[str] | None:
    els = [e for e in snap.elements if e.window == fg]
    start = next((i for i, e in enumerate(els) if e.role == "split button" and e.name == "All locations"), None)
    if start is None:
        return None
    crumbs: list[str] = []
    for e in els[start + 1 :]:
        if e.role == "button" and e.name.startswith("Refresh"):
            return crumbs
        if e.role == "button" and e.name == "Previous Locations":
            continue
        if e.role not in ("split button", "menu item"):
            return None
        crumbs.append(e.name)
    return None


def _mail_ok(c: MailCheck, task: Task) -> bool:
    sender = f"{task.owner}@{mail.DOMAIN}" if task.owner else ""
    attachment = None
    if c.attachment is not None:
        rel = task.fmt(c.attachment)
        if (data := task.sources_before.get(rel)) is None:
            return False
        attachment = _leaf(rel).casefold(), hashlib.sha256(data).hexdigest()
    return mail.delivered(task.fmt(c.mailbox), c.folder, task.fmt(c.subject), task.fmt(c.body), sender,
                          task.started_ms, c.above_quote, task.mail_before, task.fmt(c.reply_to),
                          attachment)  # fmt: skip


def local_ms(date: str, hhmm: str, tz: str) -> int:
    return int(dt.datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def _invite_ok(c: InviteCheck, task: Task) -> bool:
    if not task.owner:
        return False
    date = task.fmt(c.date)
    return mail.invited(task.fmt(c.mailbox), task.fmt(c.subject), task.fmt(c.body), f"{task.owner}@{mail.DOMAIN}",
                        task.started_ms, local_ms(date, task.fmt(c.start), task.tz),
                        local_ms(date, task.fmt(c.end), task.tz), task.mail_before)  # fmt: skip


def _sent_check(done: Done | None) -> tuple[str, str, str, str] | None:
    if done and done.mail:
        return done.mail.mailbox, done.mail.folder, done.mail.subject, "sent"
    if done and done.invite:
        return done.invite.mailbox, "inbox", done.invite.subject, "calendar"
    return None


def mail_baseline(task: Task) -> mail.Seen | None:
    sender = f"{task.owner}@{mail.DOMAIN}"
    seen: set[tuple[str, str, int]] = set()
    for sg in task.template.subgoals.values():
        if c := _sent_check(sg.done):
            found = mail.present(task.fmt(c[0]), c[1], task.fmt(c[2]), sender, c[3])
            if found is None:
                return None
            seen |= found
    return frozenset(seen)


def send_confirmation(snap: obs.Snapshot, task: Task) -> obs.Element | None:
    fg = next((i for i, w in enumerate(snap.windows) if w.kind == obs.Window.FOREGROUND), None)
    apps = {p.lower() for p in task.template.processes}
    if fg is None or snap.windows[fg].process_name.lower() not in apps:
        return None
    mine = [e for e in snap.elements if e.window == fg]
    form = profile(task.template.app).forms.get(task.template.send_form)
    for c in form.confirm if form and task.template.sends_mail else []:
        if any(re.fullmatch(c.prompt, e.name) or re.fullmatch(c.prompt, e.value) for e in mine):
            return next((e for e in mine if e.role == "button" and e.name.replace("&", "") == c.button), None)
    return None


RESERVED_RECIPIENT = "precondition: the planned recipient is scenario-reserved"


def recipient_reserved(sg: Subgoal, task: Task, policy: Any) -> bool:
    return bool((c := _sent_check(sg.done)) and policy.reserved(task.fmt(c[0])))


def write_paths(task: Task) -> list[str]:
    return [task.fmt(w) for w in task.template.writes]


def file_reserved(user: str, rel: str, policy: Any) -> bool:
    any_ext = ".*" if rel.endswith(".*") else ""
    full = guestfile.profile_path(user, rel.removesuffix(".*")) if user else None
    return bool(policy.path_reserved(rel) or (full and policy.path_reserved(full + any_ext)))


def file_baseline(task: Task, policy: Any) -> str | None:
    files = [sg.done.file for sg in task.template.subgoals.values() if sg.done and sg.done.file]
    sources = [task.fmt(c.source or c.export_of or c.extract_of or "") for c in files
               if c.source or c.export_of or c.extract_of]  # fmt: skip
    attached = [task.fmt(a) for a in task.template.attaches]
    for rel in [*write_paths(task), *(task.fmt(r) for r in task.template.removes), *sources, *attached]:
        if file_reserved(task.owner, rel, policy):
            return f"{rel}: scenario-reserved file"
    absent: set[str] = set()
    present: set[str] = set()
    for rel in sorted({task.fmt(c.path) for c in files if c.recycled}):
        f = guestfile.read(task.owner, rel)
        if f is None:
            return f"{rel}: unreadable on the guest"
        if not f.exists:
            return f"{rel}: not there, nothing to recycle"
        present.add(rel)
    task.files_present = frozenset(present)
    for c in (c for c in files if c.share is not None):
        rel, dest = task.fmt(c.path), task.fmt(c.share or "")
        copy = share.unc(dest, _leaf(rel))
        if file_reserved(task.owner, rel, policy) or not copy or policy.path_reserved(copy):
            return f"{rel} or its copy {copy or '(LUDIUS_SHARE_HOST unset)'}: scenario-reserved or unknown"
        f, s = guestfile.read(task.owner, rel), share.read(dest, _leaf(rel))
        if f is None or s is None:
            return f"{rel if f is None else copy}: unreadable"
        if not f.exists:
            return f"{rel}: not there, nothing to copy"
        if s.exists:
            return f"{copy}: already exists, never overwritten"
        absent.add(copy)
    for src in sorted({task.fmt(c.moved_from) for c in files if c.moved_from is not None}):
        f = guestfile.read(task.owner, src)
        if f is None:
            return f"{src}: unreadable on the guest"
        if not f.exists:
            return f"{src}: not there, nothing to move"
        task.sources_before[src] = f.data
    for rel in sorted(set(attached)):
        f = guestfile.read(task.owner, rel)
        if f is None:
            return f"{rel}: unreadable on the guest"
        if not f.exists or len(f.data) > ATTACH_MAX_BYTES:
            return f"{rel}: not there or over {ATTACH_MAX_BYTES} bytes, nothing to attach"
        task.sources_before[rel] = f.data
    for src in sorted({task.fmt(c.export_of) for c in files if c.export_of is not None}):
        f = guestfile.read(task.owner, src)
        if f is None:
            return f"{src}: unreadable on the guest"
        if not f.exists:
            return f"{src}: not there, nothing to export"
        task.sources_before[src] = f.data
    for c in (c for c in files if c.extract_of is not None):
        rel, src = task.fmt(c.path), task.fmt(c.extract_of or "")
        f = guestfile.read(task.owner, src)
        if f is None:
            return f"{src}: unreadable on the guest"
        member = _zip_single(f.data) if f.exists else None
        if member is None or member[0].casefold() != _leaf(rel).casefold():
            return f"{src}: not an archive of exactly {_leaf(rel)}, nothing to extract"
        task.sources_before[rel] = member[1]
        parent = rel.rpartition("\\")[0]
        folder = guestfile.read(task.owner, parent)
        if folder is not None and folder.exists:
            return f"{parent}: a file, not a folder to extract into"
        if folder is not None:
            absent.add(rel)
    for c in (c for c in files if c.attachment_of):
        rel, (sender, subject) = task.fmt(c.path), (task.fmt(x) for x in c.attachment_of or [])
        got = mail.matching(task.owner, "inbox", subject, True)
        if got is None:
            return f"{subject!r}: inbox unreadable"
        address = f"{sender.strip().lower().partition('@')[0]}@{mail.DOMAIN}"
        hits = [m for m in got if m.sender.strip().lower() == address]
        if len(hits) != 1 or hits[0].attachments is None:
            return f"{subject!r} from {address}: not exactly one mail with readable attachments"
        if policy.mail_reserved(hits[0].sender, hits[0].subject, hits[0].body):
            return f"{subject!r} from {address}: scenario-reserved mail"
        sha = [h for n, h in hits[0].attachments if n == _leaf(rel).casefold()]
        if len(sha) != 1:
            return f"{subject!r}: no single attachment named {_leaf(rel)!r}"
        task.saved_sha[rel] = sha[0]
    edits = {task.fmt(c.path): task.fmt(c.source or c.path) for c in files if c.append is not None}
    appends = {task.fmt(c.path): c.append or "" for c in files if c.append is not None}
    for rel, src in sorted(edits.items()):
        f = guestfile.read(task.owner, src)
        lines = _doc_lines(src, f.data) if f is not None and f.exists else None
        if f is not None and not f.exists:
            return f"{src}: not there, nothing to edit"
        if lines is None or f is None:
            return f"{src}: unreadable on the guest"
        if rel.lower().endswith(".xlsx") and (why := _completes(lines, task.fmt(appends[rel]))):
            return f"{rel}: {why}"
        task.files_before[rel] = lines
        if src != rel:
            task.sources_before[src] = f.data
            n = guestfile.read(task.owner, rel)
            if n is None:
                return f"{rel}: unreadable on the guest"
            if n.exists:
                return f"{rel}: already exists, never overwritten"
            absent.add(rel)
    for rel in dict.fromkeys(task.fmt(c.path) for c in files if c.comment is not None):
        f = guestfile.read(task.owner, rel)
        if f is not None and not f.exists:
            return f"{rel}: not there, nothing to comment on"
        paras, notes = (guestfile.docx_paragraphs(f.data), guestfile.docx_comments(f.data)) if f else (None, None)
        if paras is None or notes is None:
            return f"{rel}: unreadable on the guest"
        task.files_before[rel], task.comments_before[rel] = [x.casefold() for x in paras], notes
    checked = [task.fmt(c.path) for c in files
               if c.append is None and c.comment is None and not c.recycled and c.share is None]
    if task.inst.request:
        checked += [w for w in write_paths(task) if not w.endswith(".*") and w not in edits]
    for rel in dict.fromkeys(r for r in checked if r not in absent):
        f = guestfile.read(task.owner, rel)
        if f is None:
            return f"{rel}: unreadable on the guest"
        if f.exists:
            return f"{rel}: already exists, never overwritten"
        absent.add(rel)
    task.files_absent = frozenset(absent)
    return None


def _file_ok(c: FileCheck, task: Task) -> bool:
    rel = task.fmt(c.path)
    if c.share is not None:
        dest, name = task.fmt(c.share), _leaf(rel)
        if task.files_absent is None or share.unc(dest, name) not in task.files_absent:
            return False
        s, f = share.read(dest, name), guestfile.read(task.owner, rel)
        return s is not None and s.exists and f is not None and f.exists and s.data == f.data
    if c.recycled:
        f = guestfile.read(task.owner, rel)
        return rel in task.files_present and f is not None and not f.exists
    if c.comment is not None:
        f, before = guestfile.read(task.owner, rel), task.comments_before.get(rel)
        if before is None or f is None or not f.exists or f.written_ms < task.started_ms:
            return False
        paras, notes = guestfile.docx_paragraphs(f.data), guestfile.docx_comments(f.data)
        if paras is None or notes is None or [x.casefold() for x in paras] != task.files_before.get(rel):
            return False
        new = Counter(notes) - Counter(before)
        want = " ".join(task.fmt(c.comment).split()).casefold()
        return Counter(before) <= Counter(notes) and [" ".join(x.split()).casefold() for x in new.elements()] == [want]
    if c.append is not None:
        if c.source is not None:
            src = task.fmt(c.source)
            s = guestfile.read(task.owner, src)
            unchanged = s is not None and s.exists and s.data == task.sources_before.get(src)
            if rel not in (task.files_absent or ()) or not unchanged:
                return False
        f = guestfile.read(task.owner, rel)
        before = task.files_before.get(rel)
        if before is None or f is None or not f.exists or f.written_ms < task.started_ms:
            return False
        if rel.lower().endswith(".xlsx"):
            got, want = _doc_lines(rel, f.data), _below(before, task.fmt(c.append))
            new = sorted(_fold_value(x) for x in set(got or []) - set(before))
            return got is not None and bool(want) and set(before) <= set(got) and new == sorted(want)
        add = _lines(task.fmt(c.append))
        ok = bool(add) and _doc_lines(rel, f.data) == before + add
        if ok and rel in task.sources_before and rel in map(task.fmt, task.template.attaches):
            task.sources_before[rel] = f.data
        return ok
    if task.files_absent is None or rel not in task.files_absent:
        return False
    f = guestfile.read(task.owner, rel)
    if c.moved_from is not None:
        s = guestfile.read(task.owner, task.fmt(c.moved_from))
        moved = f is not None and f.exists and f.data == task.sources_before.get(task.fmt(c.moved_from))
        return moved and s is not None and not s.exists
    if c.download is not None:
        return f is not None and f.exists and f.data == intranet.download_bytes(task.fmt(c.download))
    if c.extract_of is not None:
        return f is not None and f.exists and f.data == task.sources_before.get(rel)
    if c.attachment_of is not None:
        return f is not None and f.exists and hashlib.sha256(f.data).hexdigest() == task.saved_sha.get(rel)
    if f is None or not f.exists or f.written_ms < task.started_ms:
        return False
    if c.export_of is not None:
        s = guestfile.read(task.owner, task.fmt(c.export_of))
        unchanged = s is not None and s.exists and s.data == task.sources_before.get(task.fmt(c.export_of))
        if rel.lower().endswith(".zip"):
            return unchanged and _zip_holds(f.data, _leaf(task.fmt(c.export_of)), s.data if s else b"")
        if rel.lower().endswith(".xlsx"):
            cells, want = guestfile.xlsx_cells(f.data), _csv_cells(s.data if s else b"")
            return unchanged and bool(want) and cells is not None and _fold(cells) == _fold(want)
        return unchanged and f.data.startswith(b"%PDF-") and b"%%EOF" in f.data[-1024:]
    if c.cells is not None:
        cells = guestfile.xlsx_cells(f.data)
        want = guestfile.grid_cells(task.fmt(c.cells))
        return cells is not None and bool(want) and _fold(cells) == _fold(want)
    if c.paragraphs is not None:
        got, lines = guestfile.docx_paragraphs(f.data), _lines(task.fmt(c.paragraphs))
        return got is not None and bool(lines) and [x.casefold() for x in got] == lines
    slides = guestfile.pptx_slides(f.data)
    want = [w for s in c.slides or [] if (w := _lines(task.fmt(s)))]
    return slides is not None and bool(want) and [[x.casefold() for x in s] for s in slides] == want


def _zip_holds(data: bytes, name: str, want: bytes) -> bool:
    m = _zip_single(data)
    return m is not None and m[0].casefold() == name.casefold() and m[1] == want


def _csv_cells(data: bytes) -> dict[str, str]:
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error):
        return {}
    cells = ((f"{guestfile.column(i)}{r}", c) for r, row in enumerate(rows, 1) for i, c in enumerate(row))
    return {ref: _excel_value(c) for ref, c in cells if c}


def _excel_value(cell: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cell):
        return str((dt.date.fromisoformat(cell) - dt.date(1899, 12, 30)).days)
    return cell


def _zip_single(data: bytes) -> tuple[str, bytes] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            items = z.infolist()
            return (items[0].filename, z.read(items[0])) if len(items) == 1 and not items[0].is_dir() else None
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, EOFError):
        return None


def _leaf(rel: str) -> str:
    return re.split(r"[\\/]", rel)[-1]


def _doc_lines(rel: str, data: bytes) -> list[str] | None:
    if rel.lower().endswith(".xlsx"):
        cells = guestfile.xlsx_cells(data)
        return None if cells is None else sorted(f"{k}	{v}" for k, v in cells.items())
    if rel.lower().endswith(".docx"):
        got = guestfile.docx_paragraphs(data)
        return None if got is None else [x.casefold() for x in got]
    try:
        return _lines(data.decode("utf-8-sig").replace("\r", ""))
    except UnicodeDecodeError:
        return None


def _row(ref: str) -> int:
    return int(re.sub(r"^[A-Z]+", "", ref))


def _fold_value(cell: str) -> str:
    ref, v = cell.split("	", 1)
    return f"{ref}	{v.casefold()}"


def _below(before: list[str], rows: str) -> list[str]:
    last = max((_row(x.split("	")[0]) for x in before), default=0)
    cells = guestfile.grid_cells(rows)
    return [f"{re.sub(r'[0-9]+$', '', k)}{_row(k) + last}	{v.casefold()}" for k, v in cells.items()]


def _completes(before: list[str], rows: str) -> str | None:
    col: dict[str, set[str]] = {}
    for x in before:
        ref, v = x.split("	", 1)
        col.setdefault(re.sub(r"[0-9]+$", "", ref), set()).add(v.casefold())
    for ref, v in guestfile.grid_cells(rows).items():
        v = v.casefold()
        if v[:1].isalpha() and any(a != v and a.startswith(v) for a in col.get(re.sub(r"[0-9]+$", "", ref), ())):
            return f"Excel would complete {v!r} to a longer cell above it"
    return None


def _lines(text: str) -> list[str]:
    return [x.casefold() for x in text.split("\n") if x.strip()]


def _fold(cells: dict[str, str]) -> dict[str, str]:
    return {k: v.casefold() for k, v in cells.items()}
