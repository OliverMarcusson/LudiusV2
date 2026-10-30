from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, mail, reply
from ludius.decisions import Decision
from ludius.llm import LlmError
from ludius.loop import GroundTruth, Runner
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Persona, Task, TaskInstance
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

NOW = 10 * reply.DAY_MS
TO = "mvellinga@ludius.internal"
PEOPLE = {"aberg": "Anna Berg", "mvellinga": "Marcus Vellinga", "lnord": "Lars Nord"}
ANNA = Persona(name="Anna Berg", user="aberg", role="accountant")


def msg(i: int, subject: str = "lund q4", sender: str = TO, age_days: float = 0, cls: str = "IPM.Note") -> mail.Message:
    return mail.Message(sender, 0, NOW - int(age_days * reply.DAY_MS), "Hi Anna, are the lund accruals booked?",
                        id=i, cls=cls, subject=subject, msgid=f"<{i}@ludius.internal>")  # fmt: skip


def server(monkeypatch: pytest.MonkeyPatch, inbox: list[mail.Message] | None, sent: list[mail.Message] = ()) -> None:  # type: ignore[assignment]
    def newest(user: str, folder: str) -> list[mail.Message] | None:
        return inbox if (user, folder) == ("aberg", "inbox") else None

    monkeypatch.setattr(mail, "newest", newest)

    def matching(user: str, folder: str, subject: str) -> list[mail.Message] | None:
        if inbox is None:
            return None
        return [m for m in (inbox if folder == "inbox" else list(sent)) if m.subject == subject]

    monkeypatch.setattr(mail, "matching", matching)


def test_pick_takes_the_newest_answerable_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    answered = mail.Message("aberg@ludius.internal", 0, NOW, "ok", subject="old thread",
                            in_reply_to="<3@ludius.internal>")  # fmt: skip
    inbox = [
        msg(1, "from lars", sender="lnord@ludius.internal"),
        msg(8, "meeting", cls="IPM.Schedule.Meeting.Request"),
        msg(2, "lund q4"),
        msg(3, "old thread"),
        msg(4, "dup"), msg(5, "dup"),
        msg(6, "ancient", age_days=reply.RECENT_DAYS + 1),
    ]  # fmt: skip
    server(monkeypatch, inbox, [answered])
    picked = reply.pick("aberg", TO, "", NOW, PEOPLE, Policy())
    assert isinstance(picked, mail.Message) and picked.id == 2
    for subject in ("old thread", "dup", "ancient", "missing"):
        why = reply.pick("aberg", TO, subject, NOW, PEOPLE, Policy())
        assert isinstance(why, str) and why.startswith("no mail to answer"), subject
    assert reply.pick("aberg", "lnord@ludius.internal", "from lars", NOW, PEOPLE, Policy()) == inbox[0]


def test_pick_refuses_strangers_reserved_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    server(monkeypatch, [msg(1)])
    for to in ("someone@example.com", "jdoe@ludius.internal", "aberg@ludius.internal"):
        assert "not a colleague" in str(reply.pick("aberg", to, "", NOW, PEOPLE, Policy()))
    p = Policy(reserved_paths=[r"mvellinga@ludius.internal"])
    assert "reserved mailbox" in str(reply.pick("aberg", TO, "", NOW, PEOPLE, p))
    assert "scenario-reserved" in str(reply.pick("aberg", TO, "", NOW, PEOPLE, Policy(reserved_subjects=["lund q4"])))
    assert "scenario-reserved" in str(reply.pick("aberg", TO, "", NOW, PEOPLE, Policy(reserved_paths=["accruals"])))
    server(monkeypatch, None)
    assert reply.pick("aberg", TO, "", NOW, PEOPLE, Policy()) == "inbox unreadable on the server"
    server(monkeypatch, [msg(1)])

    def unreadable(user: str, folder: str, subject: str) -> None:
        return None

    monkeypatch.setattr(mail, "matching", unreadable)
    assert "unreadable" in str(reply.pick("aberg", TO, "", NOW, PEOPLE, Policy()))


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("Hi Marcus, yes, they are booked. Anna", None),
        ("Hi Marcus,\nyes, booked since Monday.\n- Anna", None),
        ("Hi Marcus, yes – booked. Anna", "not plain ASCII"),
        ("Hi Marcus,\n\nyes, booked.\n- Anna", "not one paragraph"),
        ("a\nb\nc\nd\ne lines too many", "not one paragraph"),
        ("ok", "length"),
        ("x" * 301, "length"),
        ("Hi Marcus, see C:\\Users\\aberg\\Documents\\payroll q4.xlsx", "scenario-reserved"),
        ("Hi Marcus, Lars Nord has the numbers. Anna", "scenario-reserved"),
        ("Hi Marcus, Lars\nNord has the numbers. Anna", "scenario-reserved"),
    ],
)
def test_reply_text_rules(text: str, why: str | None) -> None:
    p = Policy(reserved_paths=[r"payroll q4", r"lnord@ludius.internal"])
    got = reply.problem(text, PEOPLE, p)
    assert (got is None) if why is None else (got is not None and why in got)


