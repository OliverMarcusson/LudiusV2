from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from test_schedule_service import CLI

from ludius import cli, durable, scheduled
from ludius.loop import Clock
from ludius.planner import DayPlan

VM = "rsvvm"
S = WorkflowExecutionStatus


def say(*a: object) -> None:
    print(*a, file=sys.__stderr__)


@pytest.mark.service
def test_reservation_inspect_and_clear_against_a_real_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if not CLI.exists():
        pytest.skip(f"no cached Temporal CLI at {CLI}; not downloading one")
    asyncio.run(_reservation(tmp_path, monkeypatch, capsys))


async def _reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    runs = tmp_path / "runs"
    desk, lease = runs / "locks" / f"{VM}.lock", runs / "locks" / f"clock-{VM}.json"
    resets: list[str] = []
    fail_reset = False

    def mocked_reset(at: dt.datetime | None, *, vm: str | None = None, agent: str | None = None) -> None:
        assert at is None and vm == VM, "only an explicit reset may ever run"
        resets.append("mocked reset")
        if fail_reset:
            raise RuntimeError("mocked reset failed")

    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setattr(cli, "set_clock", mocked_reset)

    def reserve(owner: str, clock: Clock) -> None:
        plan = DayPlan(persona="aberg", date="2026-10-27", items=[])
        scheduled.DayOwner(desk).claim(owner, durable.DayInput(plan=plan, clock=clock, utc_offset_s=0))

    def held() -> str | None:
        rec = scheduled.DayOwner(desk).read()
        return rec and rec.owner

    def fresh_lease(age_s: int) -> None:
        at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_s)
        lease.write_text(json.dumps({"vm": VM, "pid": 1, "renewed": at.isoformat(timespec="seconds")}), "utf-8")

    async def run(addr: str, what: str, vm: str = "", owner: str = "") -> dict[str, Any]:
        argv = ["--vm", VM, "reservation", what, "--temporal", addr]
        if what == "clear":
            argv += ["--confirm-vm", vm, "--confirm-owner", owner]
        rc = await asyncio.to_thread(cli.main, argv)
        assert rc == 0
        return json.loads(capsys.readouterr().out)

    async def refused(addr: str, owner: str, match: str, *, vm: str = VM, confirm: str | None = None) -> None:
        with pytest.raises(Exception, match=match) as e:
            await run(addr, "clear", vm, confirm or owner)
        say(f"  refused: {type(e.value).__name__}: {e.value}")

    async with await WorkflowEnvironment.start_local(  # pyright: ignore[reportUnknownMemberType]
        data_converter=pydantic_data_converter, ui=False, ip="127.0.0.1",
        dev_server_existing_path=str(CLI), dev_server_database_filename=str(tmp_path / "temporal.db"),
    ) as env:  # fmt: skip
        client: Client = env.client
        addr = client.service_client.config.target_host
        assert not addr.endswith(":7233"), addr
        say(f"REAL private dev server on {addr.rsplit(':', 1)[0]}:<random port>, temp SQLite, temp runs root")

        async def start(wid: str, **kw: Any) -> str:
            await client.start_workflow("ScheduledDay", "unused", id=wid, task_queue="no-worker-here", **kw)
            return f"default/{wid}"

        running = await start("day-aberg-running")
        terminated = await start("day-aberg-terminated")
        await client.get_workflow_handle("day-aberg-terminated").terminate("reservation test")
        timed_out = await start("day-aberg-timedout", execution_timeout=dt.timedelta(seconds=1))
        for _ in range(100):
            if (await client.get_workflow_handle("day-aberg-timedout").describe()).status == S.TIMED_OUT:
                break
            await asyncio.sleep(0.1)
        status = {o: (await client.get_workflow_handle(o.split("/", 1)[1]).describe()).status
                  for o in (running, terminated, timed_out)}  # fmt: skip
        assert list(status.values()) == [S.RUNNING, S.TERMINATED, S.TIMED_OUT], status
        say("REAL workflow states:", {o: s and s.name for o, s in status.items()})

        assert await run(addr, "inspect") == {"vm": VM, "reservation": None}
        await refused(addr, "", "no reservation", confirm="x")

        reserve(running, "compressed")
        out = await run(addr, "inspect")
        assert (out["workflow"], out["clearable"], out["cleared"], out["clock"]) == (
            "RUNNING",
            False,
            False,
            "compressed",
        )
        say("REAL inspect running:", out["workflow"], "clearable =", out["clearable"])
        await refused(addr, running, "not confirmed closed")
        assert held() == running and not resets
        scheduled.DayOwner(desk).release(running)

        reserve(terminated, "realtime")
        out = await run(addr, "inspect")
        assert (out["workflow"], out["clearable"]) == ("TERMINATED", True)
        await refused(addr, terminated, "repeat this VM", vm="other-vm")
        await refused(addr, terminated, "repeat this VM", confirm="default/day-aberg-running")
        await refused(addr, terminated, "repeat this VM", confirm="day-aberg-terminated")
        assert held() == terminated
        with durable.desk_lock(desk):
            for what in ("inspect", "clear"):
                with pytest.raises(durable.DeskBusy):
                    await run(addr, what, VM, terminated)
        assert held() == terminated
        say("REAL desk lock: inspect and clear refused with DeskBusy while another holder has the lock")

        scheduled.DayOwner(desk).release(terminated)
        for bad, why in (
            ("nosuchns/day-aberg-terminated", "wrong namespace"),
            ("default/day-aberg-notfound", "not found"),
            ("default/day-aberg-terminated/extra", "id with an extra segment"),
        ):
            reserve(bad, "realtime")
            out = await run(addr, "inspect")
            assert out["workflow"].startswith("UNKNOWN") and not out["clearable"], out
            say(f"REAL inspect {why}:", out["workflow"][:110])
            await refused(addr, bad, "not confirmed closed")
            assert held() == bad
            scheduled.DayOwner(desk).release(bad)
        reserve(terminated, "realtime")
        out = await run("127.0.0.1:1", "inspect")
        assert out["workflow"].startswith("UNKNOWN") and not out["clearable"], out
        await refused("127.0.0.1:1", terminated, "not confirmed closed")
        assert held() == terminated
        say("REAL inspect unreachable server:", out["workflow"][:110])

        out = await run(addr, "clear", VM, terminated)
        assert out["cleared"] and held() is None and not resets
        say("REAL clear realtime/TERMINATED: cleared, no reset")
        reserve(timed_out, "compressed")
        fresh_lease(5)
        out = await run(addr, "clear", VM, timed_out)
        assert (out["workflow"], out["cleared"]) == ("TIMED_OUT", True) and held() is None and not resets
        assert lease.exists()
        say("REAL clear compressed/TIMED_OUT: cleared, no reset, lease untouched")
        lease.unlink()

        reserve(terminated, "time-jump")
        fresh_lease(5)
        await refused(addr, terminated, "live controller")
        assert held() == terminated and not resets
        say("MOCKED reset not called while a live lease exists (real lease file)")
        fresh_lease(3600)
        fail_reset = True
        await refused(addr, terminated, "mocked reset failed")
        assert held() == terminated and resets == ["mocked reset"]
        say("MOCKED reset failed: reservation retained")
        fail_reset = False
        out = await run(addr, "clear", VM, terminated)
        assert out["cleared"] and held() is None and resets == ["mocked reset"] * 2
        say("MOCKED reset ok on a stale lease: cleared after reset (cli passes explicit reset only)")
