from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, planner, revision
from ludius.planner import DayPlan, Event, Narrative, Person, World
from ludius.tasks import Persona

DAY = dt.date(2026, 11, 2)


class ScriptedLlm:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers, self.prompts = list(answers), list[str]()

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9) -> Any:
        self.prompts.append(user)
        return cls.model_validate(self.answers.pop(0))


def _narrative() -> Narrative:
    return Narrative(
        company="Harbourline", city="Rotterdam", summary="Packaging maker.",
        people=[Person(name="Anna Berg", role="Finance controller", department="Finance", user="aberg")],
        projects=["ERP migration", "office move"],
        events=[Event(date="2026-10-28", what="ERP test review")],
    )  # fmt: skip


PERSONAS = [Persona(name="Anna Berg", user="aberg"), Persona(name="Lars Nord", user="lnord", role="IT administrator")]


def test_revision_keeps_continuity_adds_personas_and_is_dated(tmp_path: Path) -> None:
    llm = ScriptedLlm({"summary": "ERP tests passed; the move date is set.", "finished_projects": ["ERP migration"],
                       "new_projects": ["ERP rollout"],
                       "new_events": [{"date": "2026-11-12", "what": "ERP go-live"}]})  # fmt: skip
    n, dropped = revision.revise(llm, _narrative(), PERSONAS, DAY, "- 2026-10-28 aberg erp review done")  # type: ignore[arg-type]
    assert dropped == []
    assert (n.company, n.city, n.summary) == ("Harbourline", "Rotterdam", "Packaging maker.")
    assert [p.user for p in n.people] == ["aberg", "lnord"]
    assert "lnord" in llm.prompts[0]
    assert n.projects == ["office move", "ERP rollout"]
    assert [e.what for e in n.events] == ["ERP test review", "ERP go-live"]
    assert n.revised == "2026-11-02" and n.history[-1].date == "2026-11-02"
    with pytest.raises(ValueError, match="already revised"):
        revision.revise(llm, n, PERSONAS, DAY + dt.timedelta(days=4), "")  # type: ignore[arg-type]
    assert revision.due(n, DAY + dt.timedelta(days=7))

    world = World(tmp_path)
    world.save_narrative(n)
    assert world.narrative() == n
    assert (tmp_path / "world" / "revisions" / "2026-11-02.yaml").exists()


def test_invalid_parts_are_sent_back_once_then_dropped() -> None:
    bad = {"summary": "Week done.", "finished_projects": ["no such project"],
           "new_people": [{"name": "Anna Berg", "role": "x", "department": "x"}],
           "new_events": [{"date": "2027-03-01", "what": "far away"},
                          {"date": "2026-11-05", "what": "Budget review"}]}  # fmt: skip
    llm = ScriptedLlm(bad, bad)
    n, dropped = revision.revise(llm, _narrative(), PERSONAS, DAY, "")  # type: ignore[arg-type]
    assert len(llm.prompts) == 2 and "rejected" in llm.prompts[1]
    assert any("no such project" in d for d in dropped) and any("far away" in d for d in dropped)
    assert n.projects == ["ERP migration", "office move"]
    assert [p.name for p in n.people] == ["Anna Berg", "Lars Nord"]
    assert [e.what for e in n.events] == ["ERP test review", "Budget review"]


def _world(tmp_path: Path, revised: str = "") -> World:
    world = World(tmp_path)
    world.save_narrative(_narrative().model_copy(update={"revised": revised}))
    return world


def _log(world: World, user: str, day: dt.date, what: str) -> None:
    path = world.day_log(user, day.isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"event": "item_end", "at": "09:00", "template": "excel_report", "status": "done", "context": what}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def _answer(summary: str, event: str = "Warehouse stock count") -> dict[str, Any]:
    return {"summary": summary, "new_events": [{"date": "2026-11-12", "what": event}]}