class FakeLlm:
    def __init__(self, body: str | Exception) -> None:
        self.body, self.calls, self.cost = body, list[dict[str, Any]](), 0.001

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9, attempts: int = 2) -> Any:
        self.calls.append({"user": user, "attempts": attempts})
        if isinstance(self.body, Exception):
            raise self.body
        return cls(body=self.body)


class FakeDecisions:
    def __init__(self, yes: float | Exception) -> None:
        self.yes, self.calls = yes, list[dict[str, Any]]()

    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision:
        self.calls.append({"state": state, "options": options, "ask_progress": ask_progress})
        if isinstance(self.yes, Exception):
            raise self.yes
        y = float(self.yes)
        return Decision("yes" if y >= 0.5 else "no", 0.9, {"yes": y, "no": 1 - y}, 0, 0, "decision")


def reply_task() -> Task:
    t = cli.load_templates()["outlook_reply_mail"]
    task = Task(t, TaskInstance(template=t.task, params={"to": TO, "to_name": "Marcus Vellinga"}), random.Random(0))
    task.owner, task.started_ms = "aberg", NOW
    return task


@pytest.fixture
def narrative(tmp_path: Path) -> Path:
    path = tmp_path / "narrative.yaml"
    people = "\n".join(f"- {{name: {n}, role: r, department: d, user: {u}}}" for u, n in PEOPLE.items())
    path.write_text(f"people:\n{people}\n", encoding="utf-8")
    return path


def test_prepare_fills_the_reply_with_one_llm_call_and_a_decision_yes(
    monkeypatch: pytest.MonkeyPatch, narrative: Path
) -> None:
    server(monkeypatch, [msg(7)])
    llm, decider, task = FakeLlm("Hi Marcus,\nyes, all booked yesterday.\n- Anna"), FakeDecisions(0.9), reply_task()
    why, rec = reply.Composer(llm, narrative).prepare(task, ANNA, decider, Policy())  # type: ignore[arg-type]
    assert why is None and rec["mail_id"] == 7 and rec["decision_yes"] == 0.9
    assert [c["attempts"] for c in llm.calls] == [1]
    assert "are the lund accruals booked?" in llm.calls[0]["user"]
    assert decider.calls[0]["ask_progress"] is False and decider.calls[0]["state"]["reply"].startswith("Hi Marcus")
    assert {k: task.values[k] for k in ("to", "to_name", "subject", "reply_to")} == {
        "to": TO, "to_name": "Marcus Vellinga", "subject": "lund q4", "reply_to": "<7@ludius.internal>"}  # fmt: skip
    assert task.text(task.subgoal("write_reply")) == "Hi Marcus,\nyes, all booked yesterday.\n- Anna"


@pytest.mark.parametrize(
    ("llm_body", "decision_yes", "why"),
    [
        ("Hi Marcus, the weather is lovely today. Anna", 0.2, "Decisions says it does not answer"),
        ("Hi Marcus, yes, booked. Anna", 0.55, "Decisions says it does not answer"),
        ("Hi Marcus, yes, booked. Anna", RuntimeError("timeout"), "Decisions failed"),
        ("Hi Marcus,\n\nbooked.", 0.9, "not one paragraph"),
        (LlmError("OpenRouter 500"), 0.9, "reply text: OpenRouter 500"),
    ],
)
def test_prepare_fails_closed(
    monkeypatch: pytest.MonkeyPatch, narrative: Path, llm_body: Any, decision_yes: Any, why: str
) -> None:
    server(monkeypatch, [msg(7)])
    decider, task = FakeDecisions(decision_yes), reply_task()
    got, _ = reply.Composer(FakeLlm(llm_body), narrative).prepare(task, ANNA, decider, Policy())  # type: ignore[arg-type]
    assert got is not None and why in got
    assert "body" not in task.values and "reply_to" not in task.values
    if "paragraph" in why or "OpenRouter" in why:
        assert decider.calls == []


