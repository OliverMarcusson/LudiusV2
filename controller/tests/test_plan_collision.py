from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, nightly, planner, world_state
from ludius.planner import DayPlan, Narrative, Person, PlanItem, World, plan_day, validate
from ludius.policy import Policy

DAY = dt.date(2026, 10, 28)
EXISTING = [
    "C:\\Users\\aberg\\Documents\\Close Checklist Q4.txt",
    "Documents\\oct close q4",
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


def check(*items: dict[str, Any], existing: list[str] = EXISTING, owned: dict[str, str] | None = None) -> list[str]:
    templates = cli.plan_sources().templates
    its = [PlanItem.model_validate(i) for i in items]
    return [why for _, why in validate(its, templates, [], Policy(), existing=existing, owned=owned)]


def _slots(m: pytest.MonkeyPatch, *items: dict[str, Any]) -> None:
    m.setattr(planner, "day_slots", lambda *_a, **_k: [  # pyright: ignore[reportUnknownLambdaType]
        PlanItem(at=i["at"], template=i["template"], followup=i.get("followup", "")) for i in items])  # fmt: skip


@pytest.mark.parametrize(
    "name",
    [
        "close checklist q4",
        "CLOSE CHECKLIST Q4.TXT",
        "close checklist q4.rtf",
        "Oct Close Q4",
        "ar ageing note",
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
    again = note("Budget Q4.txt", "10:00", body="ask sdekker about freight estimates")
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
            "file_ext": "txt",
            "new_name": "close checklist q4 old",
            "folder_name": "oct close q4",
        },
    }
    owned = {"documents\\close checklist q4.txt": "2026-10-01"}
    assert check(move, owned=owned) == []
    clash = {**move, "params": {**move["params"], "new_name": "ar ageing note"}}
    assert "new_name: 'ar ageing note' already exists" in check(clash, owned=owned)[0]
    fresh = note("another one")
    assert check(fresh, move, owned=owned) == []


def test_collision_is_repaired_once_then_dropped_if_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    people = [Person(name="Anna Berg", role="r", department="d", user="aberg")]
    narrative = Narrative(company="H", city="R", summary="s", people=people, projects=[], events=[])
    anna = next(p for p in cli.plan_sources().personas if p.user == "aberg")
    templates = cli.plan_sources().templates
    bad, good = note("close checklist q4"), note("close checklist q4 v2")
    _slots(monkeypatch, bad)
    llm = ScriptedLlm({"items": [bad]}, {"items": [good]})
    plan, rejects = plan_day(llm, narrative, anna, DAY, templates, "", [], existing=EXISTING)  # type: ignore[arg-type]
    assert "- Close Checklist Q4.txt".casefold() in llm.prompts[0].casefold() and "- oct close q4" in llm.prompts[0]
    assert "already exists" in llm.prompts[1] and "item 0 (notepad_write_note at 09:00)" in llm.prompts[1]
    assert [it.params["file_name"] for it in plan.items] == ["close checklist q4 v2"] and rejects == []

    _slots(monkeypatch, bad, note("fresh topic notes", "10:00"))
    llm = ScriptedLlm({"items": [bad]}, {"items": [bad, note("fresh topic notes", "10:00")]})
    plan, rejects = plan_day(llm, narrative, anna, DAY, templates, "", [], existing=EXISTING)  # type: ignore[arg-type]
    assert [it.params["file_name"] for it in plan.items] == ["fresh topic notes"]
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
    assert world_state.existing_files(world, "lnord") == ["old plan note"]


def test_generate_day_feeds_the_inventory_to_the_planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(tmp_path)
    world.save_narrative(Narrative(company="H", city="R", summary="s", people=[], projects=[], events=[]))
    (tmp_path / "world" / "guest-files-aberg.txt").write_text("Documents\\admission test note.txt\n", encoding="utf-8")
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    _slots(monkeypatch, note("admission test note"))
    llm = ScriptedLlm({"items": [note("admission test note")]}, {"items": [note("admission note oct")]})
    g = nightly.generate_day(llm, world, anna, src, DAY)  # type: ignore[arg-type]
    assert "- admission test note.txt" in llm.prompts[0]
    assert [it.params["file_name"] for it in g.plan.items] == ["admission note oct"]
    assert g.rejects == ["requests: aberg: no manager with a mailbox in the org chart"]


def test_review_gaps_content_override_long_extension_and_renaming_earlier_content() -> None:
    assert "already exists" in check({**note("fresh"), "content": {"file_name": "ar ageing note"}})[0]
    assert "already exists" in check(note("notes"), existing=["notes.markdown"])[0]
    templates = cli.plan_sources().templates
    move = PlanItem.model_validate(
        {
            "at": "11:00",
            "template": "explorer_rename_move",
            "context": "Files it away.",
            "params": {"file_name": "supplier brief", "file_ext": "docx", "new_name": "signed", "folder_name": "oct close q4"},
        }
    )
    owned = {"documents\\supplier brief.docx": "2026-10-01"}
    assert validate([move], templates, ["supplier brief"], Policy(), existing=EXISTING, owned=owned) == []


def test_dotted_folder_names_stay_distinct() -> None:
    folder = note("finance.payables", template="explorer_new_folder")
    assert check(folder, existing=["finance.receivables"]) == []
    assert check(note("finance.receivables", at="09:30", template="explorer_new_folder"), folder, existing=[]) == []


def test_code_builds_the_day_skeleton() -> None:
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    templates = planner.role_templates(src.templates, anna)
    inbox = {("lnord", "ERP roles"), ("sdekker", "Freight")}
    for day in range(10):
        date = DAY + dt.timedelta(days=day)
        window = planner.Scenario(date=date.isoformat(), start="08:00", end="09:00", signal="powershell")
        items = planner.day_slots(
            anna, date, templates, {"lnord": "Lars Nord"}, {"lnord", "sdekker"}, ["general"],
            {"f1": "Lars waits", "f2": "Send Lars the list"}, {"f1": ("lnord", "ERP roles")}, inbox,
            {("sdekker", "Close sync")}, {}, [], window, ["powershell_admin_check"], {}, {"f2": "word_revise_and_mail"},
            {("sdekker", "Freight")}, ["10:00"], {},
        )  # fmt: skip
        times = [planner._minutes(it.at) for it in items]  # pyright: ignore[reportPrivateUsage]
        assert times == sorted(times) and all(m % 5 == 0 for m in times) and "10:00" not in [it.at for it in items]
        assert min(times) >= planner._minutes("08:10")  # pyright: ignore[reportPrivateUsage]
        work = [it for it in items if it.template != planner.BREAK]
        assert 5 <= len(work) <= 8 and len(items) == len(work) + 1
        [look] = [it for it in work if it.template == "powershell_admin_check"]
        assert look.at >= "09:15"
        [f2] = [it for it in work if it.followup == "f2"]
        assert f2.template != "word_revise_and_mail"
        [read] = [it for it in work if it.template == "outlook_read_mail"]
        assert all(b - a >= planner.SLOT_GAP for a, b in zip(times, times[1:], strict=False))
        [reply] = [it for it in work if it.followup == "f1"]
        assert reply.params["to"] == "lnord@ludius.internal" and reply.params["subject"] == "ERP roles"
        [answer] = [it for it in work if it.template == "outlook_answer_invite"]
        assert answer.params["subject"] == "Close sync" and answer.handoff == ""
        assert all(it.params["subject"] == "Freight" for it in work if it.template == "outlook_read_mail")


def test_social_runs_plan_more_chat_and_gossip_every_day(monkeypatch: pytest.MonkeyPatch) -> None:
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    templates = planner.role_templates(src.templates, anna)

    def chat(social: float) -> tuple[int, int]:
        monkeypatch.setenv("LUDIUS_SOCIAL", str(social))
        n = gossip = 0
        for day in range(20):
            items = planner.day_slots(
                anna, DAY + dt.timedelta(days=day), templates, {"lnord": "Lars Nord"}, {"lnord"}, ["general"], {},
                {}, set(), set(), {"documents\\todo.txt": "2026-10-01"}, [], None, [], {}, {}, set(), [], {},
            )  # fmt: skip
            caps = [templates[it.template].capability or "" for it in items if it.template in templates]
            n += sum(c.startswith("chat.") for c in caps)
            gossip += any("gossip" in it.context for it in items)
        return n, gossip

    calm, busy = chat(1.0), chat(6.0)
    assert busy[0] > 2 * calm[0] and busy[1] == 20
    for bad in ("0", "-1", "nan", "inf", "lots"):
        monkeypatch.setenv("LUDIUS_SOCIAL", bad)
        with pytest.raises(ValueError, match="LUDIUS_SOCIAL"):
            planner.day_slots(anna, DAY, templates, {}, set(), [], {}, {}, set(), set(), {}, [], None, [], {}, {},
                              set(), [], {})  # fmt: skip


def test_follow_ups_stay_within_the_budget_and_threads_need_a_reply() -> None:
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    templates = {k: t for k, t in planner.role_templates(src.templates, anna).items() if t.capability != "mail.reply"}
    followups = {f"f{i}": "Lars waits" for i in range(9)}
    for day in range(10):
        items = planner.day_slots(
            anna, DAY + dt.timedelta(days=day), templates, {"lnord": "Lars Nord"}, {"lnord"}, ["general"],
            followups, {"f0": ("lnord", "ERP roles")}, {("lnord", "ERP roles")}, set(), {}, [], None, [], {}, {},
            set(), [], {},
        )  # fmt: skip
        work = [it for it in items if it.template != planner.BREAK]
        assert 5 <= len(work) <= 8 and "f0" not in {it.followup for it in work}


def test_code_picks_an_own_file_that_fits_the_template() -> None:
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    keys = ("explorer_file_download", "explorer_zip_file", "explorer_extract_zip")
    templates = {k: src.templates[k] for k in keys}
    for owned, want in (({"downloads\\q4 plan.xlsx": "2026-01-01"}, {"explorer_file_download"}),
                        ({"documents\\notes.txt": "2026-01-01"}, {"explorer_zip_file"}),
                        ({"documents\\notes.zip": "2026-01-01"}, {"explorer_extract_zip"})):  # fmt: skip
        items = planner.day_slots(
            anna, DAY, templates, {}, set(), [], {}, {}, set(), set(), owned, [], None, [], {}, {}, set(), [], {},
        )  # fmt: skip
        work = [it for it in items if it.template != planner.BREAK]
        assert {it.template for it in work} == want
        [it] = work
        assert (
            it.params
            == {
                "explorer_file_download": {"file_name": "q4 plan.xlsx"},
                "explorer_zip_file": {"file_name": "notes", "file_ext": "txt"},
                "explorer_extract_zip": {"file_name": "notes"},
            }[it.template]
        )


def test_code_picks_a_real_attachment_and_skips_threads_outside_the_inbox() -> None:
    src = cli.plan_sources()
    anna = next(p for p in src.personas if p.user == "aberg")
    templates = {k: src.templates[k] for k in ("outlook_save_attachment", "outlook_reply_mail")}
    inbox = {("lnord", "Q3"), ("sdekker", "Old")}
    attached = {("lnord", "Q3"): ("a.xlsx", "A.xlsx", "plan.docx", "x.exe"), ("sdekker", "Old"): ("b.docx",),
                ("eyilmaz", "Gone"): ("c.docx",)}  # fmt: skip
    for owned, want in (({}, {"plan.docx", "b.docx"}), ({"documents\\b.docx": "2026-01-01"}, {"plan.docx"})):
        items = planner.day_slots(
            anna, DAY, templates, {}, set(), [], {"f0": "Lars waits"}, {"f0": ("eyilmaz", "Gone")}, inbox, set(),
            owned, [], None, [], {}, {}, set(), [], attached,
        )  # fmt: skip
        work = [it for it in items if it.template != planner.BREAK]
        saves = [it for it in work if it.template == "outlook_save_attachment"]
        assert saves and not any(it.followup for it in work)
        for it in saves:
            assert it.params["file_name"] in want
            assert (it.params["sender"].partition("@")[0], it.params["subject"]) in inbox
    items = planner.day_slots(anna, DAY, templates, {}, set(), [], {}, {}, inbox, set(), {}, [], None, [], {}, {},
                              set(), [], {})  # fmt: skip
    assert not [it for it in items if it.template == "outlook_save_attachment"]


def test_the_writer_cannot_change_fixed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    people = [
        Person(name=n, role="r", department="d", user=u) for n, u in (("Anna Berg", "aberg"), ("Lars Nord", "lnord"))
    ]
    narrative = Narrative(
        company="H", city="R", summary="s", people=people, projects=[], events=[], accounts=[p.user for p in people]
    )
    anna = next(p for p in cli.plan_sources().personas if p.user == "aberg")
    fixed = {"to": "lnord@ludius.internal", "to_name": "Lars Nord", "subject": "ERP roles"}
    monkeypatch.setattr(planner, "day_slots", lambda *_a, **_k: [  # pyright: ignore[reportUnknownLambdaType]
        PlanItem(at="09:00", template="outlook_send_mail", params=dict(fixed), handoff="lnord")])  # fmt: skip
    mail = {"at": "09:00", "template": "outlook_send_mail", "params": {"subject": "Freight", "body": "Hi Lars"},
            "content": {"subject": "Freight", "to": "aberg@ludius.internal"}, "context": "Sends it."}  # fmt: skip
    plan, _ = plan_day(ScriptedLlm({"items": [mail]}), narrative, anna, DAY, cli.plan_sources().templates, "", [])  # type: ignore[arg-type]
    [it] = plan.items
    assert {**it.params, **it.content}["subject"] == "ERP roles" and {**it.params, **it.content}["to"] == fixed["to"]


def test_managers_plan_their_requests_in_parallel_but_each_manager_once(tmp_path: Path) -> None:
    import threading

    shared = nightly.shared_lock(World(tmp_path))
    held: list[str] = []

    def other() -> None:
        with shared("boss-b"):
            held.append("boss-b")

    with shared("boss-a"):
        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        assert held == ["boss-b"]
        with (
            pytest.raises(nightly.durable.DeskBusy),
            nightly.durable.desk_lock(tmp_path / "locks" / "nightly-boss-a.lock"),
        ):
            pass


def test_a_move_names_an_owned_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDIUS_APPS", raising=False)
    templates = cli.plan_sources().templates
    params = {"file_name": "Todo", "file_ext": "txt", "new_name": "todo old", "folder_name": "archive"}
    move = PlanItem(at="11:00", template="explorer_rename_move", context="tidy", params=params)
    assert validate([move], templates, [], Policy(), owned={"documents\\todo.txt": "2026-11-01"}) == []
    for owned in ({"documents\\other.txt": "2026-11-01"}, None):
        assert "own files" in validate([move], templates, [], Policy(), owned=owned)[0][1]
