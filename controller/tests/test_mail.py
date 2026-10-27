"""Outlook mail templates: loading, planner validation of recipients, and the server-side done check (offline)."""

from __future__ import annotations

import random
import subprocess
from typing import Any

import pytest

from ludius import cli, mail
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Task, check_done, preconditions
from ludius.v1 import observation_pb2 as obs

COLLEAGUES = {"mvellinga", "lnord"}
THREADS = {"f1": ("mvellinga", "lund accruals q4 status")}  # f2: an open follow-up that is not a mail


def item(template: str = "outlook_send_mail", **extra: Any) -> dict[str, Any]:
    params = {"to": "mvellinga@corp.ludius.test", "to_name": "Marcus Vellinga", "subject": "lund accruals q4 status",
              "body": "hi marcus\nlund accruals for q4 are booked"}  # fmt: skip
    return {"at": "09:00", "template": template, "params": params | extra.pop("params", {}),
            "context": "Marcus asked for the status.", **extra}  # fmt: skip


def check(*items: dict[str, Any]) -> list[str]:
    templates = cli.plan_sources().templates
    its = [PlanItem.model_validate(i) for i in items]
    return [why for _, why in validate(its, templates, [], Policy(), COLLEAGUES, followups={"f1", "f2"},
                                       mail_threads=THREADS)]  # fmt: skip


def test_templates_load_with_server_side_send_checks() -> None:
    templates = cli.load_templates()
    for name in ("outlook_send_mail", "outlook_reply_mail"):
        t = templates[name]
        sends = [sg for sg in t.subgoals.values() if sg.done and sg.done.mail]
        assert sends and all(sg.done and sg.done.mail and sg.done.mail.mailbox == "{to}" for sg in sends)
        assert all(sg.require and sg.forbid for sg in sends)  # every send is guarded
        assert all(any(t.subgoals[n].done and t.subgoals[n].done.mail for n in v) for v in t.variants)  # pyright: ignore[reportOptionalMemberAccess]


def test_mail_to_a_colleague_is_valid() -> None:
    assert check(item(handoff="mvellinga")) == []
    assert check(item()) == []


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"params": {"to": "someone@example.com"}}, "not a colleague's address"),
        ({"params": {"to": "jdoe@corp.ludius.test"}}, "not a colleague's address"),
        ({"handoff": "lnord"}, "must be the mail's recipient"),
    ],
)
def test_mail_recipient_is_checked(extra: dict[str, Any], why: str) -> None:
    (got,) = check(item(**extra))
    assert why in got


def test_reply_must_answer_the_matching_mail_followup() -> None:
    assert check(item("outlook_reply_mail", followup="f1")) == []
    assert "set followup" in check(item("outlook_reply_mail"))[0]
    assert "set followup" in check(item("outlook_reply_mail", followup="f2"))[0]  # a folder handoff, not a mail
    wrong_subject = item("outlook_reply_mail", followup="f1", params={"subject": "other thing"})
    wrong_sender = item("outlook_reply_mail", followup="f1", params={"to": "lnord@corp.ludius.test"})
    for it in (wrong_subject, wrong_sender):
        assert "reply to that, with that subject" in check(it)[0]


def test_done_is_read_on_the_server_whatever_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item()), random.Random(0))
    task.owner, task.started_ms = "aberg", 1_000
    calls: list[tuple[object, ...]] = []

    def delivered(*a: object) -> bool:
        calls.append(a)
        return len(calls) == 1

    monkeypatch.setattr(mail, "delivered", delivered)
    done = t.subgoals["send_shortcut"].done
    assert check_done(done, obs.Snapshot(), task, typed_ok=False) is True
    assert calls == [("mvellinga@corp.ludius.test", "inbox", "lund accruals q4 status",
                      "hi marcus\nlund accruals for q4 are booked", "aberg@corp.ludius.test", 1_000)]  # fmt: skip
    assert check_done(done, obs.Snapshot(), task, typed_ok=True) is False


