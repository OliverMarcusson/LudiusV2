from __future__ import annotations

import dataclasses
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, mail, tasks
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Task, check_done, preconditions
from ludius.v1 import observation_pb2 as obs

COLLEAGUES = {"mvellinga", "lnord"}
THREADS = {"f1": ("mvellinga", "lund accruals q4 status")}
PEOPLE = {"mvellinga": "Marcus Vellinga", "lnord": "Lars Nord", "aberg": "Anna Berg"}


def item(template: str = "outlook_send_mail", **extra: Any) -> dict[str, Any]:
    params = {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga", "subject": "lund accruals q4 status",
              "body": "hi marcus\nlund accruals for q4 are booked"}  # fmt: skip
    return {"at": "09:00", "template": template, "params": params | extra.pop("params", {}),
            "context": "Marcus asked for the status.", **extra}  # fmt: skip


def check(*items: dict[str, Any]) -> list[str]:
    templates = cli.plan_sources().templates
    its = [PlanItem.model_validate(i) for i in items]
    return [why for _, why in validate(its, templates, [], Policy(), COLLEAGUES, followups={"f1", "f2"},
                                       mail_threads=THREADS, people=PEOPLE)]  # fmt: skip


def test_templates_load_with_server_side_send_checks() -> None:
    templates = cli.load_templates()
    for name in ("outlook_send_mail", "outlook_reply_mail"):
        t = templates[name]
        sends = [sg for sg in t.subgoals.values() if sg.done and sg.done.mail]
        assert sends and all(sg.done and sg.done.mail and sg.done.mail.mailbox == "{to}" for sg in sends)
        assert all(sg.require and sg.forbid for sg in sends)
        assert all(any(t.subgoals[n].done and t.subgoals[n].done.mail for n in v) for v in t.variants)  # pyright: ignore[reportOptionalMemberAccess]


def test_mail_to_a_colleague_is_valid() -> None:
    assert check(item(handoff="mvellinga")) == []
    assert check(item()) == []


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"params": {"to": "someone@example.com"}}, "not a colleague's address"),
        ({"params": {"to": "jdoe@ludius.internal"}}, "not a colleague's address"),
        ({"handoff": "lnord"}, "must be the mail's recipient"),
        ({"params": {"to_name": "Marcus Vellinga; outside@example.com"}}, "name from the company story"),
        ({"params": {"to_name": "Marcus Vellinga <outside@example.com>"}}, "to_name"),
        ({"params": {"to_name": "Lars Nord"}}, "name from the company story"),
        ({"params": {"to_name": ""}}, "missing parameters"),
    ],
)
def test_mail_recipient_is_checked(extra: dict[str, Any], why: str) -> None:
    (got,) = check(item(**extra))
    assert why in got


def test_to_name_is_derived_from_the_narrative_not_the_llm() -> None:
    from ludius.planner import catalog, fill_recipient_names

    its = [PlanItem.model_validate(item(params={"to_name": "Marcus Vellinga; outside@example.com"})),
           PlanItem.model_validate(item(params={"to": "jdoe@ludius.internal"}))]  # fmt: skip
    fill_recipient_names(its, PEOPLE)
    assert [it.params["to_name"] for it in its] == ["Marcus Vellinga", ""]
    assert "to_name" not in catalog(cli.load_templates())


def test_reply_must_answer_the_matching_mail_followup() -> None:
    assert check(item("outlook_reply_mail", followup="f1")) == []
    assert "set followup" in check(item("outlook_reply_mail"))[0]
    assert "set followup" in check(item("outlook_reply_mail", followup="f2"))[0]
    wrong_subject = item("outlook_reply_mail", followup="f1", params={"subject": "other thing"})
    wrong_sender = item(
        "outlook_reply_mail", followup="f1", params={"to": "lnord@ludius.internal", "to_name": "Lars Nord"}
    )
    for it in (wrong_subject, wrong_sender):
        assert "reply to that, with that subject" in check(it)[0]


