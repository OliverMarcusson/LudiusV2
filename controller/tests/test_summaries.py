"""Monitor activity lines by rule from the day log (no model), persona health and interactions."""

import json
import time
from pathlib import Path
from typing import Any

import yaml

from ludius import summaries


def test_lines_from_the_day_log(tmp_path: Path) -> None:
    days = tmp_path / "days"
    days.mkdir()
    log = days / "2026-10-29-aberg.jsonl"
    now = int(time.time() * 1000)

    def event(**e: object) -> None:
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"host_ms": now, **e}) + "\n")

    s = summaries.Summariser(["aberg"], days, out=tmp_path / "out.json")
    out = lambda: json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["personas"]["aberg"]  # noqa: E731
    s.step()  # no log yet
    assert (out()["state"], out()["text"]) == ("idle", "idle: no day log yet")
    event(event="item_start", template="mail.read", at="09:00")
    event(event="task_start", task="mail.read", subgoals=["open"])
    event(event="step", subgoal="open", step=1, action="type", option="secret typed text")
    s.step()
    assert out() | {"at": None} == {"vm": "ludius-aberg", "state": "ok", "text": "working on mail.read: open",
                                    "at": None, "health": "ok", "reason": ""}  # fmt: skip


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
    assert act.state == "active" and act.text == "working on edge_web_lookup: search"
    assert "lisbon" not in act.text.lower()


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
    assert at(*outer, *inner).text == "working on mail.read"
    assert at(*outer, *inner, *after).text == "working on word.write: type"
    resumed: list[dict[str, Any]] = [*outer, *inner, *after, {"event": "resume", "task": "word.write"},
               {"event": "step", "subgoal": "type", "step": 2, "action": "click"}]  # fmt: skip
    assert at(*resumed).text == "working on word.write: type"
    # The outer item's start is cut off (tail): the resume names it; with no resume either, fresh steps are active.
    assert at(*inner, *after, *resumed[-2:]).text == "working on word.write: type"
    cut = at({"event": "step", "subgoal": "type", "step": 7, "action": "type"})
    assert cut.state == "active" and cut.text == "working on an unknown task: type"
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
    sup = tmp_path / "supervisor.jsonl"
    sup.write_text("", encoding="utf-8")
    s = summaries.Summariser(["pvanloon"], days, out=tmp_path / "out.json",
                             alerts=tmp_path / "alerts.jsonl", supervisor=sup, rules=rules)  # fmt: skip
    s.step()
    s.step()
    p = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["personas"]["pvanloon"]
    assert p["health"] == "warn" and p["reason"].startswith("looping")
    write(lnord, 30)
    s.step()
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
    alerts = tmp_path / "alerts.jsonl"

    def summariser() -> summaries.Summariser:
        return summaries.Summariser(["pvanloon"], days, out=tmp_path / "out.json",
                                    alerts=alerts, supervisor=sup, rules=rules)  # fmt: skip

    def sent() -> list[tuple[str, str]]:
        return [(a["health"], a["reason"]) for a in map(json.loads, alerts.read_text(encoding="utf-8").splitlines())]

    write(unlock_failed[:2], 0)
    s = summariser()
    s.step()
    assert s.verdicts["pvanloon"].health == "ok" and not alerts.exists()
    log.unlink()
    s.step()
    assert sent() == [("warn", "unknown: the running day's log is missing")]
    write(unlock_failed, 6)
    s.step()
    assert sent()[-1][0] == "fail" and len(sent()) == 2
    s2 = summariser()  # restarted: the same failure is no new alert
    s2.step()
    assert len(sent()) == 2
    write([*unlock_failed, {"event": "item_start", "template": "x"}, {"event": "item_end", "status": "done"},
           {"event": "day_end"}], 0)  # fmt: skip
    summariser().step()  # restarted again: the recovery is still written
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
