"""The controller step loop: observe, ask Jev, enforce policy, act, verify.

Failure ladder steps 1-3 (rescan, in-app recovery, human-like abandonment) live here.
Steps 4+ (restart app ... mark VM unhealthy) are logged as escalations for the operator.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ludius import keys
from ludius.jev import Decision
from ludius.options import Option, StepView, build_options, build_state, describe, view
from ludius.policy import Policy
from ludius.tasks import Persona, Subgoal, Task, check_done
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

Clock = Literal["realtime", "compressed"]
DONE_THRESHOLD = 0.85
OFF_TRACK_THRESHOLD = 0.8


class Agent(Protocol):
    vm: str

    def move(self, x: int, y: int, duration_ms: int = 0) -> int: ...
    def click(self, button: int = ..., count: int = 1) -> int: ...
    def chord(self, vks: list[int]) -> int: ...
    def type(self, text: str, mean_ms: int, jitter_ms: int) -> int: ...
    def scroll(self, notches: int) -> int: ...
    def ctrl_alt_del(self) -> int: ...
    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult: ...


class Decider(Protocol):
    def decide(self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True) -> Decision: ...


class GroundTruth:
    """Append-only JSONL: every step with host and guest time, persona, window, element, action, result, task."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8")

    def write(self, **rec: Any) -> None:
        self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


@dataclass
class Outcome:
    status: Literal["done", "abandoned", "failed"]
    steps: int
    reason: str = ""
    subgoals_done: list[str] = field(default_factory=list[str])


def fingerprint(s: obs.Snapshot) -> tuple[Any, ...]:
    """What counts as a visible change: windows, focus and element names/values."""
    return (
        tuple((w.title, w.kind) for w in s.windows),
        next((e.key for e in s.elements if e.focused), ""),
        tuple((e.key, e.value, e.selected, e.expanded) for e in s.elements),
    )


