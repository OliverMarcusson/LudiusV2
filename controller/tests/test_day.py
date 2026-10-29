from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ludius import react
from ludius.cli import load_templates
from ludius.day import BreakUnlockFailed, DayRunner, slots
from ludius.loop import GroundTruth, Outcome
from ludius.planner import DayPlan, PlanItem
from ludius.policy import Policy
from ludius.react import Post, Reactor
from ludius.tasks import Persona, Task
from ludius.v1 import observation_pb2 as obs


def note(at: str, interrupt: bool = False) -> PlanItem:
    return PlanItem(at=at, template="notepad_write_note", interrupt=interrupt,
                    params={"file_name": f"note {at.replace(':', '')}", "body": "text"})  # fmt: skip


PLAN = DayPlan(persona="aberg", date="2026-11-02", items=[
    note("10:00"), note("08:55", interrupt=True), note("09:00"), note("09:10", interrupt=True),
    PlanItem(at="12:00", template="break", minutes=30), note("12:05", interrupt=True),
])  # fmt: skip


class Agent:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def chord(self, keys: Any) -> None:
        self.calls.append("lock")


class FakeRunner:
    def __init__(self, log: GroundTruth, calls: list[str]) -> None:
        self.log, self.calls, self.agent = log, calls, Agent(calls)
        self.fail: set[str] = set()

    def observe(self) -> obs.Snapshot:
        return obs.Snapshot()

    def run(self, task: Task, between: Any = None) -> Outcome:
        name = task.inst.params.get("file_name", task.template.task)
        self.calls.append(f"run {name}")
        if between:
            between()
        return Outcome("failed" if name in self.fail else "done", 1)


class FakeSession:
    def __init__(self, calls: list[str], unlock_ok: bool = True) -> None:
        self.calls, self.unlock_ok = calls, unlock_ok

    def normalize(self, keep: set[str] | None = None) -> bool:
        self.calls.append(f"normalize keep {sorted(keep)}" if keep else "normalize")
        return True

    def escalate(self) -> int:
        return 0

    def unlock(self) -> bool:
        self.calls.append("unlock")
        return self.unlock_ok


def test_time_jump_day_keeps_order_breaks_and_interrupts(tmp_path: Path) -> None:
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "time-jump", 0,  # type: ignore[arg-type]
                    sleep=lambda s: calls.append(f"sleep {s:g}"),
                    set_clock=lambda t: calls.append(f"clock {t:%H:%M}" if t else "clock reset"))  # fmt: skip
    results = day.run(PLAN)
    assert [(i.at, [x.at for x in m]) for i, m in slots(PLAN)] == [
        ("08:55", []), ("09:00", ["09:10"]), ("10:00", []), ("12:00", ["12:05"])]  # fmt: skip
    # The 08:55 interrupt has no task to cut into: it runs on its own, first, at its logical time.
    assert calls == [
        "clock 08:55", "normalize", "run note 0855", "clock 09:00", "normalize", "run note 0900", "run note 0910",
        "clock 10:00", "normalize", "run note 1000",
        "clock 12:00", "normalize", "lock", "sleep 5", "clock 12:30", "unlock", "normalize", "run note 1205",
        "clock reset",
    ]  # fmt: skip
    assert [(r.item.at, r.status) for r in results] == [
        ("08:55", "done"), ("09:10", "done"), ("09:00", "done"), ("10:00", "done"), ("12:00", "done"),
        ("12:05", "done")]  # fmt: skip
    assert day.slot(dt.date(2026, 11, 2), note("13:00"), [])[0].item.at == "13:00"
    # A late interrupt resumed after a break: the clock is set to the break's end, not left to chance.
    calls.clear()
    day.slot(dt.date(2026, 11, 2), note("12:05"), [], jump_at="12:30")
    assert calls[:2] == ["clock 12:30", "normalize"]
    # An interrupt that writes the same file waits until after the task: its file would pass the task's check.
    calls.clear()
    same = note("09:00", interrupt=True).model_copy(update={"at": "09:10"})
    day.slot(dt.date(2026, 11, 2), note("09:00"), [same])
    assert [c for c in calls if c.startswith("run")] == ["run note 0900", "run note 0900"]
    assert [r.item.at for r in day.results[-2:]] == ["09:00", "09:10"]
    log.close()