def test_one_revision_per_week_across_personas_then_due_next_week(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _log(world, "aberg", DAY - dt.timedelta(days=3), "closed the ERP test")
    _log(world, "lnord", DAY - dt.timedelta(days=2), "patched the file server")
    llm = ScriptedLlm(_answer("Week one."), _answer("Week two.", "Supplier contract review"))
    first = revision.revise_if_due(llm, world, PERSONAS, DAY)  # type: ignore[arg-type]
    other = revision.revise_if_due(llm, world, PERSONAS, DAY + dt.timedelta(days=1))  # type: ignore[arg-type]
    again = revision.revise_if_due(llm, world, PERSONAS, DAY)  # type: ignore[arg-type]
    assert [first.revised, other.revised, again.revised] == [True, False, False]
    assert len(llm.prompts) == 1
    assert "closed the ERP test" in llm.prompts[0] and "patched the file server" in llm.prompts[0]
    assert other.narrative == again.narrative == first.narrative == world.narrative()
    assert "this week" in other.note and world.narrative().revised == "2026-11-02"
    assert (tmp_path / "world" / "revisions" / "2026-11-02.yaml").exists()

    nxt = DAY + dt.timedelta(days=7)
    _log(world, "aberg", DAY + dt.timedelta(days=2), "booked the move")
    second = revision.revise_if_due(llm, world, PERSONAS, nxt)  # type: ignore[arg-type]
    assert second.revised and len(llm.prompts) == 2 and "Week one." in llm.prompts[1]
    assert [h.what for h in world.narrative().history] == ["Week one.", "Week two."]
    assert (tmp_path / "world" / "revisions" / "2026-11-09.yaml").exists()
    assert not revision.revise_if_due(llm, world, PERSONAS, nxt).revised and len(llm.prompts) == 2  # type: ignore[arg-type]


def test_no_evidence_or_future_revision_keeps_the_narrative_and_calls_no_model(tmp_path: Path) -> None:
    llm = ScriptedLlm()
    world = _world(tmp_path)
    quiet = revision.revise_if_due(llm, world, PERSONAS, DAY)  # type: ignore[arg-type]
    assert not quiet.revised and "no revision yet" in quiet.note and world.narrative().revised == ""
    assert world.narrative().accounts == ["aberg", "lnord"]
    assert not (tmp_path / "world" / "revisions").exists()

    future = _world(tmp_path, revised="2026-11-09")
    _log(future, "aberg", DAY - dt.timedelta(days=1), "evidence exists")
    out = revision.revise_if_due(llm, future, PERSONAS, DAY)  # type: ignore[arg-type]
    assert not out.revised and "after 2026-11-09" in out.note and future.narrative().revised == "2026-11-09"

    old = _world(tmp_path, revised="2026-11-02")
    out = revision.revise_if_due(llm, old, PERSONAS, DAY + dt.timedelta(days=7))  # type: ignore[arg-type]
    assert not out.revised and "no logged work" in out.note and old.narrative().revised == "2026-11-02"
    assert llm.prompts == []


def test_busy_lock_fails_clearly_and_is_released_afterwards(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _log(world, "aberg", DAY - dt.timedelta(days=1), "evidence")
    inner = list[Exception]()

    class Racing(ScriptedLlm):
        def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9) -> Any:
            try:
                revision.revise_if_due(self, world, PERSONAS, DAY)  # type: ignore[arg-type]
            except revision.RevisionBusy as e:
                inner.append(e)
            return super().json(cls, system, user, temperature)

    llm = Racing(_answer("Only once."))
    assert revision.revise_if_due(llm, world, PERSONAS, DAY).revised  # type: ignore[arg-type]
    assert len(inner) == 1 and "another planner is revising" in str(inner[0]) and len(llm.prompts) == 1
    assert not revision.revise_if_due(llm, world, PERSONAS, DAY).revised  # type: ignore[arg-type]  # lock was released


def test_plan_day_revises_first_and_plans_with_the_revised_narrative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    for user in ("aberg", "lnord"):
        _log(world, user, DAY - dt.timedelta(days=1), f"{user} did the daily work")
    llm = ScriptedLlm(_answer("Auto revised."))
    planned = list[Narrative]()

    def fake_plan_day(_llm: Any, narrative: Narrative, persona: Any, date: dt.date, *_a: Any, **_k: Any) -> Any:
        planned.append(narrative)
        return DayPlan(persona=persona.user, date=date.isoformat(), items=[]), []

    class FakeClient:
        cost = 0.0

        def __new__(cls, *_a: Any, **_k: Any) -> Any:
            return llm

    llm.cost = 0.0  # type: ignore[attr-defined]
    llm.close = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    monkeypatch.setattr(cli, "LlmClient", FakeClient)
    monkeypatch.setattr(planner, "plan_day", fake_plan_day)
    monkeypatch.delenv("LUDIUS_SCENARIO", raising=False)
    for user in ("aberg", "lnord"):
        args = argparse.Namespace(what="day", persona=str(cli.DATA / "personas" / f"{user}.yaml"),
                                  date=DAY.isoformat(), model="m", scenario=None, idea=None)  # fmt: skip
        assert cli.cmd_plan(args) == 0
    assert len(llm.prompts) == 1
    assert [n.revised for n in planned] == ["2026-11-02", "2026-11-02"]
    assert planned[0].history[-1].what == "Auto revised." and planned[0] == planned[1]
    out = capsys.readouterr().out
    assert "narrative: revised 2026-11-02" in out and "not due: narrative last revised this week" in out


def test_only_persona_accounts_are_colleagues() -> None:
    n = _narrative().model_copy(
        update={
            "people": [
                *_narrative().people,
                Person(name="Rogier Jansen", role="Operations director", department="Operations", user="rjansen"),
                Person(name="Lars Nord", role="IT administrator", department="IT", user="lnord"),
            ]
        }
    )
    assert n.colleagues("aberg") == set()
    n = revision.with_personas(n, PERSONAS)
    assert n.accounts == ["aberg", "lnord"] and n.colleagues("aberg") == {"lnord"}
    item = planner.PlanItem(
        template="outlook_send_mail",
        at="10:00",
        params={"to": f"rjansen@{planner.MAIL_DOMAIN}", "to_name": "Rogier Jansen"},
    )
    assert planner._recipient(item, n.colleagues("aberg"), {"rjansen": "Rogier Jansen"})
