"""Monitor activity lines: a request only on a changed record, at most once per gap, never past the daily cap."""

import json
import time
from pathlib import Path

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
