from __future__ import annotations

import asyncio
import datetime as dt
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import CancelledError
from temporalio.testing import WorkflowEnvironment
from test_day import FakeRunner, FakeSession, note

from ludius import durable
from ludius.cli import load_templates
from ludius.day import DayRunner
from ludius.loop import GroundTruth, Outcome
from ludius.planner import DayPlan, PlanItem
from ludius.tasks import Task


async def _env(local: bool) -> WorkflowEnvironment:
    try:  # the SDK downloads its test server (time-skipping) or the Temporal CLI dev server (local) on first use
        if local:
            return await WorkflowEnvironment.start_local(  # pyright: ignore[reportUnknownMemberType]
                data_converter=pydantic_data_converter, ui=False
            )
        return await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as e:
        pytest.skip(f"Temporal server unavailable: {e}")


def _day(runner: type[FakeRunner], log: Path, calls: list[str], clock: str, **kw: Any) -> DayRunner:
    gt = durable.HeartbeatLog(log)
    return DayRunner(runner(gt, calls), FakeSession(calls), load_templates(), gt, clock, 0, **kw)  # type: ignore[arg-type]


class FlakyRunner(FakeRunner):
    """The 10:00 task loses its connection once (an infrastructure error, not a task failure)."""

    def run(self, task: Task, between: Any = None) -> Outcome:
        if task.inst.params["file_name"] == "note 1000" and "run note 1000" not in self.calls:
            self.calls.append("run note 1000")
            raise RuntimeError("UNAVAILABLE: agent connection reset")
        return super().run(task, between)


def test_realtime_day_keeps_the_timeline_and_retries_in_place(tmp_path: Path) -> None:
    asyncio.run(_realtime_day(tmp_path))


async def _realtime_day(tmp_path: Path) -> None:
    async with await _env(local=False) as env:
        start = await env.get_current_time()
        date = (start.astimezone() + dt.timedelta(days=1)).date()
        plan = DayPlan(persona="aberg", date=date.isoformat(), items=[
            note("09:00"), note("09:10", interrupt=True), note("10:00"),
            PlanItem(at="12:00", template="break", minutes=30), note("08:55", interrupt=True),
        ])  # fmt: skip
        log = tmp_path / "day.jsonl"
        calls: list[str] = []

        def sleep(s: float) -> None:
            calls.append(f"sleep {s:g}")

        day = _day(FlakyRunner, log, calls, "realtime", sleep=sleep, now=lambda: dt.datetime(9999, 1, 1))
        done = await durable.play(env.client, day, plan, "realtime", tmp_path / "desk.lock")
        assert [(d.at, d.status) for d in done] == [("08:55", "done"), ("09:10", "done"), ("09:00", "done"),
                                                    ("10:00", "done"), ("12:00", "failed")]  # fmt: skip
        # Finished slots are not redone by the retry; waits and the break are workflow timers, not sleeps.
        assert calls == ["normalize", "run note 0855", "normalize", "run note 0900", "run note 0910", "normalize",
                         "run note 1000", "normalize", "run note 1000", "normalize", "lock", "unlock"]  # fmt: skip
        noon = dt.datetime.combine(date, dt.time(12, 30)).astimezone()
        assert await env.get_current_time() >= noon  # the clock waited for the day: 12:00 plus the 30-minute break

        again: list[str] = []
        assert await durable.play(env.client, _day(FakeRunner, log, again, "realtime"), plan, "realtime",
                                  tmp_path / "desk.lock") == done  # fmt: skip
        assert again == []  # read back, nothing re-run
        events = log.read_text(encoding="utf-8")
        assert events.count('"day_start"') == 1 and events.count('"day_end"') == 1
        assert events.count('"item_retry"') == 1 and '"attempt": 2' in events  # the repeat is in ground truth


