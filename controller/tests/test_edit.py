"""file.edit: the owned-file registry, plan-load ownership, the append check on the guest, and the Word gate."""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path
from typing import Any

import pytest
from test_word import docx
from test_world_state import _day

from ludius import cli, guestfile, planner, tasks
from ludius.loop import Runner
from ludius.planner import PlanItem, World, owned_files, role_templates, validate
from ludius.policy import Policy
from ludius.tasks import Persona, Task, TaskInstance, check_done, file_baseline, preconditions
from ludius.v1 import observation_pb2 as obs

MON, TUE = dt.date(2026, 11, 2), dt.date(2026, 11, 3)


def it(at: str, template: str, **params: str) -> PlanItem:
    return PlanItem(at=at, template=template, params=params, context="work")


def test_owned_files_follow_done_writes_and_moves(tmp_path: Path) -> None:
    world = World(tmp_path)
    _day(world, "aberg", MON, [
        it("09:00", "word_short_document", file_name="close memo", title="Close memo", body="accruals by friday"),
        it("09:20", "notepad_write_note", file_name="todo", body="call bank"),
        it("09:40", "notepad_write_note", file_name="failed note", body="x"),
        it("10:00", "explorer_rename_move", file_name="todo", new_name="todo old", folder_name="archive"),
    ], done=["09:00", "09:20", "10:00"])  # fmt: skip
    assert owned_files(tmp_path, "aberg", TUE.isoformat()) == {
        "documents\\close memo.docx",
        "documents\\archive\\todo old.txt",
    }  # moved: keeps .txt, source gone
    assert owned_files(tmp_path, "aberg", MON.isoformat()) == set()  # only days before
    assert owned_files(tmp_path, "lnord", TUE.isoformat()) == set()


def test_an_edit_names_an_owned_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDIUS_APPS")
    templates = cli.plan_sources().templates
    edit = it("11:00", "word_edit_document", file_name="Close Memo", body="one more point")
    assert validate([edit], templates, [], Policy(), owned={"documents\\close memo.docx"}) == []
    for owned in ({"documents\\other.docx"}, None):
        assert "own files" in validate([edit], templates, [], Policy(), owned=owned)[0][1]


def test_notepad_only_where_word_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    templates, p = cli.plan_sources().templates, Persona(name="A B", user="ab")
    monkeypatch.delenv("LUDIUS_APPS")
    role = role_templates(templates, p)
    assert "word_edit_document" in role and "notepad_write_note" not in role
    monkeypatch.setenv("LUDIUS_APPS", "notepad,explorer")
    role = role_templates(templates, p)
    assert "notepad_write_note" in role and not {t.app for t in role.values()} - {"notepad", "explorer"}
    assert planner.installed("explorer") and not planner.installed("word")


@pytest.fixture
def task(monkeypatch: pytest.MonkeyPatch) -> tuple[Task, dict[str, Any]]:
    disk: dict[str, Any] = {}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk.get("file"))
    t = cli.plan_sources().templates["word_edit_document"]
    inst = TaskInstance(template=t.task, params={"file_name": "close memo", "body": "one more point\nand another"})
    tk = Task(t, inst, random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    return tk, disk


def test_append_needs_the_old_paragraphs_then_the_new(task: tuple[Task, dict[str, Any]]) -> None:
    tk, disk = task
    assert tk.values["body_line"] == "\none more point\nand another"  # a new paragraph after the last one
    done = tk.template.subgoals["save_keys"].done
    snap = obs.Snapshot()
    snap.windows.add(process_name="WINWORD.EXE", title="close memo - Word", kind=obs.Window.FOREGROUND)
    old = docx("Close memo", "Accruals by friday")
    disk["file"] = guestfile.GuestFile(True, 2_000_000, docx("Close memo", "Accruals by friday", "One more point",
                                                             "and another"))  # fmt: skip
    assert check_done(done, snap, tk, False) is False  # no baseline read
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) == "Documents\\close memo.docx: not there, nothing to edit"
    for unknown in (None, guestfile.GuestFile(True, 500, b"not a zip")):
        disk["file"] = unknown
        assert file_baseline(tk, Policy()) == "Documents\\close memo.docx: unreadable on the guest"
    disk["file"] = guestfile.GuestFile(True, 500, old)
    assert file_baseline(tk, Policy()) is None and tk.files_absent == frozenset()
    disk["file"] = guestfile.GuestFile(True, 2_000_000, docx("Close memo", "Accruals by friday", "", "One more point",
                                                             "and another"))  # fmt: skip
    assert check_done(done, snap, tk, False) is True
    for wrong in (old,  # nothing added
                  docx("Close memo", "One more point", "and another"),  # an old paragraph lost
                  docx("One more point", "and another", "Close memo", "Accruals by friday"),  # added at the start
                  docx("Close memo", "Accruals by friday", "One more point")):  # fmt: skip
        disk["file"] = guestfile.GuestFile(True, 2_000_000, wrong)
        assert check_done(done, snap, tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 999_999, docx("Close memo", "Accruals by friday", "One more point",
                                                           "and another"))  # fmt: skip
    assert check_done(done, snap, tk, False) is False  # not written in the task


