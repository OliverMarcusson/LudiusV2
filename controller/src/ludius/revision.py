from __future__ import annotations

import datetime as dt
import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import NamedTuple

from ludius.llm import LlmClient
from ludius.planner import NARRATIVE_SYSTEM, Event, Narrative, Person, World, near_duplicate
from ludius.tasks import Persona, Strict

HORIZON_DAYS = 28
MAX_PROJECTS = 8


class Revision(Strict):
    summary: str
    finished_projects: list[str] = []
    new_projects: list[str] = []
    new_people: list[Person] = []
    new_events: list[Event] = []


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def due(n: Narrative, day: dt.date) -> bool:
    return not n.revised or week_start(dt.date.fromisoformat(n.revised)) < week_start(day)


def with_personas(n: Narrative, personas: list[Persona]) -> Narrative:
    have = {p.user for p in n.people if p.user}
    extra = [Person(name=p.name, role=p.role, department=p.department, user=p.user)
             for p in personas if p.user not in have]  # fmt: skip
    return n.model_copy(update={"people": [*n.people, *extra], "accounts": [p.user for p in personas]})


def check(r: Revision, n: Narrative, day: dt.date) -> list[str]:
    problems: list[str] = []
    if not r.summary.strip():
        problems.append("summary is empty")
    if bad := [p for p in r.finished_projects if p not in n.projects]:
        problems.append(f"finished_projects not in the current projects: {bad}")
    if dup := [p for p in r.new_projects if p in n.projects]:
        problems.append(f"new_projects already exist: {dup}")
    left = len(n.projects) - len(set(r.finished_projects)) + len(r.new_projects)
    if not 1 <= left <= MAX_PROJECTS:
        problems.append(f"would leave {left} projects; keep 1-{MAX_PROJECTS}")
    names = {p.name.lower() for p in n.people}
    users = {p.user for p in n.people if p.user}
    for p in r.new_people:
        if p.name.lower() in names:
            problems.append(f"new person {p.name!r} already exists")
        if p.user:
            problems.append(f"new person {p.name!r}: user must be empty, only personas have accounts")
        if p.user in users and p.user:
            problems.append(f"new person {p.name!r} reuses account {p.user!r}")
    end = day + dt.timedelta(days=HORIZON_DAYS)
    earlier = [e.what for e in n.events]
    for e in r.new_events:
        try:
            when = dt.date.fromisoformat(e.date)
        except ValueError:
            problems.append(f"event {e.what[:40]!r}: date {e.date!r} is not YYYY-MM-DD")
            continue
        if not day <= when <= end:
            problems.append(f"event {e.what[:40]!r} on {e.date}: must be between {day} and {end}")
        if near_duplicate(e.what, earlier):
            problems.append(f"event {e.what[:40]!r} repeats an existing event")
    return problems


def apply(r: Revision, n: Narrative, day: dt.date) -> Narrative:
    done = set(r.finished_projects)
    return n.model_copy(
        update={
            "projects": [p for p in n.projects if p not in done] + r.new_projects,
            "people": [*n.people, *r.new_people],
            "events": sorted([*n.events, *r.new_events], key=lambda e: e.date),
            "history": [*n.history, Event(date=day.isoformat(), what=r.summary.strip())],
            "revised": day.isoformat(),
        }
    )


def revise(
    llm: LlmClient, n: Narrative, personas: list[Persona], day: dt.date, happened: str
) -> tuple[Narrative, list[str]]:
    if not due(n, day):
        raise ValueError(f"narrative already revised this week ({n.revised})")
    n = with_personas(n, personas)
    user = (
        f"Company story so far:\n{n.model_dump_json(indent=1)}\n\nToday is {day.strftime('%A')} {day.isoformat()}. "
        f"What really happened since the last revision ({n.revised or 'the start'}):\n{happened or '- nothing logged'}"
        "\n\nRevise the story for the coming weeks without contradicting it. Return JSON with keys summary (1-3 "
        "sentences: what changed this week, grounded in what happened), finished_projects (exact names from "
        "projects that are now done, may be empty), new_projects (short strings, may be empty), new_people (rarely; "
        "name, role, department, user empty) and new_events (2-5 entries: date YYYY-MM-DD between today and four "
        "weeks ahead, what). Keep it mundane: follow-ups of what happened, routine meetings, deadlines."
    )
    r = llm.json(Revision, NARRATIVE_SYSTEM, user)
    problems = check(r, n, day)
    if problems:
        r = llm.json(Revision, NARRATIVE_SYSTEM, user + "\n\nYour proposal was rejected:\n- " + "\n- ".join(problems)
                     + "\nSend the corrected JSON object.")  # fmt: skip
        problems = check(r, n, day)
    if problems:
        room = max(0, MAX_PROJECTS - len(n.projects))
        r = r.model_copy(update={
            "finished_projects": [p for p in r.finished_projects if p in n.projects],
            "new_projects": [p for p in r.new_projects if p not in n.projects][:room],
            "new_people": [p for p in r.new_people if not check(Revision(summary="x", new_people=[p]), n, day)],
            "new_events": [e for e in r.new_events if not check(Revision(summary="x", new_events=[e]), n, day)],
        })  # fmt: skip
        if check(r, n, day):
            raise ValueError(f"revision unusable: {check(r, n, day)}")
    return apply(r, n, day), problems


def happened(world: World, personas: list[Persona], day: dt.date) -> str:
    start = (day - dt.timedelta(days=7)).isoformat()
    parts: list[str] = []
    for p in personas:
        lines = [x for x in world.summary(p.user, day, days=7).splitlines() if x[2:12] >= start or x[:2] != "- "]
        if any(x.startswith("- ") for x in lines):
            parts.append(f"{p.name} ({p.user}):\n" + "\n".join(lines))
    return "\n\n".join(parts)


class RevisionBusy(RuntimeError):
    pass


class AutoRevision(NamedTuple):
    narrative: Narrative
    revised: bool
    note: str
    dropped: list[str]


@contextmanager
def revision_lock(world: World) -> Generator[None]:
    path = world.root / "world" / "revision.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RevisionBusy(f"another planner is revising the narrative (lock {path}); try again shortly") from e
        yield
    finally:
        os.close(fd)


def revise_if_due(llm: LlmClient, world: World, personas: list[Persona], day: dt.date) -> AutoRevision:
    with revision_lock(world):
        n = with_personas(world.narrative(), personas)
        if not due(n, day):
            when = "after" if n.revised > day.isoformat() else "this week, on"
            return AutoRevision(n, False, f"not due: narrative last revised {when} {n.revised}", [])
        evidence = happened(world, personas, day)
        since = f"since the revision of {n.revised}" if n.revised else "narrative has no revision yet"
        if not evidence:
            return AutoRevision(n, False, f"due but no logged work in the 7 days before ({since}); kept as is", [])
        revised, dropped = revise(llm, n, personas, day, evidence)
        world.save_narrative(revised)
        return AutoRevision(revised, True, f"revised {revised.revised}: {revised.history[-1].what}", dropped)