def test_body_match_ignores_outlook_autocorrect() -> None:
    sent = "Hi marcus\r\n\r\n•\tLund accruals for Q4 are booked – anna's\r\n"
    assert mail.letters("hi marcus\n- lund accruals for q4 are booked - anna's") == mail.letters(sent)
    assert mail.letters("booked") in mail.letters(sent)
    assert mail.letters("not booked") not in mail.letters(sent)


def test_foreign_or_malformed_mailbox_never_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "_host", lambda: pytest.fail("no query expected"))
    assert not mail.delivered("aberg@example.com", "inbox", "s", "b", "aberg@corp.ludius.test", 1)
    assert mail.messages("a;rm -rf", "inbox", "s") == []


SEED = mail.Message("mvellinga@corp.ludius.test", 1_000, "Hi Anna,\r\nyes\r\n")
REPLY = mail.Message("aberg@corp.ludius.test", 5_000, "Yes, booked.\r\n-----\r\nHi Anna, yes")


@pytest.mark.parametrize(
    ("body", "sender", "since", "ok"),
    [
        ("yes, booked", "aberg@corp.ludius.test", 4_000, True),
        ("yes", "aberg@corp.ludius.test", 6_000, False),  # only the seed and an older reply: before the task
        ("hi anna", "aberg@corp.ludius.test", 4_000, True),
        ("yes", "mvellinga@corp.ludius.test", 4_000, False),  # the seed's sender is not the persona
        ("yes", "aberg@corp.ludius.test", 0, False),  # no task start: never
        ("- ...", "aberg@corp.ludius.test", 4_000, False),  # punctuation-only body normalises to nothing
        ("yes", "", 4_000, False),  # no persona
    ],
)
def test_delivered_needs_sender_time_and_a_real_body(
    monkeypatch: pytest.MonkeyPatch, body: str, sender: str, since: int, ok: bool
) -> None:
    def messages(*_: str) -> list[mail.Message]:
        return [SEED, REPLY]

    monkeypatch.setattr(mail, "messages", messages)
    assert mail.delivered("mvellinga@corp.ludius.test", "inbox", "s", body, sender, since) is ok


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
        return obs.Snapshot(elements=[obs.Element(role="edit", name=k, value=v) for k, v in fields.items()])

    assert preconditions(sg, snap(To="mvellinga@corp.ludius.test", Cc=""), task) is None
    assert preconditions(sg, snap(To="mvellinga@corp.ludius.test;"), task) is None
    assert preconditions(sg, snap(To="Marcus Vellinga", Cc=""), task) is None  # Outlook resolved the address
    for bad in (snap(To="mvellinga@corp.ludius.test; outside@example.com"),
                snap(To="Marcus Vellinga; outside@example.com"), snap(To="Lars Nord"),
                snap(To="mvellinga@corp.ludius.test", Cc="outside@example.com"),
                snap(To="mvellinga@corp.ludius.test", Bcc="lnord@corp.ludius.test"),
                snap(To="lnord@corp.ludius.test"), snap()):  # fmt: skip
        assert preconditions(sg, bad, task) is not None


def test_typing_into_a_body_without_a_readable_value_counts_as_landed() -> None:
    from ludius.loop import _typed_landed  # pyright: ignore[reportPrivateUsage]
    from ludius.options import Option

    body = obs.Element(key="b", role="document", name="Untitled Message")  # Word editor: no Value pattern
    edit = obs.Element(key="e", role="edit", name="Subject", actions=["value"])
    after = obs.Snapshot(elements=[obs.Element(key="e", role="edit", name="Subject", actions=["value"])])
    task = Task(cli.load_templates()["outlook_send_mail"], PlanItem.model_validate(item()), random.Random(0))
    assert _typed_landed(after, Option("o1", "type", "t", element=body), task)
    assert not _typed_landed(after, Option("o2", "type", "t", element=edit), task)  # value unchanged: not landed


def test_recipient_and_answered_subject_may_repeat() -> None:
    first, again = item(), item(at="11:00", params={"subject": "lund accruals q4 reversal", "body": "reversal done"})
    reply = item("outlook_reply_mail", at="13:00", followup="f1", params={"body": "thanks, noted"})
    assert check(first, again, reply) == []  # same colleague, and the reply reuses the answered subject
