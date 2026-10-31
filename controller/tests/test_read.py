from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, loop, mail, reply, requests
from ludius.loop import GroundTruth, Runner
from ludius.planner import Person, PlanItem, asked_files, validate
from ludius.policy import Policy
from ludius.tasks import Persona, Task, TaskInstance, Template, check_done
from ludius.v1 import observation_pb2 as obs

BOSS = "mvellinga@ludius.internal"
SUBJECT = "Points for tomorrow's ERP cost policy review"
PEOPLE = {"aberg": "Anna Berg", "mvellinga": "Marcus Vellinga", "lnord": "Lars Nord"}
ANNA = Persona(name="Anna Berg", user="aberg", role="accountant")


def msg(i: int, subject: str = SUBJECT, sender: str = BOSS, read: bool = False) -> mail.Message:
    return mail.Message(sender, 0, 0, "Hi Anna, please save the points.", id=i, cls="IPM.Note", subject=subject,
                        msgid=f"<{i}@ludius.internal>", read=read)  # fmt: skip


def server(monkeypatch: pytest.MonkeyPatch, inbox: list[mail.Message] | None, states: list[bool | None]) -> list[int]:
    asked: list[int] = []

    def matching(user: str, folder: str, subject: str) -> list[mail.Message] | None:
        return None if inbox is None else [m for m in inbox if m.subject == subject]

    def read_state(user: str, message_id: int, folder: str = "inbox") -> bool | None:
        asked.append(message_id)
        return states.pop(0) if len(states) > 1 else states[0]

    monkeypatch.setattr(mail, "matching", matching)
    monkeypatch.setattr(mail, "read_state", read_state)
    return asked


def template() -> Template:
    return cli.load_templates()["outlook_read_mail"]


def read_task(subject: str = SUBJECT, sender: str = BOSS) -> Task:
    t = template()
    task = Task(t, TaskInstance(template=t.task, params={"sender": sender, "subject": subject}, variant=0),
                random.Random(0))  # fmt: skip
    task.owner = "aberg"
    return task


@pytest.fixture
def narrative(tmp_path: Path) -> Path:
    path = tmp_path / "narrative.yaml"
    people = "\n".join(f"- {{name: {n}, role: r, department: d, user: {u}}}" for u, n in PEOPLE.items())
    path.write_text(f"people:\n{people}\n", encoding="utf-8")
    return path


def test_template_declares_a_read_only_task() -> None:
    t = template()
    assert t.capability == "mail.read" and not t.sends_mail and not t.writes and t.processes == ["outlook.exe"]
    assert t.subgoals["open_message"].keys == ["Enter"] and t.subgoals["close_message"].keys == ["Esc"]
    assert all(not sg.allow_destructive for sg in t.subgoals.values())
    raw = t.model_dump(exclude={"processes"})
    with pytest.raises(ValueError, match="mail.read"):
        Template.model_validate({**raw, "capability": None})
    with pytest.raises(ValueError, match="mail.read"):
        bad = {**raw, "subgoals": {**raw["subgoals"], "close_outlook": {"goal": "x", "text": "subject"}}}
        Template.model_validate(bad)


def test_read_state_is_unknown_unless_the_server_says_0_or_1(monkeypatch: pytest.MonkeyPatch) -> None:
    out: list[Any] = []

    def ssh(remote: str, *_: object) -> Any:
        return out[0]

    monkeypatch.setattr(mail, "_ssh", ssh)
    for stdout, code, want in ((b"1\n", 0, True), (b"0\n", 0, False), (b"", 0, None), (b"1\n", 1, None)):
        out[:] = [subprocess.CompletedProcess([], code, stdout, b"")]
        assert mail.read_state("aberg", 196615) is want
    out[:] = [None]
    assert mail.read_state("aberg", 196615) is None
    assert mail.read_state("a;b", 1) is None and mail.read_state("aberg", 0) is None