def test_plan_load_refuses_an_edit_of_a_file_not_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDIUS_APPS")
    world = World(tmp_path)
    _day(world, "aberg", MON, [it("09:00", "word_short_document", file_name="close memo", title="Close memo",
                                  body="accruals by friday")], done=["09:00"])  # fmt: skip
    for name, ok in (("close memo", True), ("someone elses", False)):
        path = tmp_path / "plans" / f"{TUE}-aberg.yaml"
        path.write_text(f"persona: aberg\ndate: '{TUE}'\nitems:\n- at: '10:00'\n  template: word_edit_document\n"
                        f"  params: {{file_name: {name}, body: one more point}}\n", encoding="utf-8")  # fmt: skip
        if ok:
            planner.load_plan(path)
        else:
            with pytest.raises(ValueError, match="own files"):
                planner.load_plan(path)


def test_the_file_is_opened_only_from_documents(task: tuple[Task, dict[str, Any]]) -> None:
    tk, _ = task
    open_file, save = tk.template.subgoals["open_file"], tk.template.subgoals["save_keys"]

    def snap(process: str, title: str, *crumbs: str, history: bool = False) -> obs.Snapshot:
        s = obs.Snapshot()
        s.windows.add(process_name=process, class_name="CabinetWClass", title=title, kind=obs.Window.FOREGROUND)
        s.elements.add(name="All locations", role="split button", window=0)
        for c in crumbs:  # as read live: parents are split buttons, the current folder a menu item
            s.elements.add(name=c, role="menu item" if c == crumbs[-1] and not history else "split button", window=0)
        if history:  # live, via Quick access: the current folder has subfolders, and the window has a history
            s.elements.add(name="Previous Locations", role="button", window=0)
        s.elements.add(name=f'Refresh "{title}" (F5)', role="button", window=0)
        s.elements.add(name="close memo", role="list item", window=0)
        return s

    assert preconditions(open_file, snap("explorer.exe", "Documents", "This PC", "Documents"), tk, None) is None
    quick = snap("explorer.exe", "Documents", "This PC", "Documents", history=True)
    assert preconditions(open_file, quick, tk, None) is None
    for elsewhere in (
        ("Desktop", "This PC", "Desktop"),
        ("archive", "This PC", "Documents", "archive"),
        ("Documents", "Libraries", "Documents"),  # the library: Public Documents' files too
        ("Documents", "This PC", "Desktop", "Documents"),  # a same-named folder elsewhere
        ("Documents",),  # address bar not read
    ):
        assert preconditions(open_file, snap("explorer.exe", *elsewhere), tk, None), elsewhere
    assert preconditions(save, snap("WINWORD.EXE", "close memo - Word"), tk, None) is None
    assert preconditions(save, snap("WINWORD.EXE", "other memo - Word"), tk, None)
    # A popup of the task's own app over the folder: Esc may dismiss it; the folder itself or another app: never.
    assert Runner._popup_over(open_file, snap("WINWORD.EXE", "Check out our new look"), tk) == "Check out our new look"
    assert Runner._popup_over(open_file, snap("explorer.exe", "Documents", "This PC", "Documents"), tk) is None
    assert Runner._popup_over(open_file, snap("msedge.exe", "Intranet"), tk) is None
