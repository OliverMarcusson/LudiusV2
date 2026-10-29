from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from ludius import world_state
from ludius.cli import load_templates
from ludius.planner import DayPlan, Event, Narrative, Person, PlanItem, World, plan_day
from ludius.tasks import Persona

ANNA = Persona(name="Anna Berg", user="aberg", role="Finance controller")
LARS = Persona(name="Lars Nord", user="lnord", role="IT administrator")
MON, TUE, WED = dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)


class ScriptedLlm:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers, self.prompts = list(answers), list[str]()

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9) -> Any:
        self.prompts.append(user)
        return cls.model_validate(self.answers.pop(0))


NARRATIVE = Narrative(
    company="Harbourline", city="Rotterdam", summary="Packaging maker.",
    people=[Person(name=p.name, role=p.role, department="", user=p.user) for p in (ANNA, LARS)],
    projects=["ERP migration"], events=[Event(date="2026-11-04", what="Lars and Finance check ERP user access")],
)  # fmt: skip


def _day(world: World, user: str, date: dt.date, items: list[PlanItem], done: list[str]) -> None:
    world.save_plan(DayPlan(persona=user, date=date.isoformat(), items=items), load_templates())
    log = world.day_log(user, date.isoformat())
    log.parent.mkdir(parents=True, exist_ok=True)
    ends = [{"event": "item_end", "at": it.at, "template": it.template, "status": "done" if it.at in done else "failed"}
            for it in items]  # fmt: skip
    log.write_text("\n".join(map(json.dumps, ends)), encoding="utf-8")


def test_handoff_becomes_a_scheduled_followup_until_done(tmp_path: Path) -> None:
    world = World(tmp_path)
    note = PlanItem(at="09:00", template="notepad_write_note", handoff="lnord",
                    params={"file_name": "erp access list", "body": "finance users\nroles"},
                    context="Lists the finance users who need ERP access for Lars.")  # fmt: skip
    failed = PlanItem(at="10:00", template="explorer_new_folder", handoff="lnord", params={"folder_name": "erp q4"})
    _day(world, "aberg", MON, [note, failed], done=["09:00"])

    state = world_state.build(world, NARRATIVE, [ANNA, LARS], TUE)
    assert [d.path for d in state.documents] == ["Documents\\erp access list.txt"]  # failed item left nothing
    assert [(m.sender, m.to) for m in state.messages] == [("aberg", "lnord")]
    [f] = world_state.due(state, "lnord", TUE)
    assert (f.due, f.document) == ("2026-11-03", "Documents\\erp access list.txt")
    assert world_state.due(state, "lnord", MON) == []  # not before it is due
    text = world_state.brief(state, "lnord", TUE)
    assert "Inbox:" in text and f.id in text and "ERP user access" in text  # calendar names Lars
    assert world_state.save(state, world).exists()

    # Planning: a draft without the follow-up is sent back; the corrected one schedules it by id.
    item = {"at": "08:30", "template": "edge_web_lookup", "params": {"search_query": "erp role based access finance"},
            "context": "Checks role options for Anna's list."}  # fmt: skip
    llm = ScriptedLlm({"items": [item]}, {"items": [item | {"followup": f.id}]})
    plan, rejects = plan_day(llm, NARRATIVE, LARS, TUE, load_templates(), text, [],  # type: ignore[arg-type]
                             followups={f.id: f.about})  # fmt: skip
    assert f"follow-up {f.id} is not scheduled" in llm.prompts[1]
    assert rejects == [] and plan.items[0].followup == f.id

    _day(world, "lnord", TUE, plan.items, done=["08:30"])
    later = world_state.build(world, NARRATIVE, [ANNA, LARS], WED)
    assert later.followups[0].done_on == "2026-11-03" and world_state.due(later, "lnord", WED) == []


def test_unknown_followup_id_is_rejected() -> None:
    bad = {"at": "08:30", "template": "edge_web_lookup", "params": {"search_query": "erp access"}, "followup": "x"}
    llm = ScriptedLlm({"items": [bad]}, {"items": [bad]})
    plan, rejects = plan_day(llm, NARRATIVE, LARS, TUE, load_templates(), "", [])  # type: ignore[arg-type]
    assert plan.items == [] and "not an open follow-up" in rejects[0]


def test_chat_request_becomes_followup_settled_by_the_work_not_the_read(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.save_narrative(NARRATIVE)
    ask = PlanItem(at="09:00", template="chat_direct_message", handoff="lnord",
                   params={"to": "lnord@ludius.internal", "to_name": "Lars Nord",
                           "message": "could you list the erp roles finance needs by friday"},
                   context="Asks Lars for the ERP roles.")  # fmt: skip
    _day(world, "aberg", MON, [ask], done=["09:00"])
    [f] = world_state.due(world_state.build(world, NARRATIVE, [ANNA, LARS], TUE), "lnord", TUE)
    assert "chat message 'could you list the erp roles finance needs by friday'" in f.about and not f.subject

    read = {"at": "08:30", "template": "chat_read_messages", "followup": f.id,
            "params": {"to": "aberg@ludius.internal", "to_name": "Anna Berg"}}  # fmt: skip
    llm = ScriptedLlm({"items": [read]}, {"items": [read]})
    plan, rejects = plan_day(llm, NARRATIVE, LARS, TUE, load_templates(), "", [],  # type: ignore[arg-type]
                             followups={f.id: f.about})  # fmt: skip
    assert plan.items == [] and "does not settle a follow-up" in rejects[0]
