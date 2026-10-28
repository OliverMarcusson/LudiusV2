"""guestfile.read: the observer's answer through the host agent first, PowerShell Direct only when it gave none."""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from ludius import guestfile
from ludius.v1 import observation_pb2 as obs

REL = r"Documents\a.txt"


class _Down(grpc.RpcError):
    pass


def _agent(answer: obs.FileContent | Exception, seen: list[str]) -> Any:
    class Agent:
        def __init__(self, address: str, vm: str) -> None:
            seen.append(f"{address} {vm}")

        def read_file(self, path: str, max_bytes: int) -> obs.FileContent:
            seen.append(f"{path} {max_bytes}")
            if isinstance(answer, Exception):
                raise answer
            return answer

        def close(self) -> None:
            pass

    return Agent


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("LUDIUS_VM", "ludius-win10-01")
    monkeypatch.setenv("LUDIUS_AGENT", "agent:7451")
    seen: list[str] = []

    def powershell(path: str) -> guestfile.GuestFile | None:
        seen.append("powershell")
        return None

    monkeypatch.setattr(guestfile, "_read_via_powershell", powershell)
    return seen


def test_the_observers_answer_is_used(env: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    content = obs.FileContent(exists=True, written_ms=5, data=b"x")
    monkeypatch.setattr(guestfile, "HostAgent", _agent(content, env))
    assert guestfile.read("aberg", REL) == guestfile.GuestFile(True, 5, b"x")
    assert env == ["agent:7451 ludius-win10-01", rf"C:\Users\aberg\Documents\a.txt {guestfile.MAX_BYTES}"]

    monkeypatch.setattr(guestfile, "HostAgent", _agent(obs.FileContent(exists=False), env))
    assert guestfile.read("aberg", REL) == guestfile.GuestFile(False)
    assert "powershell" not in env


def test_no_answer_falls_back_and_stays_unknown(env: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestfile, "HostAgent", _agent(_Down(), env))  # UNIMPLEMENTED (old observer), or unreachable
    assert guestfile.read("aberg", REL) is None
    assert env[-1] == "powershell"

    big = obs.FileContent(exists=True, data=b"x" * (guestfile.MAX_BYTES + 1))
    monkeypatch.setattr(guestfile, "HostAgent", _agent(big, env))
    assert guestfile.read("aberg", REL) is None  # over the limit: unknown, never parsed

    env.clear()
    assert guestfile.read("aberg", r"Desktop\a.txt") is None  # outside the noise folders: nothing is asked
    assert env == []
