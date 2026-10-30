from __future__ import annotations

import datetime as dt
import random
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ludius import calls, keys
from ludius.loop import Clock, GroundTruth, Outcome, Runner
from ludius.planner import BREAK, DayPlan, PlanItem
from ludius.session import EscalationDenied, Session
from ludius.tasks import Task, Template, write_paths

if TYPE_CHECKING:
    from ludius.react import Reactor


def run_task(
    runner: Runner,
    session: Session,
    task: Task,
    between: Callable[[], None] | None = None,
    hygiene: bool = True,
    keep: set[str] | None = None,
) -> Outcome:
    if hygiene and not session.normalize(keep):
        try:
            session.escalate()
        except EscalationDenied:
            reason = "desktop not clean, escalation denied"
            runner.log.write(event="task_end", host_ms=_now(), task=task.template.task, status="abandoned",
                             reason=reason, steps=0)  # fmt: skip
            return Outcome("abandoned", 0, reason)
    apps_before = [f"{a.title} ({a.process_name})" for a in runner.observe().apps]
    out = runner.run(task, between)
    apps_after = [f"{a.title} ({a.process_name})" for a in runner.observe().apps]
    runner.log.write(event="side_effects", host_ms=_now(), task=task.template.task,
                     opened=[a for a in apps_after if a not in apps_before], left=apps_after)  # fmt: skip
    if hygiene and out.status != "done" and not session.normalize(keep):
        with suppress(EscalationDenied):
            session.escalate()
    return out


def slots(plan: DayPlan) -> list[tuple[PlanItem, list[PlanItem]]]:
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
    pass


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
        set_clock: Callable[[dt.datetime | None], None] | None = None,
        reactor: Reactor | None = None,
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
        self.reactor = reactor

    def run(self, plan: DayPlan) -> list[ItemResult]:
        day = dt.date.fromisoformat(plan.date)
        self.start(plan)
        try:
            for item, interrupts in slots(plan):
                self._wait_for(day, item.at)
                self.slot(day, item, interrupts)
        except BreakUnlockFailed:
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
        with calls.on_day(day):
            return self._slot(day, item, interrupts, jump_at)

    def _slot(
        self, day: dt.date, item: PlanItem, interrupts: list[PlanItem], jump_at: str | None = None
    ) -> list[ItemResult]:
        first = len(self.results)
        if item.template != BREAK:
            self._jump(day, jump_at or item.at)
            if self.reactor:
                self.reactor.react(day.isoformat(), item.at, lambda it: self._reaction(day, it))
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

        mine = {w.casefold() for w in write_paths(task)}

        def writes(i: PlanItem) -> set[str]:
            return {w.casefold() for w in write_paths(Task(self.templates[i.template], i, random.Random(0)))}

        def between() -> None:
            due = [i for i in interrupts if (self.clock != "realtime" or self._clock_reached(day, i.at))
                   and (i.template == BREAK or not writes(i) & mine)]  # fmt: skip
            for i in due:
                interrupts.remove(i)
                self.log.write(event="interrupt", host_ms=_now(), task=item.template, by=i.template, at=i.at)
                self._item(i, day, [], nested=True)
                self.log.write(event="resume", host_ms=_now(), task=item.template)

        status = run_task(self.runner, self.session, task, between, hygiene=not nested).status
        self._ended(day, item, status)

    def _reaction(self, day: dt.date, item: PlanItem) -> str:
        logical = f"{day.isoformat()}T{item.at}"
        self.log.write(event="reaction_start", host_ms=_now(), logical=logical, template=item.template,
                       context=item.context, params=item.params)  # fmt: skip
        chat = set(self.templates["chat_read_messages"].processes)
        status = run_task(
            self.runner, self.session, Task(self.templates[item.template], item, self.rng), keep=chat
        ).status
        self.log.write(event="reaction_end", host_ms=_now(), logical=logical, template=item.template,
                       params=item.params, status=status)  # fmt: skip
        return status

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
        self._jump(day, item.at)
        self._started(day, item)
        if not self.session.normalize():
            with suppress(EscalationDenied):
                self.session.escalate()
        self.runner.agent.chord(keys.chord("Win+L"))
        self.log.write(event="break", host_ms=_now(), minutes=item.minutes, away_s=self.away_s(item))

    def end_break(self, day: dt.date, item: PlanItem) -> ItemResult:
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
        target = dt.datetime.combine(day, dt.time.fromisoformat(at)) + dt.timedelta(seconds=self.rng.uniform(0, 120))
        while (left := (target - self.now()).total_seconds()) > 0:
            self.sleep(min(left, 60))


def back_at(day: dt.date, brk: PlanItem) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.fromisoformat(brk.at)) + dt.timedelta(minutes=brk.minutes)


def _now() -> int:
    return int(time.time() * 1000)
