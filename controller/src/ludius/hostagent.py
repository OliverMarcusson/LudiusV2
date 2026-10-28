"""Thin typed client for the host agent's gRPC API. Hypervisor-agnostic by design."""

from __future__ import annotations

import time
from typing import Any

import grpc

from ludius.v1 import agent_pb2 as pb
from ludius.v1 import agent_pb2_grpc as rpc
from ludius.v1 import observation_pb2 as obs


class HostAgent:
    def __init__(self, address: str, vm: str) -> None:
        self.vm = vm
        self._channel = grpc.insecure_channel(address)
        # Generated stubs are untyped; results are annotated at each call.
        self._stub: Any = rpc.HostAgentStub(self._channel)

    def close(self) -> None:
        self._channel.close()

    def _input(self, **action: Any) -> int:
        result: pb.InputResult = self._stub.Input(pb.InputRequest(vm=self.vm, **action), timeout=120)
        return result.host_time_ms

    def move(self, x: int, y: int, duration_ms: int = 0) -> int:
        return self._input(move=pb.Move(x=x, y=y, duration_ms=duration_ms))

    def click(self, button: int = pb.Click.LEFT, count: int = 1) -> int:
        return self._input(click=pb.Click(button=button, count=count))  # pyright: ignore[reportArgumentType]

    def key(self, vk: int, down: bool) -> int:
        return self._input(key=pb.Key(vk=vk, down=down))

    def chord(self, vks: list[int]) -> int:
        return self._input(chord=pb.Chord(vk=vks))

    def type(self, text: str, mean_ms: int, jitter_ms: int) -> int:
        return self._input(type=pb.TypeText(text=text, mean_ms=mean_ms, jitter_ms=jitter_ms))

    def scroll(self, notches: int) -> int:
        return self._input(scroll=pb.Scroll(notches=notches))

    def ctrl_alt_del(self) -> int:
        return self._input(ctrl_alt_del=pb.CtrlAltDel())

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        req = obs.SnapshotRequest(wait_settle=wait_settle, quiet_ms=quiet_ms, timeout_ms=timeout_ms)
        for attempt in range(3):
            try:
                result: pb.ObserveResult = self._stub.Observe(pb.ObserveRequest(vm=self.vm, request=req), timeout=60)
                return result
            except grpc.RpcError as e:
                # INTERNAL: the observer threw mid-snapshot (UI changing under it). UNAVAILABLE: the guest channel
                # dropped (seen right after a host agent restart) and the agent reconnects. Look again.
                code = e.code()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
                if code not in (grpc.StatusCode.INTERNAL, grpc.StatusCode.UNAVAILABLE) or attempt == 2:
                    raise
                time.sleep(0.5 if code == grpc.StatusCode.INTERNAL else 3)
        raise AssertionError("unreachable")

    def lifecycle(self, op: int, checkpoint: str = "", target: str = "") -> str:
        req = pb.LifecycleRequest(vm=self.vm, op=op, checkpoint=checkpoint, target=target)  # pyright: ignore[reportArgumentType]
        result: pb.LifecycleResult = self._stub.Lifecycle(req, timeout=1800)  # a clone copies the whole disk
        return result.state

    def read_file(self, path: str, max_bytes: int) -> obs.FileContent:
        """One guest file through the observer (read-only); raises grpc.RpcError when it can't say."""
        req = pb.GuestFileRequest(vm=self.vm, file=obs.ReadFile(path=path, max_bytes=max_bytes))
        result: obs.FileContent = self._stub.ReadGuestFile(req, timeout=60)
        return result

    def screenshot(self) -> bytes:
        result: pb.ScreenshotResult = self._stub.Screenshot(pb.ScreenshotRequest(vm=self.vm), timeout=60)
        return result.png
