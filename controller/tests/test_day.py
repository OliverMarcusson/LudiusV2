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
from ludius.decisions import Answers, DecisionError
from ludius.loop import GroundTruth, Outcome
from ludius.planner import DayPlan, PlanItem, day_records
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


def _person(user: str, name: str) -> dict[str, str]:
    return {"user": user, "name": name, "role": "Clerk", "department": "Ops"}


WORLD: dict[str, Any] = {"company": "Acme", "city": "Delft", "summary": "s", "projects": [], "events": []}


def test_time_jump_day_keeps_order_breaks_and_interrupts(tmp_path: Path) -> None:
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "time-jump", 0,  # type: ignore[arg-type]
                    sleep=lambda s: calls.append(f"sleep {s:g}"),
                    set_clock=lambda t: calls.append(f"clock {t:%H:%M}" if t else "clock reset"))  # fmt: skip
    results = day.run(PLAN)
    assert [(i.at, [x.at for x in m]) for i, m in slots(PLAN)] == [
        ("08:55", []), ("09:00", ["09:10"]), ("10:00", []), ("12:00", ["12:05"])]  # fmt: skip
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
    calls.clear()
    day.slot(dt.date(2026, 11, 2), note("12:05"), [], jump_at="12:30")
    assert calls[:2] == ["clock 12:30", "normalize"]
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
    assert calls == ["clock 09:00", "normalize", "run note 0900", "clock 12:00", "normalize", "lock", "sleep 5",
                     "clock 12:30", "unlock", "clock reset"]  # fmt: skip
    assert [(r.item.at, r.status) for r in day.results] == [("09:00", "done"), ("12:00", "failed")]
    events = (tmp_path / "day.jsonl").read_text(encoding="utf-8")
    assert '"status": "failed"' in events and '"day_end"' not in events


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
    assert runs[0] in ("08:55", "08:56", "08:57") and runs[1] == "run note 0855"
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
    assert not [c for c in calls if c.startswith("run")]
    assert (tmp_path / "day.jsonl").read_text(encoding="utf-8").count("escalation denied") == 2


class ReactionLlm:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items, self.prompts, self.systems, self.cost = items, list[str](), list[str](), 0.0

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9, attempts: int = 2) -> Any:
        assert attempts == 1
        self.prompts.append(user)
        self.systems.append(system)
        return cls.model_validate({"items": self.items})