def test_runner_abandons_before_any_input(monkeypatch: pytest.MonkeyPatch, narrative: Path, tmp_path: Path) -> None:
    server(monkeypatch, [])

    class Agent:
        vm, inputs = "t", list[str]()

        def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
            return pb.ObserveResult(snapshot=obs.Snapshot(guest_time_ms=NOW))

        def press(self, *_: object) -> int:
            return 0

        def __getattr__(self, name: str) -> Any:
            self.inputs.append(name)
            return self.press

    def baseline(task: Task) -> None:
        pytest.fail("no baseline without a picked mail")

    monkeypatch.setattr("ludius.loop.mail_baseline", baseline)
    agent, log = Agent(), GroundTruth(tmp_path / "gt.jsonl")
    composer = reply.Composer(FakeLlm("unused"), narrative)  # type: ignore[arg-type]
    r = Runner(agent, FakeDecisions(0.9), ANNA, log, composer=composer)  # type: ignore[arg-type]
    out = r.run(reply_task())
    log.close()
    assert out.status == "abandoned" and out.steps == 0 and "start: no mail to answer" in out.reason
    assert agent.inputs == [] and composer.llm.calls == []  # type: ignore[attr-defined]
    assert '"event": "task_content"' in (tmp_path / "gt.jsonl").read_text(encoding="utf-8")
    without = Runner(Agent(), FakeDecisions(0.9), ANNA, GroundTruth(tmp_path / "gt2.jsonl"))  # type: ignore[arg-type]
    assert "no reply composer" in without.run(reply_task()).reason


def test_done_needs_the_reply_link(monkeypatch: pytest.MonkeyPatch) -> None:
    def copy(user: str, prefix: str = "RE: ", irt: str = "<7@ludius.internal>") -> mail.Message:
        return mail.Message("aberg@ludius.internal", NOW + 1000, NOW + 1000, "Yes, booked.\r\nFrom: Marcus\r\nSent: x",
                            (TO,), id=1 if user == "mvellinga" else 2, prefix=prefix, in_reply_to=irt)  # fmt: skip

    def args(**kw: Any) -> bool:
        def messages(user: str, folder: str, subject: str, *_: Any) -> list[mail.Message]:
            return [copy(user, **kw)]

        monkeypatch.setattr(mail, "messages", messages)
        return mail.delivered(TO, "inbox", "lund q4", "yes booked", "aberg@ludius.internal", NOW, True, frozenset(),
                              "<7@ludius.internal>")  # fmt: skip

    assert args()
    assert not args(prefix="")
    assert not args(irt="<8@ludius.internal>")
    assert not args(irt="")


def test_template_and_plan_time_inbox_hints() -> None:
    t = cli.load_templates()["outlook_reply_mail"]
    assert t.task_content == "reply" and "body" not in t.params
    hints = reply.hints([msg(1), msg(2, sender="lnord@ludius.internal"), msg(3, "secret", sender="x@example.com")],
                        "aberg", PEOPLE, Policy(reserved_paths=[r"lnord@ludius.internal"]))  # fmt: skip
    assert hints == [("mvellinga", "lund q4")]
    assert reply.hints(None, "aberg", PEOPLE, Policy()) == []

    def check(params: dict[str, str], followup: str = "") -> list[str]:
        it = PlanItem(at="09:00", template=t.task, params={"to": TO, "to_name": "Marcus Vellinga", **params},
                      followup=followup)  # fmt: skip
        return [w for _, w in validate([it], cli.plan_sources().templates, [], Policy(), {"mvellinga", "lnord"},
                                       followups={"f1"}, mail_threads={"f1": ("mvellinga", "lund q4")},
                                       people=PEOPLE, inbox=set(hints))]  # fmt: skip

    assert check({"subject": "lund q4"}) == [] and check({}) == [] and check({"subject": "lund q4"}, "f1") == []
    assert "listed inbox mail" in check({"subject": "not in the inbox"})[0]
