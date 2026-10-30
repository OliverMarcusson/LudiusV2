from __future__ import annotations

import datetime as dt
import errno
import functools
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
from typing import Any, cast

import yaml
from pydantic import Field, field_validator, model_validator

from ludius import intranet
from ludius.llm import LlmClient
from ludius.mail import DOMAIN as MAIL_DOMAIN
from ludius.policy import Policy
from ludius.tasks import (
    ATTACHABLE,
    REFERS_TO_EXISTING,
    SENDS,
    Persona,
    Strict,
    Task,
    TaskInstance,
    Template,
    file_reserved,
    load_template,
    write_paths,
)

BREAK = "break"
NEAR_DUPLICATE = 0.85
MIN_COMPARE = 12
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_LOCATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .-]{1,39}")
_MAIL_FOLDER = re.compile(r"[A-Za-z][A-Za-z0-9 -]{1,29}")
_DEFAULT_FOLDERS = {
    "inbox",
    "outbox",
    "drafts",
    "sent items",
    "deleted items",
    "junk email",
    "archive",
    "calendar",
    "contacts",
    "tasks",
    "notes",
    "journal",
    "conversation history",
    "rss feeds",
    "sync issues",
}
_PRINTABLE = re.compile(r"^[\x20-\x7e\n]*$")
VALUE_MAX = 300
_NAME_KEYS = ("file_name", "folder_name", "new_name")
RECYCLE_AGE_DAYS = 3
OWN_FILES = ("file.edit", "file.reuse", "file.export", "file.recycle", "file.share", "file.organize")
_DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)
WORK_DAY, MEETING_MINUTES, MEETING_DAYS_AHEAD = ("08:00", "17:00"), (15, 120), 14
CHAT_CHANNELS: list[str] = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "data" / "chat.yaml").read_text("utf-8")
)["channels"]
SHOWN_EXISTING = 80


class Person(Strict):
    name: str
    role: str
    department: str
    user: str = ""
    manager: str = ""


class Event(Strict):
    date: str
    what: str


class Narrative(Strict):
    company: str
    city: str
    summary: str
    people: list[Person]
    channels: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    shares: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    projects: list[str]
    events: list[Event]
    revised: str = ""

    history: list[Event] = Field(default_factory=list[Event])

    @model_validator(mode="after")
    def _members(self) -> Narrative:
        users = {p.user for p in self.people if p.user}
        handles = [c.lower().replace(" ", "-") for c in [*CHAT_CHANNELS, *self.channels]]
        for name, members in self.channels.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}", name) or not set(members) <= users:
                raise ValueError(f"team channel {name!r}: a plain name, every member a narrative person's account")
            if handles.count(name.lower().replace(" ", "-")) > 1:
                raise ValueError(f"team channel {name!r}: its handle is another channel's")
        for name, members in self.shares.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}", name) or name.upper() in ("NETLOGON", "SYSVOL"):
                raise ValueError(f"team share {name!r}: a plain name without spaces, not a system share")
            if not members or not set(members) <= users:
                raise ValueError(f"team share {name!r}: every member a narrative person's account")
        return self


