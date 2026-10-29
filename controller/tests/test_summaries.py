"""Monitor activity lines: a request only on a changed record, at most once per gap, never past the daily cap."""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

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
    want |= {"model": "routed/model", "health": "ok", "reason": ""}
    assert len(sent) == 1 and out() | {"at": None} == want
    assert sent[0]["reasoning"] == {"effort": "high"}
    s.step(mono=1010)  # nothing changed: no call
    event(event="step", subgoal="open", step=1, action="type", option="secret typed text")
    s.step(mono=1020)  # changed, but inside the gap: no call
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


def test_health_rules_on_a_pvanloon_like_day(tmp_path: Path) -> None:
    from ludius import health

    days = tmp_path / "days"
    days.mkdir()
    log = days / "2026-11-02-pvanloon.jsonl"
    t0 = int(time.time() * 1000) - 20 * 60_000
    rules = health.Rules(stall_s=300, normalize_fails=2, retry_attempt=3, abandoned=6, errors=3)
    explorer = {"event": "normalize_failed", "closed": [" (explorer.exe)"] * 12}
    blocked = {"event": "escalate_blocked"}
    start = {"event": "item_start", "template": "outlook_send_mail", "at": "10:00", "logical": "2026-11-02T10:00"}
    day: list[dict[str, Any]] = [
        {"event": "day_start", "clock": "compressed"},
        {"event": "item_start", "template": "excel_new_workbook", "at": "09:00"},
        {"event": "item_end", "template": "excel_new_workbook", "status": "abandoned"},
        start, explorer, blocked, {"event": "item_retry", "template": "outlook_send_mail", "attempt": 2},
        start, explorer, blocked, {"event": "item_retry", "template": "outlook_send_mail", "attempt": 3},
        start, explorer, blocked,
    ]  # fmt: skip

    def write(events: list[dict[str, Any]], quiet_min: int) -> None:
        end = int(time.time() * 1000) - quiet_min * 60_000
        ms = [t0 + (end - t0) * i // max(1, len(events) - 1) for i in range(len(events))]
        log.write_text(
            "".join(json.dumps({"host_ms": m, **e}) + "\n" for m, e in zip(ms, events, strict=True)), encoding="utf-8"
        )

    now = int(time.time() * 1000)
    write(day, 1)  # looping, still logging
    v = health.classify(log, now, rules)
    assert (v.health, v.rules) == ("warn", ("looping", "retrying")) and "normalize failed 3x" in v.reason
    assert "outlook_send_mail retry attempt 3" in v.reason and v.logical == "2026-11-02T10:00"
    write(day, 6)  # then silence: stalled as well
    assert health.classify(log, now, rules).rules == ("stalled", "looping", "retrying")
    assert health.classify(log, now, rules).health == "fail"
    # One failed normalize that recovered, an attempt-2 retry, one decision error and a finished day: ok (lnord).
    lnord = [day[0], explorer, blocked, {"event": "item_retry", "template": "x", "attempt": 2}, start,
             {"event": "normalized"}, {"event": "decision_error"}, {"event": "item_end", "status": "abandoned"},
             {"event": "day_end"}]  # fmt: skip
    write(lnord, 30)
    assert health.classify(log, now, rules).health == "ok"
    # A realtime day waiting between items logs nothing for long: not stalled. Too many abandons or errors: failing.
    write([{**day[0], "clock": "realtime"}, *day[1:3]], 40)
    assert health.classify(log, now, rules).health == "ok"
    ends = [{"event": "item_end", "status": "abandoned"}] * 7 + [{"event": "decision_error"}] * 2
    write([day[0], *ends], 0)
    assert health.classify(log, now, rules).rules == ("abandoning",)
    assert health.classify(log, now, rules, exits=[now]).rules == ("abandoning", "errors")  # a worker exit is an error

    # The summaries file carries health; a change to warn/fail (and the recovery) is one alert line, not one a pass.
    write(day, 1)
    llm = LlmClient("key")
    llm._http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))  # pyright: ignore[reportPrivateUsage]
    sup = tmp_path / "supervisor.jsonl"
    sup.write_text("", encoding="utf-8")
    s = summaries.Summariser(["pvanloon"], days, llm, out=tmp_path / "out.json", counter=tmp_path / "c.json",
                             alerts=tmp_path / "alerts.jsonl", supervisor=sup, rules=rules)  # fmt: skip
    s.step(mono=1000)
    s.step(mono=1005)
    p = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["personas"]["pvanloon"]
    assert p["health"] == "warn" and p["reason"].startswith("looping")
    write(lnord, 30)
    s.step(mono=1010)
    alerts = [json.loads(x) for x in (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(a["persona"], a["health"]) for a in alerts] == [("pvanloon", "warn"), ("pvanloon", "ok")]
    assert alerts[0]["logical"] == "2026-11-02T10:00" and alerts[0]["host"] and alerts[1]["reason"] == "recovered"


def test_a_stopped_day_unknown_inputs_and_a_restart(tmp_path: Path, monkeypatch: Any) -> None:
    import pytest

    from ludius import health

    days = tmp_path / "days"
    days.mkdir()
    log = days / "2026-11-02-pvanloon.jsonl"
    sup = tmp_path / "supervisor.jsonl"
    sup.write_text("", encoding="utf-8")
    rules = health.Rules(stall_s=300, normalize_fails=2, retry_attempt=3, abandoned=6, errors=3)
    now = int(time.time() * 1000)

    def write(events: list[dict[str, Any]], quiet_min: int) -> None:
        ms = now - quiet_min * 60_000
        log.write_text("".join(json.dumps({"host_ms": ms, **e}) + "\n" for e in events), encoding="utf-8")

    # A failed break unlock: item_end failed, the workflow fails non-retryably, no day_end.
    unlock_failed = [
        {"event": "day_start", "clock": "realtime"},
        {"event": "item_start", "template": "break", "at": "12:00", "minutes": 30},
        {"event": "break", "minutes": 30},
        {"event": "item_end", "template": "break", "status": "failed"},
    ]
    write(unlock_failed, 1)
    assert health.classify(log, now, rules).health == "ok"  # not yet: the worker may still log
    write(unlock_failed, 6)
    v = health.classify(log, now, rules)
    assert (v.health, v.rules) == ("fail", ("stopped",)) and "after break failed, no day_end" in v.reason
    write([*unlock_failed, {"event": "day_aborted"}], 0)  # the durable layer's signal: at once, on any clock
    assert health.classify(log, now, rules).rules == ("stopped",)
    # A realtime day waiting after an ordinary failed task is not stopped.
    write([*unlock_failed[:1], {"event": "item_start", "template": "x"}, {"event": "item_end", "template": "x",
           "status": "failed"}], 40)  # fmt: skip
    assert health.classify(log, now, rules).health == "ok"

    # An unreadable supervisor log while the day runs is unknown, not ok; a finished day doesn't need it.
    write(unlock_failed[:2], 0)
    assert health.classify(log, now, rules, None).rules == ("supervisor",)
    write([*unlock_failed, {"event": "day_end"}], 0)
    assert health.classify(log, now, rules, None).health == "ok"

    # The summariser: a running day's log that disappears is unknown; a restart seeds from the alerts file.
    llm = LlmClient("key")
    llm._http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))  # pyright: ignore[reportPrivateUsage]
    alerts = tmp_path / "alerts.jsonl"

    def summariser() -> summaries.Summariser:
        return summaries.Summariser(["pvanloon"], days, llm, out=tmp_path / "out.json", counter=tmp_path / "c.json",
                                    alerts=alerts, supervisor=sup, rules=rules)  # fmt: skip

    def sent() -> list[tuple[str, str]]:
        return [(a["health"], a["reason"]) for a in map(json.loads, alerts.read_text(encoding="utf-8").splitlines())]

    write(unlock_failed[:2], 0)
    s = summariser()
    s.step(mono=1000)
    assert s.verdicts["pvanloon"].health == "ok" and not alerts.exists()
    log.unlink()
    s.step(mono=1005)
    assert sent() == [("warn", "unknown: the running day's log is missing")]
    write(unlock_failed, 6)
    s.step(mono=1010)
    assert sent()[-1][0] == "fail" and len(sent()) == 2
    s2 = summariser()  # restarted: the same failure is no new alert
    s2.step(mono=2000)
    assert len(sent()) == 2
    write([*unlock_failed, {"event": "item_start", "template": "x"}, {"event": "item_end", "status": "done"},
           {"event": "day_end"}], 0)  # fmt: skip
    summariser().step(mono=3000)  # restarted again: the recovery is still written
    assert sent()[-1] == ("ok", "recovered") and len(sent()) == 3

    for bad in ("0", "-5"):
        monkeypatch.setenv("LUDIUS_HEALTH_STALL_S", bad)
        with pytest.raises(ValueError, match="at least 1"):
            health.Rules()