def test_done_is_read_on_the_server_whatever_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item()), random.Random(0))
    task.owner, task.started_ms, task.mail_before = "aberg", 1_000, frozenset()
    calls: list[tuple[object, ...]] = []

    def delivered(*a: object) -> bool:
        calls.append(a)
        return len(calls) == 1

    monkeypatch.setattr(mail, "delivered", delivered)
    done = t.subgoals["send_shortcut"].done
    assert check_done(done, obs.Snapshot(), task, typed_ok=False) is True
    assert calls == [("mvellinga@ludius.internal", "inbox", "lund accruals q4 status",
                      "hi marcus\nlund accruals for q4 are booked", "aberg@ludius.internal", 1_000,
                      False, frozenset(), "", None)]  # fmt: skip
    assert check_done(done, obs.Snapshot(), task, typed_ok=True) is False


def test_body_match_ignores_outlook_autocorrect() -> None:
    sent = "Hi marcus\r\n\r\n•\tLund accruals for Q4 are booked – anna's\r\n"
    assert mail.letters("hi marcus\n- lund accruals for q4 are booked - anna's") == mail.letters(sent)
    assert mail.letters("booked") in mail.letters(sent)
    assert mail.letters("not booked") not in mail.letters(sent)


def test_foreign_or_malformed_mailbox_never_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "_host", lambda: pytest.fail("no query expected"))
    assert not mail.delivered("aberg@example.com", "inbox", "s", "b", "aberg@ludius.internal", 1)
    assert mail.messages("a;rm -rf", "inbox", "s") == []


SEED = mail.Message("mvellinga@ludius.internal", 1_000, 1_000, "Hi Anna,\r\nyes\r\n")
REPLY = mail.Message(
    "aberg@ludius.internal", 5_000, 5_000, "Yes, booked.\r\n-----\r\nHi Anna, yes", ("mvellinga@ludius.internal",)
)


@pytest.mark.parametrize(
    ("body", "sender", "since", "ok"),
    [
        ("yes, booked", "aberg@ludius.internal", 4_000, True),
        ("yes", "aberg@ludius.internal", 6_000, False),
        ("hi anna", "aberg@ludius.internal", 4_000, True),
        ("yes", "mvellinga@ludius.internal", 4_000, False),
        ("yes", "aberg@ludius.internal", 0, False),
        ("- ...", "aberg@ludius.internal", 4_000, False),
        ("yes", "", 4_000, False),
    ],
)
def test_delivered_needs_sender_time_and_a_real_body(
    monkeypatch: pytest.MonkeyPatch, body: str, sender: str, since: int, ok: bool
) -> None:
    def messages(*_: str) -> list[mail.Message]:
        return [SEED, REPLY]

    monkeypatch.setattr(mail, "messages", messages)
    assert mail.delivered("mvellinga@ludius.internal", "inbox", "s", body, sender, since) is ok


@pytest.mark.parametrize(
    "sent_to",
    [
        ("mvellinga@ludius.internal", "outside@example.com"),
        ("lnord@ludius.internal", "mvellinga@ludius.internal"),
        ("?",),
        (),
    ],
)
def test_sent_copy_must_go_to_the_planned_recipient_alone(
    monkeypatch: pytest.MonkeyPatch, sent_to: tuple[str, ...]
) -> None:
    sent = mail.Message("aberg@ludius.internal", 5_000, 0, "Yes, booked.", sent_to)

    def messages(user: str, folder: str, _subject: str, _attachments: bool = False) -> list[mail.Message]:
        return [sent] if (user, folder) == ("aberg", "sent") else [REPLY]

    monkeypatch.setattr(mail, "messages", messages)
    assert not mail.delivered("mvellinga@ludius.internal", "inbox", "s", "yes booked", "aberg@ludius.internal", 1)
    sent = mail.Message("aberg@ludius.internal", 5_000, 0, "Yes, booked.", ("mvellinga@ludius.internal",))
    assert mail.delivered("mvellinga@ludius.internal", "inbox", "s", "yes booked", "aberg@ludius.internal", 1)


