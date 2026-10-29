"""Task templates, personas and task instances, plus code-side completion checks.

A template is a skeleton (variants of subgoal sequences); an instance fills its
parameters, from the planner or a hand-written YAML file.
"""

from __future__ import annotations

import datetime as dt
import functools
import random
import re
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ludius import guestfile, intranet, mail
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
    focused: bool | None = None  # has the keyboard focus (PowerPoint's text box after Ctrl+Enter)
    selected: bool | None = None  # is selected (PowerPoint's thumbnail of the slide being edited)
    # Only the item an `added` check recorded under this mark (its position, top to bottom, among that check's
    # items): an older item carrying the same name never counts. An unknown mark matches nothing.
    at: str | None = None


class AddedCheck(Strict):
    """An item was added during this subgoal: more elements match `items` than when the subgoal started (the
    runner's baseline, added_baseline), and exactly one of them is selected and matches `new`, e.g. the new untitled
    slide's thumbnail. Its position among the items is recorded as `mark` for later checks (ElementCheck.at).
    ponytail: positions are top-to-bottom among on-screen items; a list scrolled past its first items shifts them
    (fails closed: the marked item no longer matches). Mark by a stable item identity if lists grow long."""

    items: ElementCheck
    new: ElementCheck
    mark: str


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
    It must come from the persona running the task and be delivered after the task started (Task.owner and
    Task.started_ms, set by the runner): an older or seeded message with the same subject never counts.
    """

    mailbox: str
    folder: Literal["inbox", "sent"] = "inbox"
    subject: str = "{subject}"
    body: str = "{body}"
    above_quote: bool = False  # a reply: the body must come before the quoted original's From:/Sent: header
    reply_to: str = ""  # a reply: the answered mail's Internet message id (mail.delivered), e.g. "{reply_to}"


class InviteCheck(Strict):
    """A meeting the persona organised and sent (ludius.mail.invited), checked on the range server and never in the
    UI: the request in the attendee's Inbox and the appointment in the persona's Calendar, both new since the task
    started, with this subject, one attendee and the planned start and end. `date` (YYYY-MM-DD) and the HH:MM times
    are local to the persona's time zone (Persona.tz, the guest's), converted to UTC explicitly. `body`, when
    planned, is contained in the request."""

    mailbox: str = "{to}"
    subject: str = "{subject}"
    body: str = "{body}"
    date: str = "{date}"
    start: str = "{start}"
    end: str = "{end}"


class FileCheck(Strict):
    """A file the task creates, read on the guest and never in the UI (ludius.guestfile). It declares the template's
    write: `path` is profile-relative in a noise folder. The runner refuses to start the task if the file exists
    (a task never overwrites); done needs it written after the task started, holding exactly the planned content,
    case-insensitively (AutoCorrect capitalises, Excel's AutoComplete keeps an earlier cell's case). One content
    check, by the file's type: `cells` for .xlsx (planned rows, cells split by tabs: the workbook's only sheet),
    `paragraphs` for .docx (planned lines: the document's non-empty paragraphs in order), `slides` for .pptx (one
    entry per slide, its lines the title then each bullet: the deck's slides in order). `download` instead: the file
    holds exactly the bytes the range intranet serves for that download (ludius.intranet), any of its file types.
    """

    path: str
    cells: str | None = None
    paragraphs: str | None = None
    slides: list[str] | None = None
    download: str | None = None

    @model_validator(mode="after")
    def _noise_folder(self) -> FileCheck:
        need = {".xlsx": "cells", ".docx": "paragraphs", ".pptx": "slides"}
        ext = self.path.lower()[-5:]
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
    """All given conditions must hold. `typed` means the content landed in the target element.

    `pixels` applies only to pixels-only snapshots and comes from the template's pixels block. `mail` is read on
    the range server and `file` on the guest's disk, so they hold the same on any snapshot.
    """

    window: WindowCheck | None = None
    foreground: WindowCheck | None = None
    element: ElementCheck | None = None
    added: AddedCheck | None = None
    typed: bool = False
    pixels: PixelsCheck | None = None
    mail: MailCheck | None = None
    invite: InviteCheck | None = None
    file: FileCheck | None = None
    # mail.read: the mail picked at task start (reply.prepare_read: proven unread then) is marked read on the server.
    read: bool = False
    # A yes/no question for the Decider, on a screenshot: the subgoal's whole done check, for an on-screen state a
    # code check reads unreliably (an attendee added through a picker reads differently), or the outcome itself (a
    # chat post shown in its channel). Stands alone; fails closed: no screenshot, no answer or an unsure yes is no.
    ask: str | None = None

    @model_validator(mode="after")
    def _ask_alone(self) -> Done:
        if self.ask is not None and self.model_dump(exclude={"ask"}, exclude_defaults=True):
            raise ValueError("done.ask stands alone: the Decider's answer is the whole check")
        return self


class Subgoal(Strict):
    goal: str
    text: str | None = None  # content key whose value may be typed
    # Navigation text the template writes (never Decisions or an LLM: growth refuses it), e.g. "save as" for an app's
    # search box: the text to type in a subgoal without `text`. One printable ASCII line, no Enter; the scenario
    # policy checks it at plan load (planner.item_problem) and on every step (options, loop._post_check).
    search: str = Field(default="", pattern=r"^[ -~]{0,40}$")
    keys: list[str] = Field(default_factory=list[str], max_length=16)  # extra shortcuts; capped: always offered
    launch: bool = False  # may use the shell (Start, taskbar, Run) to open apps
    copy_text: bool = False  # may copy text visible on screen into the focused field (type:copy[n])
    allow_destructive: list[str] = Field(default_factory=list[str])  # element names (regex) allowed despite the hint
    # Preconditions for any input in this subgoal (rescans excepted): every `require` check matches an element on
    # screen and no `forbid` check does, e.g. the To field holds exactly the planned address and Cc is empty.
    require: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    # The text target exposes no readable value (Outlook's Word body): typing there counts as landed and the
    # subgoal's own check decides. Off everywhere else, where an unchanged value means the text did not land.
    unreadable_ok: bool = False
    needs: str | None = None  # an optional parameter (one with a default): the subgoal is skipped when it is empty
    # Where a click in a document leaves the caret: at the end (writers append), or at the start (a reply goes
    # above the quoted mail).
    caret: Literal["end", "start"] = "end"
    # The target holds a value the typed text replaces (Outlook's prefilled meeting date and times): select all
    # (Ctrl+A) in the focused field right before typing, or the text is inserted into the old value.
    replace: bool = False
    forbid: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    # Elements never offered as a target in this subgoal, e.g. Outlook's Info Bar, whose click opens the
    # conversation's latest message and took the caret away from the reply being typed.
    avoid: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    done: Done | None = None
    steps: int = 12
    pixels_hint: str = ""  # from the template's pixels hints

    @model_validator(mode="after")
    def _one_text(self) -> Subgoal:
        if self.text and self.search:
            raise ValueError("a subgoal types either a content value (text) or template text (search), not both")
        return self


# The shared send contract (AGENTS.md: one shared layer for anything that sends mail). A template whose capability
# sends (SENDS) gets it from its app profile's form: every subgoal whose done check is a mail or meeting check then
# allows no input unless the form's recipient field holds exactly the planned colleague (as typed, or as the app
# resolves it) and every `empty` field is readable and empty, all in the foreground window; no `empty` or
# `never_filled` field anywhere holds a value. preconditions() reads them readable-only and ASCII-only, so an unknown
# or unreadable field is never empty. A profile only says where the fields are: these rules and values are fixed here.
# Defense in depth: the done check requires the Sent Items copy to name {to} alone (ludius.mail), and srv01 refuses
# non-local recipients.
RECIPIENT = "^({to}|{to_name}|{to_name} <{to}>);?$"
# file.write: a new file; mail.read: a colleague's Inbox mail opened, its only effect the read flag (never sends)
# chat.post / chat.dm: one line posted in the range chat (Mattermost), irreversible for noise (never deleted);
# chat.read: a direct message conversation opened, no effect beyond Mattermost's read marker
Capability = Literal[
    "mail.send",
    "mail.reply",
    "mail.read",
    "calendar.invite",
    "file.move",
    "file.write",
    "file.download",
    "chat.post",
    "chat.dm",
    "chat.read",
]
SENDS: dict[str, Literal["mail", "meeting"]] = {"mail.send": "mail", "mail.reply": "mail", "calendar.invite": "meeting"}
# Values a capability's parameters name rather than create (a recipient, the subject a reply answers, a rename's
# source): they may repeat, so the planner's near-duplicate check skips them.
REFERS_TO_EXISTING: dict[str, set[str]] = {
    "mail.send": {"to", "to_name"},
    "mail.reply": {"to", "to_name", "subject"},
    "mail.read": {"sender", "subject"},
    "calendar.invite": {"to", "to_name"},
    "file.move": {"file_name", "folder_name"},
    "chat.post": {"channel"},
    "chat.dm": {"to", "to_name"},
    "chat.read": {"to", "to_name"},
}
APPS = Path(__file__).resolve().parents[2] / "data" / "apps"
# Buttons that commit something: no discard or leave word in a profile may name one (abandon clicks those).
COMMITS = ["send", "send anyway", "save", "yes", "ok", "replace", "delete"]
# The text a launching subgoal types: only ever into the shell's Start search box (options.launch_target).
LAUNCH_TEXT = "launch_query"


def declared_commits(task: Task, sg: Subgoal) -> list[str]:
    """Names (regex) of committing or destructive controls the subgoal may activate by key, as declared: its own
    allow_destructive, and Save when the template declares the files it writes. Empty: it declares no such effect."""
    return [*sg.allow_destructive, *(["Save"] if task.template.writes else [])]


class Confirm(Strict):
    prompt: str  # regex, the prompt's whole text
    button: str  # exact name of the button that answers it


class Form(Strict):
    """Where an app keeps one send form's fields (selectors without values: the contract sets those)."""

    recipients: Literal["value"] = "value"  # how the recipient field is read: its text value (chips: not yet)
    to: ElementCheck
    empty: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    never_filled: list[ElementCheck] = Field(default_factory=list[ElementCheck])
    avoid: list[ElementCheck] = Field(default_factory=list[ElementCheck])  # never a target in the task
    confirm: list[Confirm] = Field(default_factory=list[Confirm])  # prompts the guarded send raises (send_confirmation)

    @model_validator(mode="after")
    def _selectors(self) -> Form:
        if any(c.value is not None or not c.name for c in [self.to, *self.empty, *self.never_filled]):
            raise ValueError("form fields are selectors: a name and no value")
        return self

    def contract(self) -> tuple[list[ElementCheck], list[ElementCheck], list[ElementCheck]]:
        """(require, forbid, avoid) for the subgoals that send."""
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
        # OK only confirms the discard choice already selected; any other committing button is refused.
        if self.choice_ok.lower() != "ok" and any(self.choice_ok.lower() == c for c in COMMITS):
            raise ValueError(f"choice_ok {self.choice_ok!r} names a button that commits")
        return self


class Overwrite(Strict):
    text: str
    titles: str
    safe: str


class Profile(Strict):
    """How one app does what the capabilities need (data/apps/<app>.yaml over default.yaml)."""

    process: str = ""
    launch_query: str = ""
    keys: dict[str, str]
    locale: dict[Literal["date"], str]
    dialogs: Dialogs
    forms: dict[Literal["mail", "meeting"], Form] = Field(default_factory=dict[Literal["mail", "meeting"], Form])
    overwrite: Overwrite

    @model_validator(mode="after")
    def _safe_close(self) -> Profile:
        # Abandon presses this without any send, reserved or overwrite check: only keys that close or cancel.
        if self.keys.get("close") not in CLOSE_KEYS:
            raise ValueError(f"close key {self.keys.get('close')!r} is not one of {sorted(CLOSE_KEYS)}")
        return self


CLOSE_KEYS = {"Alt+F4", "Esc", "Ctrl+W"}


def _merge(base: Any, own: Any) -> Any:
    """`own` over `base`: mappings merged, lists extended, anything else replaced."""
    if isinstance(base, dict) and isinstance(own, dict):
        b, o = cast(dict[str, Any], base), cast(dict[str, Any], own)
        return {**b, **{k: _merge(b.get(k), v) for k, v in o.items()}}
    if isinstance(base, list) and isinstance(own, list):
        return [*cast(list[Any], base), *cast(list[Any], own)]
    return own


@functools.cache
def profile(app: str) -> Profile:
    """The app's profile over the shared one; an app without a file (or a name that isn't one) gets the shared one."""
    base = yaml.safe_load((APPS / "default.yaml").read_text(encoding="utf-8"))
    path = APPS / f"{app}.yaml"
    mine = app != "default" and re.fullmatch(r"[a-z0-9_]+", app) and path.exists()
    own: Any = yaml.safe_load(path.read_text(encoding="utf-8")) if mine else {}
    if "overwrite" in own:
        raise ValueError(f"{path}: the overwrite guard is range-wide, not an app's")
    return Profile.model_validate(_merge(base, own))


NAME_PARAMS = {"file_name", "folder_name", "new_name"}  # parameters that name a file or folder in the profile


# What each saving app appends to the typed file name (its Save As default type).
SAVE_EXTENSIONS = {"notepad": ".txt", "wordpad": ".rtf", "paint": ".png", "excel": ".xlsx", "word": ".docx",
                   "powerpoint": ".pptx"}  # fmt: skip


class KeyCommit(Strict):
    keys: str
    focus: ElementCheck


class Template(Strict):
    task: str
    app: str
    capability: Capability | None = None  # what the task does; its app profile says how (profile)
    # Foreground processes the task may act in (shell is added by `launch`); default: the profile's process.
    processes: list[str] = Field(default_factory=list[str])
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
    plan_weight: float = Field(default=1.0, gt=0, le=1)  # below 1: the planner's catalog asks for it less often
    pixels: Pixels | None = None  # pixels-only mode opt-in; without it the task gets no rows in that mode
    # What the task creates, profile-relative in a noise folder, as the app writes it (with the extension it adds;
    # ".*" for one it keeps, an Explorer rename). The shared reservation check (file_reserved) resolves these.
    writes: list[str] = Field(default_factory=list[str])
    # Keys that commit while a matching element has the focus (Enter in a chat composer sends): offered only in a
    # subgoal with `require` checks (its guarded send), never elsewhere and never with the focus unknown.
    key_commits: list[KeyCommit] = Field(default_factory=list[KeyCommit])

    @property
    def sends_mail(self) -> bool:
        """The shared send contract applies (the profile form's Form.contract); required with a mail check."""
        return self.capability in SENDS

    @property
    def send_form(self) -> Literal["mail", "meeting"]:
        return SENDS.get(self.capability or "", "mail")

    @property
    def task_content(self) -> Literal["reply"] | None:
        """Content written at task time, not plan time (ludius.reply): a reply picks a real inbox mail and fills to,
        to_name, subject, body and reply_to before any input; its mail check must then require the reply link."""
        return "reply" if self.capability == "mail.reply" else None

    @model_validator(mode="after")
    def _profile(self) -> Template:
        """Process, launch query and named keys (`keys: [send]`) from the app profile, where the template has none."""
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
        """A template that names something it creates must declare where it lands; every write is in a noise folder,
        and every file done check reads a declared write."""
        if set(self.params) & NAME_PARAMS and not self.writes:
            raise ValueError(f"{self.task}: creates {sorted(set(self.params) & NAME_PARAMS)} but declares no writes")
        for w in self.writes:
            if guestfile.profile_path("u", w.removesuffix(".*")) is None:
                raise ValueError(f"{self.task}: write {w!r} is not in a noise folder {guestfile.NOISE_FOLDERS}")
            # The extension is the app's, not the template's: a write without it would check `x`, not the `x.txt`
            # the app saves, and miss a reservation (re-check of 97820c6).
            ext = SAVE_EXTENSIONS.get(self.app)
            if ext and "{file_name}" in w and not w.lower().endswith(ext):
                raise ValueError(f"{self.task}: {self.app} saves {ext}, so write {w!r} must end with it")
        marks = {sg.done.added.mark for sg in self.subgoals.values() if sg.done and sg.done.added}
        for name, sg in self.subgoals.items():
            if sg.done and sg.done.element and sg.done.element.at not in (None, *marks):
                raise ValueError(
                    f"{self.task}: {name} checks the item at {sg.done.element.at!r}, which no added check marks"
                )
        for sg in self.subgoals.values():
            if sg.done and sg.done.file and sg.done.file.path not in self.writes:
                raise ValueError(f"{self.task}: file check {sg.done.file.path!r} is not a declared write")
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
    tz: str = "Europe/Stockholm"  # IANA zone of the persona's guest: what typed dates and times mean (meetings)


class TaskInstance(Strict):
    template: str
    params: dict[str, str] = Field(default_factory=dict[str, str])
    content: dict[str, str] = Field(default_factory=dict[str, str])
    variant: int | None = None
    context: str = ""  # one line of narrative for Decisions: why the persona is doing this
    request: str = ""  # Internet message id of the manager's mail this task does (ludius.requests)


class Task:
    """A template bound to an instance: formatted subgoals for the chosen variant."""

    def __init__(self, template: Template, inst: TaskInstance, rng: random.Random) -> None:
        have = {**template.defaults, **inst.params, **inst.content}
        # A parameter with a default is optional (a meeting's body); a subgoal typing it is skipped when it is empty.
        missing = [p for p in template.params if not have.get(p) and p not in template.defaults]
        if missing:
            raise ValueError(f"{template.task}: missing parameters {missing}")
        self.template = template
        self.inst = inst
        self.owner = ""  # persona account and task start (guest clock, Unix ms): set by the runner
        self.started_ms = 0
        self.tz = "Europe/Stockholm"  # the persona's time zone (Persona.tz), set by the runner
        self.mail_before: mail.Seen | None = None  # mail present at task start (mail_baseline), set by the runner
        self.files_absent: frozenset[str] | None = None  # file checks' paths proven absent at start (file_baseline)
        self.read_id = 0  # mail.read: the Inbox message proven unread at start (reply.prepare_read); 0: none
        self.added_base: int | None = None  # items matching the current subgoal's added check at its start
        self.marks: dict[str, tuple[ElementCheck, int]] = {}  # AddedCheck.mark -> (its items, the new one's position)
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
        # Mistake variants (spec section 6): `{p}_typo` is the value with one stray neighbouring key at the end,
        # typed first and then fixed (Backspace, rename) by a later subgoal.
        self.values = have | {f"{k}_typo": v + _neighbour(v[-1]) for k, v in have.items() if v and "\n" not in v}
        # `{p}_enter`: the value ending in one Enter, so a spreadsheet's last cell is committed, not left in edit mode.
        self.values |= {f"{k}_enter": v.rstrip("\n") + "\n" for k, v in have.items() if v}
        # `{p}_ui`: an ISO date as the app's locale types and shows it (en-GB Outlook reads 2026-11-05 as 11 May).
        date = profile(template.app).locale["date"]
        self.values |= {f"{k}_ui": date.format(y=v[:4], m=v[5:7], d=v[8:]) for k, v in have.items()
                        if ISO_DATE.fullmatch(v)}  # fmt: skip
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
        return self.values.get(sg.text) if sg.text else sg.search or None


ISO_DATE = re.compile(r"\d{4}-\d\d-\d\d")
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


def check_done(done: Done | None, snap: obs.Snapshot, task: Task, typed_ok: bool, outside: bool = True) -> bool | None:
    """True/False when the subgoal has a code check, None when it has none (Decisions decides). `outside=False`: the
    checks on screen only (None when there are none), so the runner can wait for the server instead of acting."""
    if done is None or done.ask:
        return None  # no code check: Decisions, or the Decider's done question (loop), decides
    if (done.mail or done.invite or done.file or done.read) and not (
        done.window or done.foreground or done.element or done.added or done.typed or done.pixels
    ):
        return _outside_ok(done, task) if outside else None
    on_pixels = any(pixels_window(w) for w in snap.windows)
    if (
        done.pixels
        and not on_pixels
        and not (done.window or done.foreground or done.element or done.added or done.typed)
    ):
        return None  # a completion only pixels mode checks: with an accessibility tree, Decisions decides as before
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
        candidates = snap.elements if c.at is None else _marked(c.at, snap, task)
        if not any(element_matches(e, c, task) for e in candidates):
            return False
    if done.added and not _added_ok(done.added, snap, task):
        return False
    return _outside_ok(done, task) if outside else True


def _items(c: ElementCheck, snap: obs.Snapshot, task: Task) -> list[obs.Element]:
    """Elements matching `c`, top to bottom (then left to right): their order on screen, not in the snapshot."""
    return sorted((e for e in snap.elements if element_matches(e, c, task)), key=lambda e: (e.bounds.y, e.bounds.x))


def added_baseline(done: Done | None, snap: obs.Snapshot, task: Task) -> None:
    """At a subgoal's start (the runner): how many items its added check sees, so only a new one counts."""
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
    """The checks read outside the UI (range server, guest disk); last, since they cost a remote call."""
    return ((done.mail is None or _mail_ok(done.mail, task)) and (done.invite is None or _invite_ok(done.invite, task))
            and (done.file is None or _file_ok(done.file, task)) and (not done.read or _read_ok(task)))  # fmt: skip


def _read_ok(task: Task) -> bool:
    """mail.read: the mail proven unread at task start (Task.read_id) is marked read on the server now."""
    return task.read_id > 0 and bool(task.owner) and mail.read_state(task.owner, task.read_id) is True


def element_matches(e: obs.Element, c: ElementCheck, task: Task, flags: int = re.I) -> bool:
    # Edit and RichEdit values end lines with \r or \r\n; multi-line params use \n.
    value = re.sub(r"\r\n?", "\n", e.value)
    return (
        (not c.role or e.role == c.role)
        and (not c.name or re.search(task.fmt_re(c.name), e.name, flags) is not None)
        and (not c.value or re.search(task.fmt_re(c.value), value, flags) is not None)
        and (c.focused is None or e.focused == c.focused)
        and (c.selected is None or e.selected == c.selected)
    )


def _guard_matches(e: obs.Element, c: ElementCheck, task: Task) -> bool:
    """A require check reads the field itself: only an element exposing its value (UIA Value pattern) counts, since
    an unreadable field reads "" and would pass as empty; and only ASCII, case-folded as ASCII, so a lookalike such
    as mvellınga (dotless i, which Unicode re.I folds to i) is not the planned address."""
    # ponytail: ASCII only; a colleague whose name needs non-ASCII (Åberg) can't be mailed until guards fold Unicode
    # without the lookalikes.
    return "value" in e.actions and e.value.isascii() and element_matches(e, c, task, re.I | re.A)


def preconditions(sg: Subgoal, snap: obs.Snapshot, task: Task, target: obs.Element | None = None) -> str | None:
    """Why input is not allowed in this subgoal right now, or None when its require/forbid checks hold.

    Every require check must hold in the foreground window, and a clicked or typed-into `target` must be in that
    same window: fields in an owned dialog or popup must not authorise the foreground window's Send.
    """
    fg = next((i for i, w in enumerate(snap.windows) if w.kind == obs.Window.FOREGROUND), None)
    if sg.require and target is not None and target.window != fg:
        return "precondition: the target is not in the foreground window"
    for c in sg.require:
        if not any(e.window == fg and _guard_matches(e, c, task) for e in snap.elements):
            seen = [
                e.value[:80]
                for e in snap.elements
                if e.window == fg and element_matches(e, c.model_copy(update={"value": None}), task)
            ]
            return (f"precondition: no readable {c.name or c.role!r} field with value {task.fmt(c.value or '')!r}"
                    f" (read: {seen!r})")  # fmt: skip
    for c in sg.forbid:
        if any(element_matches(e, c, task) for e in snap.elements):
            return f"precondition: {c.name or c.role!r} must not match {task.fmt(c.value or '')!r}"
    return None


def _mail_ok(c: MailCheck, task: Task) -> bool:
    sender = f"{task.owner}@{mail.DOMAIN}" if task.owner else ""
    return mail.delivered(task.fmt(c.mailbox), c.folder, task.fmt(c.subject), task.fmt(c.body), sender,
                          task.started_ms, c.above_quote, task.mail_before, task.fmt(c.reply_to))  # fmt: skip


def local_ms(date: str, hhmm: str, tz: str) -> int:
    """A local date and time in the IANA zone `tz` as Unix ms (UTC)."""
    return int(dt.datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def _invite_ok(c: InviteCheck, task: Task) -> bool:
    if not task.owner:
        return False
    date = task.fmt(c.date)
    return mail.invited(task.fmt(c.mailbox), task.fmt(c.subject), task.fmt(c.body), f"{task.owner}@{mail.DOMAIN}",
                        task.started_ms, local_ms(date, task.fmt(c.start), task.tz),
                        local_ms(date, task.fmt(c.end), task.tz), task.mail_before)  # fmt: skip


def _sent_check(done: Done | None) -> tuple[str, str, str, str] | None:
    """(mailbox, recipient's folder, subject, sender's own folder) of a subgoal's mail or meeting done check."""
    if done and done.mail:
        return done.mail.mailbox, done.mail.folder, done.mail.subject, "sent"
    if done and done.invite:
        return done.invite.mailbox, "inbox", done.invite.subject, "calendar"
    return None


def mail_baseline(task: Task) -> mail.Seen | None:
    """What every mail or meeting done check of the task could already match at its start (mail.present); None if
    unread."""
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
    """The button that confirms a prompt the guarded send raised (its form's confirm), in a foreground window of the
    task's app; None for any other screen. The caller clicks it only right after an input the contract allowed."""
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
    """The shared send contract's policy check, at send time: a plan made before a mailbox became reserved must
    not send to it. `policy` is a ludius.policy.Policy (which imports this module)."""
    return bool((c := _sent_check(sg.done)) and policy.reserved(task.fmt(c[0])))


def write_paths(task: Task) -> list[str]:
    """What the task creates (its declared writes), profile-relative, as the app writes them."""
    return [task.fmt(w) for w in task.template.writes]


def file_reserved(user: str, rel: str, policy: Any) -> bool:
    """The shared write rule: a created file or folder's final path, relative and in `user`'s profile (when known),
    must not be scenario-reserved; checked at generation, plan load and task start (before any guest read).
    `policy` is a ludius.policy.Policy."""
    any_ext = ".*" if rel.endswith(".*") else ""
    full = guestfile.profile_path(user, rel.removesuffix(".*")) if user else None
    return bool(policy.path_reserved(rel) or (full and policy.path_reserved(full + any_ext)))


def file_baseline(task: Task, policy: Any) -> str | None:
    """Why the task must not start (something it would create is reserved; a file it checks exists or can't be
    read), else None; sets Task.files_absent. The runner calls it before any input: a task never overwrites a file,
    and a reserved one is never even read."""
    for rel in write_paths(task):
        if file_reserved(task.owner, rel, policy):
            return f"{rel}: scenario-reserved file"
    absent: set[str] = set()
    checked = [task.fmt(sg.done.file.path) for sg in task.template.subgoals.values() if sg.done and sg.done.file]
    if task.inst.request:  # the request check (ludius.requests) reads what the task writes: new, never pre-existing
        checked += [w for w in write_paths(task) if not w.endswith(".*")]
    for rel in dict.fromkeys(checked):
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
    if task.files_absent is None or rel not in task.files_absent:
        return False  # not proven absent (and not reserved) at start: pre-existing state, or never to be read
    f = guestfile.read(task.owner, rel)
    if c.download is not None:  # absent at start is the proof it is new: Edge sets its write time only on close
        return f is not None and f.exists and f.data == intranet.download_bytes(task.fmt(c.download))
    if f is None or not f.exists or f.written_ms < task.started_ms:
        return False
    if c.cells is not None:
        cells = guestfile.xlsx_cells(f.data)
        want = guestfile.grid_cells(task.fmt(c.cells))
        return cells is not None and bool(want) and _fold(cells) == _fold(want)
    if c.paragraphs is not None:
        got, lines = guestfile.docx_paragraphs(f.data), _lines(task.fmt(c.paragraphs))
        return got is not None and bool(lines) and [x.casefold() for x in got] == lines
    slides = guestfile.pptx_slides(f.data)
    want = [w for s in c.slides or [] if (w := _lines(task.fmt(s)))]  # a slide whose optional values are empty: none
    return slides is not None and bool(want) and [[x.casefold() for x in s] for s in slides] == want


def _lines(text: str) -> list[str]:
    return [x.casefold() for x in text.split("\n") if x.strip()]


def _fold(cells: dict[str, str]) -> dict[str, str]:
    return {k: v.casefold() for k, v in cells.items()}
