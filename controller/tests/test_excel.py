from __future__ import annotations

import io
import random
import zipfile
from typing import Any

import pytest
import yaml

from ludius import cli, guestfile, tasks
from ludius.options import build_options, view
from ludius.planner import PlanItem, load_plan, validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, Template, check_done, file_baseline
from ludius.v1 import observation_pb2 as obs

ROWS = "item\tcount\npens\t12\npads\t3"
NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def xlsx(cells: dict[str, tuple[str, str]], sheets: int = 1) -> bytes:
    shared: list[str] = []
    xml = ""
    for ref, (kind, v) in cells.items():
        if kind == "s":
            shared.append(v)
            xml += f'<row><c r="{ref}" t="s"><v>{len(shared) - 1}</v></c></row>'
        elif kind == "f":
            xml += f'<row><c r="{ref}"><f>{v}</f><v>0</v></c></row>'
        elif kind == "b":
            xml += f'<row><c r="{ref}" t="b"><v>{v}</v></c></row>'
        else:
            xml += f'<row><c r="{ref}"><v>{v}</v></c></row>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {NS}>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        for n in range(1, sheets + 1):
            z.writestr(f"xl/worksheets/sheet{n}.xml", f"<worksheet {NS}><sheetData>{xml}</sheetData></worksheet>")
    return buf.getvalue()


SAVED = {"A1": ("s", "Item"), "B1": ("s", "count"), "A2": ("s", "pens"), "B2": ("n", "12"), "A3": ("s", "pads"),
         "B3": ("n", "3")}  # fmt: skip


def check(rows: str, **params: str) -> list[str]:
    params = {"file_name": "stock count q4", **params}
    it = PlanItem.model_validate({"at": "09:00", "template": "excel_new_workbook", "context": "stock count",
                                  "params": params, "content": {"rows": rows}})  # fmt: skip
    return [why for _, why in validate([it], cli.plan_sources().templates, [], Policy())]


def test_planner_rows() -> None:
    assert check(ROWS) == []
    assert check("a\tb\n" * 9) and check("a\tb\tc\td\te\tf")
    assert check("item\t=SUM(B1)")
    assert check("item\t1/11") and check("item\t8:30") and check("item\t1.5")
    assert check("item\t\tcount")
    assert check("pens box\t1\npens\t2")
    assert check("bl\u00e5\t1")
    assert check(ROWS, file_name="a/b")


def test_xlsx_reader() -> None:
    assert guestfile.xlsx_cells(xlsx(SAVED)) == {k: v for k, (_, v) in SAVED.items()}
    assert guestfile.xlsx_cells(xlsx(SAVED, sheets=2)) is None
    assert guestfile.xlsx_cells(b"not a zip") is None
    assert guestfile.grid_cells(ROWS + "\n") == {"A1": "item", "B1": "count", "A2": "pens", "B2": "12", "A3": "pads",
                                                 "B3": "3"}  # fmt: skip


def test_profile_path_stays_in_noise_folders() -> None:
    assert guestfile.profile_path("aberg", "Documents\\a.xlsx") == "C:\\Users\\aberg\\Documents\\a.xlsx"
    for bad in ("Desktop\\a.xlsx", "Documents\\..\\a.xlsx", "Documents", "Documents\\c:x.xlsx"):
        assert guestfile.profile_path("aberg", bad) is None
    assert guestfile.profile_path("..", "Documents\\a.xlsx") is None


