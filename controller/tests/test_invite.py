"""outlook_send_invite: the shared send contract on the meeting form, plan-load checks, the server-side done check."""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, keys, mail
from ludius.loop import GroundTruth, Runner
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import ElementCheck, Task, check_done, local_ms, mail_baseline, preconditions
from ludius.v1 import observation_pb2 as obs

PEOPLE = {"mvellinga": "Marcus Vellinga", "aberg": "Anna Berg"}
AT = local_ms("2026-11-05", "10:30", "Europe/Stockholm"), local_ms("2026-11-05", "11:15", "Europe/Stockholm")


def item(**params: str) -> dict[str, Any]:
    p = {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga", "subject": "lund accruals q4 review",
         "date": "2026-11-05", "start": "10:30", "end": "11:15"}  # fmt: skip
    return {"at": "09:00", "template": "outlook_send_invite", "params": p | params, "context": "Review with Marcus."}


def check(*items: dict[str, Any], day: str = "2026-11-02") -> list[str]:
    its = [PlanItem.model_validate(i) for i in items]
    return [why for _, why in validate(its, cli.plan_sources().templates, [], Policy(), {"mvellinga"},
                                       people=PEOPLE, day=day)]  # fmt: skip


def task(**params: str) -> Task:
    t = Task(cli.load_templates()["outlook_send_invite"], PlanItem.model_validate(item(**params)), random.Random(0))
    t.owner, t.started_ms, t.tz = "aberg", 1_000, "Europe/Stockholm"
    return t


def test_template_gets_the_meeting_send_contract() -> None:
    t = cli.load_templates()["outlook_send_invite"]
    send = t.subgoals["send_shortcut"]
    # As hardcoded before app profiles (d5620c0); the forbid regex ^(Optional|Location|Resources)$ is now one per field.
    required = "^({to}|{to_name}|{to_name} <{to}>);?$"
    meeting_require = [ElementCheck(role="edit", name="^Required$", value=required),
                       ElementCheck(role="edit", name="^Optional$", value="^$"),
                       ElementCheck(role="edit", name="^Location$", value="^$")]  # fmt: skip
    meeting_forbid = [ElementCheck(name=f"^{n}$", value=r"\S") for n in ("Optional", "Location", "Resources")]
    meeting_avoid = [
        ElementCheck(role="edit", name="^Info Bar$"),
        ElementCheck(
            role="button",
            name="^(Required|Optional|Location|Rooms|Room Finder|Address Book|Add Attendees|Scheduling Assistant)$",
        ),
        ElementCheck(name="^Scheduling Assistant$"),
        ElementCheck(role="data item"),
    ]
    assert send.done and send.done.invite and send.require[:3] == meeting_require
    assert send.forbid[:3] == meeting_forbid and t.processes == ["outlook.exe"]
    assert all(sg.avoid[: len(meeting_avoid)] == meeting_avoid for sg in t.subgoals.values())
    assert all("send_shortcut" in v for v in t.variants)


def test_planned_meeting_is_valid_and_bounded() -> None:
    assert check(item()) == []
    assert check(item(body="agenda: lund accruals\n- anna")) == []
    for bad, why in (
        ({"date": "2026-11-07"}, "weekday"),
        ({"date": "05/11/2026"}, "YYYY-MM-DD"),
        ({"start": "07:30"}, "between 08:00 and 17:00"),
        ({"end": "17:30"}, "between 08:00 and 17:00"),
        ({"end": "10:40"}, "15-120 minutes"),
        ({"end": "13:00"}, "15-120 minutes"),
        ({"start": "10.30"}, "HH:MM"),
        ({"to": "someone@example.com"}, "not a colleague"),
    ):
        assert any(why in r for r in check(item(**bad))), bad
    assert any("after 2026-11-05" in r for r in check(item(), day="2026-11-05"))  # not on the plan's own day
    assert any("within 14 days" in r for r in check(item(), day="2026-10-20"))


def test_dates_typed_as_the_guest_reads_them_and_empty_body_skipped() -> None:
    t = task()
    assert t.values["date_ui"] == "05/11/2026"  # Outlook read 2026-11-05 as 11 May on the en-GB guest
    assert "write_body" not in t.subgoal_names
    assert "write_body" in task(body="agenda").subgoal_names
    assert AT == (1793871000000, 1793873700000)  # 09:30Z-10:15Z: CET, the guest's zone in November


