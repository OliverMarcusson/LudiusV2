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
        items = sorted(plan.items, key=lambda i: i.at)
        regular = [i for i in items if not i.interrupt]
        pending = [i for i in items if i.interrupt]
        self.log.write(event="day_start", host_ms=_now(), persona=plan.persona, date=plan.date, clock=self.clock,
                       items=len(items))  # fmt: skip
        for n, item in enumerate(regular):
            slot_end = regular[n + 1].at if n + 1 < len(regular) else "24:00"
            mine = [i for i in pending if item.at <= i.at < slot_end]
            pending = [i for i in pending if i not in mine]
            self._wait_for(day, item.at)
            self._jump(day, item.at)
            self._item(item, day, mine)
            for late in mine:  # the task had no boundary left: run them after it
                self._item(late, day, [])
        for late in pending:  # interrupts before the first regular item
            self._item(late, day, [])
        if self.clock == "time-jump" and self.set_clock:
            self.set_clock(None)
        self.log.write(event="day_end", host_ms=_now(), persona=plan.persona, date=plan.date,
                       done=sum(r.status == "done" for r in self.results), items=len(self.results))  # fmt: skip
        return self.results

    def _item(self, item: PlanItem, day: dt.date, interrupts: list[PlanItem], nested: bool = False) -> None:
        logical = f"{day.isoformat()}T{item.at}"
        self.log.write(event="item_start", host_ms=_now(), logical=logical, at=item.at, template=item.template,
                       context=item.context, interrupt=item.interrupt)  # fmt: skip
        if item.template == BREAK:
            status = self._break(item)
        else:
            task = Task(self.templates[item.template], item, self.rng)

            def between() -> None:
                due = [i for i in interrupts if self.clock != "realtime" or self._clock_reached(day, i.at)]
                for i in due:
                    interrupts.remove(i)
                    self.log.write(event="interrupt", host_ms=_now(), task=item.template, by=i.template, at=i.at)
                    self._item(i, day, [], nested=True)
                    self.log.write(event="resume", host_ms=_now(), task=item.template)

            status = run_task(self.runner, self.session, task, between, hygiene=not nested).status
        self.results.append(ItemResult(item, status))
        self.log.write(event="item_end", host_ms=_now(), logical=logical, at=item.at, template=item.template,
                       context=item.context, params=item.params, status=status)  # fmt: skip

    def _break(self, item: PlanItem) -> str:
        """Away from the desk: lock the screen, come back, unlock (real lock/unlock logon events)."""
        if not self.session.normalize():
            self.session.escalate()
        self.runner.agent.chord(keys.chord("Win+L"))
        away = item.minutes * 60 if self.clock == "realtime" else 5
        self.log.write(event="break", host_ms=_now(), minutes=item.minutes, away_s=away)
        self.sleep(away)
        return "done" if self.session.unlock() else "failed"

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


def _now() -> int:
    return int(time.time() * 1000)
