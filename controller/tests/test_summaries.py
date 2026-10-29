"""Monitor activity lines: a request only on a changed record, at most once per gap, never past the daily cap."""

import json
import time
from pathlib import Path
from typing import Any

import httpx

from ludius import summaries
from ludius.llm import LlmClient


def test_cadence_and_cap(tmp_path: Path) -> None:
    days = tmp_path / "days"
    days.mkdir()
    log = days / "2026-10-29-aberg.jsonl"
    now = int(time.time() * 1000)

    def event(**e: object) -> None:
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"host_ms": now, **e}) + "\n")

    sent: list[dict[str, object]] = []

    def reply(r: httpx.Request) -> httpx.Response:
        sent.append(json.loads(r.content))
        answer = {"message": {"content": "Aberg is opening Outlook."}}
        return httpx.Response(200, json={"model": "routed/model", "usage": {"cost": 0.001}, "choices": [answer]})

    llm = LlmClient("key", model="openrouter/auto-beta")
    llm._http = httpx.Client(transport=httpx.MockTransport(reply))  # pyright: ignore[reportPrivateUsage]
    s = summaries.Summariser(["aberg"], days, llm, cap=2, out=tmp_path / "out.json", counter=tmp_path / "c.json")
    out = lambda: json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["personas"]["aberg"]  # noqa: E731

    s.step(mono=1000)  # no log yet: idle, no call
    assert out()["state"] == "idle" and not sent
    event(event="item_start", template="mail.read", at="09:00")
    event(event="task_start", task="mail.read", subgoals=["open"])
    s.step(mono=1001)
    want = {"vm": "ludius-aberg", "state": "ok", "at": None, "text": "Aberg is opening Outlook."}
    want["model"] = "routed/model"
    assert len(sent) == 1 and out() | {"at": None} == want
    assert sent[0]["reasoning"] == {"effort": "high"}
    s.step(mono=1010)  # nothing changed: no call
    event(event="step", subgoal="open", step=1, action="type", option="secret typed text")
    s.step(mono=1050)  # changed, but inside the gap: no call
    assert len(sent) == 1
    s.step(mono=1001 + summaries.MIN_GAP_S)
    assert len(sent) == 2 and "secret typed text" not in json.dumps(sent[1])
    event(event="step", subgoal="open", step=2, action="click")
    s.step(mono=2000)  # changed after the gap, but the day's 2 calls are spent
    assert len(sent) == 2 and out()["state"] == "paused" and out()["text"] == "summaries paused (cap)"
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["models"] == {"routed/model": 2}
    event(event="item_end", template="mail.read")
    event(event="item_start", template="web.lookup", at="09:30")  # a new task while capped: still paused
    s.step(mono=2010)
    assert len(sent) == 2 and out()["state"] == "paused"


def test_no_typed_text_from_titles_or_names(tmp_path: Path) -> None:
    days = tmp_path / "days"
    days.mkdir()
    now = int(time.time() * 1000)
    secret = "cheap flights to lisbon"
    steps = [
        {"event": "item_start", "template": "edge_web_lookup", "at": "10:00"},
        {"event": "task_start", "task": "edge_web_lookup", "subgoals": ["search"]},
        {"event": "step", "subgoal": "search", "step": 1, "action": "type", "option": secret, "process": "msedge",
         "window": f"{secret} - Search - Microsoft Edge", "element": {"role": "edit", "name": secret}},
    ]  # fmt: skip
    (days / "2026-10-29-aberg.jsonl").write_text(
        "".join(json.dumps({"host_ms": now, **e}) + "\n" for e in steps), encoding="utf-8"
    )
    act = summaries.activity(days, "aberg", now)
    assert act.state == "active" and "msedge" in act.text and "edit" in act.text
    assert "lisbon" not in act.text.lower()


def test_a_malformed_counter_pauses(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    today = "2026-10-29"
    assert summaries.take(path, 5, today)  # none yet: a fresh day
    bads: list[Any] = [
        {},
        {"calls": 0},
        {"day": "2026-10-28"},
        {"day": today, "calls": "1", "cost": 0, "models": {}},
        {"day": today, "calls": -1, "cost": 0, "models": {}},
        {"day": today, "calls": 0, "cost": 0, "models": []},
        {"day": "2026-99-99", "calls": 5, "cost": 0, "models": {}},  # impossible date
        {"day": "2026-10-30", "calls": 5, "cost": 0, "models": {}},  # future day
        [],
        "x",
    ]
    for bad in bads:
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert summaries.counter(path, today) is None and not summaries.take(path, 5, today), bad
        assert json.loads(path.read_text(encoding="utf-8")) == bad  # left as it was
    path.write_text("{not json", encoding="utf-8")
    assert not summaries.take(path, 5, today)
    path.write_text(json.dumps({"day": "2026-10-28", "calls": 5, "cost": 0.1, "models": {"m": 5}}), encoding="utf-8")
    assert summaries.take(path, 5, today)  # a valid counter from yesterday rolls over


def test_interruptions_and_a_cut_off_start_are_not_idle(tmp_path: Path) -> None:
    days = tmp_path / "days"
    days.mkdir()
    log = days / "2026-10-29-aberg.jsonl"
    now = int(time.time() * 1000)

    def at(*events: dict[str, Any]) -> summaries.Activity:
        log.write_text("".join(json.dumps({"host_ms": now, **e}) + "\n" for e in events), encoding="utf-8")
        return summaries.activity(days, "aberg", now)

    outer: list[dict[str, Any]] = [{"event": "item_start", "template": "word.write", "at": "09:00"},
             {"event": "task_start", "task": "word.write", "subgoals": ["type"]},
             {"event": "step", "subgoal": "type", "step": 1, "action": "type"}]  # fmt: skip
    inner: list[dict[str, Any]] = [{"event": "interrupt", "task": "word.write", "by": "mail.read"},
             {"event": "item_start", "template": "mail.read", "at": "09:10", "interrupt": True},
             {"event": "task_start", "task": "mail.read", "subgoals": ["open"]}]  # fmt: skip
    after: list[dict[str, Any]] = [
        {"event": "task_end", "task": "mail.read"},
        {"event": "item_end", "template": "mail.read"},
    ]
    assert at(*outer, *inner).brief == "working on mail.read"
    assert at(*outer, *inner, *after).brief == "working on word.write: type"
    resumed: list[dict[str, Any]] = [*outer, *inner, *after, {"event": "resume", "task": "word.write"},
               {"event": "step", "subgoal": "type", "step": 2, "action": "click"}]  # fmt: skip
    assert at(*resumed).brief == "working on word.write: type"
    # The outer item's start is cut off (tail): the resume names it; with no resume either, fresh steps are active.
    assert at(*inner, *after, *resumed[-2:]).brief == "working on word.write: type"
    cut = at({"event": "step", "subgoal": "type", "step": 7, "action": "type"})
    assert cut.state == "active" and cut.brief == "working on an unknown task: type" and "Task: unknown" in cut.text
    assert at(*outer, {"event": "task_end"}, {"event": "item_end"}).state == "idle"