class Runner:
    def __init__(
        self,
        agent: Agent,
        jev: Decider,
        persona: Persona,
        log: GroundTruth,
        *,
        clock: Clock = "compressed",
        policy: Policy | None = None,
        seed: int | None = None,
    ) -> None:
        self.agent = agent
        self.jev = jev
        self.persona = persona
        self.log = log
        self.clock: Clock = clock
        self.policy = policy or Policy()
        self.rng = random.Random(seed)
        self.snap: obs.Snapshot = obs.Snapshot()

    # Observation and pacing -------------------------------------------------------

    def observe(self, timeout_ms: int = 5000) -> obs.Snapshot:
        self.snap = self.agent.observe(wait_settle=True, timeout_ms=timeout_ms).snapshot
        return self.snap

    def think(self) -> None:
        if self.clock == "realtime":
            time.sleep(min(20.0, self.rng.lognormvariate(0, 0.6) * self.persona.think_mean_s))

    # Acting -----------------------------------------------------------------------

    def _point(self, e: obs.Element) -> tuple[int, int]:
        b = e.bounds
        # Inside the box, biased to its middle, never on the edge.
        x = b.x + int(b.width * min(0.85, max(0.15, self.rng.gauss(0.5, 0.12))))
        y = b.y + int(b.height * min(0.8, max(0.2, self.rng.gauss(0.5, 0.1))))
        return max(1, x), max(1, y)  # Hyper-V ignores an absolute (0, 0)

    def _move_to(self, e: obs.Element) -> None:
        x, y = self._point(e)
        lo, hi = self.persona.pointer_ms
        self.agent.move(x, y, self.rng.randint(lo, hi) if self.clock == "realtime" else self.rng.randint(60, 140))

    def _type(self, text: str) -> None:
        mean, jitter = self.persona.typing_mean_ms, self.persona.typing_jitter_ms
        if self.clock == "compressed":
            mean, jitter = 25, 10
        self.agent.type(text, mean, jitter)

    def act(self, o: Option, task: Task) -> None:
        e = o.element
        if o.kind in ("click", "double_click") and e is not None:
            self._move_to(e)
            self.agent.click(count=2 if o.kind == "double_click" else 1)
        elif o.kind == "type":
            text = task.values[o.content]
            if e is not None and not e.focused:
                self._move_to(e)
                self.agent.click()
                time.sleep(0.15)
            self._type(text)
        elif o.kind == "key":
            self.agent.chord(keys.chord(o.keys))
        elif o.kind == "scroll":
            target = next((x for x in self.snap.elements if "list continues" in " ".join(x.hints)), None)
            target = target or next((x for x in self.snap.elements if x.role == "document"), None)
            if target is not None:
                self._move_to(target)
            self.agent.scroll(o.notches)
        # rescan: no input

    # The loop ---------------------------------------------------------------------

    def run(self, task: Task) -> Outcome:
        t0 = time.monotonic()
        steps = 0
        done: list[str] = []
        self.log.write(
            event="task_start",
            host_ms=_now(),
            persona=self.persona.user,
            vm=self.agent.vm,
            task=task.template.task,
            variant=task.variant,
            subgoals=task.subgoal_names,
        )
        for name in task.subgoal_names:
            sg = task.subgoal(name)
            result = self._subgoal(task, name, sg, budget=min(sg.steps, task.template.budget_steps - steps), t0=t0)
            steps += result[1]
            if result[0] != "done":
                reason = result[2]
                self.abandon(task, reason)
                out = Outcome("abandoned", steps, f"{name}: {reason}", done)
                self.log.write(
                    event="task_end",
                    host_ms=_now(),
                    task=task.template.task,
                    status=out.status,
                    reason=out.reason,
                    steps=steps,
                )
                return out
            done.append(name)
        self.log.write(event="task_end", host_ms=_now(), task=task.template.task, status="done", steps=steps)
        return Outcome("done", steps, "", done)

    def _subgoal(self, task: Task, name: str, sg: Subgoal, budget: int, t0: float) -> tuple[str, int, str]:
        history: list[str] = []
        last_result = "start of subgoal"
        no_change = 0
        typed_ok = False
        recovering = False
        snap = self.observe()
        for step in range(1, budget + 1):
            if time.monotonic() - t0 > task.template.budget_minutes * 60:
                return "failed", step - 1, "time budget"
            if snap.locked:
                return "failed", step - 1, "session locked"
            code_done = check_done(sg.done, snap, task, typed_ok)
            if code_done:
                self.log.write(
                    event="subgoal_done",
                    host_ms=_now(),
                    guest_ms=snap.guest_time_ms,
                    task=task.template.task,
                    subgoal=name,
                    by="code",
                    steps=step - 1,
                )
                return "done", step - 1, ""
            v = view(snap, task, sg, self.policy)
            options = self._recovery_options(v) if recovering else build_options(v, task, sg, self.policy)
            state = build_state(v, task, sg, history, last_result)
            decision = self.jev.decide(state, {o.key: o.text for o in options}, ask_progress=not recovering)
            chosen = self._choose(decision, options)

            if code_done is None and decision.subgoal_done >= DONE_THRESHOLD and not recovering:
                self.log.write(
                    event="subgoal_done",
                    host_ms=_now(),
                    guest_ms=snap.guest_time_ms,
                    task=task.template.task,
                    subgoal=name,
                    by="jev",
                    p=decision.subgoal_done,
                    steps=step - 1,
                )
                return "done", step - 1, ""
            if not recovering and decision.off_track >= OFF_TRACK_THRESHOLD:
                # Ladder step 2: in-app recovery with a restricted choice next step.
                recovering = True
                last_result = "screen looked unrelated to the goal"
                self._log_step(task, name, snap, chosen, decision, "off_track -> recovery", step)
                continue
            reason = self._post_check(chosen, sg, task)
            if reason:
                self._log_step(task, name, snap, chosen, decision, f"blocked: {reason}", step)
                last_result = f"that action is not allowed ({reason})"
                continue

            self.think()
            before = fingerprint(snap)
            host_ms = _now()
            self.act(chosen, task)
            snap = self.observe(timeout_ms=10000 if chosen.kind == "rescan" else 5000)
            changed = fingerprint(snap) != before
            if chosen.kind == "type":
                typed_ok = _typed_landed(snap, chosen, task)
            last_result = "changed" if changed else "no visible change"
            if not snap.settled:
                last_result += " (screen still busy)"
            history.append(f"{_short(chosen)} -> {'changed' if changed else 'no change'}")
            self._log_step(task, name, snap, chosen, decision, last_result, step, host_ms=host_ms)
            recovering = False

            no_change = 0 if changed else no_change + 1
            if no_change == 2:
                # Ladder step 1: rescan with a longer settle before anything else.
                snap = self.observe(timeout_ms=10000)
                self.log.write(event="ladder", step=1, action="rescan", host_ms=_now(), task=task.template.task)
            elif no_change >= 3:
                recovering = True
                no_change = 0
                self.log.write(
                    event="ladder", step=2, action="in-app recovery", host_ms=_now(), task=task.template.task
                )
        return "failed", budget, "step budget"

    def _choose(self, d: Decision, options: list[Option]) -> Option:
        """Top option; variety comes from the planning layer, not step-level randomness."""
        by_key = {o.key: o for o in options}
        return by_key[d.choice]

    def _post_check(self, o: Option, sg: Subgoal, task: Task) -> str | None:
        """Defense in depth: re-check the chosen option against policy."""
        if o.element is not None:
            r = self.policy.element_allowed(o.element, sg.allow_destructive)
            if r:
                return r
        if o.kind == "type":
            return self.policy.text_allowed(task.values[o.content], o.element)
        return None

    def _recovery_options(self, v: StepView) -> list[Option]:
        opts = [
            Option("o1", "key", "press Esc", keys="Esc"),
            Option("o2", "key", "press Alt+Left (back)", keys="Alt+Left"),
        ]
        for e in v.allowed:
            if "closes dialog" in e.hints or e.name.lower() in ("close", "cancel", "back"):
                opts.append(Option(f"o{len(opts) + 1}", "click", f"click {describe(e)}", element=e))
        opts.append(Option(f"o{len(opts) + 1}", "rescan", "rescan: wait and look again"))
        return opts

    def abandon(self, task: Task, reason: str) -> None:
        """Ladder step 3: leave like a person would. Close the task window and discard, never crash out."""
        self.log.write(
            event="ladder",
            step=3,
            action=task.template.on_abandon,
            reason=reason,
            host_ms=_now(),
            task=task.template.task,
        )
        apps = {p.lower() for p in task.template.processes}
        for _ in range(4):
            snap = self.observe()
            fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
            discard = next(
                (e for e in snap.elements if re.fullmatch(r"(don'?t save|no|discard)", e.name.replace("&", ""), re.I)),
                None,
            )
            if discard is not None:
                self._move_to(discard)
                self.agent.click()
            elif fg is not None and fg.process_name.lower() in apps:
                self.agent.chord(keys.chord("Esc") if fg.kind != obs.Window.FOREGROUND else keys.chord("Alt+F4"))
            else:
                return
            time.sleep(0.3)
        self.log.write(event="ladder", step=4, action="restart app (operator)", host_ms=_now(), task=task.template.task)

    def _log_step(
        self, task: Task, sg: str, snap: obs.Snapshot, o: Option, d: Decision, result: str, step: int, host_ms: int = 0
    ) -> None:
        fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
        e = o.element
        self.log.write(
            event="step",
            host_ms=host_ms or _now(),
            guest_ms=snap.guest_time_ms,
            persona=self.persona.user,
            vm=self.agent.vm,
            task=task.template.task,
            subgoal=sg,
            step=step,
            window=fg.title if fg else "",
            process=fg.process_name if fg else "",
            element={"id": e.id, "key": e.key, "role": e.role, "name": e.name} if e else None,
            action=o.kind,
            option=o.text,
            jev={
                "choice": d.choice,
                "confidence": d.confidence,
                "top": d.top(3),
                "subgoal_done": d.subgoal_done,
                "off_track": d.off_track,
                "cost": d.cost,
            },
            result=result,
        )


def _typed_landed(snap: obs.Snapshot, o: Option, _task: Task) -> bool:
    if o.element is None:
        return True  # typed at the focus: the subgoal's own check decides
    target = next((e for e in snap.elements if e.key == o.element.key), None)
    return target is not None and target.value != o.element.value


def _short(o: Option) -> str:
    return o.text if len(o.text) <= 90 else o.text[:90] + "…"


def _now() -> int:
    return int(time.time() * 1000)
