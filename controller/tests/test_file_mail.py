from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, ews, mail, reply
from ludius.planner import PlanItem, item_problem
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, _filed_ok, check_done, preconditions  # pyright: ignore[reportPrivateUsage]
from ludius.v1 import observation_pb2 as obs

BOSS = "mvellinga@ludius.internal"
SUBJECT = "Points for tomorrow's ERP cost policy review"
MSGID = "<7@ludius.internal>"


def file_task(folder: str = "ERP cost policy") -> Task:
    t = cli.load_templates()["outlook_file_mail"]
    params = {"sender": BOSS, "subject": SUBJECT, "folder": folder}
    task = Task(t, TaskInstance(template=t.task, params=params, variant=0), random.Random(0))
    task.owner = "aberg"
    return task


def boxes(monkeypatch: pytest.MonkeyPatch, inbox: list[str] | None, there: list[str] | None) -> None:
    monkeypatch.setattr(mail, "_exchange", lambda: True)

    def fake_msgids(box: str, folder: str):
        return inbox if folder == "inbox" else there

    monkeypatch.setattr(ews, "msgids", fake_msgids)


def test_filed_needs_the_mail_gone_from_the_inbox_and_once_in_the_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    task = file_task()
    task.file_msgid = MSGID
    for inbox, there, ok in [([], [MSGID], True), ([MSGID], [MSGID], False), ([], [], False),
                             ([], [MSGID, MSGID], False), ([], None, False), (None, [MSGID], False)]:  # fmt: skip
        boxes(monkeypatch, inbox, there)
        assert _filed_ok(task) is ok
    task.file_msgid = ""
    boxes(monkeypatch, [], [""])
    assert not _filed_ok(task)


def test_prepare_refuses_a_mail_already_in_the_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.yaml"
    narrative.write_text("people:\n- {name: Marcus Vellinga, role: r, department: d, user: mvellinga}\n"
                         "- {name: Anna Berg, role: r, department: d, user: aberg}\n", encoding="utf-8")  # fmt: skip
    m = mail.Message(BOSS, 0, 0, "body", id=7, cls="IPM.Note", subject=SUBJECT, msgid=MSGID)

    def fake_matching(*_: Any):
        return [m]

    monkeypatch.setattr(mail, "matching", fake_matching)
    boxes(monkeypatch, [MSGID], [MSGID])
    task = file_task()
    why, _ = reply.prepare_file(task, Policy(), narrative)
    assert why and not task.file_msgid
    boxes(monkeypatch, [MSGID], [])
    why, _ = reply.prepare_file(task, Policy(), narrative)
    assert why is None and task.file_msgid == MSGID and task.values["sender_name"] == "Marcus Vellinga"


@pytest.mark.parametrize("folder", ["Deleted Items", "inbox", "x", "a@b", "ERP, costs"])
def test_plan_load_refuses_outlook_folders_and_odd_names(folder: str) -> None:
    it = PlanItem(at="10:00", template="outlook_file_mail", params={"sender": BOSS, "subject": SUBJECT,
                                                                      "folder": folder})  # fmt: skip
    assert item_problem(it, cli.load_templates(), Policy()) is not None
    ok = PlanItem(at="10:00", template="outlook_file_mail", params={"sender": BOSS, "subject": SUBJECT,
                                                                      "folder": "ERP cost policy"})  # fmt: skip
    assert item_problem(ok, cli.load_templates(), Policy()) is None


def test_move_opens_only_with_the_planned_mail_alone_selected() -> None:
    task = file_task()
    task.values["sender_name"] = "Marcus Vellinga"
    sg = task.template.subgoals["open_move"]
    ours = f"From Marcus Vellinga, Subject {SUBJECT}, Received 09:10"

    def snap(*rows: tuple[str, bool]) -> obs.Snapshot:
        els = [obs.Element(id=i, role="data item", name=n, selected=sel) for i, (n, sel) in enumerate(rows)]
        return obs.Snapshot(elements=els, windows=[obs.Window(title="Inbox - Outlook", process_name="outlook.exe",
                                                              kind=obs.Window.FOREGROUND)])  # fmt: skip

    assert preconditions(sg, snap((ours, True), ("From Hr, Subject other, Received 08:00", False)), task) is None
    assert preconditions(sg, snap((ours, True), ("From Hr, Subject other, Received 08:00", True)), task)
    assert preconditions(sg, snap((ours, False)), task)
    assert preconditions(sg, snap(), task)


def test_select_counts_only_after_a_plain_click_on_the_planned_row() -> None:
    task = file_task()
    task.values["sender_name"] = "Marcus Vellinga"
    done = task.template.subgoals["select_mail"].done
    row = obs.Element(id=1, role="data item", name=f"From Marcus Vellinga, Subject {SUBJECT}, Received 09:10",
                      selected=True)  # fmt: skip
    other = obs.Element(id=2, role="data item", name="From Hr, Subject other, Received 08:00")
    snap = obs.Snapshot(elements=[row], windows=[obs.Window(title="Inbox - Outlook", process_name="outlook.exe",
                                                            kind=obs.Window.FOREGROUND)])  # fmt: skip
    assert check_done(done, snap, task, False) is False
    assert check_done(done, snap, task, False, last_click=other) is False
    assert check_done(done, snap, task, False, last_click=row) is True