class PlanItem(TaskInstance):
    at: str
    minutes: int = 0
    interrupt: bool = False
    handoff: str = ""
    followup: str = ""

    @field_validator("handoff", "followup", mode="before")
    @classmethod
    def _none_is_empty(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("params", "content", mode="before")
    @classmethod
    def _newlines(cls, v: object) -> object:
        if not isinstance(v, dict):
            return v
        return {k: x.replace("\r\n", "\n").replace("\r", "\n") if k in MULTILINE and isinstance(x, str) else x
                for k, x in v.items()}  # pyright: ignore[reportUnknownVariableType]  # fmt: skip

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("at must be HH:MM")
        return v


class Scenario(Strict):
    date: str
    start: str
    end: str
    signal: str

    @classmethod
    def parse(cls, s: str) -> Scenario:
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


DERIVED = {"to_name"}
MESSAGE_ID = re.compile(r"<[!-;=?-~]{1,200}@[!-;=?-~]{1,100}>")
_NOT_A_NAME = re.compile(r"[;,<>@]")


def catalog(templates: dict[str, Template], seed: str = "") -> str:
    lines = [f"- {t.task}: {t.about} Parameters: {', '.join(p for p in t.params if p not in DERIVED) or 'none'}."
             + (f" Plan it rarely, about {t.plan_weight:g} times as often as other tasks." if t.plan_weight < 1 else "")
             for t in templates.values()]  # fmt: skip
    if seed:
        random.Random(seed).shuffle(lines)
    lines.append(f"- {BREAK}: away from the desk (lunch, coffee, meeting), screen locked. Set minutes.")
    return "\n".join(lines)


def make_narrative(llm: LlmClient, personas: list[Persona], start: dt.date) -> Narrative:
    who = "\n".join(f"- {p.name} (account {p.user}): {p.role}, {p.department}" for p in personas)
    user = (
        f"Range start date: {start.isoformat()}.\nThese employees are personas and must appear in people with their "
        f"account as user:\n{who}\n\nReturn JSON with keys company, city, summary (3-5 sentences), people "
        "(8-15 entries: name, role, department, user (a short lower-case account for everyone), manager (the user of "
        "the person they report to; empty only for the head of the company; every employee listed above reports to "
        "someone who is not one of them)), projects (3-6 short strings) and events (6-12 entries: "
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
    inbox: set[tuple[str, str]] | None = None,
    owned: dict[str, str] | None = None,
    invites: set[tuple[str, str]] | None = None,
) -> tuple[DayPlan, list[str]]:
    policy = policy or Policy()
    followups = followups or {}
    templates = role_templates(templates, persona)
    window = scenario if scenario and scenario.date == date.isoformat() else None
    lookalikes = sorted(k for k, t in templates.items() if window and t.resembles == window.signal)
    colleagues = {p.user for p in narrative.people if p.user and p.user != persona.user}
    people = {p.user: p.name for p in narrative.people if p.user}
    teams = [c for c, members in narrative.channels.items() if persona.user in members]
    shares = [c for c, members in narrative.shares.items() if persona.user in members]
    existing = list(existing)
    names = sorted({_leaf(e) for e in existing if _leaf(e)}, key=str.casefold)[:SHOWN_EXISTING]
    mail_tasks = ", ".join(sorted(k for k, t in templates.items() if t.capability in SENDS)) or "none"
    reads = sorted(k for k, t in templates.items() if t.capability == "mail.read")
    files = sorted(k for k, t in templates.items() if t.capability == "mail.file")
    chats = {t.capability: k for k, t in templates.items() if t.capability in ("chat.post", "chat.dm", "chat.read")}
    user = (
        f"Company story:\n{narrative.model_dump_json(indent=1)}\n\n"
        f"Persona: {persona.name}, {persona.role} ({persona.department}). Usual hours {persona.hours[0]}-"
        f"{persona.hours[1]}.\nStyle and habits: {persona.style or 'ordinary'}\n\n"
        f"Day to plan: {date.strftime('%A')} {date.isoformat()}.\n\n"
        f"What happened so far:\n{world or '- nothing yet'}\n\n"
        f"Task catalog (in no particular order):\n{catalog(templates, f'{persona.user}:{date}')}\n\n"
        + (
            "Intranet pages (a lookup's search_query should find one of them):\n"
            + intranet.index(skip=policy.page_reserved)
            + "\n\n"
            if "edge_web_lookup" in templates
            else ""
        )
        + (
            "Intranet downloads (edge_download_file: page is the exact page title, file_name the exact file name):\n"
            + "\n".join(f"- {page}: {f}" for f, page in intranet.downloads(skip=policy.page_reserved).items())
            + "\n\n"
            if any(t.capability == "file.download" for t in templates.values())
            else ""
        )
        + "Recent content the persona already wrote (do not repeat or paraphrase it):\n"
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
            f"Pick up threads colleagues handed to this persona. Mail ({mail_tasks}) goes to one colleague: to is "
            f"<account>@{MAIL_DOMAIN} and handoff is that account; reply only to a due follow-up's mail or a mail "
            "listed under Inbox on the mail server (to its sender, with its subject exactly)."
            + (
                f" Reading a mail ({', '.join(reads)}) opens one listed Inbox mail marked unread (sender is its "
                "sender's address, subject exactly as listed), e.g. the morning look at new mail; at most once each."
                if reads
                else ""
            )
            + (
                f" Filing a mail ({', '.join(files)}) moves one listed Inbox mail the persona is done with into an "
                "Inbox subfolder named for its topic or project (reuse the same folder name for related mail); plan "
                "it after any read or reply of that mail."
                if files
                else ""
            )  # fmt: skip
            if colleagues
            else ""
        )
        + (
            f" Team chat: now and then one line of small talk ({chats['chat.post']} in Coffee Corner or Off-Topic, "
            f"or {chats['chat.dm']} to a colleague). To ask a colleague for something concrete the persona needs (a "
            f"note, a file, a mail with a figure), {chats['chat.dm']} saying what, no handoff: they see it in the "
            f"chat and answer there the same day. Chat messages to this persona are answered as they come in, not "
            f"planned; {chats['chat.read']} only to look up an older conversation."
            if colleagues and len(chats) == 3
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
                       mail_threads, people, persona.user, date.isoformat(), inbox, teams, owned, shares,
                       invites)  # fmt: skip
    missing = sorted(set(followups) - {it.followup for it in draft.items})
    if rejects or missing:
        fix = "These items were rejected; return the full corrected item list:\n" + "\n".join(
            [f"- item {i} ({draft.items[i].template} at {draft.items[i].at}): {why}" for i, why in rejects]
            + [f"- follow-up {k} is not scheduled" for k in missing]
        )
        draft = llm.json(_Draft, PLAN_SYSTEM, user + "\n\n" + fix)
        fill_recipient_names(draft.items, people)
        rejects = validate(draft.items, templates, history, policy, colleagues, window, set(followups), existing,
                           mail_threads, people, persona.user, date.isoformat(), inbox, teams, owned,
                           shares, invites)  # fmt: skip
    bad = {i for i, _ in rejects}
    items = sorted((it for i, it in enumerate(draft.items) if i not in bad), key=lambda it: it.at)
    missing = sorted(set(followups) - {it.followup for it in items})
    return DayPlan(persona=persona.user, date=date.isoformat(), items=items), [
        f"{draft.items[i].template} at {draft.items[i].at}: {why}" for i, why in rejects
    ] + [f"follow-up {k}: not scheduled, stays open" for k in missing]


REACTION_ONLY = {"chat.read_channel"}


def installed(app: str) -> bool:
    apps = os.environ.get("LUDIUS_APPS")
    return apps is None or app in {a.strip() for a in apps.split(",")}


def role_templates(templates: dict[str, Template], persona: Persona) -> dict[str, Template]:
    return {k: t for k, t in templates.items()
            if (not t.resembles or k in persona.risk_profile) and t.capability not in REACTION_ONLY
            and installed(t.app) and not (t.fallback_for and installed(t.fallback_for))
            and all(os.environ.get(v[1:]) for v in t.defaults.values() if v.startswith("$"))}  # fmt: skip


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
    user: str = "",
    day: str = "",
    inbox: set[tuple[str, str]] | None = None,
    teams: Iterable[str] = (),
    owned: dict[str, str] | None = None,
    shares: Iterable[str] = (),
    invites: set[tuple[str, str]] | None = None,
) -> list[tuple[int, str]]:
    rejects: list[tuple[int, str]] = []
    seen = list(history)
    taken = taken_names(existing)
    for i, it in enumerate(items):
        why = _check_item(it, templates, seen, policy, user, teams, shares)
        if not why:
            why = _recipient(it, colleagues or set[str](), people or {})
        if not why:
            why = _meeting_day(it, day)
        cap = (t.capability if (t := templates.get(it.template)) else None) or ""
        if not why and cap == "mail.reply":
            why = _reply_thread(it, mail_threads or {}, inbox or set[tuple[str, str]]())
        if not why and cap == "calendar.respond":
            why = _invite(it, invites or set[tuple[str, str]]())
        if not why and cap == "mail.save" and not it.params.get("file_name", "").lower().endswith(ATTACHABLE):
            why = f"file_name: the attachment's exact name, a {', '.join(ATTACHABLE)} file"
        if not why and cap in ("mail.read", "mail.save", "mail.file"):
            why = _read_mail(it, colleagues or set[str](), inbox or set[tuple[str, str]](), items[:i])
        if not why and t and (cap in OWN_FILES or t.attaches):
            why = _owned(it, t, owned or {}, day, items)
        if not why and it.handoff and it.handoff not in (colleagues or set[str]()):
            why = f"handoff {it.handoff!r} is not a colleague's account"
        if not why and it.followup and cap == "chat.read":
            why = "reading the chat does not settle a follow-up: set followup on the asked work"
        if not why and it.followup and it.followup not in (followups or set[str]()):
            why = f"followup {it.followup!r} is not an open follow-up"
        if not why and (it.at, it.template) in {(x.at, x.template) for x in items[:i]}:
            why = f"another item is already {it.template} at {it.at}"
        if not why and it.followup and it.followup in {x.followup for x in items[:i]}:
            why = f"followup {it.followup!r} is already scheduled"
        t = templates.get(it.template)
        if not why and scenario and t and t.resembles and scenario.start <= it.at <= scenario.end:
            why = f"{t.resembles}-like work must not overlap the scenario window {scenario.start}-{scenario.end}"
        if not why:
            for key, value in targets(it, templates):
                if taken_names([value], stem=key != "folder_name") & taken:
                    why = f"{key}: {value!r} already exists or is used by another item, choose a different name"
                    break
        if why:
            rejects.append((i, why))
        else:
            seen += content_values(it, templates)
            for key, value in targets(it, templates):
                taken |= taken_names([value], stem=key != "folder_name")
    return rejects


