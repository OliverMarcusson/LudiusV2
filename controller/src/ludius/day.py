"""Day runner (spec sections 6 and 7): plays a day plan on its logical timeline through a clock adapter.

Realtime waits for each item's wall-clock time (delivery). Compressed runs items back to back (testing).
Time-jump is compressed plus the guest clock set to each regular item's logical time (testing only).
Interrupt items form the task stack: they are pushed onto the task running in their slot at its next
subgoal boundary (realtime: once their time has come), run to completion, and the task resumes.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from ludius import keys
from ludius.loop import Clock, GroundTruth, Outcome, Runner
from ludius.planner import BREAK, DayPlan, PlanItem
from ludius.session import Session
from ludius.tasks import Task, Template


def run_task(
    runner: Runner, session: Session, task: Task, between: Callable[[], None] | None = None, hygiene: bool = True
) -> Outcome:
    """One task with session hygiene around it and its side effects logged.

    Interruptions run without hygiene: normalizing would close the interrupted task's window.
    """
    if hygiene and not session.normalize():
        session.escalate()
    apps_before = [f"{a.title} ({a.process_name})" for a in runner.observe().apps]
    out = runner.run(task, between)
    apps_after = [f"{a.title} ({a.process_name})" for a in runner.observe().apps]
    runner.log.write(event="side_effects", host_ms=_now(), task=task.template.task,
                     opened=[a for a in apps_after if a not in apps_before], left=apps_after)  # fmt: skip
    if hygiene and out.status != "done" and not session.normalize():
        session.escalate()
    return out


def slots(plan: DayPlan) -> list[tuple[PlanItem, list[PlanItem]]]:
    """The day's slots in logical-time order: each regular item with the interrupts due while it runs.

    An interrupt before the first regular item has no task to cut into: it is a slot of its own at its time.
    """
    items = sorted(plan.items, key=lambda i: i.at)
    regular = [i for i in items if not i.interrupt]
    pending = [i for i in items if i.interrupt]
    out: list[tuple[PlanItem, list[PlanItem]]] = []
    for n, item in enumerate(regular):
        slot_end = regular[n + 1].at if n + 1 < len(regular) else "24:00"
        mine = [i for i in pending if item.at <= i.at < slot_end]
        pending = [i for i in pending if i not in mine]
        out.append((item, mine))
    return sorted(out + [(orphan, []) for orphan in pending], key=lambda s: s[0].at)


class BreakUnlockFailed(RuntimeError):
    """Back from a break, the screen did not unlock: nothing later in the day may act on the desktop."""


@dataclass
class ItemResult:
    item: PlanItem
    status: str


class DayRunner:
    def __init__(
        self,
        runner: Runner,
        session: Session,
        templates: dict[str, Template],
        log: GroundTruth,
        clock: Clock,
        seed: int,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], dt.datetime] = dt.datetime.now,
        set_clock: Callable[[dt.datetime | None], None] | None = None,  # None resets to host time
    ) -> None:
        self.runner = runner
        self.session = session
        self.templates = templates
        self.log = log
        self.clock: Clock = clock
        self.rng = random.Random(seed)
        self.sleep = sleep
        self.now = now
        self.set_clock = set_clock
        self.results: list[ItemResult] = []

    def run(self, plan: DayPlan) -> list[ItemResult]:
        day = dt.date.fromisoformat(plan.date)
        self.start(plan)
        try:
            for item, interrupts in slots(plan):
                self._wait_for(day, item.at)
                self.slot(day, item, interrupts)
        except BreakUnlockFailed:  # the day stops: no day_end, and the range goes back on host time
            if self.clock == "time-jump" and self.set_clock:
                self.set_clock(None)
            raise
        self.finish(plan, self.results)
        return self.results

    def start(self, plan: DayPlan) -> None:
        self.log.write(event="day_start", host_ms=_now(), persona=plan.persona, date=plan.date, clock=self.clock,
                       items=len(plan.items))  # fmt: skip

    def finish(self, plan: DayPlan, results: list[ItemResult]) -> None:
        if self.clock == "time-jump" and self.set_clock:
            self.set_clock(None)
        self.log.write(event="day_end", host_ms=_now(), persona=plan.persona, date=plan.date,
                       done=sum(r.status == "done" for r in results), items=len(results))  # fmt: skip

    def slot(
        self, day: dt.date, item: PlanItem, interrupts: list[PlanItem], jump_at: str | None = None
    ) -> list[ItemResult]:
        """One slot: the item at its logical time (or `jump_at`), then the interrupts it had no subgoal boundary
        left for. Time-jump sets the clock every slot: a resumed day can't know its clock survived (clocklease)."""
        first = len(self.results)
        if item.template != BREAK:  # a break sets its own clock, at both ends
            self._jump(day, jump_at or item.at)
        self._item(item, day, interrupts)
        for late in interrupts:
            self._item(late, day, [])
        return self.results[first:]

    def _item(self, item: PlanItem, day: dt.date, interrupts: list[PlanItem], nested: bool = False) -> None:
        if item.template == BREAK:
            self.begin_break(day, item)
            self.sleep(self.away_s(item))
            if self.end_break(day, item).status != "done":
                raise BreakUnlockFailed(f"break at {item.at}: unlock failed, day stopped")
            return
        self._started(day, item)
        task = Task(self.templates[item.template], item, self.rng)

        def between() -> None:
            due = [i for i in interrupts if self.clock != "realtime" or self._clock_reached(day, i.at)]
            for i in due:
                interrupts.remove(i)
                self.log.write(event="interrupt", host_ms=_now(), task=item.template, by=i.template, at=i.at)
                self._item(i, day, [], nested=True)
                self.log.write(event="resume", host_ms=_now(), task=item.template)

        status = run_task(self.runner, self.session, task, between, hygiene=not nested).status
        self._ended(day, item, status)

    def _started(self, day: dt.date, item: PlanItem) -> None:
        self.log.write(event="item_start", host_ms=_now(), logical=f"{day.isoformat()}T{item.at}", at=item.at,
                       template=item.template, context=item.context, interrupt=item.interrupt)  # fmt: skip

    def _ended(self, day: dt.date, item: PlanItem, status: str) -> ItemResult:
        self.results.append(ItemResult(item, status))
        self.log.write(event="item_end", host_ms=_now(), logical=f"{day.isoformat()}T{item.at}", at=item.at,
                       template=item.template, context=item.context, params=item.params, status=status)  # fmt: skip
        return self.results[-1]

    def away_s(self, item: PlanItem) -> int:
        return item.minutes * 60 if self.clock == "realtime" else 5

    def begin_break(self, day: dt.date, item: PlanItem) -> None:
        """Away from the desk: lock the screen (a real lock logon event); end_break comes back and unlocks."""
        self._jump(day, item.at)
        self._started(day, item)
        if not self.session.normalize():
            self.session.escalate()
        self.runner.agent.chord(keys.chord("Win+L"))
        self.log.write(event="break", host_ms=_now(), minutes=item.minutes, away_s=self.away_s(item))

    def end_break(self, day: dt.date, item: PlanItem) -> ItemResult:
        # Back at the desk at the break's logical end; set first, so a clock reset while away can't leak into the
        # unlock (a set_clock failure fails the break: no acting on an unknown clock).
        self._jump(day, f"{back_at(day, item):%H:%M}")
        return self._ended(day, item, "done" if self.session.unlock() else "failed")

    def _jump(self, day: dt.date, at: str) -> None:
        if self.clock != "time-jump" or not self.set_clock:
            return
        logical = dt.datetime.combine(day, dt.time.fromisoformat(at))
        self.set_clock(logical)
        self.log.write(event="clock_set", host_ms=_now(), logical=logical.isoformat(timespec="seconds"))

    def _clock_reached(self, day: dt.date, at: str) -> bool:
        return self.now() >= dt.datetime.combine(day, dt.time.fromisoformat(at))

    def _wait_for(self, day: dt.date, at: str) -> None:
        if self.clock != "realtime":
            return
        # Late items run at once (a late start); early ones wait, with a minute or two of human slack.
        target = dt.datetime.combine(day, dt.time.fromisoformat(at)) + dt.timedelta(seconds=self.rng.uniform(0, 120))
        while (left := (target - self.now()).total_seconds()) > 0:
            self.sleep(min(left, 60))


def back_at(day: dt.date, brk: PlanItem) -> dt.datetime:
    """A break's logical end: when the persona is back at the desk."""
    return dt.datetime.combine(day, dt.time.fromisoformat(brk.at)) + dt.timedelta(minutes=brk.minutes)


def _now() -> int:
    return int(time.time() * 1000)
