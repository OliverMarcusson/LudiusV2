"""World state for planning (spec section 6): inbox, calendar, shared documents and cross-persona follow-ups.

Built from what really happened, never from what was planned alone: a plan item counts only when its day
ground-truth log ends it "done". The state is derived again before each plan (idempotent, so it never double
counts) and written to runs/world/state.yaml as the readable record.

- documents: what completed items left on disk (path from the template's parameters), with owner and date.
- messages: a completed item handed off to a colleague is a note from its author to that colleague.
- follow-ups: each handoff becomes a concrete task due for the colleague on the next working day. It stays open
  until a plan item that names it (PlanItem.followup) is logged done; the planner must schedule open ones.
- calendar: the narrative's dated events, per persona mentioned by name.

This is planning data. Mail, calendar and sharing UIs are not driven from it until templates for them exist.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import yaml

from ludius.planner import Narrative, World, load_plan, targets
from ludius.tasks import Persona, Strict

# Where each template's result lands, relative to the persona's profile. Templates without a lasting
# file (web lookups, read-only checks) leave no document.
ARTIFACTS = {
    "notepad_write_note": "Documents\\{file_name}.txt",
    "wordpad_formatted_note": "Documents\\{file_name}.rtf",
    "paint_quick_sketch": "Pictures\\{file_name}.png",
    "explorer_new_folder": "Documents\\{folder_name}",
    "explorer_rename_move": "Documents\\{folder_name}\\{new_name}",
}


class Document(Strict):
    path: str
    owner: str
    date: str
    template: str


class Message(Strict):
    id: str
    date: str
    sender: str
    to: str
    subject: str
    body: str


class Followup(Strict):
    id: str
    user: str  # who has to act
    sender: str
    due: str  # YYYY-MM-DD
    about: str
    document: str = ""
    done_on: str = ""


class CalendarEntry(Strict):
    date: str
    what: str
    attendees: list[str]


class State(Strict):
    documents: list[Document] = []
    messages: list[Message] = []
    followups: list[Followup] = []
    calendar: list[CalendarEntry] = []


def next_workday(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def _done(log: Path) -> set[tuple[str, str]]:
    if not log.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        r: dict[str, Any] = json.loads(line) if line.strip() else {}
        if r.get("event") == "item_end" and r.get("status") == "done":
            out.add((str(r["at"]), str(r["template"])))
    return out


def build(world: World, narrative: Narrative, personas: list[Persona], before: dt.date) -> State:
    """Everything that happened on days before `before`."""
    state = State()
    finished: dict[str, str] = {}  # follow-up id -> date its follow-up item was done
    handoffs: list[Followup] = []
    for path in sorted(world.root.glob("plans/*.yaml")):
        date, user = path.stem[:10], path.stem[11:]
        if date >= before.isoformat():
            continue
        done = _done(world.day_log(user, date))
        for it in load_plan(path).items:
            if (it.at, it.template) not in done:
                continue
            doc = ""
            if fmt := ARTIFACTS.get(it.template):
                try:
                    doc = fmt.format_map(it.params)
                    state.documents.append(Document(path=doc, owner=user, date=date, template=it.template))
                except KeyError:
                    pass  # parameters the plan did not fill: no document recorded
            if it.followup:
                finished.setdefault(it.followup, date)
            if it.handoff:
                fid = f"{date}-{user}-{it.at.replace(':', '')}"
                about = it.context or it.template
                state.messages.append(Message(id=fid, date=date, sender=user, to=it.handoff, subject=about[:80],
                                              body=about + (f" See {doc}." if doc else "")))  # fmt: skip
                due = next_workday(dt.date.fromisoformat(date)).isoformat()
                handoffs.append(Followup(id=fid, user=it.handoff, sender=user, due=due, about=about, document=doc))
    state.followups = [f.model_copy(update={"done_on": finished.get(f.id, "")}) for f in handoffs]
    state.calendar = calendar(narrative, personas)
    return state


def existing_files(world: World, user: str) -> list[str]:
    """Names a new item must not reuse: every target in any of the persona's plans (done or not: a failed save may
    have left a file) plus the optional guest inventory world/guest-files-<user>.txt (one name or path per line,
    written from a read-only listing of the profile). The controller never sees the guest disk itself."""
    out: list[str] = []
    for path in sorted(world.root.glob("plans/*.yaml")):
        if path.stem[11:] == user:
            out += [v for it in load_plan(path).items for _, v in targets(it)]
    inventory = world.root / "world" / f"guest-files-{user}.txt"
    if inventory.exists():
        out += [x for x in inventory.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    return out


def calendar(narrative: Narrative, personas: list[Persona]) -> list[CalendarEntry]:
    """Narrative events per persona named in them (full or first name)."""
    out: list[CalendarEntry] = []
    for e in narrative.events:
        text = e.what.lower()
        who = [p.user for p in personas if p.name.lower() in text or p.name.split()[0].lower() in text]
        if who:
            out.append(CalendarEntry(date=e.date, what=e.what, attendees=who))
    return out


def due(state: State, user: str, day: dt.date) -> list[Followup]:
    return [f for f in state.followups if f.user == user and not f.done_on and f.due <= day.isoformat()]


def brief(state: State, user: str, day: dt.date) -> str:
    """The persona's inbox, calendar, documents and open follow-ups, as planner input."""
    iso, soon = day.isoformat(), (day + dt.timedelta(days=3)).isoformat()
    lines: list[str] = []
    if inbox := [m for m in state.messages if m.to == user and m.date < iso][-10:]:
        lines += ["Inbox:", *(f"- {m.date} from {m.sender}: {m.body}" for m in inbox)]
    if cal := [c for c in state.calendar if user in c.attendees and iso <= c.date <= soon]:
        lines += ["Calendar (next days):", *(f"- {c.date} {c.what}" for c in cal)]
    if docs := [d for d in state.documents if d.owner == user][-8:]:
        lines += ["Own recent files:", *(f"- {d.date} {d.path}" for d in docs)]
    if open_ := due(state, user, day):
        lines += ["Open follow-ups (must be scheduled today):",
                  *(f"- [{f.id}] from {f.sender}: {f.about}" + (f" (file {f.document})" if f.document else "")
                    for f in open_)]  # fmt: skip
    return "\n".join(lines)


def save(state: State, world: World) -> Path:
    path = world.root / "world" / "state.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(state.model_dump(), sort_keys=False, allow_unicode=True, width=110), "utf-8")
    return path