@pytest.mark.parametrize("fail", ["", "erp roles", "read", "read ui"])
def test_chat_notification_reads_then_reacts_outside_the_plan(tmp_path: Path, fail: str) -> None:
    narrative = tmp_path / "narrative.yaml"
    narrative.write_text(yaml.safe_dump({**WORLD, "people": [_person("aberg", "Anna Berg"),
                                                    _person("lnord", "Lars Nord")]}))  # fmt: skip
    post = Post("p1", "lnord", "could you put the erp roles in a note and ping me", 100)
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
    runner.fail = {"read": set[str](), "read ui": {"chat_read_messages"}}.get(fail, {fail})
    reactor = Reactor(Persona(name="Anna Berg", user="aberg"), load_templates(), Policy(), llm, narrative, log,  # type: ignore[arg-type]
                      poll=lambda user: polls.pop(0) if polls else [],
                      talk=lambda user, other: (talk, 200) if other == "lnord" else None)  # fmt: skip
    day = DayRunner(runner, FakeSession(calls), load_templates(), log, "compressed", 0, reactor=reactor)  # type: ignore[arg-type]
    results = day.slot(dt.date(2026, 11, 2), note("10:00"), [])
    R = "run chat_read_messages"
    expected = {
        "": [R, "run erp roles", "run chat_direct_message", R, "run note 1000"],
        "read ui": [R, "run erp roles", "run chat_direct_message", R, "run note 1000"],
        "erp roles": [R, "run erp roles", R, "run note 1000"],
        "read": [R, R, "run note 1000"],
    }[fail]
    assert [c for c in calls if c.startswith("run")] == expected
    assert len(llm.prompts) == {"": 1, "read ui": 1, "erp roles": 2, "read": 0}[fail]
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
    llm.items = [{"template": "chat_direct_message", "params": {"message": "hi"},
                  "content": {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga"}}]  # fmt: skip
    people = {"aberg": "Anna Berg", "lnord": "Lars Nord", "mvellinga": "Marcus Vellinga"}
    assert reactor.plan("lnord", people, [post], "10:05", "2026-11-02") == []
    plan = [json.loads(x) for x in (tmp_path / "day.jsonl").read_text().splitlines()][-1]
    assert plan["event"] == "reaction_plan"
    assert plan["rejects"] == ["chat_direct_message: to must be lnord@ludius.internal"]
    log.close()


def test_messages_sent_while_reading_or_answering_are_answered_too(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.yaml"
    narrative.write_text(yaml.safe_dump({**WORLD, "people": [_person("dkoster", "Daan Koster"),
                                                    _person("aberg", "Anna Berg")]}))  # fmt: skip
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

    def fake_ssh(remote: str, stdin: str):
        return subprocess.CompletedProcess([], 0, out, b"")

    monkeypatch.setattr(react, "_ssh", fake_ssh)
    assert react.unread("aberg") == [Post("p1", "lnord", "hi", 1)]

    def failed_ssh(remote: str, stdin: str):
        return None

    monkeypatch.setattr(react, "_ssh", failed_ssh)
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
    it = PlanItem(at="09:20", template="notepad_write_note", params={"body": "a\r\nb\rc", "file_name": "a\rb"},
                  content={"body": "x\r\ny"})  # fmt: skip
    assert it.params == {"body": "a\nb\nc", "file_name": "a\rb"} and it.content == {"body": "x\ny"}


def test_team_channel_posts_are_read_and_answered_in_the_channel(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.yaml"
    people = [_person("aberg", "Anna Berg"), _person("lnord", "Lars Nord"), _person("nokafor", "Nia Okafor")]
    narrative.write_text(yaml.safe_dump({**WORLD, "people": people,
                                         "channels": {"Ops Team": ["aberg", "lnord", "nokafor"],
                                                      "Board": ["lnord", "nokafor"]}}))  # fmt: skip
    post = Post("p1", "lnord", "anna, could you send the dock list here", 100, "Ops Team")
    other = Post("p2", "nokafor", "board only", 120, "Board")
    polls = [[post, other], [other]]
    llm = ReactionLlm([{"template": "chat_post_message", "params": {"channel": "Ops Team", "message": "on it"}}])
    calls: list[str] = []
    log = GroundTruth(tmp_path / "day.jsonl")
    seen: list[str] = []
    reactor = Reactor(Persona(name="Anna Berg", user="aberg"), load_templates(), Policy(), llm, narrative, log,  # type: ignore[arg-type]
                      poll=lambda user: polls.pop(0) if polls else [other],
                      talk=lambda user, key: (seen.append(key), ([post], 200))[1])  # fmt: skip
    day = DayRunner(FakeRunner(log, calls), FakeSession(calls), load_templates(), log, "compressed", 0, reactor=reactor)  # type: ignore[arg-type]
    day.slot(dt.date(2026, 11, 2), note("10:00"), [])
    R = "run chat_read_channel"
    assert [c for c in calls if c.startswith("run")] == [R, "run chat_post_message", R, "run note 1000"]
    assert set(seen) == {"~Ops Team"} and len(llm.prompts) == 1
    assert "Your team channel Ops Team (the others in it: Lars Nord (lnord@" in llm.prompts[0]
    assert "board only" not in llm.prompts[0]
    world = {"aberg": "Anna Berg", "lnord": "Lars Nord", "nokafor": "Nia Okafor", "mvellinga": "Marcus Vellinga"}
    for bad, why in [({"channel": "Town Square", "message": "hi"}, "channel must be Ops Team"),
                     ({"channel": "Board", "message": "hi"}, "channel must be Ops Team")]:  # fmt: skip
        llm.items = [{"template": "chat_post_message", "params": bad}]
        assert reactor.plan("~Ops Team", world, [post], "10:05", "2026-11-02", members=["lnord", "nokafor"]) == []
        assert any(why in r for r in json.loads((tmp_path / "day.jsonl").read_text().splitlines()[-1])["rejects"])
    llm.items = [{"template": "chat_direct_message", "params": {"to": "mvellinga@ludius.internal", "message": "hi"}}]
    assert reactor.plan("~Ops Team", world, [post], "10:05", "2026-11-02", members=["lnord", "nokafor"]) == []
    log.close()


def test_team_channel_cap_quoting_and_handles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ludius.react as react
    from ludius.planner import Narrative

    log = GroundTruth(tmp_path / "day.jsonl")
    for _ in range(2):
        log.write(event="chat_notification", channel="Ops Team", posts=["p1"])
    reactor = Reactor(Persona(name="Anna Berg", user="aberg"), load_templates(), Policy(), None, tmp_path, log)  # type: ignore[arg-type]
    assert reactor._counts()[0]["~Ops Team"] == 2  # pyright: ignore[reportPrivateUsage]
    sent: list[str] = []

    def ssh(remote: str, stdin: str, timeout: int = 60) -> None:
        sent.append(remote)

    monkeypatch.setattr(react, "_ssh", ssh)
    react._query("select 1", c="Ops Team")  # pyright: ignore[reportPrivateUsage]
    assert sent[0].endswith("-v 'c=Ops Team'")
    world = {**WORLD, "people": [_person("aberg", "Anna Berg"), _person("lnord", "Lars Nord")]}
    for bad in ({"Ops Team": ["aberg"], "ops-team": ["lnord"]}, {"Town Square": ["aberg"]}):
        with pytest.raises(ValueError, match="handle"):
            Narrative.model_validate({**world, "channels": bad})
    log.close()
    from ludius.planner import PlanItem, item_problem

    item = PlanItem(at="10:00", template="chat_read_channel", params={"channel": "Ops Team"})
    assert "reactions only" in str(item_problem(item, load_templates(), Policy(), "aberg", ["Ops Team"]))


class GossipDecider:
    def __init__(self, yes: float | None) -> None:
        self.yes, self.asked = yes, list[tuple[str, list[str]]]()

    def ask(self, state: dict[str, str], questions: dict[str, Any]) -> Answers:
        self.asked.append((state["conversation"], list(questions)))
        if self.yes is None:
            raise DecisionError("down")
        work = {"type": "choice", "choice": "chat", "probabilities": {"chat": 1.0}}
        noul = {"type": "noul", "noul": 0.1}
        return Answers({k: work if k == "work" else {**noul, "noul": self.yes} if k == "gossip" else noul
                        for k in questions})  # fmt: skip


@pytest.mark.parametrize("yes", [0.95, 0.3, None])
def test_gossip_gate_lets_one_dm_pass_news_on_to_a_third_colleague(tmp_path: Path, yes: float | None) -> None:
    narrative = tmp_path / "narrative.yaml"
    people = {"dkoster": "Daan Koster", "aberg": "Anna Berg", "lnord": "Lars Nord"}
    news = Post("p1", "aberg", "the coffee machine on 3 finally works", 100)
    dm = {"template": "chat_direct_message"}
    llm = ReactionLlm([{**dm, "params": {"to": "aberg@ludius.internal", "message": "nice"}},
                       {**dm, "params": {"to": "lnord@ludius.internal", "message": "coffee works on 3"}}])  # fmt: skip
    decider = GossipDecider(yes)
    log = GroundTruth(tmp_path / "day.jsonl")
    reactor = Reactor(
        Persona(name="Daan Koster", user="dkoster"),
        load_templates(),
        Policy(),
        llm,
        narrative,
        log,  # type: ignore[arg-type]
        decider=decider,
    )  # type: ignore[arg-type]
    items = reactor.plan("aberg", people, [news], "10:00", "2026-11-02")
    rules = ["request", "question", "offer", "small_talk", "work"]
    assert decider.asked == [("(new) Anna Berg: the coffee machine on 3 finally works", [*rules, "gossip"])]
    assert (react.RULES["request"] in llm.systems[0]) == (yes is None)
    assert ("- notepad_write_note:" in llm.prompts[0]) == (yes is None)
    assert len(items) == (2 if yes == 0.95 else 0)
    assert ("Lars Nord (lnord@ludius.internal)" in llm.prompts[0]) == (yes == 0.95)
    llm.items = [*llm.items, {**dm, "params": {"to": "lnord@ludius.internal", "message": "and again"}}]
    assert reactor.plan("aberg", people, [news], "10:01", "2026-11-02") == []
    assert [q[-1] for _, q in decider.asked] == ["gossip", "work" if yes == 0.95 else "gossip"]
    log.close()


def test_received_gossip_skips_the_gate_so_news_never_chains(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.yaml"
    (tmp_path / "gossip.jsonl").write_text(
        json.dumps({"from": "aberg", "to": "dkoster", "message": "the coffee machine  on 3 finally works"}) + "\n",
        encoding="utf-8")  # fmt: skip
    people = {"dkoster": "Daan Koster", "aberg": "Anna Berg", "lnord": "Lars Nord"}
    news = Post("p1", "aberg", "the coffee machine on 3 finally works", 100)
    llm = ReactionLlm([{"template": "chat_direct_message",
                        "params": {"to": "lnord@ludius.internal", "message": "coffee works on 3"}}])  # fmt: skip
    decider = GossipDecider(0.99)
    log = GroundTruth(tmp_path / "day.jsonl")
    reactor = Reactor(
        Persona(name="Daan Koster", user="dkoster"),
        load_templates(),
        Policy(),
        llm,
        narrative,
        log,  # type: ignore[arg-type]
        decider=decider,
    )  # type: ignore[arg-type]
    assert reactor.plan("aberg", people, [news], "10:00", "2026-11-02") == []
    assert decider.asked[0][1][-1] == "work" and "lnord@ludius.internal" not in llm.prompts[0]
    log.close()
    assert any(r.get("skipped") == "received gossip" for r in day_records(tmp_path / "day.jsonl"))
    (tmp_path / "gossip.jsonl").write_text(json.dumps({"from": None, "to": "dkoster", "message": "x"}) + "\n")
    assert reactor._heard_gossip([news])  # pyright: ignore[reportPrivateUsage]