@pytest.fixture
def task(monkeypatch: pytest.MonkeyPatch) -> tuple[Task, dict[str, Any]]:
    disk: dict[str, Any] = {}

    def read(user: str, rel: str) -> guestfile.GuestFile | None:
        return disk.get("file")

    monkeypatch.setattr(tasks.guestfile, "read", read)
    t = cli.plan_sources().templates["excel_new_workbook"]
    inst = TaskInstance(template=t.task, params={"file_name": "stock count q4"}, content={"rows": ROWS}, variant=0)
    tk = Task(t, inst, random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    return tk, disk


def saved_snap(task: Task) -> obs.Snapshot:
    s = obs.Snapshot()
    s.windows.add(process_name="EXCEL.EXE", title="stock count q4 - Excel", kind=obs.Window.FOREGROUND)
    return s


def test_file_done_needs_a_new_file_written_in_the_task(task: tuple[Task, dict[str, Any]]) -> None:
    tk, disk = task
    done = tk.template.subgoals["confirm_save"].done
    assert tk.values["rows_enter"].endswith("pads\t3\n")
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is False
    assert file_baseline(tk, Policy()) == "Documents\\stock count q4.xlsx: already exists, never overwritten"
    disk["file"] = None
    assert file_baseline(tk, Policy()) == "Documents\\stock count q4.xlsx: unreadable on the guest"
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is True
    assert check_done(done, obs.Snapshot(), tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 999_999, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx({**SAVED, "C9": ("s", "stray")}))
    assert check_done(done, saved_snap(tk), tk, False) is False


def prompt_snap(excel_text: bool) -> obs.Snapshot:
    s = obs.Snapshot()
    s.windows.add(process_name="EXCEL.EXE", title="Microsoft Excel" if excel_text else "Confirm Save As",
                  kind=obs.Window.FOREGROUND)  # fmt: skip
    if excel_text:
        s.context.append("text: A file named 'stock count q4.xlsx' already exists in this location. Do you want to "
                         "replace it?")  # fmt: skip
    for i, name in enumerate(("&Yes", "&No"), start=1):
        e = s.elements.add(id=i, role="button", name=name, window=0)
        e.bounds.width = e.bounds.height = 20
    return s


@pytest.mark.parametrize("excel_text", [False, True])
def test_overwrite_confirmation_offers_neither_yes_nor_enter(
    task: tuple[Task, dict[str, Any]], excel_text: bool
) -> None:
    tk, _ = task
    s, p = prompt_snap(excel_text), Policy()
    yes, no = s.elements
    assert p.element_allowed(yes, ["Yes"], s) == p.key_allowed("Enter", s) == p.key_allowed("Alt+Y", s) is not None
    assert p.element_allowed(no, [], s) is None and p.key_allowed("Esc", s) is None
    sg = tk.subgoal("confirm_save")
    opts = [o.text for o in build_options(view(s, tk, sg, p), tk, sg, p)]
    assert not any("Yes" in o or o == "press Enter" for o in opts) and any("'&No'" in o for o in opts)
    assert p.element_allowed(yes, [], saved_snap(tk)) is None


def test_reserved_files_by_final_path(
    task: tuple[Task, dict[str, Any]], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk, _ = task
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(r"reserved: [{path: 'C:\Users\*\Documents\stock count q4.xlsx'}]", encoding="utf-8")
    p = Policy.load(scenario)
    it = PlanItem.model_validate({"at": "09:00", "template": "excel_new_workbook", "context": "count",
                                  "params": {"file_name": "stock count q4"}, "content": {"rows": ROWS}})  # fmt: skip
    why = [w for _, w in validate([it], cli.plan_sources().templates, [], p, user="aberg")]
    assert why == [r"Documents\stock count q4.xlsx: scenario-reserved file, choose a different name"]
    reads: list[str] = []

    def read(user: str, rel: str) -> guestfile.GuestFile | None:
        reads.append(rel)
        return guestfile.GuestFile(False)

    monkeypatch.setattr(tasks.guestfile, "read", read)
    assert file_baseline(tk, p) == r"Documents\stock count q4.xlsx: scenario-reserved file" and reads == []
    assert check_done(tk.template.subgoals["confirm_save"].done, saved_snap(tk), tk, False) is False
    assert reads == []


def plan_file(tmp_path: Any, rows: str) -> Any:
    path = tmp_path / "plans" / "2026-10-28-aberg.yaml"
    path.parent.mkdir(parents=True)
    item = {"at": "09:00", "template": "excel_new_workbook", "context": "count",
            "params": {"file_name": "stock count q4"}, "content": {"rows": rows}}  # fmt: skip
    path.write_text(yaml.safe_dump({"persona": "aberg", "date": "2026-10-28", "items": [item]}), encoding="utf-8")
    return path


def test_load_plan_checks_items(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDIUS_SCENARIO", raising=False)
    assert load_plan(plan_file(tmp_path / "ok", ROWS)).items[0].content["rows"] == ROWS
    with pytest.raises(ValueError, match=r"rows: cell '=1\+1'"):
        load_plan(plan_file(tmp_path / "formula", "item\t=1+1"))
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(r"reserved: [{path: 'Documents\stock count q4.xlsx'}]", encoding="utf-8")
    monkeypatch.setenv("LUDIUS_SCENARIO", str(scenario))
    with pytest.raises(ValueError, match="scenario-reserved file"):
        load_plan(plan_file(tmp_path / "reserved", ROWS))


def test_declared_writes_cover_templates_without_a_file_check(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(r"reserved: [{path: 'C:\Users\*\Documents\x.txt'}, {path: 'Documents\keep\old.txt'}]",
                        encoding="utf-8")  # fmt: skip
    p = Policy.load(scenario)
    templates = cli.plan_sources().templates
    note = {"at": "09:00", "template": "notepad_write_note", "context": "note", "params": {"file_name": "x"},
            "content": {"body": "a short note"}}  # fmt: skip
    assert [w for _, w in validate([PlanItem.model_validate(note)], templates, [], p, user="aberg")] == [
        r"Documents\x.txt: scenario-reserved file, choose a different name"
    ]
    rename = {"at": "09:00", "template": "explorer_rename_move", "context": "tidy",
              "params": {"file_name": "draft", "new_name": "old", "folder_name": "keep"}}  # fmt: skip
    assert validate([PlanItem.model_validate(rename)], templates, [], p, user="aberg")
    path = tmp_path / "plans" / "2026-10-28-aberg.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({"persona": "aberg", "date": "2026-10-28", "items": [note]}), encoding="utf-8")
    monkeypatch.setenv("LUDIUS_SCENARIO", str(scenario))
    with pytest.raises(ValueError, match=r"Documents\\x\.txt: scenario-reserved"):
        load_plan(path)
    reads: list[str] = []

    def read(user: str, rel: str) -> guestfile.GuestFile | None:
        reads.append(rel)
        return None

    monkeypatch.setattr(tasks.guestfile, "read", read)
    tk = Task(templates["notepad_write_note"], PlanItem.model_validate(note), random.Random(0))
    tk.owner = "aberg"
    assert file_baseline(tk, p) == r"Documents\x.txt: scenario-reserved file" and reads == []
    assert file_baseline(tk, Policy()) is None and reads == []


def test_template_load_refuses_undeclared_writes() -> None:
    t = cli.plan_sources().templates["notepad_write_note"].model_dump()
    with pytest.raises(ValueError, match="declares no writes"):
        Template.model_validate({**t, "writes": []})
    with pytest.raises(ValueError, match="not in a noise folder"):
        Template.model_validate({**t, "writes": [r"Desktop\{file_name}.txt"]})
    with pytest.raises(ValueError, match="saves .txt"):
        Template.model_validate({**t, "writes": [r"Documents\{file_name}"]})
    x = cli.plan_sources().templates["excel_new_workbook"].model_dump()
    with pytest.raises(ValueError, match="saves .xlsx|not a declared write"):
        Template.model_validate({**x, "writes": [r"Documents\{file_name}.xls"]})


def run2_snap() -> obs.Snapshot:
    s = obs.Snapshot()
    s.windows.add(process_name="EXCEL.EXE", title="Book1 - Excel", kind=obs.Window.FOREGROUND)
    for i, (role, name, value) in enumerate([("edit", "Microsoft search", "Search"), ("edit", "Name Box", "A5"),
                                             ("menu item", "Type to search and use the up and down arrow keys to "
                                              "navigate", "collapsed"), ("button", "File Tab", ""),
                                             ("button", "Save", ""), ("data item", "A5", "")], start=1):  # fmt: skip
        e = s.elements.add(id=i, role=role, name=name, value=value, window=0, enabled=True)
        e.bounds.width = e.bounds.height = 20
    return s


def test_no_text_no_edit_clicks_and_template_search_text(task: tuple[Task, dict[str, Any]]) -> None:
    from ludius import growth

    tk, _ = task
    s, p = run2_snap(), Policy()
    sg = tk.subgoal("save_as")

    def offered(sg: tasks.Subgoal) -> list[str]:
        return [o.text for o in build_options(view(s, tk, sg, p), tk, sg, p)]

    opts = offered(sg)
    assert "press F12" in opts and any("'A5'" in o for o in opts)
    assert not any("Microsoft search" in o or "Name Box" in o or "File Tab" in o or "'Save'" in o for o in opts)
    nav = sg.model_copy(update={"search": "save as"})
    types = [o for o in build_options(view(s, tk, nav, p), tk, nav, p) if o.kind == "type"]
    assert any("Microsoft search" in o for o in offered(nav)) and types
    assert all(o.literal == "save as" and o.content == "" for o in types)
    assert tk.text(nav) == "save as" and Policy(reserved_paths=["save as"]).text_allowed("save as", None)
    with pytest.raises(ValueError):
        tasks.Subgoal(goal="g", search="save as\n")
    with pytest.raises(ValueError, match="not both"):
        tasks.Subgoal(goal="g", text="file_name", search="save as")
    caps = yaml.safe_load((cli.DATA / "capabilities.yaml").read_text(encoding="utf-8"))
    t = tk.template.model_copy(update={"task": "excel_search_book"}, deep=True)
    t.subgoals["save_as"].search = "save as"
    assert any("search not permitted" in x for x in growth.check(t, caps, {}))
    it = PlanItem.model_validate({"at": "09:00", "template": t.task, "context": "count",
                                  "params": {"file_name": "stock count q4"}, "content": {"rows": ROWS}})  # fmt: skip
    why = [w for _, w in validate([it], {t.task: t}, [], Policy(reserved_paths=["save as"]), user="aberg")]
    assert why == ["save_as: search text: scenario-reserved path"]


def test_keys_survive_the_option_limit(task: tuple[Task, dict[str, Any]]) -> None:
    from ludius.decisions import MAX_OPTIONS

    tk, _ = task
    s, p = run2_snap(), Policy()
    for i in range(300):
        e = s.elements.add(id=100 + i, role="button", name=f"Ribbon {i}", window=0)
        e.bounds.width = e.bounds.height = 20
    sg = tk.subgoal("save_as")
    opts = build_options(view(s, tk, sg, p), tk, sg, p)
    assert len(opts) == MAX_OPTIONS and [o.key for o in opts] == [f"o{i}" for i in range(1, MAX_OPTIONS + 1)]
    texts = [o.text for o in opts]
    assert "press F12" in texts and "press Esc" in texts and opts[-1].kind == "rescan"


def test_search_text_only_into_a_checked_field_of_the_task_app(task: tuple[Task, dict[str, Any]]) -> None:
    from ludius.loop import Runner
    from ludius.options import Option

    tk, _ = task
    nav = tk.subgoal("save_as").model_copy(update={"search": "save as"})

    def snap(process: str, name: str, password: bool = False) -> obs.Snapshot:
        s = obs.Snapshot()
        s.windows.add(process_name=process, title="w", kind=obs.Window.FOREGROUND)
        e = s.elements.add(id=1, role="edit", name=name, window=0, focused=True, enabled=True, password=password)
        e.bounds.width = e.bounds.height = 20
        return s

    def typed(s: obs.Snapshot, p: Policy) -> list[str]:
        return [o.text for o in build_options(view(s, tk, nav, p), tk, nav, p) if o.kind == "type"]

    assert typed(snap("EXCEL.EXE", "Microsoft search"), Policy()) == [
        "type the text to enter into [1] edit 'Microsoft search'"
    ]
    assert typed(snap("EXCEL.EXE", "Password", password=True), Policy()) == []
    assert typed(snap("EXCEL.EXE", "q4 payroll"), Policy(reserved_names=["q4 payroll"])) == []
    assert typed(snap("notepad.exe", "Text editor"), Policy()) == []
    r = Runner.__new__(Runner)
    r.policy = Policy()
    post_check = r._post_check  # pyright: ignore[reportPrivateUsage]
    for s, e in [(snap("EXCEL.EXE", "Microsoft search"), None), (snap("notepad.exe", "x"), "elem")]:
        r.snap = s
        o = Option("o1", "type", "t", element=s.elements[0] if e else None, literal="save as")
        assert post_check(o, nav, tk) is not None
    r.snap = snap("EXCEL.EXE", "Microsoft search")
    assert post_check(Option("o1", "type", "t", element=r.snap.elements[0], literal="save as"), nav, tk) is None
    with pytest.raises(ValueError):
        tasks.Subgoal(goal="g", keys=["F1"] * 17)


@pytest.mark.parametrize("planner_text", [True, False])
def test_typing_rechecks_its_target_on_a_fresh_snapshot(
    task: tuple[Task, dict[str, Any]], tmp_path: Any, planner_text: bool
) -> None:
    from ludius.loop import GroundTruth, Runner
    from ludius.options import Option
    from ludius.tasks import Persona
    from ludius.v1 import agent_pb2 as pb

    tk, _ = task
    sg = tk.subgoal("name_file") if planner_text else tk.subgoal("save_as").model_copy(update={"search": "save as"})

    def snap(process: str = "EXCEL.EXE", key: str = "k1", name: str = "File name:", pw: bool = False) -> obs.Snapshot:
        s = obs.Snapshot(screen_width=1920, screen_height=1080, dpi=96)
        s.windows.add(process_name=process, title="Book1 - Excel", kind=obs.Window.FOREGROUND)
        e = s.elements.add(id=1, key=key, role="edit", name=name, window=0, enabled=True, password=pw)
        e.bounds.width = e.bounds.height = 20
        return s

    class Agent:
        vm = "t"

        def __init__(self) -> None:
            self.fresh, self.typed = snap(), list[str]()
            self.after_click: obs.Snapshot | None = None

        def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
            return pb.ObserveResult(snapshot=self.fresh)

        def move(self, x: int, y: int, duration_ms: int = 0) -> int:
            return 0

        def click(self, button: int = 0, count: int = 1) -> int:
            self.fresh = self.after_click or self.fresh
            self.fresh.elements[0].focused = True
            return 0

        def chord(self, vks: list[int]) -> int:
            return 0

        def type(self, text: str, mean_ms: int, jitter_ms: int) -> int:
            self.typed.append(text)
            return 0

    agent = Agent()
    r = Runner(agent, None, Persona(name="t", user="t"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    content, literal = ("file_name", "") if planner_text else ("", "save as")
    for fresh in [
        snap(key="k2", name="Password", pw=True),
        snap(name="Password", pw=True),
        snap(name="q4 payroll"),
        snap(process="notepad.exe"),
    ]:
        r.snap = snap()
        agent.fresh = fresh
        o = Option("o1", "type", "t", element=r.snap.elements[0], content=content, literal=literal)
        r.policy = Policy(reserved_names=["q4 payroll"])
        assert r.act(o, tk, sg) is not None and agent.typed == []
        if planner_text:
            agent.fresh.elements[0].focused = True
            assert r.act(Option("o2", "type", "t", content=content), tk, sg) is not None and agent.typed == []
    for after in [snap(process="notepad.exe"), snap(name="Password", pw=True)]:
        r.snap, agent.fresh, agent.after_click = snap(), snap(), after
        assert r.act(Option("o1", "type", "t", element=r.snap.elements[0], content=content, literal=literal), tk, sg)
        assert agent.typed == []
    agent.fresh, agent.after_click = snap(), None
    assert (
        r.act(Option("o1", "type", "t", element=r.snap.elements[0], content=content, literal=literal), tk, sg) is None
    )
    assert agent.typed == [tk.values["file_name"] if planner_text else "save as"]
    clicks: list[int] = []
    click = agent.click
    agent.click = lambda button=0, count=1: clicks.append(1) or click(button, count)  # type: ignore[method-assign]
    agent.fresh, agent.typed = snap(), []
    agent.fresh.elements[0].focused = True
    agent.fresh.elements.append(obs.Element(id=9, role="edit", name="Address and search bar", focused=True))
    assert (
        r.act(Option("o1", "type", "t", element=r.snap.elements[0], content=content, literal=literal), tk, sg) is None
    )
    assert clicks == [1] and len(agent.typed) == 1


def test_add_rows_needs_the_old_cells_and_the_new_rows_below(monkeypatch: pytest.MonkeyPatch) -> None:
    disk: dict[str, Any] = {}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk.get("file"))
    t = cli.plan_sources().templates["excel_add_rows"]
    tk = Task(t, TaskInstance(template=t.task, params={"file_name": "stock", "rows": "folders\t4"}), random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    done, snap = t.subgoals["save_keys"].done, obs.Snapshot()
    disk["file"] = guestfile.GuestFile(True, 500, xlsx(SAVED))
    assert file_baseline(tk, Policy()) is None
    grown = {**SAVED, "A4": ("s", "Folders"), "B4": ("n", "4")}
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx(grown))
    assert check_done(done, snap, tk, False) is True
    for wrong in ({**SAVED, "A5": ("s", "folders"), "B5": ("n", "4")},
                  {**SAVED, "A3": ("s", "folders"), "B3": ("n", "4")},
                  {**grown, "C4": ("n", "1")}, {**grown, "A1": ("s", "ITEM")}, SAVED):  # fmt: skip
        disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx(wrong))
        assert check_done(done, snap, tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 999_999, xlsx(grown))
    assert check_done(done, snap, tk, False) is False
    tk = Task(t, TaskInstance(template=t.task, params={"file_name": "stock", "rows": "pen\t4"}), random.Random(0))
    tk.owner = "aberg"
    disk["file"] = guestfile.GuestFile(True, 500, xlsx(SAVED))
    assert "would complete" in (file_baseline(tk, Policy()) or "")
    flag = {**SAVED, "C2": ("b", "1")}
    assert guestfile.xlsx_cells(xlsx(flag)) == {**{k: v for k, (_, v) in SAVED.items()}, "C2": "TRUE"}
    disk["file"] = guestfile.GuestFile(True, 500, xlsx(flag))
    tk = Task(t, TaskInstance(template=t.task, params={"file_name": "stock", "rows": "folders	4"}), random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    assert file_baseline(tk, Policy()) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx({**grown, "C2": ("n", "1")}))
    assert check_done(done, snap, tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx({**grown, "C2": ("b", "1")}))
    assert check_done(done, snap, tk, False) is True
    formula = xlsx({**SAVED, "B2": ("f", "6+6")})
    assert guestfile.xlsx_cells(formula) is None
    disk["file"] = guestfile.GuestFile(True, 500, formula)
    assert file_baseline(tk, Policy()) == r"Documents\stock.xlsx: unreadable on the guest"
