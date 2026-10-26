from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from ludius import revision
from ludius.planner import Event, Narrative, Person, World
from ludius.tasks import Persona

DAY = dt.date(2026, 11, 2)  # a Monday


class ScriptedLlm:
    """Stands in for LlmClient.json: hands out prepared answers and records the prompts."""

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
    assert [p.user for p in n.people] == ["aberg", "lnord"]  # current persona added by code
    assert "lnord" in llm.prompts[0]  # and the LLM sees it
    assert n.projects == ["office move", "ERP rollout"]
    assert [e.what for e in n.events] == ["ERP test review", "ERP go-live"]  # past event kept, sorted by date
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
    assert n.projects == ["ERP migration", "office move"]  # unknown "finished" project ignored
    assert [p.name for p in n.people] == ["Anna Berg", "Lars Nord"]  # duplicate Anna not added
    assert [e.what for e in n.events] == ["ERP test review", "Budget review"]  # only the in-horizon event
