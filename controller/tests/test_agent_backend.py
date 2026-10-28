"""Host-local Hyper-V paths only for a Hyper-V agent on this machine: HostAgent.local_hyperv, the time-jump refusal,
and the contract verdict (skipped is never reported as passed)."""

from __future__ import annotations

import argparse
import socket
from typing import Any

import grpc
import pytest

from ludius import cli, hostagent
from ludius.v1 import agent_pb2 as pb


class _Rpc(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


def _with_info(answer: pb.AgentInfo | Exception, address: str = "127.0.0.1:7451") -> hostagent.HostAgent:
    agent = hostagent.HostAgent(address, "ludius-win10-01")

    class Stub:
        def Info(self, req: pb.InfoRequest, timeout: float) -> pb.AgentInfo:  # noqa: N802 - gRPC method name
            if isinstance(answer, Exception):
                raise answer
            return answer

    agent._stub = Stub()  # pyright: ignore[reportPrivateUsage]
    return agent


def test_local_hyperv() -> None:
    here = socket.gethostname()
    assert _with_info(pb.AgentInfo(backend="hyperv", host=here.upper())).local_hyperv()
    assert not _with_info(pb.AgentInfo(backend="hyperv", host="other-host")).local_hyperv()
    assert not _with_info(pb.AgentInfo(backend="proxmox", host=here)).local_hyperv()
    # An agent without Info is the old Windows build (Hyper-V only): local only on a loopback address.
    old = _Rpc(grpc.StatusCode.UNIMPLEMENTED)
    assert _with_info(old, "127.0.0.1:7451").local_hyperv()
    assert _with_info(old, "localhost:7451").local_hyperv()
    assert _with_info(old, "[::1]:7451").local_hyperv()
    assert not _with_info(old, "pve1.lan:7451").local_hyperv()
    assert not _with_info(old, "192.168.0.67:7451").local_hyperv()
    assert not _with_info(_Rpc(grpc.StatusCode.UNAVAILABLE)).local_hyperv()


@pytest.fixture
def proxmox_agent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every HostAgent reports Proxmox; records any process the controller would start."""
    cli.require_hyperv_clock.cache_clear()
    monkeypatch.setenv("LUDIUS_VM", "ludius-win10-01")
    ran: list[str] = []

    def proxmox(self: hostagent.HostAgent) -> bool:
        return False

    monkeypatch.setattr(hostagent.HostAgent, "local_hyperv", proxmox)

    def run(*a: Any, **k: Any) -> None:
        ran.append(str(a))

    monkeypatch.setattr(cli.subprocess, "run", run)
    return ran


def test_time_jump_is_refused_before_the_lease_or_the_clock_script(
    proxmox_agent: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    cli._clock_lease.cache_clear()  # pyright: ignore[reportPrivateUsage]
    for at in (None, cli.dt.datetime(2026, 1, 5, 9, 0)):
        with pytest.raises(cli.ClockRefused, match="does not drive Hyper-V"):
            cli.set_clock(at, vm="ludius-win10-01", agent="pve1.lan:7451")
    assert proxmox_agent == []  # no set-clock.ps1
    assert not (tmp_path / "locks").exists()  # no lease written


def test_a_time_jump_day_is_refused_before_anything_starts(
    proxmox_agent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []

    def record(*a: Any) -> None:
        started.append(str(a))

    monkeypatch.setattr(cli, "start_agent", record)
    monkeypatch.setattr(cli.planner, "load_plan", record)
    args = argparse.Namespace(canary=False, temporal=None, clock="time-jump", agent="pve1.lan:7451", vm=None)
    with pytest.raises(cli.ClockRefused):
        cli.cmd_day(args)
    assert started == [] and proxmox_agent == []


def test_contract_verdict_never_passes_a_failure() -> None:
    assert cli.contract_verdict(0, 0) == "PASS"
    assert cli.contract_verdict(0, 1) == "PASS, 1 skipped (old agent)"
    assert cli.contract_verdict(1, 0) == "FAIL: 1 failure(s)"
    assert cli.contract_verdict(2, 1) == "FAIL: 2 failure(s), 1 skipped (old agent)"
