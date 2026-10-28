"""OpenRouter call counters: one count per request sent, retries included, Decisions and LLM kept apart."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from ludius import calls
from ludius.decisions import DecisionsClient
from ludius.llm import LlmClient


def _replies(*responses: httpx.Response) -> httpx.Client:
    queue = list(responses)
    return httpx.Client(transport=httpx.MockTransport(lambda _: queue.pop(0)))


def _counts(path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["decision"], data["llm"]


def _chat(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


class _Answer(BaseModel):
    ok: bool


def test_counts_each_request_and_keeps_kinds_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calls, "PATH", tmp_path / "calls.json")

    decider = DecisionsClient("k")
    answer = {
        "model": decider.model,
        "answers": {"action": {"type": "choice", "choice": "a", "probabilities": {"a": 1}}},
    }
    decider._http = _replies(httpx.Response(503), httpx.Response(200, json=answer))  # pyright: ignore[reportPrivateUsage]
    assert decider.decide({}, {"a": "open"}, ask_progress=False).choice == "a"
    assert _counts(calls.PATH) == (2, 0)  # the 5xx retry was a second request

    llm = LlmClient("k")
    llm._http = _replies(_chat("nope"), _chat('{"ok": true}'))  # pyright: ignore[reportPrivateUsage]
    assert llm.json(_Answer, "s", "u").ok
    assert _counts(calls.PATH) == (2, 2)  # the validation re-ask posted again


@pytest.mark.parametrize("stored", [None, "{}", '{"llm": -1}', '{"llm": 1.5}', '{"llm": true}', "not json"])
def test_capped_counter_fails_closed_when_lost_or_invalid(
    stored: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calls, "PATH", tmp_path / "calls.json")
    monkeypatch.setenv("LUDIUS_CAP_LLM", "21")
    if stored is not None:
        calls.PATH.write_text(stored, encoding="utf-8")
    with pytest.raises(calls.CapReached):
        calls.count("llm")  # a lost counter must not grant the whole ceiling again