def test_interactions_only_from_verified_effects_between_personas(tmp_path: Path) -> None:
    from ludius import interactions

    days = tmp_path / "days"
    days.mkdir()
    reqs = tmp_path / "world" / "requests"
    reqs.mkdir(parents=True)
    now = int(time.time() * 1000)
    out = tmp_path / "monitor-interactions.jsonl"
    users = ["aberg", "sdekker", "lnord"]

    def send(to: str, status: str, ms: int, subject: str = "Board delivery numbers") -> list[dict[str, Any]]:
        params = {"to": to, "subject": subject, "body": "typed body text"}
        return [{"event": "item_start", "template": "outlook_send_mail", "host_ms": ms},
                {"event": "item_end", "template": "outlook_send_mail", "logical": "2026-11-02T10:00", "host_ms": ms,
                 "params": params, "status": status}]  # fmt: skip

    day = [
        *send("sdekker@ludius.internal", "done", now),  # persona -> persona, verified: one interaction
        *send("mvellinga@ludius.internal", "done", now + 1),  # a narrative colleague, no VM: none
        *send("lnord@ludius.internal", "abandoned", now + 2),  # planned, never proven delivered: none
        *send("aberg@ludius.internal", "done", now + 3),  # to itself: none
        *send("sdekker@other.example", "done", now + 4),  # another domain: none
    ]
    log = days / "2026-11-02-aberg.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in day), encoding="utf-8")
    t = interactions.Tracker(days, users, out, requests_dir=reqs)
    new = t.step()
    assert [(i.personas, i.kind, i.label, i.logical) for i in new] == [
        (["aberg", "sdekker"], "mail", "Board delivery numbers", "2026-11-02T10:00")
    ]
    assert "typed body text" not in out.read_text(encoding="utf-8")
    assert t.step() == []  # nothing new
    assert interactions.Tracker(days, users, out, requests_dir=reqs).step() == []  # a restart repeats nothing

    # A manager's request counts once it is recorded as sent, persona to persona; its fulfilment counts too.
    msgid = "<1.aberg@ludius.internal>"
    request = {"to": "sdekker", "task": "excel_new_workbook", "subject": "October accruals workbook",
               "manager": "aberg", "msgid": msgid, "status": "planned"}  # fmt: skip
    (reqs / "2026-11-02-aberg.yaml").write_text(yaml.safe_dump({"manager": "aberg", "date": "2026-11-02",
                                                                "requests": [request]}), encoding="utf-8")  # fmt: skip
    assert t.step() == []  # planned, not sent
    (reqs / "2026-11-02-aberg.yaml").write_text(yaml.safe_dump({"manager": "aberg", "date": "2026-11-02",
                                                                "requests": [request | {"status": "sent"}]}),
                                                encoding="utf-8")  # fmt: skip
    check = {"event": "request_check", "host_ms": now + 9, "request": msgid, "fulfilled": True}
    (days / "2026-11-02-sdekker.jsonl").write_text(json.dumps(check) + "\n", encoding="utf-8")
    got = sorted((i.kind, i.personas) for i in t.step())
    assert got == [("request", ["aberg", "sdekker"]), ("request_done", ["sdekker", "aberg"])]
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3