def _owned(it: PlanItem, t: Template, owned: dict[str, str], day: str, items: list[PlanItem]) -> str | None:
    recycle = t.capability == "file.recycle"
    shared = [sg.done.file.path for sg in t.subgoals.values() if sg.done and sg.done.file and sg.done.file.share]
    sources = [source for sg in t.subgoals.values()
               if sg.done and (f := sg.done.file) and (source := f.source or f.export_of or f.extract_of)]  # fmt: skip
    try:
        named = t.removes if recycle or t.capability == "file.organize" else shared or sources or t.attaches or t.writes
        paths = [w.format_map({**it.params, **it.content}) for w in named]
    except KeyError:
        return "missing parameters"
    if not paths or any(p.casefold() not in owned for p in paths):
        verb = "recycle" if recycle else "move" if t.removes else "copy" if shared else "reuse" if sources else "edit"
        verb = "attach" if t.attaches else verb
        return f"file_name: {verb} only one of this persona's own files (Own recent files), named exactly"
    if t.capability == "file.export" and any(w.format_map({**it.params, **it.content}).casefold() in owned
                                              for w in t.writes):  # fmt: skip
        return "file_name: this file already has that copy, choose a different one"
    if t.attaches and not all(p.lower().endswith(ATTACHABLE) for p in paths):
        return f"file_name: attach only {', '.join(ATTACHABLE)} files"
    if recycle:
        cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=RECYCLE_AGE_DAYS)).isoformat()
        if any(owned[p.casefold()] > cutoff for p in paths):
            return f"file_name: recycle only an own file untouched for {RECYCLE_AGE_DAYS} days or more"
        name = it.params.get("file_name", "").casefold()
        if any(x is not it and name in (v.casefold() for v in {**x.params, **x.content}.values()) for x in items):
            return "file_name: another item today uses this file, recycle a different one"
    return None


