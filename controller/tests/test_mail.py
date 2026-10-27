"""Outlook mail templates: loading, planner validation of recipients, and the server-side done check (offline)."""

from __future__ import annotations

import random
from typing import Any

import pytest

from ludius import cli, mail
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Task, check_done
from ludius.v1 import observation_pb2 as obs

COLLEAGUES = {"mvellinga", "lnord"}


def item(template: str = "outlook_send_mail", **extra: Any) -> dict[str, Any]:
    params = {"to": "mvellinga@corp.ludius.test", "subject": "lund accruals q4 status",
              "body": "hi marcus\nlund accruals for q4 are booked"}  # fmt: skip
    return {"at": "09:00", "template": template, "params": params | extra.pop("params", {}),
            "context": "Marcus asked for the status.", **extra}  # fmt: skip


def check(*items: dict[str, Any]) -> list[str]:
    templates = cli.plan_sources().templates
    its = [PlanItem.model_validate(i) for i in items]
    return [why for _, why in validate(its, templates, [], Policy(), COLLEAGUES, followups={"f1"})]


def test_templates_load_with_server_side_send_checks() -> None:
    templates = cli.load_templates()
    for name in ("outlook_send_mail", "outlook_reply_mail"):
        t = templates[name]
        sends = [sg for sg in t.subgoals.values() if sg.done and sg.done.mail]
        assert sends and all(sg.done and sg.done.mail and sg.done.mail.mailbox == "{to}" for sg in sends)
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


def test_reply_needs_an_open_followup() -> None:
    assert "set followup" in check(item("outlook_reply_mail"))[0]
    assert check(item("outlook_reply_mail", followup="f1")) == []


def test_done_is_read_on_the_server_whatever_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    t = cli.load_templates()["outlook_send_mail"]
    task = Task(t, PlanItem.model_validate(item()), random.Random(0))
    calls: list[tuple[str, ...]] = []

    def delivered(*a: str) -> bool:
        calls.append(a)
        return len(calls) == 1

    monkeypatch.setattr(mail, "delivered", delivered)
    done = t.subgoals["send_shortcut"].done
    assert check_done(done, obs.Snapshot(), task, typed_ok=False) is True
    assert calls == [("mvellinga@corp.ludius.test", "inbox", "lund accruals q4 status",
                      "hi marcus\nlund accruals for q4 are booked")]  # fmt: skip
    assert check_done(done, obs.Snapshot(), task, typed_ok=True) is False


def test_body_match_ignores_outlook_autocorrect() -> None:
    sent = "Hi marcus\r\n\r\n•\tLund accruals for Q4 are booked – anna's\r\n"
    assert mail.letters("hi marcus\n- lund accruals for q4 are booked - anna's") == mail.letters(sent)
    assert mail.letters("booked") in mail.letters(sent)
    assert mail.letters("not booked") not in mail.letters(sent)


def test_foreign_or_malformed_mailbox_never_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "_host", lambda: pytest.fail("no query expected"))
    assert not mail.delivered("aberg@example.com", "inbox", "s", "b")
    assert mail.bodies("a;rm -rf", "inbox", "s") == []