def test_send_needs_the_planned_meeting_in_the_form() -> None:
    t = task()
    sg = t.subgoal("send_shortcut")

    def form(**over: str) -> obs.Snapshot:
        values = {"Title": "lund accruals q4 review", "Required": "Marcus Vellinga <mvellinga@ludius.internal>",
                  "Optional": "", "Location": "", "Start date, combo": "Thu 05/11/2026",
                  "Start time, combo": "10:30", "End date, combo": "Thu 05/11/2026",
                  "End time, combo": "11:15"} | over  # fmt: skip
        els = [obs.Element(role="edit", name=k, value=v, actions=["value"]) for k, v in values.items()]
        return obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE")], elements=els)

    assert preconditions(sg, form(), t) is None
    for over in ({"Optional": "lnord@ludius.internal"}, {"Location": "Room 1"},
                 {"Start time, combo": "11:30"}, {"End time, combo": "11:00"},
                 {"Required": "mvellinga@ludius.internal; lnord@ludius.internal"},
                 {"Start date, combo": "Wed 04/11/2026"}):  # fmt: skip
        assert preconditions(sg, form(**over), t), over


def test_invite_done_only_on_new_request_and_appointment(monkeypatch: pytest.MonkeyPatch) -> None:
    me, guest = "aberg@ludius.internal", "mvellinga@ludius.internal"
    req = mail.Message(me, 5_000, 5_000, "", (me, guest), 7, mail.MEETING_REQUEST, *AT, 5_000, me)
    appt = mail.Message("-", 0, 0, "", tuple(sorted((me, guest))), 3, mail.APPOINTMENT, *AT, 60_000, me)
    store: dict[tuple[str, str], list[mail.Message]] = {}

    def query(user: str, folder: str, _subject: str) -> list[mail.Message] | None:
        return list(store.get((user, folder), []))

    monkeypatch.setattr(mail, "_query", query)

    def messages(user: str, folder: str, subject: str) -> list[mail.Message]:
        return query(user, folder, subject) or []

    monkeypatch.setattr(mail, "messages", messages)
    t = task()
    done = t.template.subgoals["send_shortcut"].done
    t.mail_before = mail_baseline(t)
    assert t.mail_before == frozenset() and not check_done(done, obs.Snapshot(), t, typed_ok=False)
    store[("mvellinga", "inbox")] = [req]
    assert not check_done(done, obs.Snapshot(), t, typed_ok=False)  # no appointment in the organiser's Calendar
    store[("aberg", "calendar")] = [dataclasses.replace(appt, organizer=guest)]
    assert not check_done(done, obs.Snapshot(), t, typed_ok=False)  # organised by someone else
    store[("aberg", "calendar")] = [appt]
    assert check_done(done, obs.Snapshot(), t, typed_ok=False)
    for bad in (dataclasses.replace(req, start_ms=AT[0] + 3_600_000), dataclasses.replace(req, cls="IPM.Note"),
                dataclasses.replace(req, recipients=(guest, "lnord@ludius.internal")),
                dataclasses.replace(req, sender="lnord@ludius.internal")):  # fmt: skip
        store[("mvellinga", "inbox")] = [bad]
        assert not check_done(done, obs.Snapshot(), t, typed_ok=False), bad
    store[("mvellinga", "inbox")] = [req]
    t.mail_before = mail_baseline(t)  # both present at task start: never done
    assert not check_done(done, obs.Snapshot(), t, typed_ok=False)
    t.mail_before = None
    assert not check_done(done, obs.Snapshot(), t, typed_ok=False)
    t2 = task(body="walk through the lund accruals")
    t2.mail_before = frozenset()
    assert not check_done(done, obs.Snapshot(), t2, typed_ok=False)  # planned body missing from the request
    store[("mvellinga", "inbox")] = [dataclasses.replace(req, body="Walk through the Lund accruals.\r\n")]
    assert check_done(done, obs.Snapshot(), t2, typed_ok=False)
    t3 = task(body="???")  # no letters: can't be told from an empty body
    t3.mail_before = frozenset()
    store[("mvellinga", "inbox")] = [req]
    assert not check_done(done, obs.Snapshot(), t3, typed_ok=False)


class FakeAgent:
    vm = "t"

    def __init__(self, snaps: list[obs.Snapshot]) -> None:
        self.snaps, self.clicked, self.chords = snaps, list[tuple[int, int]](), list[list[int]]()
        self.at = (0, 0)

    def observe(self, **_: Any) -> Any:
        return type("R", (), {"snapshot": self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]})()

    def move(self, x: int, y: int, duration_ms: int = 0) -> int:
        self.at = (x, y)
        return 0

    def click(self, count: int = 1) -> int:
        self.clicked.append(self.at)
        return 0

    def chord(self, vks: list[int]) -> int:
        self.chords.append(vks)
        return 0