def owned_files(root: Path, user: str, before: str) -> dict[str, str]:
    templates, world, owned = _templates(), World(root), dict[str, str]()
    for path in sorted(root.glob("plans/*.yaml")):
        date = path.stem[:10]
        if path.stem[11:] != user or date >= before:
            continue
        done = done_slots(world.day_log(user, date))
        for it in sorted(load_plan(path, history=True).items, key=lambda x: x.at):
            if (it.at, it.template) not in done or (t := templates.get(it.template)) is None:
                continue
            try:
                gone = [r.format_map({**it.params, **it.content}).casefold() for r in t.removes]
                made = [w.format_map({**it.params, **it.content}).casefold() for w in t.writes]
            except KeyError:
                continue
            removed = {p for p in owned for g in gone if p == g or (g.endswith(".*") and _stem(p) == g[:-2])}
            exts = {p[len(_stem(p)) :] for p in removed}
            for p in removed:
                del owned[p]
            for w in made:
                if not w.endswith(".*"):
                    owned[w] = date
                elif len(exts) == 1:
                    owned[w[:-2] + exts.pop()] = date
    return owned


def _stem(path: str) -> str:
    return re.sub(r"\.[^.\\/]*$", "", path)


def fill_recipient_names(items: Iterable[PlanItem], people: dict[str, str]) -> None:
    for it in items:
        if "to" in it.params:
            it.content.pop("to_name", None)
            it.params["to_name"] = people.get(it.params["to"].strip().lower().partition("@")[0], "")


def _recipient(it: PlanItem, colleagues: set[str], people: dict[str, str]) -> str | None:
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


