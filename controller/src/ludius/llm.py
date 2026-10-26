"""Planner LLM through OpenRouter chat completions. Per range, per day, per task; never per action."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class LlmError(Exception):
    pass


class LlmClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 180) -> None:
        if not api_key:
            raise LlmError("OPENROUTER_API_KEY is not set")
        self.model = model
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Ludius planner"},
        )
        self.cost = 0.0

    def json[M: BaseModel](self, cls: type[M], system: str, user: str, temperature: float = 0.9) -> M:
        """One JSON answer validated as `cls`; a validation error goes back to the model once."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system + "\nAnswer with one JSON object only, no prose, no code fence."},
            {"role": "user", "content": user},
        ]
        for attempt in range(2):
            text = self._complete(messages, temperature)
            try:
                return cls.model_validate(json.loads(_FENCE.sub("", text.strip())))
            except (json.JSONDecodeError, ValidationError) as e:
                if attempt:
                    raise LlmError(f"invalid {cls.__name__}: {e}") from e
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That did not validate: {e}\nSend the corrected JSON object."},
                ]
        raise AssertionError("unreachable")

    def _complete(self, messages: list[dict[str, str]], temperature: float) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "usage": {"include": True},
        }
        r = self._http.post(ENDPOINT, json=body)
        if r.status_code != 200:
            raise LlmError(f"OpenRouter {r.status_code}: {r.text[:300]}")
        data: dict[str, Any] = r.json()
        usage: dict[str, Any] = data.get("usage") or {}
        self.cost += float(usage.get("cost") or 0)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as e:
            raise LlmError(f"unexpected response: {str(data)[:300]}") from e

    def close(self) -> None:
        self._http.close()
