"""Weekly narrative revision (spec section 6: company narrative "per range, revised weekly").

The LLM only proposes changes from what really happened (the day ground-truth logs of every persona): a dated
summary of the week, finished and new projects, new people and new dated events. Code applies them, so
continuity holds by construction: company, city, summary, existing people and past events never change, and
every current persona (e.g. a newly added IT admin) is in the org chart. Proposals are validated; invalid parts
are sent back once, then dropped and reported.
"""

from __future__ import annotations

import datetime as dt

from ludius.llm import LlmClient
from ludius.planner import NARRATIVE_SYSTEM, Event, Narrative, Person, World, near_duplicate
from ludius.tasks import Persona, Strict

HORIZON_DAYS = 28  # new events land within four weeks of the revision
MAX_PROJECTS = 8


class Revision(Strict):
    summary: str  # what changed in the company this week, 1-3 sentences
    finished_projects: list[str] = []
    new_projects: list[str] = []
    new_people: list[Person] = []
    new_events: list[Event] = []


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def due(n: Narrative, day: dt.date) -> bool:
    """A revision is due once per calendar week (Monday-based); never twice in the same week."""
    return not n.revised or week_start(dt.date.fromisoformat(n.revised)) < week_start(day)


def with_personas(n: Narrative, personas: list[Persona]) -> Narrative:
    """Every persona appears in people with its account; added by code, never left to the LLM."""
    have = {p.user for p in n.people if p.user}
    extra = [Person(name=p.name, role=p.role, department=p.department, user=p.user)
             for p in personas if p.user not in have]  # fmt: skip
    return n.model_copy(update={"people": [*n.people, *extra]}) if extra else n


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
    """One weekly revision. Returns the new narrative and the problems that made parts of the proposal drop."""
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
        # Keep what is valid on its own; drop the rest and report it.
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
    """The last week of every persona's day logs and handed-off threads, as the revision's evidence."""
    parts = [f"{p.name} ({p.user}):\n{s}" for p in personas if (s := world.summary(p.user, day, days=7))]
    return "\n\n".join(parts)
