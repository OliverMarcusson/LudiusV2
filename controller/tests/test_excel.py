"""Excel workbook template: planner validation of rows, the .xlsx reader and the guest-side done check (offline)."""

from __future__ import annotations

import io
import random
import zipfile
from typing import Any

import pytest

from ludius import cli, guestfile, tasks
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, check_done, file_baseline
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
    assert file_baseline(tk) == "Documents\\stock count q4.xlsx: already exists, never overwritten"
    disk["file"] = None
    assert file_baseline(tk) == "Documents\\stock count q4.xlsx: unreadable on the guest"
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is True
    assert check_done(done, obs.Snapshot(), tk, False) is False  # the title check still applies
    disk["file"] = guestfile.GuestFile(True, 999_999, xlsx(SAVED))
    assert check_done(done, saved_snap(tk), tk, False) is False  # written before the task started
    disk["file"] = guestfile.GuestFile(True, 2_000_000, xlsx({**SAVED, "C9": ("s", "stray")}))
    assert check_done(done, saved_snap(tk), tk, False) is False  # stray typing in another cell