def _meeting(values: dict[str, str]) -> str | None:
    if not _LOCATION.fullmatch(values.get("location", "")):
        return "location: 2-40 letters, digits, spaces, '-' or '.' (no @, comma or semicolon)"
    try:
        date = dt.date.fromisoformat(values["date"])
    except ValueError:
        return "date: must be YYYY-MM-DD"
    if date.weekday() >= 5 or len(values["date"]) != 10:
        return "date: must be a weekday (Monday-Friday) as YYYY-MM-DD"
    start, end = values.get("start", ""), values.get("end", "")
    if not (_HHMM.match(start) and _HHMM.match(end)):
        return "start and end: must be HH:MM"
    minutes = (int(end[:2]) - int(start[:2])) * 60 + int(end[3:]) - int(start[3:])
    if not (WORK_DAY[0] <= start and end <= WORK_DAY[1]) or not MEETING_MINUTES[0] <= minutes <= MEETING_MINUTES[1]:
        return (
            f"a meeting runs {MEETING_MINUTES[0]}-{MEETING_MINUTES[1]} minutes between {WORK_DAY[0]} and {WORK_DAY[1]}"
        )
    return None


def _meeting_day(it: PlanItem, day: str) -> str | None:
    date = {**it.params, **it.content}.get("date")
    if date is None or not day:
        return None
    if not day < date <= (dt.date.fromisoformat(day) + dt.timedelta(days=MEETING_DAYS_AHEAD)).isoformat():
        return f"date: a meeting is set after {day} and within {MEETING_DAYS_AHEAD} days of it"
    return None


def _reply_thread(it: PlanItem, threads: dict[str, tuple[str, str]], inbox: set[tuple[str, str]]) -> str | None:
    values = {**it.params, **it.content}
    sender = values.get("to", "").strip().lower().partition("@")[0]
    subject = values.get("subject", "").strip()
    if not it.followup and ((sender, subject) in inbox or (not subject and any(s == sender for s, _ in inbox))):
        return None
    thread = threads.get(it.followup)
    if thread is None:
        return "a reply answers a colleague's mail: set followup to that mail's open follow-up, or a listed inbox mail"
    if thread != (sender, subject):
        return f"followup {it.followup!r} is {thread[0]!r}'s mail {thread[1]!r}: reply to that, with that subject"
    return None


def _sender(it: PlanItem, colleagues: set[str]) -> str | None:
    sender = {**it.params, **it.content}.get("sender")
    if sender is None:
        return None
    user, _, domain = sender.strip().lower().partition("@")
    return None if domain == MAIL_DOMAIN and user in colleagues else f"sender {sender!r} is not a colleague's address"


def _invite(it: PlanItem, invites: set[tuple[str, str]]) -> str | None:
    values = {**it.params, **it.content}
    if values.get("response") not in ("accept", "decline"):
        return "response: accept or decline"
    if (values.get("to", "").strip().lower().partition("@")[0], values.get("subject", "")) not in invites:
        return "answer only a listed meeting invitation: to is its organizer, subject its exact subject"
    return None


def _read_mail(it: PlanItem, colleagues: set[str], inbox: set[tuple[str, str]], earlier: list[PlanItem]) -> str | None:
    if why := _sender(it, colleagues):
        return why
    values = {**it.params, **it.content}
    key = (values["sender"].strip().lower().partition("@")[0], values.get("subject", ""))
    if key not in inbox:
        return "a read opens a mail listed under Inbox on the mail server: its sender and exact subject"
    if any(x.template == it.template and {**x.params, **x.content}.get("subject") == key[1] for x in earlier):
        return f"the mail {key[1]!r} is already read earlier in the plan"
    return None


def targets(it: PlanItem, templates: dict[str, Template]) -> list[tuple[str, str]]:
    skip = _refers_to_existing(templates.get(it.template))
    return [(k, v) for k, v in {**it.params, **it.content}.items() if k in _NAME_KEYS and k not in skip]


def _leaf(path: str) -> str:
    return re.split(r"[\\/]", path.strip())[-1].strip().rstrip(" .")


def taken_names(paths: Iterable[str], stem: bool = True) -> set[str]:
    out: set[str] = set()
    for p in paths:
        leaf = _leaf(p).casefold()
        out |= {n for n in (leaf, re.sub(r"\.[^.]+$", "", leaf) if stem else leaf) if n}
    return out