def test_to_read_picks_only_an_unread_colleague_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    server(monkeypatch, [msg(7)], [False])
    assert reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, Policy()) == msg(7)
    server(monkeypatch, [msg(7)], [True])
    assert "already read at task start" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, Policy()))
    server(monkeypatch, [msg(7)], [None])
    assert "read state unreadable" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, Policy()))
    server(monkeypatch, None, [False])
    assert "inbox unreadable" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, Policy()))
    server(monkeypatch, [msg(7), msg(8, sender="lnord@ludius.internal")], [False])
    assert "in the Inbox" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, Policy()))
    server(monkeypatch, [msg(7)], [False])
    for sender in ("someone@ludius.internal", "aberg@ludius.internal", "mvellinga@example.com"):
        assert "not a colleague" in str(reply.to_read("aberg", sender, SUBJECT, PEOPLE, Policy()))
    reserved = Policy(reserved_paths=[r"mvellinga@ludius.internal"])
    assert "reserved" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, reserved))


def test_done_is_the_read_flag_of_the_mail_proven_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    done = template().subgoals["open_message"].done
    task = read_task()
    win = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title=f"{SUBJECT} - Message (Plain Text)")
    open_ = obs.Snapshot(windows=[win])
    asked = server(monkeypatch, [msg(7)], [True])
    assert not check_done(done, open_, task, False)
    assert asked == []
    task.read_id = 7
    assert check_done(done, open_, task, False) and asked == [7]
    assert not check_done(done, obs.Snapshot(), task, False)
    server(monkeypatch, [msg(7)], [None])
    assert not check_done(done, open_, task, False)
    assert check_done(done, open_, task, False, outside=False)


def test_runner_waits_for_the_read_flag_without_acting(
    monkeypatch: pytest.MonkeyPatch, narrative: Path, tmp_path: Path
) -> None:
    asked = server(monkeypatch, [msg(7)], [False, False, False, True])
    monkeypatch.setattr(loop, "NARRATIVE", narrative)
    monkeypatch.setattr(loop, "BLOCKED_WAIT_S", 0)
    main = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title="Inbox - aberg - Outlook")
    message = obs.Window(process_name="OUTLOOK.EXE", title=f"{SUBJECT} - Message")
    reading, closed = obs.Snapshot(windows=[main], apps=[message], guest_time_ms=5), obs.Snapshot(guest_time_ms=5)

    class Agent:
        vm = "t"

        def observe(self, **_: Any) -> Any:
            return type("R", (), {"snapshot": closed if len(asked) >= 4 else reading})()

    class Decisions:
        def decide(self, *_: Any, **__: Any) -> Any:
            pytest.fail("no Decisions call: every subgoal is done by code or waits for the server")

    log = GroundTruth(tmp_path / "gt.jsonl")
    out = Runner(Agent(), Decisions(), ANNA, log).run(read_task())  # type: ignore[arg-type]
    log.close()
    text = (tmp_path / "gt.jsonl").read_text(encoding="utf-8")
    assert out.status == "done" and out.subgoals_done == template().variants[0]
    assert asked == [7, 7, 7, 7] and text.count("waiting for the outside check") == 2
    assert '"mail_id": 7' in text and '"read_at_start": false' in text


def test_runner_abandons_a_mail_read_before_the_task(
    monkeypatch: pytest.MonkeyPatch, narrative: Path, tmp_path: Path
) -> None:
    server(monkeypatch, [msg(7)], [True])
    monkeypatch.setattr(loop, "NARRATIVE", narrative)

    class Agent:
        vm, inputs = "t", list[str]()

        def observe(self, **_: Any) -> Any:
            return type("R", (), {"snapshot": obs.Snapshot(guest_time_ms=5)})()

        def press(self, *_: object, **__: object) -> int:
            return 0

        def __getattr__(self, name: str) -> Any:
            self.inputs.append(name)
            return self.press

    agent = Agent()
    out = Runner(agent, None, ANNA, GroundTruth(tmp_path / "gt.jsonl")).run(read_task())  # type: ignore[arg-type]
    assert out.status == "abandoned" and out.steps == 0 and "already read" in out.reason and agent.inputs == []


def item(at: str, subject: str = SUBJECT, sender: str = BOSS) -> PlanItem:
    return PlanItem(template="outlook_read_mail", at=at, params={"sender": sender, "subject": subject})