MIN = mail.MINUTE_MS
START = 100 * MIN + 8_000


@pytest.mark.parametrize(
    ("submit", "delivery", "ok"),
    [
        (START + 26_000, START + 27_000, True),
        (START + 26_000, 100 * MIN, True),
        (START + 30 * MIN, 50 * MIN, False),
        (50 * MIN, START + MIN, False),
        (START + 5 * MIN, START + MIN, False),
        (START + 1_000, 0, False),
        (0, START + 1_000, False),
    ],
)
def test_delivered_needs_submit_and_delivery_times_in_the_task(
    monkeypatch: pytest.MonkeyPatch, submit: int, delivery: int, ok: bool
) -> None:
    m = mail.Message("aberg@ludius.internal", submit, delivery, "Yes, booked.", ("mvellinga@ludius.internal",))

    def messages(*_: str) -> list[mail.Message]:
        return [m]

    monkeypatch.setattr(mail, "messages", messages)
    assert mail.delivered("mvellinga@ludius.internal", "inbox", "s", "yes", "aberg@ludius.internal", START) is ok


@pytest.mark.parametrize("error", [FileNotFoundError("pwsh"), subprocess.TimeoutExpired("pwsh", 30)])
def test_host_discovery_failure_is_not_delivered(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def boom(*_: object, **__: object) -> None:
        raise error

    mail._host.cache_clear()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(subprocess, "run", boom)
    assert mail.messages("aberg", "inbox", "s") == []
    mail._host.cache_clear()  # pyright: ignore[reportPrivateUsage]


def test_send_is_blocked_unless_to_is_exact_and_cc_bcc_empty() -> None:
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item()), random.Random(0))
    sg = t.subgoals["send_button"]

    def snap(**fields: str) -> obs.Snapshot:
        return obs.Snapshot(windows=[obs.Window(kind=obs.Window.POPUP), obs.Window(kind=obs.Window.FOREGROUND)],
                            elements=[obs.Element(role="edit", name=k, value=v, actions=["value"], window=1)
                                      for k, v in fields.items()])  # fmt: skip

    resolved = ("Marcus Vellinga", "Marcus Vellinga <mvellinga@ludius.internal>")
    for to in ("mvellinga@ludius.internal", "mvellinga@ludius.internal;", *resolved):
        assert preconditions(sg, snap(To=to, Cc="", Bcc=""), task) is None
        assert preconditions(sg, snap(To=to, Cc=""), task) is None
    for bad in (snap(To="mvellinga@ludius.internal; outside@example.com", Cc="", Bcc=""),
                snap(To="Marcus Vellinga; outside@example.com", Cc="", Bcc=""), snap(To="Lars Nord", Cc="", Bcc=""),
                snap(To="Marcus Vellinga <outside@example.com>", Cc="", Bcc=""),
                snap(To="mvellinga@ludius.internal", Cc="outside@example.com", Bcc=""),
                snap(To="mvellinga@ludius.internal", Cc="", Bcc="lnord@ludius.internal"),
                snap(To="lnord@ludius.internal", Cc="", Bcc=""),
                snap(To="mvellinga@ludius.internal", Bcc=""),
                snap()):  # fmt: skip
        assert preconditions(sg, bad, task) is not None
    reply = cli.load_templates()["outlook_reply_mail"].subgoals["send_shortcut"]
    assert reply.require == sg.require and reply.forbid == sg.forbid