def _check_item(
    it: PlanItem,
    templates: dict[str, Template],
    seen: list[str],
    policy: Policy,
    user: str = "",
    teams: Iterable[str] = (),
    shares: Iterable[str] = (),
) -> str | None:
    if why := item_problem(it, templates, policy, user, teams, shares):
        return why
    for value in content_values(it, templates):
        if dup := near_duplicate(value, seen):
            return f"too similar to earlier content {dup[:60]!r}"
    return None


def item_problem(
    it: PlanItem,
    templates: dict[str, Template],
    policy: Policy,
    user: str = "",
    teams: Iterable[str] = (),
    shares: Iterable[str] = (),
) -> str | None:
    if it.template == BREAK:
        return None if 1 <= it.minutes <= 120 else "break needs 1-120 minutes"
    t = templates.get(it.template)
    if t is None:
        return f"unknown template {it.template!r}"
    if "date" in {**it.params, **it.content} and (why := _meeting({**it.params, **it.content})):
        return why
    try:
        task = Task(t, it, random.Random(0))
    except ValueError as e:
        return str(e)
    if t.capability in REACTION_ONLY:
        return f"{it.template} is read by chat reactions only (their outcome check is the server's read marker)"
    channel = task.values.get("channel")
    if t.capability == "chat.post" and channel not in [*CHAT_CHANNELS, *teams]:
        return f"channel {channel!r} is not one of {[*CHAT_CHANNELS, *teams]}"
    folder = task.values.get("folder", "")
    if t.capability == "mail.file" and (not _MAIL_FOLDER.fullmatch(folder) or folder.casefold() in _DEFAULT_FOLDERS):
        return "folder: 2-30 letters, digits, spaces or '-', not one of Outlook's own folders"
    if t.capability == "file.share" and task.values.get("share") not in list(shares):
        return f"share {task.values.get('share')!r} is not one of this persona's team shares {sorted(shares)}"
    if t.capability in ("chat.post", "chat.dm") and not 2 <= len(task.values.get("message", "").strip()) <= 200:
        return "message: one line of 2-200 characters"
    if t.capability == "file.download":
        page = intranet.downloads(skip=policy.page_reserved).get(task.values.get("file_name", ""))
        if page is None or page != task.values.get("page"):
            return "file_name and page: one download and its page from the intranet downloads list, exactly"
    for name, sg in t.subgoals.items():
        check = sg.done.element.value if sg.done and sg.done.element else None
        if check and len(task.fmt(check)) > VALUE_MAX:
            return f"{name}: text too long, keep it under {VALUE_MAX - 20} characters in total"
    office = _office_keys(t)
    for c in (sg.done.file for sg in t.subgoals.values() if sg.done and sg.done.file and sg.done.file.slides):
        for slide in c.slides or []:
            if len({bool(task.values.get(k)) for k in re.findall(r"\{(\w+)\}", slide)}) > 1:
                return f"a slide is planned whole or not at all: {slide!r}"
    for key, value in {**it.params, **it.content}.items():
        if not _PRINTABLE.match(value.replace("\t", " ") if key == "rows" else value):
            return f"{key}: non-ASCII text cannot be typed"
        if "\n" in value and key not in MULTILINE:
            return f"{key}: must be one line"
        if key == "rows" and (why := _grid(value)):
            return f"rows: {why}"
        if key in office and value and (why := _office_text(value, OFFICE_LINES.get(key, 1))):
            return f"{key}: {why}"
        if key.endswith("_name") and re.search(r'[<>:"/\\|?*]', value):
            return f"{key}: not a valid file name"
        if key.endswith("_name") and (value != value.strip() or value.endswith(".") or _DEVICE.fullmatch(value)):
            return f"{key}: not a valid file name"
        if r := policy.text_allowed(value, None):
            return f"{key}: {r}"
    for name, sg in t.subgoals.items():
        if sg.search and (r := policy.text_allowed(sg.search, None)):
            return f"{name}: search text: {r}"
    for rel in write_paths(task):
        if file_reserved(user, rel, policy):
            return f"{rel}: scenario-reserved file, choose a different name"
    return None


GRID_ROWS, GRID_COLS, CELL_MAX = 8, 5, 30
_CELL = re.compile(rf"[A-Za-z][A-Za-z0-9 .,'&/-]{{0,{CELL_MAX - 1}}}|0|[1-9][0-9]{{0,8}}")