def test_failed_break_unlock_stops_the_day_and_resets_the_clock(tmp_path: Path) -> None:
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls, unlock_ok=False), load_templates(), log, "time-jump", 0,  # type: ignore[arg-type]
                    sleep=lambda s: calls.append(f"sleep {s:g}"),
                    set_clock=lambda t: calls.append(f"clock {t:%H:%M}" if t else "clock reset"))  # fmt: skip
    plan = DayPlan(persona="aberg", date="2026-11-02", items=[
        note("09:00"), PlanItem(at="12:00", template="break", minutes=30), note("12:05", interrupt=True), note("13:00"),
    ])  # fmt: skip
    with pytest.raises(BreakUnlockFailed, match="12:00"):
        day.run(plan)
    log.close()
    # Nothing after the failed unlock: no late interrupt, no later slot, no hygiene; the clock goes back.
    assert calls == ["clock 09:00", "normalize", "run note 0900", "clock 12:00", "normalize", "lock", "sleep 5",
                     "clock 12:30", "unlock", "clock reset"]  # fmt: skip
    assert [(r.item.at, r.status) for r in day.results] == [("09:00", "done"), ("12:00", "failed")]
    events = (tmp_path / "day.jsonl").read_text(encoding="utf-8")
    assert '"status": "failed"' in events and '"day_end"' not in events  # logged as failed, never as a finished day


def test_break_end_fails_closed_when_the_clock_cannot_be_set(tmp_path: Path) -> None:
    calls: list[str] = []

    def set_clock(t: dt.datetime | None) -> None:
        raise RuntimeError("set-clock failed")

    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "time-jump", 0,  # type: ignore[arg-type]
                    sleep=lambda s: None, set_clock=set_clock)  # fmt: skip
    brk = PlanItem(at="12:00", template="break", minutes=30)
    try:
        day.end_break(dt.date(2026, 11, 2), brk)
        raise AssertionError("end_break acted on an unknown clock")
    except RuntimeError:
        pass
    assert "unlock" not in calls
    log.close()


def test_realtime_day_waits_for_each_slot_in_logical_order(tmp_path: Path) -> None:
    calls: list[str] = []
    t = [dt.datetime(2026, 11, 2, 8, 0)]

    def sleep(s: float) -> None:
        t[0] += dt.timedelta(seconds=s)

    class Timed(FakeRunner):
        def run(self, task: Task, between: Any = None) -> Outcome:
            self.calls.append(f"{t[0]:%H:%M}")
            return super().run(task, between)

    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(Timed(log, calls), FakeSession(calls), load_templates(), log, "realtime", 0,  # type: ignore[arg-type]
                    sleep=sleep, now=lambda: t[0])  # fmt: skip
    plan = DayPlan(persona="aberg", date="2026-11-02", items=[note("09:00"), note("08:55", interrupt=True)])
    day.run(plan)
    log.close()
    runs = [c for c in calls if c != "normalize"]
    assert runs[0] in ("08:55", "08:56", "08:57") and runs[1] == "run note 0855"  # its time, plus human slack
    assert runs[2] >= "09:00" and runs[3] == "run note 0900"


def test_denied_escalation_abandons_the_task_and_the_day_goes_on(tmp_path: Path) -> None:
    from ludius.session import EscalationDenied

    class Dirty(FakeSession):
        def normalize(self, keep: set[str] | None = None) -> bool:
            self.calls.append("normalize")
            return False

        def escalate(self) -> int:
            raise EscalationDenied("LUDIUS_NO_ESCALATE=1")

    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    plan = DayPlan(persona="aberg", date="2026-11-02", items=[note("09:00"), note("10:00")])
    day = DayRunner(FakeRunner(log, calls), Dirty(calls), load_templates(), log, "compressed", 0)  # type: ignore[arg-type]
    assert [r.status for r in day.run(plan)] == ["abandoned", "abandoned"]
    assert not [c for c in calls if c.startswith("run")]  # nothing acted on the unclean desktop
    assert (tmp_path / "day.jsonl").read_text(encoding="utf-8").count("escalation denied") == 2