def test_send_guard_reads_readable_ascii_fields_in_the_foreground_window() -> None:
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item()), random.Random(0))
    sg = t.subgoals["send_button"]
    fields = {"To": "mvellinga@ludius.internal", "Cc": "", "Bcc": ""}

    def snap(where: dict[str, int] | None = None, readable: str = "To Cc Bcc", **values: str) -> obs.Snapshot:
        f = fields | values
        return obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND), obs.Window(kind=obs.Window.OWNED)],
                            elements=[obs.Element(role="edit", name=k, value=v, window=(where or {}).get(k, 0),
                                                  actions=["value"] if k in readable.split() else [])
                                      for k, v in f.items()])  # fmt: skip

    assert preconditions(sg, snap(), task) is None
    assert preconditions(sg, snap(readable="To Bcc"), task) is not None
    assert preconditions(sg, snap(where={"To": 1}), task) is not None
    assert preconditions(sg, snap(where={"Cc": 1}), task) is not None
    send = obs.Element(role="button", name="Send", actions=["invoke"])
    assert preconditions(sg, snap(), task, send) is None
    send.window = 1
    assert preconditions(sg, snap(), task, send) is not None
    assert preconditions(sg, obs.Snapshot(elements=snap().elements), task) is not None
    for to in ("mvell\u0131nga@ludius.internal", "mvellinga@corp.lud\u0131us.test", "Marcus Vell\u0131nga",
               "Marcus Vellinga <mvellinga@ludius.internal>\u200b"):  # fmt: skip
        assert preconditions(sg, snap(To=to), task) is not None, to
    assert preconditions(sg, snap(To="MVellinga@Ludius.Internal"), task) is None


def test_value_less_typing_counts_only_where_the_template_says_so() -> None:
    from ludius.loop import _typed_landed  # pyright: ignore[reportPrivateUsage]
    from ludius.options import Option

    templates = cli.load_templates()
    body = obs.Element(key="b", role="document", name="Untitled Message")
    edit = obs.Element(key="e", role="edit", name="Subject", actions=["value"])
    after = obs.Snapshot(elements=[obs.Element(key="e", role="edit", name="Subject", actions=["value"]),
                                   obs.Element(key="b", role="document", name="Untitled Message")])  # fmt: skip
    outlook = templates["outlook_send_mail"].subgoals["write_body"]
    assert _typed_landed(after, Option("o1", "type", "t", element=body), outlook)
    assert not _typed_landed(after, Option("o2", "type", "t", element=edit), outlook)
    for sg in (templates["notepad_write_note"].subgoals["write_body"],
               templates["wordpad_formatted_note"].subgoals["bold_keys_heading"],
               templates["wordpad_formatted_note"].subgoals["bullets_body"]):  # fmt: skip
        assert not sg.unreadable_ok
        assert not _typed_landed(after, Option("o1", "type", "t", element=body), sg)


def test_recipient_and_answered_subject_may_repeat() -> None:
    first, again = item(), item(at="11:00", params={"subject": "lund accruals q4 reversal", "body": "reversal done"})
    reply = item("outlook_reply_mail", at="13:00", followup="f1", params={"body": "thanks, noted"})
    assert check(first, again, reply) == []


def _range(tmp_path: Path, to_name: str, narrative: bool = True) -> Path:
    import yaml

    from ludius.planner import Narrative, Person

    people = [Person(name=n, role="r", department="d", user=u) for u, n in PEOPLE.items()]
    if narrative:
        n = Narrative(company="c", city="Lund", summary="s", people=people, projects=[], events=[])
        (tmp_path / "world").mkdir()
        (tmp_path / "world" / "narrative.yaml").write_text(yaml.safe_dump(n.model_dump()), encoding="utf-8")
    plan = {"persona": "aberg", "date": "2026-10-29", "items": [item(params={"to_name": to_name})]}
    path = tmp_path / "plans" / "2026-10-29-aberg.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("to_name", "narrative"),
    [("Marcus Vellinga; outside@example.com", True), ("Lars Nord", True), ("Marcus Vellinga", False)],
)
def test_ludius_day_refuses_a_plan_with_a_bad_recipient(tmp_path: Path, to_name: str, narrative: bool) -> None:
    import argparse

    path = _range(tmp_path, to_name, narrative)
    args = argparse.Namespace(plan=str(path), canary=False, temporal=None)
    with pytest.raises(ValueError, match="company story|colleague.s address"):
        cli.cmd_day(args)