class Recorded(list[str]):
    """Calls written through to a file, so the test can see what a killed process did."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def append(self, x: str) -> None:
        super().append(x)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(x + "\n")


class StuckRunner(FakeRunner):
    def run(self, task: Task, between: Any = None) -> Outcome:
        if task.inst.params["file_name"] == "note 1000":
            self.calls.append("run note 1000")
            threading.Event().wait()  # stuck in the task until the process is killed
        return super().run(task, between)


PLAN_3 = DayPlan(persona="lnord", date="2026-11-02", items=[note("09:00"), note("10:00"), note("11:00")])


def controller_a(address: str, tmp: str) -> None:
    """Entry point of the controller process that gets killed (run by the test in a subprocess)."""
    calls = Recorded(Path(tmp) / "calls_a.txt")
    durable.run(address, _day(StuckRunner, Path(tmp) / "day.jsonl", calls, "compressed"), PLAN_3, "compressed",
                Path(tmp) / "desk.lock", heartbeat_s=2, retry_s=1)  # fmt: skip


def test_killed_controller_resumes_the_day_and_desktop_has_one_controller(tmp_path: Path) -> None:
    asyncio.run(_killed(tmp_path))


async def _killed(tmp_path: Path) -> None:
    async with await _env(local=True) as env:
        code = f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r}); import test_durable as t; " \
               f"t.controller_a({env.client.service_client.config.target_host!r}, {str(tmp_path)!r})"  # fmt: skip
        a = subprocess.Popen([sys.executable, "-c", code])
        marker, log = tmp_path / "calls_a.txt", tmp_path / "day.jsonl"
        second: list[str] = []
        try:
            for _ in range(600):
                if marker.exists() and "run note 1000" in marker.read_text(encoding="utf-8"):
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("controller A never reached the 10:00 task")
            # While A lives, a second controller for the same desktop refuses to start.
            with pytest.raises(durable.DeskBusy):
                await durable.play(env.client, _day(FakeRunner, log, second, "compressed"), PLAN_3, "compressed",
                                   tmp_path / "desk.lock", heartbeat_s=2, retry_s=1)  # fmt: skip
        finally:
            a.kill()  # mid-task: no completion, no failure report, no more heartbeats
            a.wait()
        done = await durable.play(env.client, _day(FakeRunner, log, second, "compressed"), PLAN_3, "compressed",
                                  tmp_path / "desk.lock", heartbeat_s=2, retry_s=1)  # fmt: skip
        assert marker.read_text(encoding="utf-8").splitlines() == ["normalize", "run note 0900", "normalize",
                                                                   "run note 1000"]  # fmt: skip
        assert second == ["normalize", "run note 1000", "normalize", "run note 1100"]  # 09:00 is not redone
        assert [(d.at, d.status) for d in done] == [("09:00", "done"), ("10:00", "done"), ("11:00", "done")]
        events = log.read_text(encoding="utf-8")
        assert events.count('"day_start"') == 1 and events.count('"day_end"') == 1
        assert '"item_retry"' in events  # 10:00 ran twice: at-least-once, and ground truth says so


class HungRunner(FakeRunner):
    """The first 10:00 attempt hangs while still pressing keys (no steps, so no ground-truth writes)."""

    def __init__(self, log: GroundTruth, calls: list[str]) -> None:
        super().__init__(log, calls)
        self.presses: list[tuple[int, float]] = []
        self.attempts = 0

    def run(self, task: Task, between: Any = None) -> Outcome:
        if task.inst.params["file_name"] == "note 1000":
            self.attempts += 1
            attempt = self.attempts
            self.calls.append(f"start 1000 attempt {attempt}")
            try:
                end = time.monotonic() + (30 if attempt == 1 else 0.3)
                while time.monotonic() < end:
                    self.presses.append((attempt, time.monotonic()))
                    time.sleep(0.05)
            except CancelledError:
                self.calls.append(f"cancelled attempt {attempt}")
                raise
            self.calls.append(f"end 1000 attempt {attempt}")
        return super().run(task, between)


def test_hung_attempt_is_stopped_before_its_retry_types(tmp_path: Path) -> None:
    asyncio.run(_hung(tmp_path))


async def _hung(tmp_path: Path) -> None:
    async with await _env(local=True) as env:
        calls: list[str] = []
        day = _day(HungRunner, tmp_path / "day.jsonl", calls, "compressed")
        started = time.monotonic()
        done = await durable.play(
            env.client, day, PLAN_3, "compressed", tmp_path / "desk.lock", heartbeat_s=2, retry_s=1
        )
        runner: HungRunner = day.runner  # type: ignore[assignment]
        assert [(d.at, d.status) for d in done] == [("09:00", "done"), ("10:00", "done"), ("11:00", "done")]
        assert "cancelled attempt 1" in calls and "end 1000 attempt 1" not in calls  # timed out and stopped
        assert time.monotonic() - started < 25  # not left to run its 30 s
        first = [t for a, t in runner.presses if a == 1]
        second = [t for a, t in runner.presses if a == 2]
        assert first and second and max(first) < min(second)  # never both attempts on the desktop at once


class BusyRunner(FakeRunner):
    """The 10:00 task keeps making steps (so it heartbeats) until it is cancelled."""

    def run(self, task: Task, between: Any = None) -> Outcome:
        if task.inst.params["file_name"] == "note 1000":
            self.calls.append("start 1000")
            try:
                for n in range(600):
                    self.log.write(event="step", n=n)
                    time.sleep(0.05)
            except CancelledError:
                self.calls.append("cancelled 1000")
                raise
        return super().run(task, between)


def test_cancelled_time_jump_day_resets_the_clock_after_the_task_stops(tmp_path: Path) -> None:
    asyncio.run(_cancelled(tmp_path))


async def _cancelled(tmp_path: Path) -> None:
    async with await _env(local=True) as env:
        calls: list[str] = []

        def set_clock(t: dt.datetime | None) -> None:
            calls.append(f"clock {t:%H:%M}" if t else "clock reset")

        day = _day(BusyRunner, tmp_path / "day.jsonl", calls, "time-jump", set_clock=set_clock)
        running = asyncio.create_task(
            durable.play(env.client, day, PLAN_3, "time-jump", tmp_path / "desk.lock", heartbeat_s=2, retry_s=1)
        )
        for _ in range(300):
            if "start 1000" in calls:
                break
            await asyncio.sleep(0.1)
        await env.client.get_workflow_handle(durable.workflow_id(PLAN_3)).cancel()
        with pytest.raises(RuntimeError, match="failed"):
            await running
        assert calls == ["clock 09:00", "normalize", "run note 0900", "clock 10:00", "normalize", "start 1000",
                         "cancelled 1000", "clock reset"]  # fmt: skip
        assert '"clock_reset"' in (tmp_path / "day.jsonl").read_text(encoding="utf-8")


class CanaryRunner(FakeRunner):
    """Notepad's UI has drifted: its tasks fail. Everything else works."""

    def run(self, task: Task, between: Any = None) -> Outcome:
        self.calls.append(f"run {task.template.task} {next(iter(task.inst.params.values()))}")
        return Outcome("failed" if task.template.app.lower() == "notepad" else "done", 1)


