"""Excel workbook template: planner validation of rows, the .xlsx reader and the guest-side done check (offline)."""

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
    """A minimal workbook as Excel writes it: text in sharedStrings (t="s"), numbers inline."""
    shared: list[str] = []
    xml = ""
    for ref, (kind, v) in cells.items():
        if kind == "s":
            shared.append(v)
            xml += f'<row><c r="{ref}" t="s"><v>{len(shared) - 1}</v></c></row>'
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
    assert check("a\tb\n" * 9) and check("a\tb\tc\td\te\tf")  # bounded rows and columns
    assert check("item\t=SUM(B1)")  # a formula
    assert check("item\t1/11") and check("item\t8:30") and check("item\t1.5")  # Excel would convert these
    assert check("item\t\tcount")  # an empty cell
    assert check("pens box\t1\npens\t2")  # AutoComplete would finish "pens" to "pens box"
    assert check("bl\u00e5\t1")  # non-ASCII
    assert check(ROWS, file_name="a/b")  # file names stay under the shared rules


def test_xlsx_reader() -> None:
    assert guestfile.xlsx_cells(xlsx(SAVED)) == {k: v for k, (_, v) in SAVED.items()}
    assert guestfile.xlsx_cells(xlsx(SAVED, sheets=2)) is None  # which sheet was meant is unknown
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
    disk: dict[str, Any] = {}  # what guestfile.read sees: None = unreadable

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
    assert check_done(done, saved_snap(tk), tk, False) is False  # no baseline taken: could be pre-existing
    assert file_baseline(tk, Policy()) == "Documents\\stock count q4.xlsx: already exists, never overwritten"
    disk["file"] = None
    assert file_baseline(tk, Policy()) == "Documents\\stock count q4.xlsx: unreadable on the guest"
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is True
    assert check_done(done, obs.Snapshot(), tk, False) is False  # the title check still applies
    disk["file"] = guestfile.GuestFile(True, 999_999, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is False  # written before the task started
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx({**SAVED, "C9": ("s", "stray")}))
    assert check_done(done, saved_snap(tk), tk, False) is False  # stray typing in another cell


def prompt_snap(excel_text: bool) -> obs.Snapshot:
    """Windows' Confirm Save As (by title) or Excel's own message box (by its text), with Yes and No."""
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
    assert p.element_allowed(yes, [], saved_snap(tk)) is None  # no prompt: the rule is silent


def test_reserved_files_by_final_path(
    task: tuple[Task, dict[str, Any]], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk, _ = task
    scenario = tmp_path / "scenario.yaml"
    # Reserved with its extension; the plan names it without one, as Save As adds .xlsx.
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
    assert reads == []  # never read, not even by the done check


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
    """Notepad has no file done check: its declared write Documents\\{file_name}.txt is what the reservation sees."""
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
    assert validate([PlanItem.model_validate(rename)], templates, [], p, user="aberg")  # the .txt it keeps: reserved
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
    assert file_baseline(tk, Policy()) is None and reads == []  # no file check: nothing is read


def test_template_load_refuses_undeclared_writes() -> None:
    t = cli.plan_sources().templates["notepad_write_note"].model_dump()
    with pytest.raises(ValueError, match="declares no writes"):
        Template.model_validate({**t, "writes": []})
    with pytest.raises(ValueError, match="not in a noise folder"):
        Template.model_validate({**t, "writes": [r"Desktop\{file_name}.txt"]})
    with pytest.raises(ValueError, match="saves .txt"):  # re-check of 97820c6: no extension misses x.txt
        Template.model_validate({**t, "writes": [r"Documents\{file_name}"]})
    x = cli.plan_sources().templates["excel_new_workbook"].model_dump()
    with pytest.raises(ValueError, match="saves .xlsx|not a declared write"):
        Template.model_validate({**x, "writes": [r"Documents\{file_name}.xls"]})


def run2_snap() -> obs.Snapshot:
    """The shape of Excel run2's save_as screen: the ribbon search box, the Name Box, the File tab, the grid."""
    s = obs.Snapshot()
    s.windows.add(process_name="EXCEL.EXE", title="Book1 - Excel", kind=obs.Window.FOREGROUND)
    for i, (role, name, value) in enumerate([("edit", "Microsoft search", "Search"), ("edit", "Name Box", "A5"),
                                             ("menu item", "Type to search and use the up and down arrow keys to "
                                              "navigate", "collapsed"), ("button", "File Tab", ""),
                                             ("button", "Save", ""), ("data item", "A5", "")], start=1):  # fmt: skip
        e = s.elements.add(id=i, role=role, name=name, value=value, window=0)
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
    # Template text makes the search box a route: clicked, and typed as written, never planner content.
    nav = sg.model_copy(update={"search": "save as"})
    types = [o for o in build_options(view(s, tk, nav, p), tk, nav, p) if o.kind == "type"]
    assert any("Microsoft search" in o for o in offered(nav)) and types
    assert all(o.literal == "save as" and o.content == "" for o in types)
    assert tk.text(nav) == "save as" and Policy(reserved_paths=["save as"]).text_allowed("save as", None)
    with pytest.raises(ValueError):
        tasks.Subgoal(goal="g", search="save as\n")
    with pytest.raises(ValueError, match="not both"):
        tasks.Subgoal(goal="g", text="file_name", search="save as")
    # Ours to write: a proposal may not set it, and the scenario policy checks it at plan load.
    caps = yaml.safe_load((cli.DATA / "capabilities.yaml").read_text(encoding="utf-8"))
    t = tk.template.model_copy(update={"task": "excel_search_book"}, deep=True)
    t.subgoals["save_as"].search = "save as"
    assert any("search not permitted" in x for x in growth.check(t, caps, {}))
    it = PlanItem.model_validate({"at": "09:00", "template": t.task, "context": "count",
                                  "params": {"file_name": "stock count q4"}, "content": {"rows": ROWS}})  # fmt: skip
    why = [w for _, w in validate([it], {t.task: t}, [], Policy(reserved_paths=["save as"]), user="aberg")]
    assert why == ["save_as: search text: scenario-reserved path"]