def test_plan_reads_only_listed_inbox_mail_once() -> None:
    ts, colleagues, inbox = cli.load_templates(), {"mvellinga", "lnord"}, {("mvellinga", SUBJECT)}

    def why(items: list[PlanItem]) -> dict[int, str]:
        return dict(validate(items, ts, [], Policy(), colleagues, inbox=inbox))

    assert why([item("08:30")]) == {}
    assert "already read earlier" in why([item("08:30"), item("09:00")])[1]
    assert "listed under Inbox" in why([item("08:30", subject="other")])[0]
    assert "not a colleague" in why([item("08:30", sender="x@example.com")])[0]
    assert "listed under Inbox" in dict(validate([item("08:30")], ts, [], Policy(), colleagues, inbox=set()))[0]


def test_a_request_mail_is_read_before_its_task() -> None:
    ts = cli.load_templates()
    req = PlanItem(template="notepad_write_note", at="10:31", params={}, request="<7@ludius.internal>")
    other = PlanItem(template="break", at="10:26", minutes=5)
    read = requests.read_first(req, [msg(7)], ts, [req, other])
    assert read is not None and read.at == "10:25" and read.params == {"sender": BOSS, "subject": SUBJECT}
    assert requests.read_first(req, [msg(7, read=True)], ts, [req]) is None
    assert requests.read_first(req, [msg(8)], ts, [req]) is None
    no_read = {k: t for k, t in ts.items() if t.capability != "mail.read"}
    assert requests.read_first(req, [msg(7)], no_read, [req]) is None


def outlook(*els: obs.Element, process: str = "OUTLOOK.EXE", title: str = "Inbox - aberg - Outlook") -> obs.Snapshot:
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name=process, title=title)
    return obs.Snapshot(windows=[fg], elements=list(els))


def el(i: int, role: str, name: str, **kw: Any) -> obs.Element:
    box = obs.Rect(x=10, y=10 * i, width=50, height=9)
    return obs.Element(id=i, key=f"k{i}", role=role, name=name, enabled=True, bounds=box, window=0, **kw)


def keys_offered(snap: obs.Snapshot, task: Task, subgoal: str, policy: Policy) -> set[str]:
    from ludius.options import build_options, view

    sg = task.subgoal(subgoal)
    return {o.keys for o in build_options(view(snap, task, sg, policy), task, sg, policy) if o.kind == "key"}