def test_day_starts_with_its_canaries_and_skips_drifted_apps(tmp_path: Path) -> None:
    asyncio.run(_canary_day(tmp_path))


async def _canary_day(tmp_path: Path) -> None:
    from ludius import canary

    async with await _env(local=False) as env:
        admission = tmp_path / "admission.yaml"
        record = "{admitted: true, admitted_variants: [0]}"
        admission.write_text(f"notepad_write_note: {record}\nexplorer_new_folder: {record}\n", encoding="utf-8")
        plan = DayPlan(persona="aberg", date="2026-11-03", items=[
            note("09:00"), PlanItem(at="10:00", template="explorer_new_folder", params={"folder_name": "q4 drafts"}),
        ])  # fmt: skip
        calls: list[str] = []
        day = _day(CanaryRunner, tmp_path / "day.jsonl", calls, "compressed")
        health = tmp_path / "health.json"

        def check(apps: set[str]) -> dict[str, str]:
            return canary.run(day.templates, admission, day.runner, day.session, health, apps, report=lambda _: None)

        done = await durable.play(env.client, day, plan, "compressed", tmp_path / "desk.lock", canary=check)
        assert [(d.at, d.template, d.status) for d in done] == [
            ("09:00", "notepad_write_note", "skipped_drift"), ("10:00", "explorer_new_folder", "done")]  # fmt: skip
        # Canaries first, by app (notepad twice: one retry before drift), then only the explorer item of the day.
        assert [c for c in calls if c.startswith("run")] == [
            "run explorer_new_folder admission test folder", "run notepad_write_note admission test note",
            "run notepad_write_note admission test note", "run explorer_new_folder q4 drafts"]  # fmt: skip
        events = (tmp_path / "day.jsonl").read_text(encoding="utf-8")
        assert '"skipped_drift"' in events and events.count('"canary"') == 2
        assert canary.drifted(health) == {"notepad"}