def test_abandon_discards_the_meeting_with_dont_save_and_ok(tmp_path: Path) -> None:
    def prompt(selected: bool) -> obs.Snapshot:
        def box(y: int) -> obs.Rect:
            return obs.Rect(x=10, y=y, width=9, height=9)

        els = [obs.Element(role="radio button", name="Save changes and send meeting", bounds=box(10)),
               obs.Element(role="radio button", name="Don't save changes", selected=selected, bounds=box(50)),
               obs.Element(role="button", name="OK", bounds=box(90))]  # fmt: skip
        fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title="Microsoft Outlook")
        return obs.Snapshot(windows=[fg], elements=els)

    desktop = obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND, process_name="explorer.exe")])
    agent = FakeAgent([prompt(False), prompt(True), desktop])
    runner = Runner(agent, None, cli.load_persona(None), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    runner.abandon(task(), "test")
    assert [y // 40 for _, y in agent.clicked] == [1, 2]  # "Don't save changes", then OK; never the send choice


def test_replace_selects_the_prefilled_value_before_typing(tmp_path: Path) -> None:
    from ludius.options import Option

    field = obs.Element(key="k", role="edit", name="Start time, combo", value="00:30", focused=True, enabled=True,
                        actions=["value"], bounds=obs.Rect(x=1, y=1, width=50, height=20))  # fmt: skip
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title="Untitled - Meeting")
    snap = obs.Snapshot(windows=[fg], elements=[field])
    agent = FakeAgent([snap])
    agent.type = lambda text, mean, jitter: agent.chords.append([-1])  # type: ignore[attr-defined]
    runner = Runner(agent, None, cli.load_persona(None), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    runner.snap = snap
    t = task()
    refused = runner.act(Option("o1", "type", "type", element=field, content="start"), t, t.subgoal("fill_start"))
    assert refused is None and agent.chords == [keys.chord("Ctrl+A"), [-1]]


def test_a_launch_query_replaces_an_old_query_in_the_start_search(tmp_path: Path) -> None:
    from ludius.options import Option

    box = obs.Element(key="s", role="edit", name="Search box", value="edgeoutlook", focused=True, enabled=True,
                      actions=["value"], bounds=obs.Rect(x=1, y=1, width=50, height=20))  # fmt: skip
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="SearchApp.exe", title="Search")
    snap = obs.Snapshot(windows=[fg], elements=[box])
    agent = FakeAgent([snap])
    agent.type = lambda text, mean, jitter: agent.chords.append([-1])  # type: ignore[attr-defined]
    runner = Runner(agent, None, cli.load_persona(None), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    runner.snap = snap
    t = task()
    for o in (Option("o1", "type", "type", element=box, content="launch_query"),
              Option("o2", "type", "type", content="launch_query")):  # fmt: skip  # into the box, and at its focus
        agent.chords.clear()
        assert runner.act(o, t, t.subgoal("open_outlook")) is None
        assert agent.chords == [keys.chord("Ctrl+A"), [-1]]


def test_abandon_leaves_a_modal_that_ignores_close_keys_by_dont_send_or_cancel(tmp_path: Path) -> None:
    def box(y: int) -> obs.Rect:
        return obs.Rect(x=10, y=y, width=9, height=9)

    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title="Microsoft Outlook")
    desktop = obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND, process_name="explorer.exe")])
    runner_of = lambda agent: Runner(agent, None, cli.load_persona(None), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]  # noqa: E731
    # Run 1's prompt: Don't Send is the discard answer; Send Anyway is never clicked.
    nolocation = obs.Snapshot(windows=[fg], elements=[
        obs.Element(role="button", name="Don't Send", bounds=box(50)),
        obs.Element(role="button", name="Send Anyway", bounds=box(90))])  # fmt: skip
    agent = FakeAgent([nolocation, desktop])
    runner_of(agent).abandon(task(), "test")
    assert [y // 40 for _, y in agent.clicked] == [1]
    # A modal without a discard answer that ignores Alt+F4: its Cancel button, after the key changed nothing.
    stuck = obs.Snapshot(windows=[fg], elements=[
        obs.Element(role="button", name="Send", bounds=box(10)),
        obs.Element(role="button", name="Cancel", bounds=box(50))])  # fmt: skip
    agent = FakeAgent([stuck, stuck, desktop])
    runner_of(agent).abandon(task(), "test")
    assert agent.chords == [keys.chord("Alt+F4")] and [y // 40 for _, y in agent.clicked] == [1]
    # No Cancel or Close either: give up (operator), never another button.
    agent = FakeAgent([obs.Snapshot(windows=[fg], elements=[obs.Element(role="button", name="Send", bounds=box(10))])])
    runner_of(agent).abandon(task(), "test")
    assert agent.clicked == [] and len(agent.chords) == 1


def test_the_guarded_sends_own_prompt_is_confirmed_once_and_nothing_else(tmp_path: Path) -> None:
    from ludius.tasks import send_confirmation

    t = task()
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title="Microsoft Outlook")
    text = "Do you want to send this meeting request without a location?"
    prompt = obs.Snapshot(windows=[fg], elements=[
        obs.Element(role="edit", name="RichEdit Control", value=text),
        obs.Element(role="button", name="Don't Send"), obs.Element(role="button", name="Send Anyway")])  # fmt: skip
    assert (b := send_confirmation(prompt, t)) is not None and b.name == "Send Anyway"
    other = obs.Snapshot(windows=[fg], elements=[obs.Element(role="edit", value="Send this to everyone?"),
                                                 obs.Element(role="button", name="Send Anyway")])  # fmt: skip
    assert send_confirmation(other, t) is None
    edge = obs.Window(kind=obs.Window.FOREGROUND, process_name="msedge.exe")
    assert send_confirmation(obs.Snapshot(windows=[edge], elements=list(prompt.elements)), t) is None
    mail_task = Task(cli.load_templates()["outlook_send_mail"], PlanItem.model_validate(
        {"at": "09:00", "template": "outlook_send_mail", "params": {"to": "mvellinga@ludius.internal",
         "to_name": "Marcus Vellinga", "subject": "s", "body": "b"}}), random.Random(0))  # fmt: skip
    assert send_confirmation(prompt, mail_task) is None  # only the meeting form's prompt


def test_a_save_name_typed_at_the_focus_replaces_the_box(tmp_path: Path) -> None:
    """Live run 11 (dkoster): Save As's File name held '*.txt', and each retry appended the name to it."""
    from ludius.options import Option
    from ludius.tasks import TaskInstance

    box = obs.Element(key="f", role="edit", name="File name:", value="*.txtmaintenance", focused=True, enabled=True,
                      actions=["value"], bounds=obs.Rect(x=1, y=1, width=50, height=20))  # fmt: skip
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="notepad.exe", title="Save As")
    snap = obs.Snapshot(windows=[fg], elements=[box])
    agent = FakeAgent([snap])
    agent.type = lambda text, mean, jitter: agent.chords.append([-1])  # type: ignore[attr-defined]
    runner = Runner(agent, None, cli.load_persona(None), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    runner.snap = snap
    t = Task(cli.load_templates()["notepad_write_note"], TaskInstance(template="notepad_write_note",
             params={"file_name": "maintenance", "body": "x"}, variant=0), random.Random(0))  # fmt: skip
    assert runner.act(Option("o1", "type", "type", content="file_name"), t, t.subgoal("name_file")) is None
    assert agent.chords == [keys.chord("Ctrl+A"), [-1]]
    # Never select-all in another field: the note's body would be replaced by the name.
    body = obs.Element(key="b", role="document", name="Text Editor", value="press 3 oil leak", focused=True,
                       enabled=True, actions=["value"], bounds=obs.Rect(x=1, y=1, width=50, height=20))  # fmt: skip
    runner.snap = obs.Snapshot(windows=[fg], elements=[body])
    agent.snaps, agent.chords = [runner.snap], []
    assert runner.act(Option("o1", "type", "type", content="file_name"), t, t.subgoal("name_file")) is not None
    assert agent.chords == []


def test_a_url_typed_again_replaces_what_the_address_bar_holds(tmp_path: Path) -> None:
    """Live team4: each retry appended chat_url to the address bar ('intranet...testchat...testchat...test')."""
    from ludius.options import Option
    from ludius.tasks import TaskInstance

    def edit(name: str, value: str) -> obs.Element:
        return obs.Element(key=name, role="edit", name=name, value=value, focused=True, enabled=True,
                           actions=["value"], bounds=obs.Rect(x=1, y=1, width=50, height=20))  # fmt: skip

    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="msedge.exe", title="Intranet")
    bar = edit("Address and search bar", "intranet.corp.ludius.test")
    agent = FakeAgent([])
    agent.type = lambda text, mean, jitter: agent.chords.append([-1])  # type: ignore[attr-defined]
    runner = Runner(agent, None, cli.load_persona(None), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    t = Task(cli.load_templates()["chat_post_message"], TaskInstance(template="chat_post_message",
             params={"channel": "Off-Topic", "message": "hi"}, variant=0), random.Random(0))  # fmt: skip
    for o in (Option("o1", "type", "type", content="chat_url"), Option("o2", "type", "type", element=bar,
                                                                         content="chat_url")):  # fmt: skip
        runner.snap = obs.Snapshot(windows=[fg], elements=[bar])
        agent.snaps, agent.chords = [runner.snap], []
        assert runner.act(o, t, t.subgoal("open_chat")) is None
        assert agent.chords == [keys.chord("Ctrl+A"), [-1]]
    # Another field holding the focus: never select-all there.
    box = edit("Search the web", "")
    runner.snap = obs.Snapshot(windows=[fg], elements=[box])
    agent.snaps, agent.chords = [runner.snap], []
    assert runner.act(Option("o1", "type", "type", content="chat_url"), t, t.subgoal("open_chat")) is not None
    assert agent.chords == []