def test_a_plan_with_the_narrative_name_loads(tmp_path: Path) -> None:
    from ludius.planner import load_plan

    assert load_plan(_range(tmp_path, "Marcus Vellinga")).items[0].params["to_name"] == "Marcus Vellinga"


def test_proposals_may_not_mark_typing_unreadable() -> None:
    import yaml

    from ludius import growth

    caps = yaml.safe_load((cli.DATA / "capabilities.yaml").read_text(encoding="utf-8"))
    note = cli.load_templates()["notepad_write_note"]
    proposal = note.model_copy(update={"task": "notepad_blind_note"}, deep=True)
    assert growth.check(proposal, caps, {}) == []
    proposal.subgoals["write_body"].unreadable_ok = True
    assert any("unreadable_ok not permitted" in p for p in growth.check(proposal, caps, {}))


def test_a_blocked_subgoal_waits_without_asking_decisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ludius import loop
    from ludius.loop import GroundTruth, Runner
    from ludius.tasks import Persona
    from ludius.v1 import agent_pb2 as pb

    class Agent:
        vm = "t"

        def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
            return pb.ObserveResult(snapshot=obs.Snapshot(screen_width=1920, screen_height=1080, dpi=96))

    class NoDecisions:
        def decide(self, *_: object, **__: object) -> None:
            pytest.fail("Decisions asked while no input was allowed")

    polls: list[object] = []

    def delivered(*a: object) -> bool:
        polls.append(a)
        return len(polls) == 3

    monkeypatch.setattr(mail, "delivered", delivered)
    monkeypatch.setattr(loop, "BLOCKED_WAIT_S", 0)
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item()), random.Random(0))
    task.owner, task.started_ms = "aberg", 1
    runner = Runner(Agent(), NoDecisions(), Persona(name="t", user="aberg"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    status, steps, _ = runner._subgoal(task, "send_button", task.subgoal("send_button"), budget=1, t0=1e18)  # pyright: ignore[reportPrivateUsage]
    assert (status, steps, len(polls)) == ("done", 0, 3)


def test_a_send_field_that_lost_focus_is_clicked_and_waiting_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ludius import loop
    from ludius.loop import GroundTruth, Runner
    from ludius.tasks import Persona

    win = obs.Window(kind=obs.Window.FOREGROUND, process_name="msedge.exe", title="Marcus Vellinga - Mattermost")
    box = obs.Element(id=1, role="edit", name="Write to Marcus Vellinga", value="lunch at noon?", actions=["value"],
                      bounds=obs.Rect(x=10, y=10, width=200, height=20))  # fmt: skip
    clicks: list[int] = []

    class Agent:
        vm = "t"

        def observe(self, **_: Any) -> Any:
            snap = obs.Snapshot(screen_width=1920, screen_height=1080, dpi=96, windows=[win], elements=[box])
            return type("R", (), {"snapshot": snap})()

        def screenshot(self) -> bytes:
            raise RuntimeError("no screen")

        def move(self, *_: object) -> None:
            pass

        def click(self, count: int = 1) -> None:
            clicks.append(count)

    monkeypatch.setattr(loop, "BLOCKED_WAIT_S", 0)
    monkeypatch.setattr(loop, "WAIT_CAP_S", 0)
    t = cli.load_templates()["chat_direct_message"]
    params = {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga", "message": "lunch at noon?"}
    task = Task(t, PlanItem.model_validate(item("chat_direct_message", params=params)), random.Random(0))
    runner = Runner(Agent(), None, Persona(name="t", user="aberg"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    status, steps, why = runner._subgoal(task, "send_message", task.subgoal("send_message"), budget=1, t0=1e18)  # pyright: ignore[reportPrivateUsage]
    assert (status, steps, clicks) == ("failed", 0, [1]) and why.startswith("waited too long: precondition")

    clicks.clear()
    box.value = "lunch at one?"
    runner._subgoal(task, "send_message", task.subgoal("send_message"), budget=1, t0=1e18)  # pyright: ignore[reportPrivateUsage]
    assert clicks == []


def test_reply_never_offers_the_info_bar() -> None:
    from ludius.options import StepView, build_options

    t = cli.load_templates()["outlook_reply_mail"]
    task = Task(t, PlanItem.model_validate(item("outlook_reply_mail", followup="f1")), random.Random(0))
    bar = obs.Element(id=1, role="edit", name="Info Bar", value="You are not responding to the latest message",
                      read_only=True, focused=True, bounds=obs.Rect(width=9, height=9))  # fmt: skip
    body = obs.Element(id=2, role="document", name="RE: lund accruals q4 status", bounds=obs.Rect(width=9, height=9))
    v = StepView(obs.Snapshot(elements=[bar, body]), allowed=[bar, body])
    for name in ("write_reply", "send_shortcut"):
        targets = {o.element.name for o in build_options(v, task, t.subgoals[name], Policy()) if o.element}
        assert "Info Bar" not in targets and body.name in targets, name
    send = cli.load_templates()["outlook_send_mail"]
    for name in ("write_body", "send_shortcut", "send_button"):
        targets = {o.element.name for o in build_options(v, task, send.subgoals[name], Policy()) if o.element}
        assert "Info Bar" not in targets and body.name in targets, name


QUOTED = (
    "\r\nFrom: Marcus Vellinga <mvellinga@ludius.internal>\r\nSent: 27 October 2026 21:23\r\n"
    "Hi Anna,\r\nThanks,\r\nMarcus"
)


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ("Hi Marcus\r\nAll booked.\r\n" + QUOTED, True),
        (QUOTED + "Hi Marcus\r\nAll booked.", False),
        ("Hi Marcus\r\n" + QUOTED + "All booked.", False),
        ("Hi Marcus\r\nAll booked.", True),
    ],
)
def test_reply_text_must_come_before_the_quoted_original(monkeypatch: pytest.MonkeyPatch, body: str, ok: bool) -> None:
    m = mail.Message("aberg@ludius.internal", 5_000, 5_000, body, ("mvellinga@ludius.internal",))

    def messages(*_: str) -> list[mail.Message]:
        return [m]

    monkeypatch.setattr(mail, "messages", messages)
    args = ("mvellinga@ludius.internal", "inbox", "s", "hi marcus\nall booked", "aberg@ludius.internal", 1)
    assert mail.delivered(*args, above_quote=True) is ok
    assert mail.delivered(*args) is (ok or body.startswith(QUOTED))
    done = cli.load_templates()["outlook_reply_mail"].subgoals["send_shortcut"].done
    assert done and done.mail and done.mail.above_quote
    send = cli.load_templates()["outlook_send_mail"].subgoals["send_shortcut"].done
    assert send and send.mail and not send.mail.above_quote


def test_reply_click_puts_the_caret_at_the_top(tmp_path: Path) -> None:
    from ludius import keys
    from ludius.loop import GroundTruth, Runner
    from ludius.options import Option
    from ludius.tasks import Persona

    class FakeAgent:
        vm = "t"

        def __init__(self) -> None:
            self.chords: list[list[int]] = []

        def move(self, x: int, y: int, duration_ms: int = 0) -> int:
            return 0

        def click(self, count: int = 1) -> int:
            return 0

        def chord(self, vks: list[int]) -> int:
            self.chords.append(vks)
            return 0

    agent = FakeAgent()
    runner = Runner(agent, None, Persona(name="t", user="t"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    box = obs.Rect(x=0, y=0, width=800, height=600)
    body = obs.Element(key="d", role="document", name="RE: s - Message", bounds=box)
    note = obs.Element(key="n", role="document", value="heading\r", bounds=box)
    templates = cli.load_templates()
    reply = templates["outlook_reply_mail"].subgoals["write_reply"]
    notepad = templates["notepad_write_note"].subgoals["write_body"]
    assert reply.caret == "start" and notepad.caret == "end"
    for e, sg, expected in ((body, reply, [keys.chord("Ctrl+Home")]), (note, notepad, [keys.chord("Ctrl+End")]),
                            (note, None, [keys.chord("Ctrl+End")])):  # fmt: skip
        agent.chords.clear()
        runner.act(Option("o1", "click", "click", element=e), None, sg)  # type: ignore[arg-type]
        assert agent.chords == expected


def test_mail_templates_get_the_shared_send_contract() -> None:
    from ludius.tasks import Dialogs, ElementCheck, Form, Template

    require = [
        ElementCheck(role="edit", name="^To$", value=r"^({to}|{to_name}|{to_name} <{to}>);?\s*$"),
        ElementCheck(role="edit", name="^Cc$", value="^$"),
    ]
    forbid = [ElementCheck(name="^Cc$", value=r"\S"), ElementCheck(name="^Bcc$", value=r"\S")]
    avoid = [ElementCheck(role="edit", name="^Info Bar$")]  # fmt: skip
    for name in ("outlook_send_mail", "outlook_reply_mail"):
        t = cli.load_templates()[name]
        assert t.sends_mail and t.processes == ["outlook.exe"] and t.defaults["launch_query"] == "outlook"
        for n, sg in t.subgoals.items():
            assert sg.avoid[: len(avoid)] == avoid, n
            sends = bool(sg.done and sg.done.mail)
            assert (sg.require[:2] == require and sg.forbid[:2] == forbid) is sends, n
            assert sg.keys == (["Alt+S"] if n == "send_shortcut" else sg.keys), n
    raw = {"task": "x", "app": "outlook", "variants": [["s"]],
           "subgoals": {"s": {"goal": "send", "done": {"mail": {"mailbox": "{to}"}}}}}  # fmt: skip
    with pytest.raises(ValueError, match="needs a sending capability"):
        Template.model_validate(raw)
    with pytest.raises(ValueError, match="without a subgoal"):
        Template.model_validate(raw | {"capability": "mail.send", "subgoals": {"s": {"goal": "type"}}})
    assert Template.model_validate(raw | {"capability": "mail.send"}).subgoals["s"].require == require
    with pytest.raises(ValueError, match="no mail form"):
        Template.model_validate(raw | {"app": "notepad", "processes": ["notepad.exe"], "capability": "mail.send"})
    with pytest.raises(ValueError, match="selectors"):
        Form.model_validate({"to": {"name": "^To$"}, "empty": [{"name": "^Cc$", "value": ".*"}]})
    with pytest.raises(ValueError, match="commits"):
        Dialogs.model_validate({"discard": ["don'?t save", "send( anyway)?"], "choice_ok": "OK", "leave": []})
    with pytest.raises(ValueError, match="commits"):
        Dialogs.model_validate({"discard": ["don'?t save"], "choice_ok": "Send", "leave": []})
    base = tasks.profile("outlook").model_dump()
    with pytest.raises(ValueError, match="close key"):
        tasks.Profile.model_validate(base | {"keys": base["keys"] | {"close": "Alt+S"}})


def test_mail_from_an_earlier_playback_never_counts_after_a_clock_rewind(monkeypatch: pytest.MonkeyPatch) -> None:
    from ludius.tasks import mail_baseline

    old_in = mail.Message("aberg@ludius.internal", 5_000, 5_000, "Yes, booked.", ("mvellinga@ludius.internal",), 7)
    old_sent = mail.Message("aberg@ludius.internal", 5_000, 0, "Yes, booked.", ("mvellinga@ludius.internal",), 3)
    store = {("mvellinga", "inbox"): [old_in], ("aberg", "sent"): [old_sent]}

    def query(user: str, folder: str, _subject: str) -> list[mail.Message] | None:
        return list(store.get((user, folder), []))

    def messages(user: str, folder: str, subject: str, _attachments: bool = False) -> list[mail.Message]:
        return query(user, folder, subject) or []

    def unreadable(*_: str) -> None:
        return None

    monkeypatch.setattr(mail, "_query", query)
    monkeypatch.setattr(mail, "messages", messages)
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item(params={"body": "yes, booked"})), random.Random(0))
    task.owner, task.started_ms = "aberg", 1_000
    task.mail_before = mail_baseline(task)
    assert task.mail_before == {("mvellinga", "inbox", 7), ("aberg", "sent", 3)}
    done = t.subgoals["send_shortcut"].done
    assert not check_done(done, obs.Snapshot(), task, typed_ok=False)
    store[("mvellinga", "inbox")].append(dataclasses.replace(old_in, id=8))
    assert not check_done(done, obs.Snapshot(), task, typed_ok=False)
    store[("aberg", "sent")].append(dataclasses.replace(old_sent, id=4))
    assert check_done(done, obs.Snapshot(), task, typed_ok=False)
    task.mail_before = None
    assert not check_done(done, obs.Snapshot(), task, typed_ok=False)
    monkeypatch.setattr(mail, "_query", unreadable)
    assert mail_baseline(task) is None


def test_send_is_refused_when_the_recipient_became_reserved(tmp_path: Path) -> None:
    from ludius.tasks import RESERVED_RECIPIENT, recipient_reserved

    scenario = tmp_path / "scenario.yaml"
    scenario.write_text("mailboxes: [mvellinga@ludius.internal]\n", encoding="utf-8")
    reserved = Policy.load(scenario)
    t = cli.load_templates()["outlook_reply_mail"]
    task = Task(t, PlanItem.model_validate(item("outlook_reply_mail", followup="f1")), random.Random(0))
    send, write = t.subgoals["send_shortcut"], t.subgoals["write_reply"]
    assert recipient_reserved(send, task, reserved)
    assert not recipient_reserved(write, task, reserved)
    assert not recipient_reserved(send, task, Policy())
    assert RESERVED_RECIPIENT.startswith("precondition")


def _git_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    return next((str(b) for d in Path(git).parents if (b := d / "bin" / "bash.exe").exists()), None) if git else None


GIT_BASH = _git_bash()


@pytest.mark.skipif(GIT_BASH is None, reason="needs (Git) bash to run the remote pipeline")
def test_failing_database_is_unreadable_not_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "sudo"
    fake.write_text('#!/bin/sh\necho "Error: database is locked" >&2\nexit 5\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    bash, real_run = GIT_BASH, subprocess.run
    assert bash

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        env = {**os.environ, "PATH": f"{tmp_path.as_posix()}:/usr/bin:/bin"}
        return real_run([bash, "-c", cmd[-1]], capture_output=True, timeout=30, env=env)

    monkeypatch.setattr(mail, "_host", lambda: "10.0.0.1")
    monkeypatch.setattr(mail.subprocess, "run", run)
    assert mail._query("aberg", "sent", "x") is None  # pyright: ignore[reportPrivateUsage]
    fake.write_text('#!/bin/sh\nif [ "$1" = sqlite3 ]; then echo "1 0 0 0 - - 0 0 0 - - 2D 2D 2D 2D c1"; exit 0; fi\n'
                    'exit 1\n', encoding="utf-8", newline="\n")  # fmt: skip
    assert mail._query("aberg", "inbox", "x") is None  # pyright: ignore[reportPrivateUsage]
