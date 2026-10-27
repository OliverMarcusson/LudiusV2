"""Planning layer (spec section 6): company narrative, nightly day plans with all content, near-duplicate check.

The LLM writes the narrative once per range and one plan per persona per day, from the narrative plus the
actual world state (what the ground truth says happened). Every plan is validated in code before it runs:
known templates, complete parameters, policy-clean ASCII text, and no content too close to earlier output.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import random
import re
import tempfile
import time
from collections import Counter
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator

from ludius.llm import LlmClient
from ludius.mail import DOMAIN as MAIL_DOMAIN
from ludius.policy import Policy
from ludius.tasks import Persona, Strict, Task, TaskInstance, Template

BREAK = "break"  # pseudo-template: away from the desk, screen locked
REPLY = "outlook_reply_mail"  # answers a real inbox mail: the planner sees mail only as follow-ups
NEAR_DUPLICATE = 0.85  # SequenceMatcher ratio at or above this is "too similar"
MIN_COMPARE = 12  # shorter values (app names, short queries) may repeat
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PRINTABLE = re.compile(r"^[\x20-\x7e\n]*$")  # the virtual keyboard types ASCII only
VALUE_MAX = 300  # guest observer Snapshotter.MaxValue: longest element value a snapshot carries
_NAME_KEYS = ("file_name", "folder_name", "new_name")
# Values that name something existing rather than new content: a rename's source, a mail's recipient, the subject
# of the mail a reply answers. They may repeat, so the near-duplicate check skips them.
_REFERS_TO_EXISTING = {
    "explorer_rename_move": {"file_name", "folder_name"},  # acts on these, creates new_name only
    "outlook_send_mail": {"to", "to_name"},
    "outlook_reply_mail": {"to", "to_name", "subject"},
}
_DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)  # Windows reserved, any extension
SHOWN_EXISTING = 80  # existing names listed in the prompt; validation checks all of them


class Person(Strict):
    name: str
    role: str
    department: str
    user: str = ""  # Windows account when this person is a persona


class Event(Strict):
    date: str  # YYYY-MM-DD
    what: str


class Narrative(Strict):
    company: str
    city: str
    summary: str
    people: list[Person]
    projects: list[str]
    events: list[Event]
    revised: str = ""  # YYYY-MM-DD of the last weekly revision (spec section 6); "" until the first
    history: list[Event] = Field(default_factory=list[Event])  # dated world-state updates, one per revision


class PlanItem(TaskInstance):
    at: str  # HH:MM on the plan's logical timeline
    minutes: int = 0  # breaks: how long the persona is away
    interrupt: bool = False  # cuts into the task running at that time, at its next subgoal boundary
    handoff: str = ""  # colleague's account: the result lands on their later plan as a thread to pick up
    followup: str = ""  # id of the open follow-up (world_state) this item settles

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("at must be HH:MM")
        return v


class Scenario(Strict):
    """Where an injected scenario lands, so legitimate look-alike work is planned near it (spec section 6)."""

    date: str  # YYYY-MM-DD
    start: str  # HH:MM
    end: str  # HH:MM
    signal: str  # matches Template.resembles: powershell, rdp, macro ...

    @classmethod
    def parse(cls, s: str) -> Scenario:
        """'2026-10-28T13:00-14:00:powershell'"""
        m = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)-(\d\d:\d\d):(\w+)", s)
        if not m:
            raise ValueError("scenario must look like 2026-10-28T13:00-14:00:powershell")
        return cls(date=m[1], start=m[2], end=m[3], signal=m[4])


class _Draft(Strict):
    items: list[PlanItem]


class DayPlan(Strict):
    persona: str
    date: str
    items: list[PlanItem]


# Prompts ------------------------------------------------------------------------------

NARRATIVE_SYSTEM = (
    "You write the backstory of a fictional, ordinary small company for a cyber-defence training range. "
    "It must read as mundane and plausible: real-sounding departments, projects and calendar events. "
    "No security incidents, no hacking, no real companies or real people."
)

PLAN_SYSTEM = (
    "You plan one ordinary working day for one employee of a fictional company in a training range. "
    "Every item is a task from the catalog, done for a concrete reason that follows from the company story, "
    "the persona's role and what really happened before. Write all content (file names, note text, search "
    "queries) in the persona's own style. Use plain ASCII only. Never mention the range, training, Ludius, "
    "security tools or anything outside the fiction."
)


DERIVED = {"to_name"}  # filled in code from the narrative (fill_recipient_names), never written by the LLM
_NOT_A_NAME = re.compile(r"[;,<>@]")  # a display name holding a second recipient or an address


def catalog(templates: dict[str, Template]) -> str:
    lines = [f"- {t.task}: {t.about} Parameters: {', '.join(p for p in t.params if p not in DERIVED) or 'none'}."
             for t in templates.values()]  # fmt: skip
    lines.append(f"- {BREAK}: away from the desk (lunch, coffee, meeting), screen locked. Set minutes.")
    return "\n".join(lines)


def make_narrative(llm: LlmClient, personas: list[Persona], start: dt.date) -> Narrative:
    who = "\n".join(f"- {p.name} (account {p.user}): {p.role}, {p.department}" for p in personas)
    user = (
        f"Range start date: {start.isoformat()}.\nThese employees are personas and must appear in people with their "
        f"account as user:\n{who}\n\nReturn JSON with keys company, city, summary (3-5 sentences), people "
        "(8-15 entries: name, role, department, user), projects (3-6 short strings) and events (6-12 entries: "
        "date YYYY-MM-DD within four weeks of the start, what). Include the routine (month-end close, team "
        "meetings) and a few one-offs (an office move, a supplier change, a system migration)."
    )
    return llm.json(Narrative, NARRATIVE_SYSTEM, user)


def plan_day(
    llm: LlmClient,
    narrative: Narrative,
    persona: Persona,
    date: dt.date,
    templates: dict[str, Template],
    world: str,
    history: list[str],
    policy: Policy | None = None,
    scenario: Scenario | None = None,
    followups: dict[str, str] | None = None,
    existing: Iterable[str] = (),
    mail_threads: dict[str, tuple[str, str]] | None = None,
) -> tuple[DayPlan, list[str]]:
    """Draft, validate, ask once to replace rejected items, drop what still fails. Returns the plan and rejects.

    `followups` (id -> what) are open tasks colleagues handed over; each must be scheduled with its id.
    `mail_threads` (follow-up id -> sender, subject) are the mail ones, the only threads a reply may answer.
    `existing` are files and folders already on the persona's profile (names or paths): no item may create or
    rename to one of them, and an item that still does after the one repair is dropped, never run.
    """
    policy = policy or Policy()
    followups = followups or {}
    templates = role_templates(templates, persona)
    window = scenario if scenario and scenario.date == date.isoformat() else None
    lookalikes = sorted(k for k, t in templates.items() if window and t.resembles == window.signal)
    colleagues = {p.user for p in narrative.people if p.user and p.user != persona.user}
    people = {p.user: p.name for p in narrative.people if p.user}
    existing = list(existing)
    names = sorted({_leaf(e) for e in existing if _leaf(e)}, key=str.casefold)[:SHOWN_EXISTING]
    user = (
        f"Company story:\n{narrative.model_dump_json(indent=1)}\n\n"
        f"Persona: {persona.name}, {persona.role} ({persona.department}). Usual hours {persona.hours[0]}-"
        f"{persona.hours[1]}.\nStyle and habits: {persona.style or 'ordinary'}\n\n"
        f"Day to plan: {date.strftime('%A')} {date.isoformat()}.\n\n"
        f"What happened so far:\n{world or '- nothing yet'}\n\n"
        f"Task catalog:\n{catalog(templates)}\n\n"
        f"Recent content the persona already wrote (do not repeat or paraphrase it):\n"
        + ("\n".join(f"- {h[:120]}" for h in history[-40:]) or "- none")
        + (
            "\n\nThese files and folders already exist for the persona. Never create, save or rename anything to one "
            "of these names, with or without an extension; only explorer_rename_move may name one of them as the "
            "existing file_name or folder_name it acts on:\n" + "\n".join(f"- {n}" for n in names)
            if names
            else ""
        )
        + '\n\nReturn JSON {"items": [...]} with 5-10 items inside the usual hours, sorted by time. Each item: '
        "at (HH:MM), template (a catalog name), params (object with every listed parameter as a string), "
        "context (one sentence: why the persona does this now), and for breaks minutes instead of params. "
        "Include a lunch break. Mark at most one short item interrupt: true when it plausibly cuts into the "
        "task before it (a quick lookup prompted by that work)."
        + (
            f" Colleagues with accounts: {', '.join(sorted(colleagues))}. When an item produces something one of "
            "them should act on (a note or folder for them, a lookup they asked for), set handoff to their account. "
            "Pick up threads colleagues handed to this persona. Mail (outlook_*) goes to one colleague: to is "
            f"<account>@{MAIL_DOMAIN} and handoff is that account; reply only to a due follow-up's mail."
            if colleagues
            else ""
        )
        + (
            f" Part of this persona's normal job ({', '.join(lookalikes)}) is due today: plan one or two such "
            f"items within 90 minutes before {window.start} or after {window.end}, never between them, each with "
            "a concrete work reason from the company story."
            if window and lookalikes
            else ""
        )
        + (
            " These follow-ups from colleagues are due: schedule each as one item (a fitting catalog task, often "
            "early in the day) with followup set to its id:\n" + "\n".join(f"- {k}: {v}" for k, v in followups.items())
            if followups
            else ""
        )
    )
    draft = llm.json(_Draft, PLAN_SYSTEM, user)
    fill_recipient_names(draft.items, people)
    rejects = validate(draft.items, templates, history, policy, colleagues, window, set(followups), existing,
                       mail_threads, people)  # fmt: skip
    missing = sorted(set(followups) - {it.followup for it in draft.items})
    if rejects or missing:
        fix = "These items were rejected; return the full corrected item list:\n" + "\n".join(
            [f"- item {i} ({draft.items[i].template} at {draft.items[i].at}): {why}" for i, why in rejects]
            + [f"- follow-up {k} is not scheduled" for k in missing]
        )
        draft = llm.json(_Draft, PLAN_SYSTEM, user + "\n\n" + fix)
        fill_recipient_names(draft.items, people)
        rejects = validate(draft.items, templates, history, policy, colleagues, window, set(followups), existing,
                           mail_threads, people)  # fmt: skip
    bad = {i for i, _ in rejects}
    items = sorted((it for i, it in enumerate(draft.items) if i not in bad), key=lambda it: it.at)
    missing = sorted(set(followups) - {it.followup for it in items})
    return DayPlan(persona=persona.user, date=date.isoformat(), items=items), [
        f"{draft.items[i].template} at {draft.items[i].at}: {why}" for i, why in rejects
    ] + [f"follow-up {k}: not scheduled, stays open" for k in missing]


# Validation ---------------------------------------------------------------------------


def role_templates(templates: dict[str, Template], persona: Persona) -> dict[str, Template]:
    """Signal-like templates only for personas whose risk profile includes them; the rest are unknown to the plan."""
    return {k: t for k, t in templates.items() if not t.resembles or k in persona.risk_profile}


def validate(
    items: list[PlanItem],
    templates: dict[str, Template],
    history: list[str],
    policy: Policy,
    colleagues: set[str] | None = None,
    scenario: Scenario | None = None,
    followups: set[str] | None = None,
    existing: Iterable[str] = (),
    mail_threads: dict[str, tuple[str, str]] | None = None,
    people: dict[str, str] | None = None,
) -> list[tuple[int, str]]:
    """Rejected items with the reason. A reply must answer a mail follow-up in `mail_threads` from its `to`
    colleague with its subject; without mail threads no reply is valid. A mail's to_name must be exactly the
    narrative name (`people`, account -> name) of its `to` colleague."""
    rejects: list[tuple[int, str]] = []
    seen = list(history)
    taken = taken_names(existing)  # grows with each accepted item: a second item with the same name would overwrite
    for i, it in enumerate(items):
        why = _check_item(it, templates, seen, policy)
        if not why:
            why = _recipient(it, colleagues or set[str](), people or {})
        if not why and it.template == REPLY:
            why = _reply_thread(it, mail_threads or {})
        if not why and it.handoff and it.handoff not in (colleagues or set[str]()):
            why = f"handoff {it.handoff!r} is not a colleague's account"
        if not why and it.followup and it.followup not in (followups or set[str]()):
            why = f"followup {it.followup!r} is not an open follow-up"
        if not why and it.followup and it.followup in {x.followup for x in items[:i]}:
            why = f"followup {it.followup!r} is already scheduled"
        t = templates.get(it.template)
        if not why and scenario and t and t.resembles and scenario.start <= it.at <= scenario.end:
            why = f"{t.resembles}-like work must not overlap the scenario window {scenario.start}-{scenario.end}"
        if not why:
            for key, value in targets(it):
                if taken_names([value], stem=key != "folder_name") & taken:
                    why = f"{key}: {value!r} already exists or is used by another item, choose a different name"
                    break
        if why:
            rejects.append((i, why))
        else:
            seen += content_values(it, templates)
            for key, value in targets(it):
                taken |= taken_names([value], stem=key != "folder_name")
    return rejects


def fill_recipient_names(items: Iterable[PlanItem], people: dict[str, str]) -> None:
    """Set each mail item's to_name from the narrative (the display name Outlook resolves `to` to), replacing
    anything the LLM wrote; an unknown account gets none, so the item fails validation."""
    for it in items:
        if "to" in it.params:
            it.content.pop("to_name", None)
            it.params["to_name"] = people.get(it.params["to"].strip().lower().partition("@")[0], "")


def _recipient(it: PlanItem, colleagues: set[str], people: dict[str, str]) -> str | None:
    """A mail item's `to` is a colleague's range address, its to_name exactly that colleague's narrative name, and
    its handoff (if any) is that colleague."""
    values = {**it.params, **it.content}
    to = values.get("to")
    if to is None:
        return None
    user, _, domain = to.strip().lower().partition("@")
    if domain != MAIL_DOMAIN or user not in colleagues:
        return f"to {to!r} is not a colleague's address (<account>@{MAIL_DOMAIN})"
    name = values.get("to_name", "")
    if _NOT_A_NAME.search(name) or name != people.get(user):
        return f"to_name {name!r} is not {user!r}'s name from the company story"
    if it.handoff and it.handoff != user:
        return f"handoff {it.handoff!r} must be the mail's recipient {user!r}"
    return None


def _reply_thread(it: PlanItem, threads: dict[str, tuple[str, str]]) -> str | None:
    values = {**it.params, **it.content}
    sender = values.get("to", "").strip().lower().partition("@")[0]
    thread = threads.get(it.followup)
    if thread is None:
        return "a reply answers a colleague's mail: set followup to that mail's open follow-up"
    if thread != (sender, values.get("subject", "").strip()):
        return f"followup {it.followup!r} is {thread[0]!r}'s mail {thread[1]!r}: reply to that, with that subject"
    return None


def targets(it: PlanItem) -> list[tuple[str, str]]:
    """(param, name) of what the item creates in the profile; the existing file/folder a rename acts on is not one."""
    skip = _REFERS_TO_EXISTING.get(it.template, set[str]())
    return [(k, v) for k, v in {**it.params, **it.content}.items() if k in _NAME_KEYS and k not in skip]


def _leaf(path: str) -> str:
    return re.split(r"[\\/]", path.strip())[-1].strip().rstrip(" .")  # Windows ignores trailing dot/space


def taken_names(paths: Iterable[str], stem: bool = True) -> set[str]:
    """Casefolded leaf names, each also without a trailing extension: Explorer hides it, and Save As may add one.

    A planned folder keeps its literal name (`finance.receivables` is not `finance.payables`); an inventory entry
    may be either, so it gets both. ponytail: a dotted planned file name may be over-rejected; costs one repair.
    """
    out: set[str] = set()
    for p in paths:
        leaf = _leaf(p).casefold()
        out |= {n for n in (leaf, re.sub(r"\.[^.]+$", "", leaf) if stem else leaf) if n}
    return out


def _check_item(it: PlanItem, templates: dict[str, Template], seen: list[str], policy: Policy) -> str | None:
    if it.template == BREAK:
        return None if 1 <= it.minutes <= 120 else "break needs 1-120 minutes"
    t = templates.get(it.template)
    if t is None:
        return f"unknown template {it.template!r}"
    try:
        task = Task(t, it, random.Random(0))
    except ValueError as e:
        return str(e)
    for name, sg in t.subgoals.items():
        # The observer reports at most VALUE_MAX chars of a value, so a longer checked value can never pass
        # (acceptance 20261027-1442: a 381-char WordPad note was retyped until the step budget ran out).
        # ponytail: counts the filled pattern, a few regex chars over the real text; fine as a cap.
        check = sg.done.element.value if sg.done and sg.done.element else None
        if check and len(task.fmt(check)) > VALUE_MAX:
            return f"{name}: text too long, keep it under {VALUE_MAX - 20} characters in total"
    for key, value in {**it.params, **it.content}.items():
        if not _PRINTABLE.match(value):
            return f"{key}: non-ASCII text cannot be typed"
        if "\n" in value and key != "body":
            return f"{key}: must be one line"
        if key.endswith("_name") and re.search(r'[<>:"/\\|?*]', value):
            return f"{key}: not a valid file name"
        if key.endswith("_name") and (value != value.strip() or value.endswith(".") or _DEVICE.fullmatch(value)):
            return f"{key}: not a valid file name"  # Windows would alias it (trailing dot/space) or reserve it
        if r := policy.text_allowed(value, None):
            return f"{key}: {r}"
    for value in content_values(it, templates):
        if dup := near_duplicate(value, seen):
            return f"too similar to earlier content {dup[:60]!r}"
    return None


def content_values(it: PlanItem, templates: dict[str, Template]) -> list[str]:
    """Planner-written text (not template defaults), long enough to compare; a rename's existing source is not new."""
    t = templates.get(it.template)
    keys = (set(t.params) if t else set[str]()) - _REFERS_TO_EXISTING.get(it.template, set[str]())
    return [v for k, v in {**it.params, **it.content}.items() if k in keys and len(v) >= MIN_COMPARE]