def post_check(snap: obs.Snapshot, task: Task, subgoal: str, o: Any, tmp_path: Path) -> str | None:
    r = Runner(None, None, ANNA, GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    r.snap = snap
    return r._post_check(o, task.subgoal(subgoal), task)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("name", ["Send", "Delete", "&Save"])
def test_enter_never_presses_a_focused_committing_control(name: str, tmp_path: Path) -> None:
    from ludius.options import Option

    task = read_task()
    snap = outlook(el(1, "button", name, focused=True), el(2, "data item", f"From Marcus Vellinga, Subject {SUBJECT}"))
    assert not {"Enter", "Space"} & keys_offered(snap, task, "open_message", Policy())
    assert "commits or destroys" in str(post_check(snap, task, "open_message", Option("o1", "key", "", keys="Enter"),
                                                   tmp_path))  # fmt: skip
    row = outlook(el(2, "data item", f"From Marcus Vellinga, Subject {SUBJECT}", focused=True))
    assert "Enter" in keys_offered(row, task, "open_message", Policy())
    assert "focus is unknown" in str(Policy().key_allowed("Enter", outlook()))
    assert Policy().key_allowed("Enter", outlook(), ["Save"]) is None
    if name == "Send":
        assert Policy().key_allowed("Enter", snap, ["Send"]) is None


def test_reserved_mail_rows_are_never_offered_or_opened(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text("messages: [Scenario evidence]\n", encoding="utf-8")
    policy = Policy.load(scenario)
    task = read_task()
    from ludius.options import build_options, view

    for name in ("Scenario evidence", "From Marcus Vellinga, Subject Scenario evidence, Received Wed 28/10"):
        row = el(1, "data item", name)
        sg = task.subgoal("open_message")
        opts = build_options(view(outlook(row), task, sg, policy), task, sg, policy)
        assert not [o for o in opts if o.element is not None], name
        for flag in ("focused", "selected"):
            snap = outlook(el(1, "data item", name, **{flag: True}))
            assert policy.key_allowed("Enter", snap) == "scenario-reserved path"
            assert policy.key_allowed("Space", snap) == "scenario-reserved path"


def test_launch_query_goes_into_the_start_search_only(tmp_path: Path) -> None:
    from ludius.options import Option, build_options, launch_target, view

    task = read_task()
    sg = task.subgoal("open_outlook")
    draft = outlook(el(1, "edit", "Subject", focused=True), title="Untitled - Message (HTML)")
    opts = build_options(view(draft, task, sg, Policy()), task, sg, Policy())
    assert not [o for o in opts if o.kind == "type"]
    for o in (Option("o1", "type", "", element=draft.elements[0], content="launch_query"),
              Option("o2", "type", "", content="launch_query")):  # fmt: skip
        assert "Start search box only" in str(post_check(draft, task, "open_outlook", o, tmp_path))
    start = outlook(el(1, "edit", "Search box", focused=True), process="SearchApp.exe", title="Search")
    assert launch_target(start.elements[0], start)
    assert [o.kind for o in build_options(view(start, task, sg, Policy()), task, sg, Policy())].count("type") == 2
    run = outlook(el(1, "edit", "Open:", focused=True), process="explorer.exe", title="Run")
    run.windows[0].class_name = "#32770"
    assert launch_target(run.elements[0], run)
    run.windows[0].title = "Untitled - Notepad"
    assert not launch_target(run.elements[0], run)
    raw = template().model_dump(exclude={"processes"})
    with pytest.raises(ValueError, match="mail.read"):
        bad = {**raw, "subgoals": {**raw["subgoals"], "close_outlook": {"goal": "x", "copy_text": True}}}
        Template.model_validate(bad)


def test_a_requested_file_is_mailed_back_to_the_manager(tmp_path: Path) -> None:
    ts = cli.load_templates()
    boss = Person(name="Marcus Vellinga", role="lead", department="ops", user="mvellinga")
    req = PlanItem(
        template="word_short_document", at="10:31", params={"file_name": "feb volumes"}, request="<7@ludius.internal>"
    )
    back = requests.deliver(req, boss, [msg(7)], ts, [req])
    assert back is not None and back.template == "outlook_send_attachment"
    assert back.at > "10:31" and back.params["file_name"] == "feb volumes.docx"
    assert back.params["to"] == "mvellinga@ludius.internal" and back.params["subject"] == f"RE: {SUBJECT}"
    owned = asked_files(back, [req, back], ts, "2026-11-02")
    assert owned == {"documents\\feb volumes.docx": "2026-11-02"} and back.request == req.request
    assert not asked_files(back, [back], ts, "2026-11-02")
    people = {"mvellinga": boss.name}
    assert not validate([back], ts, [], Policy(), {"mvellinga"}, people=people, owned=owned, day="2026-11-02")
    both = validate([req, back], ts, [], Policy(), {"mvellinga"}, people=people, day="2026-11-02")
    assert 1 not in dict(both)  # a revalidated plan finds today's write without being told
    ask = requests.Request(to="aberg", task="word_short_document", subject=SUBJECT, body="", file_name="feb volumes",
                           manager="mvellinga", msgid="<7@ludius.internal>")  # fmt: skip
    t = ts["outlook_send_attachment"]
    assert requests.mismatch(ask, t, back.params) is None
    assert requests.mismatch(ask, t, {**back.params, "file_name": "other.docx"})
    task = Task(t, back, random.Random(0))
    task.owner = "aberg"
    assert requests.waiting(task, tmp_path)
    requests.remember(tmp_path, "aberg", back.request, requests.WRITTEN)
    assert requests.waiting(task, tmp_path) is None
    assert requests.deliver(req, boss, [msg(8)], ts, [req]) is None
    assert requests.deliver(PlanItem(template="paint_quick_sketch", at="10:31", params={"file_name": "x"},
                                     request="<7@ludius.internal>"), boss, [msg(7)], ts, []) is None  # fmt: skip
