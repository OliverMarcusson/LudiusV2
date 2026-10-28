"""mail.read (outlook_read_mail): the mail is picked and proven unread at task start, done is its read flag on the
server, the runner waits for that flag instead of acting, and request plans read the mail before the work."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, loop, mail, reply, requests
from ludius.loop import GroundTruth, Runner
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Persona, Task, TaskInstance, Template, check_done
from ludius.v1 import observation_pb2 as obs

BOSS = "mvellinga@corp.ludius.test"
SUBJECT = "Points for tomorrow's ERP cost policy review"
PEOPLE = {"aberg": "Anna Berg", "mvellinga": "Marcus Vellinga", "lnord": "Lars Nord"}
ANNA = Persona(name="Anna Berg", user="aberg", role="accountant")


def msg(i: int, subject: str = SUBJECT, sender: str = BOSS, read: bool = False) -> mail.Message:
    return mail.Message(sender, 0, 0, "Hi Anna, please save the points.", id=i, cls="IPM.Note", subject=subject,
                        msgid=f"<{i}@corp.ludius.test>", read=read)  # fmt: skip


def server(monkeypatch: pytest.MonkeyPatch, inbox: list[mail.Message] | None, states: list[bool | None]) -> list[int]:
    """Inbox by subject; read_state answers `states` in turn (the last one repeats). Returns the ids asked."""
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
    with pytest.raises(ValueError, match="mail.read"):  # a read check needs the capability
        Template.model_validate({**raw, "capability": None})
    with pytest.raises(ValueError, match="mail.read"):  # and the capability types nothing of its own
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
    server(monkeypatch, [msg(7), msg(8, sender="lnord@corp.ludius.test")], [False])  # opened by subject: ambiguous
    assert "in the Inbox" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, Policy()))
    server(monkeypatch, [msg(7)], [False])
    for sender in ("someone@corp.ludius.test", "aberg@corp.ludius.test", "mvellinga@example.com"):
        assert "not a colleague" in str(reply.to_read("aberg", sender, SUBJECT, PEOPLE, Policy()))
    reserved = Policy(reserved_paths=[r"mvellinga@corp\.ludius\.test"])
    assert "reserved" in str(reply.to_read("aberg", BOSS, SUBJECT, PEOPLE, reserved))


def test_done_is_the_read_flag_of_the_mail_proven_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    done = template().subgoals["open_message"].done
    task = read_task()
    win = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title=f"{SUBJECT} - Message (Plain Text)")
    open_ = obs.Snapshot(windows=[win])
    asked = server(monkeypatch, [msg(7)], [True])
    assert not check_done(done, open_, task, False)  # no mail proven unread at start: never done
    assert asked == []
    task.read_id = 7
    assert check_done(done, open_, task, False) and asked == [7]
    assert not check_done(done, obs.Snapshot(), task, False)  # not open on screen
    server(monkeypatch, [msg(7)], [None])
    assert not check_done(done, open_, task, False)  # unknown is never done
    assert check_done(done, open_, task, False, outside=False)  # on screen it is


def test_runner_waits_for_the_read_flag_without_acting(
    monkeypatch: pytest.MonkeyPatch, narrative: Path, tmp_path: Path
) -> None:
    asked = server(monkeypatch, [msg(7)], [False, False, False, True])  # start, then Outlook syncs a little later
    monkeypatch.setattr(loop, "NARRATIVE", narrative)
    monkeypatch.setattr(loop, "BLOCKED_WAIT_S", 0)
    main = obs.Window(kind=obs.Window.FOREGROUND, process_name="OUTLOOK.EXE", title="Inbox - aberg - Outlook")
    message = obs.Window(process_name="OUTLOOK.EXE", title=f"{SUBJECT} - Message")  # open behind the main window
    reading, closed = obs.Snapshot(windows=[main], apps=[message], guest_time_ms=5), obs.Snapshot(guest_time_ms=5)

    class Agent:
        vm = "t"

        def observe(self, **_: Any) -> Any:
            return type("R", (), {"snapshot": closed if len(asked) >= 4 else reading})()

    class Jev:
        def decide(self, *_: Any, **__: Any) -> Any:
            pytest.fail("no Jev call: every subgoal is done by code or waits for the server")

    log = GroundTruth(tmp_path / "gt.jsonl")
    out = Runner(Agent(), Jev(), ANNA, log).run(read_task())  # type: ignore[arg-type]
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
    req = PlanItem(template="notepad_write_note", at="10:31", params={}, request="<7@corp.ludius.test>")
    other = PlanItem(template="break", at="10:26", minutes=5)
    read = requests.read_first(req, [msg(7)], ts, [req, other])
    assert read is not None and read.at == "10:25" and read.params == {"sender": BOSS, "subject": SUBJECT}
    assert requests.read_first(req, [msg(7, read=True)], ts, [req]) is None  # read already: it can't be shown
    assert requests.read_first(req, [msg(8)], ts, [req]) is None  # not the request's mail
    no_read = {k: t for k, t in ts.items() if t.capability != "mail.read"}
    assert requests.read_first(req, [msg(7)], no_read, [req]) is None
