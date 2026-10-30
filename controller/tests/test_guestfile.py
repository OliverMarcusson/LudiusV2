from __future__ import annotations

from typing import Any

import grpc
import pytest

from ludius import guestfile
from ludius.v1 import observation_pb2 as obs

REL = r"Documents\a.txt"


class _Rpc(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


def _agent(answer: obs.FileContent | Exception, seen: list[str], local_hyperv: bool = False) -> Any:
    class Agent:
        def __init__(self, address: str, vm: str) -> None:
            seen.append(f"{address} {vm}")

        def read_file(self, path: str, max_bytes: int) -> obs.FileContent:
            seen.append(f"{path} {max_bytes}")
            if isinstance(answer, Exception):
                raise answer
            return answer

        def local_hyperv(self) -> bool:
            seen.append("asked backend")
            return local_hyperv

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
        return guestfile.GuestFile(True, 1, b"local")

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


def test_an_old_agent_on_local_hyperv_falls_back_to_powershell_direct(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _agent(_Rpc(grpc.StatusCode.UNIMPLEMENTED), env, local_hyperv=True)
    monkeypatch.setattr(guestfile, "HostAgent", old)
    assert guestfile.read("aberg", REL) == guestfile.GuestFile(True, 1, b"local")
    assert env[-2:] == ["asked backend", "powershell"]


def test_no_fallback_to_a_machine_the_agent_does_not_drive(env: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestfile, "HostAgent", _agent(_Rpc(grpc.StatusCode.UNIMPLEMENTED), env, local_hyperv=False))
    assert guestfile.read("aberg", REL) is None
    for code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL, grpc.StatusCode.PERMISSION_DENIED):
        monkeypatch.setattr(guestfile, "HostAgent", _agent(_Rpc(code), env, local_hyperv=True))
        assert guestfile.read("aberg", REL) is None
    assert "powershell" not in env
    assert env.count("asked backend") == 1


def test_unknown_stays_unknown(env: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    big = obs.FileContent(exists=True, data=b"x" * (guestfile.MAX_BYTES + 1))
    monkeypatch.setattr(guestfile, "HostAgent", _agent(big, env))
    assert guestfile.read("aberg", REL) is None

    env.clear()
    assert guestfile.read("aberg", r"Desktop\a.txt") is None
    monkeypatch.delenv("LUDIUS_VM")
    assert guestfile.read("aberg", REL) is None
    assert env == []