def _grid(rows: str) -> str | None:
    lines = rows.strip("\n").split("\n")
    grid = [line.split("\t") for line in lines]
    if not 1 <= len(grid) <= GRID_ROWS or any(not 1 <= len(r) <= GRID_COLS for r in grid):
        return f"1-{GRID_ROWS} lines of 1-{GRID_COLS} tab-separated cells"
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if not _CELL.fullmatch(cell) or cell != cell.strip():
                return f"cell {cell!r}: a short text starting with a letter, or a whole number"
            above = [g[c].casefold() for g in grid[:r] if c < len(g)]
            if cell[0].isalpha() and any(a != cell.casefold() and a.startswith(cell.casefold()) for a in above):
                return f"cell {cell!r}: Excel would complete it to a longer cell above it"
    return None


MULTILINE = ("body", "notes", "rows", "bullets", "bullets2")
OFFICE_LINES = {"body": 10, "notes": 10, "bullets": 6, "bullets2": 6}
OFFICE_LINE_MAX = 120
_OFFICE_LINE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9 ,.;:!?&%#+/-]{{0,{OFFICE_LINE_MAX - 1}}}")
_AUTOFORMAT = re.compile(r"--| -|- |\b[13]/[24]\b|^\w{1,3}[.)] ")
_COMPLETES = ("january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
              "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")  # fmt: skip


def _office_keys(t: Template) -> set[str]:
    checks = [sg.done.file for sg in t.subgoals.values() if sg.done and sg.done.file]
    text = " ".join([c.paragraphs or "" for c in checks] + [s for c in checks for s in c.slides or []])
    text += " " + " ".join(c.append or "" for c in checks if c.path.lower().endswith(".docx"))
    text += " " + " ".join(c.comment or "" for c in checks)
    return set(re.findall(r"\{(\w+)\}", text))


def _office_text(value: str, max_lines: int) -> str | None:
    lines = value.strip("\n").split("\n")
    if not 1 <= len(lines) <= max_lines:
        return f"1-{max_lines} lines"
    for line in lines:
        if not _OFFICE_LINE.fullmatch(line) or line != line.strip() or _AUTOFORMAT.search(line):
            return (
                f"line {line!r}: up to {OFFICE_LINE_MAX} chars from a letter or digit: letters, "
                "digits, spaces and , . ; : ! ? & % # + / -; no ' - ', '--' or 1/2; not like a list ('a) ', '1. ')"
            )
        last = line.split()[-1].casefold()
        if len(last) >= 4 and any(w != last and w.startswith(last) for w in _COMPLETES):
            return f"line {line!r}: Word would complete {last!r} to a month or day name"
    return None


def content_values(it: PlanItem, templates: dict[str, Template]) -> list[str]:
    t = templates.get(it.template)
    keys = (set(t.params) if t else set[str]()) - _refers_to_existing(t)
    return [v for k, v in {**it.params, **it.content}.items() if k in keys and len(v) >= MIN_COMPARE]


def _refers_to_existing(t: Template | None) -> set[str]:
    return REFERS_TO_EXISTING.get(t.capability or "", set[str]()) if t else set[str]()


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


class HistoryBusy(RuntimeError):
    pass


def _nap() -> None:
    time.sleep(0.01)


class World:
    lock_wait = 10.0

    def __init__(self, root: Path) -> None:
        self.root = root
        self.narrative_path = root / "world" / "narrative.yaml"
        self.history_path = root / "world" / "content.jsonl"

    def narrative(self) -> Narrative:
        return Narrative.model_validate(yaml.safe_load(self.narrative_path.read_text(encoding="utf-8")))

    def save_narrative(self, n: Narrative) -> Path:
        _dump(self.narrative_path, n.model_dump())
        if n.revised:
            _dump(self.root / "world" / "revisions" / f"{n.revised}.yaml", n.model_dump())
        return self.narrative_path

    def plan_path(self, user: str, date: str) -> Path:
        return self.root / "plans" / f"{date}-{user}.yaml"

    def day_log(self, user: str, date: str) -> Path:
        return self.root / "days" / f"{date}-{user}.jsonl"

    def save_plan(self, plan: DayPlan, templates: dict[str, Template], *, new: bool = False) -> Path:
        path = self.plan_path(plan.persona, plan.date)
        _dump(path, plan.model_dump(exclude_defaults=True), mode="x" if new else "w")
        with self._journal():
            self._append(plan, [v for it in plan.items for v in content_values(it, templates)])
        return path

    def repair_history(self, plan: DayPlan, templates: dict[str, Template]) -> int:
        with self._journal():
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
            os.close(fd)

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
                if f.read(1) != b"\n":
                    f.write(b"\n")
            for v in values:
                f.write((json.dumps({"date": plan.date, "persona": plan.persona, "text": v}) + "\n").encode())

    def summary(self, user: str, before: dt.date, days: int = 3) -> str:
        out: list[str] = []
        for d in sorted(self.root.glob(f"days/*-{user}.jsonl")):
            day = d.name[:10]
            if day >= before.isoformat():
                continue
            for r in day_records(d):
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
        start = (before - dt.timedelta(days=days)).isoformat()
        out: list[str] = []
        for path in sorted(self.root.glob("plans/*.yaml")):
            date, who = path.stem[:10], path.stem[11:]
            if who == user or not start <= date < before.isoformat():
                continue
            mine = [it for it in load_plan(path, history=True).items if it.handoff == user]
            log = self.day_log(who, date)
            if not mine or not log.exists():
                continue
            done = done_slots(log)
            for it in mine:
                if (it.at, it.template) in done:
                    detail = ", ".join(f"{k}={v[:60]}" for k, v in it.params.items() if k != "body")
                    out.append(f"- {date} {it.at} from {who}, {it.template}: {it.context} {detail}".rstrip())
        return out