class ReactionLlm:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items, self.prompts, self.cost = items, list[str](), 0.0

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9, attempts: int = 2) -> Any:
        assert attempts == 1  # one call per reaction
        self.prompts.append(user)
        return cls.model_validate({"items": self.items})


@pytest.mark.parametrize("fail", ["", "erp roles", "read", "read ui"])
def test_chat_notification_reads_then_reacts_outside_the_plan(tmp_path: Path, fail: str) -> None:
    """New DMs before a slot: read them (confirmed on the server), then the reaction from their real text, in order,
    stopping at the first failure; none of it is a plan result or an item_end. The server decides whether the read
    happened: a read task abandoned after the conversation opened still reacts."""
    narrative = tmp_path / "narrative.yaml"
    narrative.write_text(yaml.safe_dump({"people": [{"user": "aberg", "name": "Anna Berg"},
                                                    {"user": "lnord", "name": "Lars Nord"}]}))  # fmt: skip
    post = Post("p1", "lnord", "could you put the erp roles in a note and ping me", 100)
    # The conversation: aberg's earlier line is context; p2 came in while it was open (seen, never notified).
    talk = [Post("p0", "aberg", "morning lars", 50), post, Post("p2", "lnord", "the dock list too", 150)]
    polls = [[post], [] if fail != "read" else [post]]
    llm = ReactionLlm([
        {"template": "notepad_write_note", "params": {"file_name": "erp roles", "body": "finance needs ap and ar"}},
        {"template": "chat_direct_message",
         "params": {"to": "lnord@ludius.internal", "message": "done, see erp roles"}},
    ])  # fmt: skip
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    runner = FakeRunner(log, calls)
    runner.fail = {"read": set(), "read ui": {"chat_read_messages"}}.get(fail, {fail})
    reactor = Reactor(Persona(name="Anna Berg", user="aberg"), load_templates(), Policy(), llm, narrative, log,  # type: ignore[arg-type]
                      poll=lambda user: polls.pop(0) if polls else [],
                      talk=lambda user, other: (talk, 200) if other == "lnord" else None)  # fmt: skip
    day = DayRunner(runner, FakeSession(calls), load_templates(), log, "compressed", 0, reactor=reactor)  # type: ignore[arg-type]
    results = day.slot(dt.date(2026, 11, 2), note("10:00"), [])
    R = "run chat_read_messages"
    expected = {
        "": [R, "run erp roles", "run chat_direct_message", R, "run note 1000"],
        "read ui": [R, "run erp roles", "run chat_direct_message", R, "run note 1000"],
        # Planned once more, told it failed: the same note again is refused (its name is taken now).
        "erp roles": [R, "run erp roles", R, "run note 1000"],
        "read": [R, R, "run note 1000"],  # the chat is closed after any reaction
    }[fail]
    assert [c for c in calls if c.startswith("run")] == expected
    assert len(llm.prompts) == {"": 1, "read ui": 1, "erp roles": 2, "read": 0}[fail]
    # Edge stays open from the read to the close: hygiene before each reaction item keeps it.
    assert "normalize keep ['msedge.exe']" in calls and "normalize" not in calls[: calls.index("run note 1000") - 1]
    if fail == "erp roles":
        assert "FAILED: notepad_write_note" in llm.prompts[1] and "You already tried" not in llm.prompts[0]
    assert (fail != "read") == any("could you put the erp roles in a note" in p for p in llm.prompts)
    if fail != "read":
        assert "You: morning lars\n(new) Lars Nord: could you" in llm.prompts[0]
        assert "(new) Lars Nord: the dock list too" in llm.prompts[0]
    assert [r.item.at for r in results] == ["10:00"]
    events = [json.loads(x) for x in (tmp_path / "day.jsonl").read_text().splitlines()]
    assert [e["template"] for e in events if e["event"] == "item_end"] == ["notepad_write_note"]
    # A recipient in content (it wins over params) must be the sender too: a DM to another colleague is refused.
    llm.items = [{"template": "chat_direct_message", "params": {"message": "hi"},
                  "content": {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga"}}]  # fmt: skip
    people = {"aberg": "Anna Berg", "lnord": "Lars Nord", "mvellinga": "Marcus Vellinga"}
    assert reactor.plan("lnord", people, [post], "10:05", "2026-11-02") == []
    plan = [json.loads(x) for x in (tmp_path / "day.jsonl").read_text().splitlines()][-1]
    assert plan["event"] == "reaction_plan"
    assert plan["rejects"] == ["chat_direct_message: to must be lnord@ludius.internal"]
    log.close()


def test_messages_sent_while_reading_or_answering_are_answered_too(tmp_path: Path) -> None:
    """Live: aberg's request came in while dkoster read her small talk; only the small talk was answered, and that
    answer marked the request read. Now it is read and answered together; one seen while answering gets its own."""
    narrative = tmp_path / "narrative.yaml"
    narrative.write_text(yaml.safe_dump({"people": [{"user": "dkoster", "name": "Daan Koster"},
                                                    {"user": "aberg", "name": "Anna Berg"}]}))  # fmt: skip
    hi, ask = Post("p1", "aberg", "morning daan", 100), Post("p2", "aberg", "could you note the deliveries", 150)
    more = Post("p3", "aberg", "and the carriers", 300)
    polls = [[hi], [ask], []]
    talks = [([hi, ask], 200), ([hi, ask, Post("p9", "dkoster", "done", 250), more], 400)]
    llm = ReactionLlm([{"template": "chat_direct_message", "params": {"to": "aberg@ludius.internal", "message": "ok"}}])
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    reactor = Reactor(Persona(name="Daan Koster", user="dkoster"), load_templates(), Policy(), llm, narrative, log,  # type: ignore[arg-type]
                      poll=lambda user: polls.pop(0) if polls else [],
                      talk=lambda user, other: talks.pop(0) if talks else None)  # fmt: skip
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "compressed", 0, reactor=reactor)  # type: ignore[arg-type]
    day.slot(dt.date(2026, 11, 2), note("10:00"), [])
    assert [c for c in calls if c.startswith("run")] == ["run chat_read_messages", "run chat_read_messages",
                                                         "run chat_direct_message", "run chat_direct_message",
                                                         "run chat_read_messages", "run note 1000"]  # fmt: skip
    assert "(new) Anna Berg: morning daan\n(new) Anna Berg: could you" in llm.prompts[0]
    assert "(new) Anna Berg: and the carriers" in llm.prompts[1] and "(new) Anna Berg: could" not in llm.prompts[1]
    log.close()


def test_unread_reads_mattermost_and_unreadable_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    out = b'[{"id": "p1", "sender": "lnord", "createat": 1, "message": "hi"}]\n'
    monkeypatch.setattr(react, "_ssh", lambda remote, stdin: subprocess.CompletedProcess([], 0, out, b""))
    assert react.unread("aberg") == [Post("p1", "lnord", "hi", 1)]
    monkeypatch.setattr(react, "_ssh", lambda remote, stdin: None)
    assert react.unread("aberg") is None
    assert react.unread("x; rm") is None


def test_chat_reset_soft_deletes_the_pair_s_posts_through_mmctl(monkeypatch: pytest.MonkeyPatch) -> None:
    ids, remotes = ["a" * 26, "b" * 26], list[str]()

    def ssh(remote: str, stdin: str, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
        remotes.append(remote)
        return subprocess.CompletedProcess([], 0, json.dumps(ids).encode() if "psql" in remote else b"", b"")

    monkeypatch.setattr(react, "_ssh", ssh)
    assert react.reset("aberg", "dkoster") == 2
    assert remotes[1].endswith(f"mmctl --local post delete {ids[0]} {ids[1]}")
    ids.append("x; rm -rf /")
    assert react.reset("aberg", "dkoster") is None
    assert react.reset("aberg", "x; rm") is None


def test_plan_item_text_has_plain_newlines() -> None:
    """Live: a reaction's note body came as \r\n lines and was refused as untypable, dropping the reaction."""
    it = PlanItem(at="09:20", template="notepad_write_note", params={"body": "a\r\nb\rc", "file_name": "a\rb"},
                  content={"body": "x\r\ny"})  # fmt: skip
    assert it.params == {"body": "a\nb\nc", "file_name": "a\rb"} and it.content == {"body": "x\ny"}
