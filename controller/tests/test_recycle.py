from __future__ import annotations

import random
from typing import Any

import pytest
from test_edit import it

from ludius import cli, guestfile, tasks
from ludius.planner import validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, check_done, file_baseline, preconditions
from ludius.v1 import observation_pb2 as obs

OWNED = {"documents\\old draft.txt": "2026-10-29", "documents\\fresh draft.txt": "2026-11-01"}


def test_recycle_only_an_own_old_file_no_other_item_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDIUS_APPS")
    templates = cli.plan_sources().templates

    def why(*items: Any, owned: dict[str, str] | None = OWNED) -> str:
        r = validate(list(items), templates, [], Policy(), owned=owned, day="2026-11-02")
        return r[0][1] if r else ""

    rec = it("11:00", "explorer_recycle_file", file_name="Old Draft", file_ext="txt")
    assert why(rec) == ""
    assert "own files" in why(it("11:00", "explorer_recycle_file", file_name="old draft", file_ext="docx"))
    assert "own files" in why(rec, owned=None)
    assert "3 days" in why(it("11:00", "explorer_recycle_file", file_name="fresh draft", file_ext="txt"))
    edit = it("10:00", "word_edit_document", file_name="old draft", body="one more point")
    assert any("another item today" in w for _, w in validate([edit, rec], templates, [], Policy(),
                                                               owned=OWNED, day="2026-11-02"))  # fmt: skip


@pytest.fixture
def task(monkeypatch: pytest.MonkeyPatch) -> tuple[Task, dict[str, Any]]:
    disk: dict[str, Any] = {}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk.get("file"))
    t = cli.plan_sources().templates["explorer_recycle_file"]
    tk = Task(t, TaskInstance(template=t.task, params={"file_name": "old draft", "file_ext": "txt"}), random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    return tk, disk


def test_done_is_present_at_start_then_definitely_gone(task: tuple[Task, dict[str, Any]]) -> None:
    tk, disk = task
    done, snap = tk.template.subgoals["recycle_keys"].done, obs.Snapshot()
    disk["file"] = guestfile.GuestFile(False)
    assert check_done(done, snap, tk, False) is False
    assert file_baseline(tk, Policy()) == "Documents\\old draft.txt: not there, nothing to recycle"
    disk["file"] = None
    assert file_baseline(tk, Policy()) == "Documents\\old draft.txt: unreadable on the guest"
    disk["file"] = guestfile.GuestFile(True, 500, b"x")
    assert file_baseline(tk, Policy()) is None
    assert check_done(done, snap, tk, False) is False
    disk["file"] = None
    assert check_done(done, snap, tk, False) is False
    disk["file"] = guestfile.GuestFile(False)
    assert check_done(done, snap, tk, False) is True


def test_delete_only_with_exactly_the_file_by_full_name_selected_in_documents(
    task: tuple[Task, dict[str, Any]],
) -> None:
    tk, _ = task
    sg = tk.template.subgoals["recycle_keys"]

    def snap(*items: tuple[str, bool], crumbs: tuple[str, ...] = ("This PC", "Documents"), text: str = "") -> Any:
        s = obs.Snapshot()
        s.windows.add(process_name="explorer.exe", class_name="CabinetWClass", title="Documents",
                      kind=obs.Window.FOREGROUND)  # fmt: skip
        s.elements.add(name="All locations", role="split button", window=0)
        for c in crumbs:
            s.elements.add(name=c, role="split button", window=0)
        s.elements.add(name='Refresh "Documents" (F5)', role="button", window=0)
        for name, selected in items:
            s.elements.add(name=name, role="list item", selected=selected, window=0)
        if text:
            s.elements.add(name=text, role="text", window=0)
        return s

    assert preconditions(sg, snap(("old draft.txt", True), ("budget.xlsx", False)), tk) is None
    for bad in (
        snap(("old draft", True)),
        snap(("old draft.txt", True), ("old draft.docx", True)),
        snap(("old draft.txt", False), ("budget.xlsx", False)),
        snap(("old draft.txt", True), ("budget.xlsx", True)),
        snap(("old draft.txt", True), crumbs=("Libraries", "Documents")),
        snap(("old draft.txt", True), text="Are you sure you want to permanently delete this file?"),
    ):
        assert preconditions(sg, bad, tk)


def test_extensions_count_as_shown_only_by_the_view_setting(task: tuple[Task, dict[str, Any]]) -> None:
    tk, _ = task
    done = tk.template.subgoals["show_extensions"].done

    def snap(setting: str | None) -> obs.Snapshot:
        s = obs.Snapshot()
        s.windows.add(process_name="explorer.exe", class_name="CabinetWClass", title="Documents",
                      kind=obs.Window.FOREGROUND)  # fmt: skip
        s.elements.add(name="old draft.txt", role="list item", window=0)
        if setting:
            s.elements.add(name="File name extensions", role="check box", value=setting, window=0)
        return s

    assert check_done(done, snap(None), tk, False) is False
    assert check_done(done, snap("off"), tk, False) is False
    assert check_done(done, snap("on"), tk, False) is True