def day_records(log: Path) -> Generator[dict[str, Any]]:
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(r, dict):
            yield cast(dict[str, Any], r)


def done_slots(log: Path) -> set[tuple[str, str]]:
    if not log.exists():
        return set()
    return {(str(r["at"]), str(r["template"])) for r in day_records(log)
            if r.get("event") == "item_end" and r.get("status") == "done"}  # fmt: skip


def load_plan(path: Path, *, history: bool = False) -> DayPlan:
    plan = DayPlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if dup := {k for k, n in Counter((it.at, it.template) for it in plan.items).items() if n > 1}:
        if not history:
            raise ValueError(f"{path}: items share a time and template: {sorted(dup)}")
        plan = plan.model_copy(update={"items": [it for it in plan.items if (it.at, it.template) not in dup]})
    policy, templates = Policy.from_env(), _templates()
    narrative = path.parent.parent / "world" / "narrative.yaml"
    world = Narrative.model_validate(yaml.safe_load(narrative.read_text("utf-8"))) if narrative.exists() else None
    teams = [c for c, members in (world.channels if world else {}).items() if plan.persona in members]
    shares = [c for c, members in (world.shares if world else {}).items() if plan.persona in members]
    edits = [it for it in plan.items
             if (t := templates.get(it.template)) and (t.capability in OWN_FILES or t.attaches)]  # fmt: skip
    owned = owned_files(path.parent.parent, plan.persona, plan.date) if edits and not history else {}
    for it in [] if history else edits:
        if why := _owned(it, templates[it.template], owned, plan.date, plan.items):
            raise ValueError(f"{path}: {it.template} at {it.at}: {why}")
    for it in plan.items:
        if it.template in templates and (why := item_problem(it, templates, policy, plan.persona, teams, shares)
                                         or _meeting_day(it, plan.date)):  # fmt: skip
            raise ValueError(f"{path}: {it.template} at {it.at}: {why}")
        if it.request and not MESSAGE_ID.fullmatch(it.request):
            raise ValueError(f"{path}: {it.template} at {it.at}: request {it.request!r} is not a message id")
    mail = [it for it in plan.items if {"to", "sender"} & {**it.params, **it.content}.keys()]
    if mail:
        people = {p.user: p.name for p in world.people if p.user} if world else {}
        for it in mail:
            if why := _recipient(it, set(people) - {plan.persona}, people) or _sender(it, set(people) - {plan.persona}):
                raise ValueError(f"{path}: {it.template} at {it.at}: {why}")
    return plan


@functools.cache
def _templates() -> dict[str, Template]:
    ts = [load_template(p) for p in sorted((Path(__file__).resolve().parents[2] / "data" / "templates").glob("*.yaml"))]
    return {t.task: t for t in ts}


def _dump(path: Path, data: Any, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)
    if mode == "w":
        with path.open("w", encoding="utf-8") as f:
            f.write(text)
        return
    assert mode == "x", mode
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
            if path.exists():
                raise FileExistsError(errno.EEXIST, "plan already exists", str(path)) from e
            raise OSError(f"cannot publish {path} atomically without replacing: hard links unavailable ({e})") from e
    finally:
        Path(tmp).unlink(missing_ok=True)
