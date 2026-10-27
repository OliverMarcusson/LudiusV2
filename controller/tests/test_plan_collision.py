"""A planned file or folder name never collides with an existing guest file, nor with another item of the plan."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, nightly, world_state
from ludius.planner import DayPlan, Narrative, Person, PlanItem, World, plan_day, validate
from ludius.policy import Policy

DAY = dt.date(2026, 10, 28)
EXISTING = [
    "C:\\Users\\aberg\\Documents\\Close Checklist Q4.txt",  # a path, as a guest listing gives it
    "Documents\\oct close q4",  # a folder
    "ar ageing note.rtf",
]


class ScriptedLlm:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers, self.prompts, self.cost = list(answers), list[str](), 0.0

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9) -> Any:
        self.prompts.append(user)
        return cls.model_validate(self.answers.pop(0))

    def close(self) -> None:
        pass


def note(name: str, at: str = "09:00", template: str = "notepad_write_note", **params: str) -> dict[str, Any]:
    key = "folder_name" if template == "explorer_new_folder" else "file_name"
    p = {key: name, **params}
    if template == "notepad_write_note":
        p.setdefault("body", f"note about {name}\nline 2")
    return {"at": at, "template": template, "params": p, "context": "Needs it for the close."}


def check(*items: dict[str, Any], existing: list[str] = EXISTING) -> list[str]:
    templates = cli.plan_sources().templates
    its = [PlanItem.model_validate(i) for i in items]
    return [why for _, why in validate(its, templates, [], Policy(), existing=existing)]


@pytest.mark.parametrize(
    "name",
    [
        "close checklist q4",  # extension hidden by Explorer
        "CLOSE CHECKLIST Q4.TXT",  # case-insensitive, with extension
        "close checklist q4.rtf",  # other extension, same stem
        "Oct Close Q4",  # an existing folder
        "ar ageing note",  # existing rtf, saved as txt
    ],
)
def test_names_of_existing_files_are_rejected(name: str) -> None:
    [why] = check(note(name))
    assert "already exists" in why and name in why


@pytest.mark.parametrize("name", ["NUL", "con.txt", "Lpt1", "notes.", "notes ", " notes", "../notes", "a/b"])
def test_reserved_or_aliased_names_are_rejected(name: str) -> None:
    [why] = check(note(name))
    assert "not a valid file name" in why


def test_same_name_twice_in_one_plan_is_rejected_whatever_the_template() -> None:
    first = note("budget q4", body="accruals first, then prepaids")
    again = note(
        "Budget Q4.txt", "10:00", body="ask sdekker about freight estimates"
    )  # short names skip near-duplicate
    folder = note("budget q4", "11:00", "explorer_new_folder")
    assert check(first, again, folder, existing=[]) == [
        "file_name: 'Budget Q4.txt' already exists or is used by another item, choose a different name",
        "folder_name: 'budget q4' already exists or is used by another item, choose a different name",
    ]


def test_intentional_existing_file_workflow_stays_valid() -> None:
    move: dict[str, Any] = {
        "at": "11:00",
        "template": "explorer_rename_move",
        "context": "Files the note away.",
        "params": {
            "file_name": "close checklist q4",
            "new_name": "close checklist q4 old",
            "folder_name": "oct close q4",
        },
    }
    assert check(move) == []  # acts on the existing file and folder, creates only new_name
    clash = {**move, "params": {**move["params"], "new_name": "ar ageing note"}}
    assert "new_name: 'ar ageing note' already exists" in check(clash)[0]
    fresh = note("another one")
    assert check(fresh, move) == []


def test_collision_is_repaired_once_then_dropped_if_unresolved() -> None:
    people = [Person(name="Anna Berg", role="r", department="d", user="aberg")]
    narrative = Narrative(company="H", city="R", summary="s", people=people, projects=[], events=[])
    anna = next(p for p in cli.plan_sources().personas if p.user == "aberg")
    templates = cli.plan_sources().templates
    bad, good = note("close checklist q4"), note("close checklist q4 v2")
    llm = ScriptedLlm({"items": [bad]}, {"items": [good]})
    plan, rejects = plan_day(llm, narrative, anna, DAY, templates, "", [], existing=EXISTING)  # type: ignore[arg-type]
    assert "- Close Checklist Q4.txt".casefold() in llm.prompts[0].casefold() and "- oct close q4" in llm.prompts[0]
    assert "already exists" in llm.prompts[1] and "item 0 (notepad_write_note at 09:00)" in llm.prompts[1]
    assert [it.params["file_name"] for it in plan.items] == ["close checklist q4 v2"] and rejects == []

    llm = ScriptedLlm({"items": [bad]}, {"items": [bad, note("fresh topic notes", "10:00")]})
    plan, rejects = plan_day(llm, narrative, anna, DAY, templates, "", [], existing=EXISTING)  # type: ignore[arg-type]
    assert [it.params["file_name"] for it in plan.items] == ["fresh topic notes"]  # the colliding item never runs
    assert len(rejects) == 1 and "already exists" in rejects[0] and not llm.answers


def test_existing_files_come_from_all_plans_and_the_guest_inventory(tmp_path: Path) -> None:
    world = World(tmp_path)
    templates = cli.plan_sources().templates
    items = [
        PlanItem.model_validate(i) for i in (note("old plan note"), note("old pics", "10:00", "explorer_new_folder"))
    ]
    for user, date in (("aberg", "2026-10-20"), ("aberg", "2026-11-02"), ("lnord", "2026-10-20")):
        world.save_plan(DayPlan(persona=user, date=date, items=items[:1] if user == "lnord" else items), templates)
    inventory = tmp_path / "world" / "guest-files-aberg.txt"
    inventory.write_text("\ufeffC:\\Users\\aberg\\Documents\\admission test note.txt\r\n\r\n", encoding="utf-8")
    assert sorted(world_state.existing_files(world, "aberg")) == sorted(
        [
            "old plan note",
            "old pics",
            "old plan note",
            "old pics",
            "C:\\Users\\aberg\\Documents\\admission test note.txt",
        ]
    )
    assert world_state.existing_files(world, "lnord") == ["old plan note"]  # per profile, no inventory


def test_generate_day_feeds_the_inventory_to_the_planner(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.save_narrative(Narrative(company="H", city="R", summary="s", people=[], projects=[], events=[]))
    (tmp_path / "world" / "guest-files-aberg.txt").write_text("Documents\\admission test note.txt\n", encoding="utf-8")
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    llm = ScriptedLlm({"items": [note("admission test note")]}, {"items": [note("admission note oct")]})
    g = nightly.generate_day(llm, world, anna, src, DAY)  # type: ignore[arg-type]
    assert "- admission test note.txt" in llm.prompts[0]
    assert [it.params["file_name"] for it in g.plan.items] == ["admission note oct"] and g.rejects == []


def test_review_gaps_content_override_long_extension_and_renaming_earlier_content() -> None:
    # Review of 4c709d9: runtime content overrides params; any extension hides the stem; a rename's source
    # may be a name the persona wrote earlier.
    assert "already exists" in check({**note("fresh"), "content": {"file_name": "ar ageing note"}})[0]
    assert "already exists" in check(note("notes"), existing=["notes.markdown"])[0]
    templates = cli.plan_sources().templates
    move = PlanItem.model_validate(
        {
            "at": "11:00",
            "template": "explorer_rename_move",
            "context": "Files it away.",
            "params": {"file_name": "supplier brief", "new_name": "signed", "folder_name": "oct close q4"},
        }
    )
    assert validate([move], templates, ["supplier brief"], Policy(), existing=EXISTING) == []