def near_duplicate(text: str, earlier: list[str]) -> str | None:
    a = _norm(text)
    for b in earlier:
        nb = _norm(b)
        sm = SequenceMatcher(None, a, nb, autojunk=False)
        if (
            sm.real_quick_ratio() >= NEAR_DUPLICATE
            and sm.quick_ratio() >= NEAR_DUPLICATE
            and sm.ratio() >= NEAR_DUPLICATE
        ):
            return b
    return None


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


# World state and persistence ------------------------------------------------------------


class HistoryBusy(RuntimeError):
    """The content journal stayed locked past `World.lock_wait`: retryable, says nothing about the journal."""


def _nap() -> None:
    time.sleep(0.01)


class World:
    """Range state on disk: narrative, content history and per-day ground truth (runs/ is gitignored)."""

    lock_wait = 10.0  # seconds a journal reader or writer waits for another before HistoryBusy

    def __init__(self, root: Path) -> None:
        self.root = root
        self.narrative_path = root / "world" / "narrative.yaml"
        self.history_path = root / "world" / "content.jsonl"

    def narrative(self) -> Narrative:
        return Narrative.model_validate(yaml.safe_load(self.narrative_path.read_text(encoding="utf-8")))

    def save_narrative(self, n: Narrative) -> Path:
        _dump(self.narrative_path, n.model_dump())
        if n.revised:  # each weekly revision is also kept, dated, so the range's story can be replayed
            _dump(self.root / "world" / "revisions" / f"{n.revised}.yaml", n.model_dump())
        return self.narrative_path

    def plan_path(self, user: str, date: str) -> Path:
        return self.root / "plans" / f"{date}-{user}.yaml"

    def day_log(self, user: str, date: str) -> Path:
        return self.root / "days" / f"{date}-{user}.jsonl"

    def save_plan(self, plan: DayPlan, templates: dict[str, Template], *, new: bool = False) -> Path:
        """new=True never replaces an existing plan file (FileExistsError), even one written concurrently."""
        path = self.plan_path(plan.persona, plan.date)
        _dump(path, plan.model_dump(exclude_defaults=True), mode="x" if new else "w")
        with self._journal():
            self._append(plan, [v for it in plan.items for v in content_values(it, templates)])
        return path

    def repair_history(self, plan: DayPlan, templates: dict[str, Template]) -> int:
        """Append the plan's content values missing from the journal; returns how many were appended.

        Recovers a crash after the plan was published but before (or during) its history append. Matched as a
        multiset per (date, persona), so repeated repair appends nothing and rows of an overwritten manual plan
        or of another persona stay as they are. Existing rows are never rewritten; an unreadable journal raises
        ValueError before anything is written.
        """
        with self._journal():  # read + append as one step: a manual save landing between them is seen, not doubled
            have = Counter(r["text"] for r in self._rows() if (r["date"], r["persona"]) == (plan.date, plan.persona))
            missing: list[str] = []
            for v in (v for it in plan.items for v in content_values(it, templates)):
                if have[v]:
                    have[v] -= 1
                else:
                    missing.append(v)
            self._append(plan, missing)
        return len(missing)

    def history(self, user: str) -> list[str]:
        with self._journal():
            return [r["text"] for r in self._rows() if r["persona"] == user]

    @contextmanager
    def _journal(self) -> Generator[None]:
        """Cross-process lock over the journal (readers, appends, repair), so no one reads a half-written append.

        Not reentrant: _rows and _append are the unlocked halves and must only run inside it. The OS drops it with
        the process; a crash mid-append still leaves a torn tail, which readers keep refusing (no fsync or rollback).
        ponytail: only lock-taking writers are excluded; bounded poll, a fair queue if contention ever matters.
        """
        path = self.root / "world" / "content.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            deadline = time.monotonic() + self.lock_wait
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if time.monotonic() >= deadline:
                        raise HistoryBusy(f"another process is using the content history ({path}); retry") from e
                    _nap()
            yield
        finally:
            os.close(fd)  # closing drops the lock

    def _rows(self) -> list[dict[str, str]]:
        if not self.history_path.exists():
            return []
        rows: list[dict[str, str]] = []
        for n, line in enumerate(self.history_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line:
                continue
            try:
                r = json.loads(line)
                rows.append({k: str(r[k]) for k in ("date", "persona", "text")})
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError(f"{self.history_path} line {n} is not a history row: {e}") from e
        return rows

    def _append(self, plan: DayPlan, values: list[str]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a+b") as f:
            if f.seek(0, os.SEEK_END):
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":  # never glue a new row onto an unterminated last line
                    f.write(b"\n")
            for v in values:
                f.write((json.dumps({"date": plan.date, "persona": plan.persona, "text": v}) + "\n").encode())

    def summary(self, user: str, before: dt.date, days: int = 3) -> str:
        """What really happened on the persona's last few days, from the day ground-truth logs."""
        out: list[str] = []
        for d in sorted(self.root.glob(f"days/*-{user}.jsonl")):
            day = d.name[:10]
            if day >= before.isoformat():
                continue
            for line in d.read_text(encoding="utf-8").splitlines():
                r: dict[str, Any] = json.loads(line)
                if r.get("event") == "item_end":
                    params: dict[str, str] = r.get("params") or {}
                    detail = ", ".join(f"{k}={v[:60]}" for k, v in params.items() if k != "body")
                    out.append(
                        f"- {day} {r['at']} {r['template']} {r['status']}: {r.get('context', '')} {detail}".rstrip()
                    )
        threads = self.threads(user, before, days)
        return "\n".join(
            out[-12 * days :] + (["Threads colleagues handed to this persona:", *threads] if threads else [])
        )

    def threads(self, user: str, before: dt.date, days: int = 3) -> list[str]:
        """Cross-persona threads (spec section 6): colleagues' completed items handed off to this user.

        Joins other personas' plans (handoff) with their day logs (item_end done), so an item that failed or
        never ran creates no thread.
        """
        # ponytail: a thread stays visible for `days` days and is not marked answered; track replies explicitly
        # once templates with real round trips (mail, shared documents) exist.
        start = (before - dt.timedelta(days=days)).isoformat()
        out: list[str] = []
        for path in sorted(self.root.glob("plans/*.yaml")):
            date, who = path.stem[:10], path.stem[11:]
            if who == user or not start <= date < before.isoformat():
                continue
            mine = [it for it in load_plan(path).items if it.handoff == user]
            log = self.day_log(who, date)
            if not mine or not log.exists():
                continue
            done: set[tuple[str, str]] = set()
            for line in log.read_text(encoding="utf-8").splitlines():
                r: dict[str, Any] = json.loads(line)
                if r.get("event") == "item_end" and r.get("status") == "done":
                    done.add((r["at"], r["template"]))
            for it in mine:
                if (it.at, it.template) in done:
                    detail = ", ".join(f"{k}={v[:60]}" for k, v in it.params.items() if k != "body")
                    out.append(f"- {date} {it.at} from {who}, {it.template}: {it.context} {detail}".rstrip())
        return out


def load_plan(path: Path) -> DayPlan:
    """Every entry point (day, worker, nightly reuse, world state) loads plans here, so mail recipients are checked
    here too: each mail item's `to` must be a colleague and its to_name exactly their name in the narrative beside
    the plan (<root>/world/narrative.yaml for <root>/plans/<plan>.yaml). No narrative: a plan with mail is refused.
    """
    plan = DayPlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    mail = [it for it in plan.items if "to" in {**it.params, **it.content}]
    if mail:
        narrative = path.parent.parent / "world" / "narrative.yaml"
        people = (
            {
                p.user: p.name
                for p in Narrative.model_validate(yaml.safe_load(narrative.read_text("utf-8"))).people
                if p.user
            }  # fmt: skip
            if narrative.exists()
            else {}
        )
        for it in mail:
            if why := _recipient(it, set(people) - {plan.persona}, people):
                raise ValueError(f"{path}: {it.template} at {it.at}: {why}")
    return plan


def _dump(path: Path, data: Any, mode: str = "w") -> None:
    """mode "w" rewrites in place; mode "x" publishes the complete bytes atomically and never replaces `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)  # serialize before touching disk
    if mode == "w":
        with path.open("w", encoding="utf-8") as f:
            f.write(text)
        return
    assert mode == "x", mode
    # Write a same-directory temp, then os.link it into place: link creation is atomic and fails with
    # FileExistsError if `path` exists (NTFS and POSIX alike), so a concurrent winner is never overwritten
    # and `path` is either absent or complete. Filesystems without hard links fail closed (no replace fallback).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise
        except OSError as e:
            if path.exists():  # Windows can report an existing target as an access error
                raise FileExistsError(errno.EEXIST, "plan already exists", str(path)) from e
            raise OSError(f"cannot publish {path} atomically without replacing: hard links unavailable ({e})") from e
    finally:
        Path(tmp).unlink(missing_ok=True)
