from __future__ import annotations

import contextlib
import json
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ludius import keys, requests
from ludius.assist import Assist
from ludius.calls import CapReached
from ludius.decisions import Decision, DecisionError
from ludius.options import (
    REASON_OTHER_FIELD,
    Option,
    StepView,
    build_options,
    build_state,
    commits_unguarded,
    describe,
    field_of_another_step,
    launch_target,
    search_target,
    view,
)
from ludius.policy import OVERWRITE_REASON, Policy, alt_f4_safe, overwrite_prompt
from ludius.reply import Composer, prepare_answer, prepare_file, prepare_read
from ludius.tasks import (
    LAUNCH_TEXT,
    RESERVED_RECIPIENT,
    Done,
    Persona,
    Subgoal,
    Task,
    added_baseline,
    check_done,
    declared_commits,
    file_baseline,
    guard_matches,
    mail_baseline,
    pixels_window,
    preconditions,
    profile,
    recipient_reserved,
    window_matches,
)
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

STUCK = "stuck"
FRONT_MOVED = "the foreground window changed before the action"
MAX_DETOURS = 2
STUCK_WAITS = 6

Clock = Literal["realtime", "compressed", "time-jump"]
DONE_THRESHOLD = 0.85
OFF_TRACK_THRESHOLD = 0.8
NEAR_TIE = 0.05
BLOCKED_WAIT_S = 1.5
SLOW_KEY_WAIT_S = 4.0
WAIT_CAP_S = 90.0
LAUNCH_REASON = "the launch query goes into the Start search box only"
REPLACE_REASON = "replacing text needs a target field that holds the keyboard focus"
NARRATIVE = requests.ROOT / "world" / "narrative.yaml"


class Agent(Protocol):
    vm: str

    def move(self, x: int, y: int, duration_ms: int = 0) -> int: ...
    def click(self, button: int = ..., count: int = 1) -> int: ...
    def chord(self, vks: list[int]) -> int: ...
    def type(self, text: str, mean_ms: int, jitter_ms: int) -> int: ...
    def scroll(self, notches: int) -> int: ...
    def ctrl_alt_del(self) -> int: ...
    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult: ...
    def screenshot(self) -> bytes: ...


class Decider(Protocol):
    def decide(
        self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True, image: bytes | None = None
    ) -> Decision: ...


class GroundTruth:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
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
    return (
        tuple((w.title, w.kind) for w in s.windows),
        next((e.key for e in s.elements if e.focused), ""),
        tuple((e.key, e.value, e.selected, e.expanded) for e in s.elements),
    )


def _replace_field(e: obs.Element, task: Task, sg: Subgoal) -> bool:
    field = sg.field or (sg.done.element.name if sg.done and sg.done.element and sg.done.element.name else "")
    return re.search(task.fmt_re(field), e.name) is not None


class Runner:
    def __init__(
        self,
        agent: Agent,
        decider: Decider,
        persona: Persona,
        log: GroundTruth,
        *,
        clock: Clock = "compressed",
        policy: Policy | None = None,
        seed: int | None = None,
        assist: Assist | None = None,
        composer: Composer | None = None,
    ) -> None:
        self.agent = agent
        self.composer = composer
        self.assist = assist
        self.decider = decider
        self.persona = persona
        self.log = log
        self.clock: Clock = clock
        self.policy = policy or Policy()
        self.rng = random.Random(seed)
        self.snap: obs.Snapshot = obs.Snapshot()
        self.display: tuple[int, int, int] | None = None
        self.mapping_ok = True
        self._steps = 0
        self.image = os.environ.get("LUDIUS_DECISION_IMAGE") == "1"

    def _refocus(self, task: Task, name: str, sg: Subgoal, snap: obs.Snapshot) -> None:
        loose_sg = sg.model_copy(update={"require": [c.model_copy(update={"focused": None}) for c in sg.require]})
        if preconditions(loose_sg, snap, task):
            return
        fg = next((i for i, w in enumerate(snap.windows) if w.kind == obs.Window.FOREGROUND), None)
        for c in sg.require:
            if not c.focused:
                continue
            loose = c.model_copy(update={"focused": None})
            hits = [e for e in snap.elements if e.window == fg and not e.focused and guard_matches(e, loose, task)
                    and not self.policy.element_allowed(e, sg.allow_destructive, snap)]  # fmt: skip
            if len(hits) == 1:
                self._move_to(hits[0])
                self.agent.click()
                self._caret_to_end(hits[0], sg)
                self.log.write(event="refocus", host_ms=_now(), task=task.template.task, subgoal=name,
                               element=describe(hits[0]))  # fmt: skip
                return

    def _shot(self, here: tuple[Any, ...]) -> bytes | None:
        try:
            shot = self.agent.screenshot()
            moved = fingerprint(self.agent.observe(wait_settle=False).snapshot) != here
        except Exception:
            return None
        return None if moved else shot

    def _decider_says_done(self, task: Task, name: str, question: str, snap: obs.Snapshot) -> bool:
        shot = self._shot(fingerprint(snap))
        if shot is None:
            return False
        q = task.fmt(question)
        try:
            options = {"yes": f"Yes: {q}", "no": "No, or it can't be seen on the screen."}
            d = self.decider.decide({"question": q}, options, ask_progress=False, image=shot)
        except CapReached:
            raise
        except Exception as e:  # noqa: BLE001  fail closed: no answer is no
            self.log.write(event="decider_check", host_ms=_now(), task=task.template.task, subgoal=name,
                           error=str(e)[:300])  # fmt: skip
            return False
        yes = d.probabilities.get("yes", 0.0)
        self.log.write(event="decider_check", host_ms=_now(), task=task.template.task, subgoal=name, yes=round(yes, 3),
                       cost=d.cost)  # fmt: skip
        return d.choice == "yes" and yes >= DONE_THRESHOLD

    def observe(self, timeout_ms: int = 5000) -> obs.Snapshot:
        self.snap = self.agent.observe(wait_settle=True, timeout_ms=timeout_ms).snapshot
        display = (self.snap.screen_width, self.snap.screen_height, self.snap.dpi)
        if not self.snap.locked and display[0] > 0 and display != self.display:
            if self.display is not None:
                self.mapping_ok = self.recheck_mapping(display)
            self.display = display
        return self.snap

    def recheck_mapping(self, display: tuple[int, int, int]) -> bool:
        w, h, _ = display
        misses: list[tuple[int, int, int, int]] = []
        for x, y in ((w // 2, h // 2), (w // 5, h // 4)):
            self.agent.move(x, y, 0)
            s = self.agent.observe(wait_settle=False).snapshot
            if abs(s.cursor_x - x) > 1 or abs(s.cursor_y - y) > 1:
                misses.append((x, y, s.cursor_x, s.cursor_y))
        self.log.write(event="mapping_recheck", host_ms=_now(), display=list(display), ok=not misses, misses=misses)
        return not misses

    def think(self) -> None:
        if self.clock == "realtime":
            time.sleep(min(20.0, self.rng.lognormvariate(0, 0.6) * self.persona.think_mean_s))

    def _point(self, e: obs.Element) -> tuple[int, int]:
        b = e.bounds
        x = b.x + int(b.width * min(0.85, max(0.15, self.rng.gauss(0.5, 0.12))))
        y = b.y + int(b.height * min(0.8, max(0.2, self.rng.gauss(0.5, 0.1))))
        return max(1, x), max(1, y)

    def _move_to(self, e: obs.Element) -> None:
        x, y = self._point(e)
        lo, hi = self.persona.pointer_ms
        self.agent.move(x, y, self.rng.randint(lo, hi) if self.clock == "realtime" else self.rng.randint(60, 140))

    def _type(self, text: str) -> None:
        mean, jitter = self.persona.typing_mean_ms, self.persona.typing_jitter_ms
        if self.clock == "compressed":
            mean, jitter = 25, 10
        self.agent.type(text, mean, jitter)

    def _caret_to_end(self, e: obs.Element, sg: Subgoal | None = None) -> None:
        if sg is not None and sg.caret == "start":
            if e.role == "document" and not e.read_only:
                self.agent.chord(keys.chord("Ctrl+Home"))
        elif e.role == "document" and not e.read_only and e.value:
            self.agent.chord(keys.chord("Ctrl+End"))

    def act(self, o: Option, task: Task, sg: Subgoal | None = None) -> str | None:
        e = o.element
        acts = o.kind in ("click", "double_click", "type", "key")
        if sg is not None and sg.confirm and acts and o.keys not in sg.keys:
            return "only this step's confirmed keys may act here (a Paste button would skip the confirm question)"
        if o.kind in ("click", "double_click") and e is not None:
            self._move_to(e)
            if self._front_moved():  # checked last, right before the input: a popup since the decision
                return FRONT_MOVED  # (Outlook Reminders) would get it
            self.agent.click(count=2 if o.kind == "double_click" else 1)
            if o.kind == "click":
                self._caret_to_end(e, sg)
        elif o.kind == "type":
            text = o.literal or task.values[o.content]
            if sg is None:
                return "typing needs its subgoal"
            why, e = self._type_target(o, task, sg, text)
            if why:
                return why
            if e is not None and (not e.focused or self._focus_shared):
                self._move_to(e)
                self.agent.click()
                time.sleep(0.15)
                why, e = self._type_target(o, task, sg, text)
                if why:
                    return why
                if e is None or not e.focused:
                    return "the focusing click did not put the keyboard focus in the target field"
                self._caret_to_end(e, sg)
            if sg.caret == "append" and e is not None and e.role == "document":
                self.agent.chord(keys.chord("Ctrl+End"))
            if sg.replace and e is not None and _replace_field(e, task, sg) or sg.text == LAUNCH_TEXT:
                self.agent.chord(keys.chord("Ctrl+A"))
            self._type(text)
        elif o.kind == "key":
            if sg is not None and any(c.keys.lower() == o.keys.lower() for c in task.template.key_commits):
                fresh = self.agent.observe(wait_settle=False).snapshot
                if commits_unguarded(o.keys, next((x for x in fresh.elements if x.focused), None), task, sg):
                    return f"{o.keys} commits in the focused field: only the guarded send step may, in its field"
            if sg is not None and sg.confirm and o.keys in sg.keys:
                fresh = self.agent.observe(wait_settle=True).snapshot
                if not self._decider_says_done(task, "confirm", sg.confirm, fresh):
                    return f"the Decider did not confirm: {task.fmt(sg.confirm)}"
            if self._front_moved():  # after the confirm question too: Alt+S in Reminders snoozes
                return FRONT_MOVED
            self.agent.chord(keys.chord(o.keys))
        elif o.kind == "scroll":
            target = next((x for x in self.snap.elements if "list continues" in " ".join(x.hints)), None)
            target = target or next((x for x in self.snap.elements if x.role == "document"), None)
            if target is not None:
                self._move_to(target)
            self.agent.scroll(o.notches)
        return None

    def _front_moved(self) -> bool:
        was = next((w for w in self.snap.windows if w.kind == obs.Window.FOREGROUND), None)
        if was is None:
            return False
        fresh = self.agent.observe(wait_settle=False).snapshot
        now = next((w for w in fresh.windows if w.kind == obs.Window.FOREGROUND), None)
        return now is None or _window_id(now) != _window_id(was)

    def _type_target(self, o: Option, task: Task, sg: Subgoal, text: str) -> tuple[str | None, obs.Element | None]:
        fresh = self.agent.observe(wait_settle=False).snapshot
        self._focus_shared = sum(e.focused for e in fresh.elements) > 1
        v = view(fresh, task, sg, self.policy)
        fg = next((i for i, w in enumerate(fresh.windows) if w.kind == obs.Window.FOREGROUND), None)
        was = next((w for w in self.snap.windows if w.kind == obs.Window.FOREGROUND), None)
        if fg is None or was is None or _window_id(fresh.windows[fg]) != _window_id(was) or fg not in v.windows:
            return FRONT_MOVED, None
        allowed = {e.key: e for e in v.allowed}
        if o.element is None:
            focus = next((e for e in fresh.elements if e.focused), None)
            if focus is not None and (focus.key not in allowed or self.policy.text_allowed(text, focus)):
                return "the keyboard focus is not a field this task may type into", None
            if field_of_another_step(focus, task, sg):
                return REASON_OTHER_FIELD, None
            if sg.text == LAUNCH_TEXT and not launch_target(focus, fresh):
                return LAUNCH_REASON, None
            if sg.replace and focus is not None:
                if self._focus_shared or not _replace_field(focus, task, sg):
                    return REPLACE_REASON, None
                return None, focus
            if sg.caret == "append":
                if focus is None or focus.role != "document" or self._focus_shared:
                    return "text is appended only at the end of the focused document", None
                return None, focus
            return None, None
        target = allowed.get(o.element.key)
        if target is None or (target.role, target.name) != (o.element.role, o.element.name):
            return "the target field changed before typing", None
        if r := self.policy.text_allowed(text, target):
            return r, None
        if field_of_another_step(target, task, sg):
            return REASON_OTHER_FIELD, None
        if not sg.text and not sg.copy_text and not search_target(target, fresh, task):
            return "template text goes into an edit field of the task's foreground app only", None
        if sg.text == LAUNCH_TEXT and not launch_target(target, fresh):
            return LAUNCH_REASON, None
        return None, target

    def run(self, task: Task, between: Callable[[], None] | None = None) -> Outcome:
        self._steps, done = 0, list[str]()
        try:
            return self._run(task, between, done)
        except CapReached as e:
            self.log.write(event="budget_spent", host_ms=_now(), task=task.template.task, error=str(e)[:300])
            self.abandon(task, "budget spent")
            self.log.write(event="task_end", host_ms=_now(), task=task.template.task, status="abandoned",
                           reason="budget spent", steps=self._steps)  # fmt: skip
            return Outcome("abandoned", self._steps, "budget spent", done)

    def _run(self, task: Task, between: Callable[[], None] | None, done: list[str]) -> Outcome:
        t0 = time.monotonic()
        steps = 0
        task.owner, task.tz = self.persona.user, self.persona.tz
        task.values["persona_name"] = self.persona.name
        task.started_ms = self.observe().guest_time_ms or int(time.time() * 1000)
        self.log.write(
            event="task_start",
            host_ms=_now(),
            persona=self.persona.user,
            vm=self.agent.vm,
            task=task.template.task,
            variant=task.variant,
            subgoals=task.subgoal_names,
        )
        why = None
        if task.template.capability == "mail.reply":
            why, rec = (self.composer.prepare(task, self.persona, self.decider, self.policy) if self.composer
                        else ("no reply composer (LLM) configured", {}))  # fmt: skip
            self.log.write(event="task_content", host_ms=_now(), task=task.template.task, reason=why, **rec)
        if task.template.capability == "mail.read":
            why, rec = prepare_read(task, self.policy, self.composer.narrative if self.composer else NARRATIVE)
            self.log.write(event="task_content", host_ms=_now(), task=task.template.task, reason=why, **rec)
        if task.template.capability == "mail.file":
            why, rec = prepare_file(task, self.policy, self.composer.narrative if self.composer else NARRATIVE)
            self.log.write(event="task_content", host_ms=_now(), task=task.template.task, reason=why, **rec)
        if task.template.capability == "calendar.respond":
            why, rec = prepare_answer(task, self.policy, self.composer.narrative if self.composer else NARRATIVE)
            self.log.write(event="task_content", host_ms=_now(), task=task.template.task, reason=why, **rec)
        if (task.template.sends_mail or task.template.capability == "calendar.respond") and not why:
            task.mail_before = mail_baseline(task)
        if why := why or requests.waiting(task) or file_baseline(task, self.policy):
            self.log.write(event="blocked", host_ms=_now(), task=task.template.task, reason=why)
            self.log.write(event="task_end", host_ms=_now(), task=task.template.task, status="abandoned",
                           reason=f"start: {why}", steps=0)  # fmt: skip
            return Outcome("abandoned", 0, f"start: {why}", done)
        for name in task.subgoal_names:
            sg = task.subgoal(name)
            carry: dict[str, Any] = {}
            result = self._subgoal(task, name, sg, budget=min(sg.steps, task.template.budget_steps - steps), t0=t0,
                                   may_detour=self.assist is not None, carry=carry)  # fmt: skip
            steps = self._steps = steps + result[1]
            detours = 0
            while (result[0] != "done" and (result[2] == "step budget" or result[2].startswith(STUCK))
                   and self.assist is not None and detours < MAX_DETOURS
                   and task.template.budget_steps - steps > 0):  # fmt: skip
                detours += 1
                steps = self._steps = steps + self._assisted(task, name, sg, t0, steps, result[2])
                left = task.template.budget_steps - steps
                result = self._subgoal(task, name, sg, budget=min(sg.steps // 2 + 2, left), t0=t0,
                                       may_detour=detours < MAX_DETOURS, carry=carry)  # fmt: skip
                steps = self._steps = steps + result[1]
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
            if between is not None and name != task.subgoal_names[-1]:
                paused = time.monotonic()
                between()
                t0 += time.monotonic() - paused
        reason, status = "", "done"
        if task.inst.request:
            ok, reason = requests.fulfilled(task)
            self.log.write(event="request_check", host_ms=_now(), task=task.template.task,
                           request=task.inst.request, fulfilled=ok, reason=reason,
                           capability=task.template.capability)  # fmt: skip
            if ok and task.template.capability == "file.write":
                with contextlib.suppress(OSError):  # unrecorded only blocks the delivery
                    requests.remember(requests.ROOT, task.owner, task.inst.request, requests.WRITTEN)
            reason = f"request: {reason}"
            status = "done" if ok else "failed"
        self.log.write(event="task_end", host_ms=_now(), task=task.template.task, status=status, steps=steps,
                       reason=reason)  # fmt: skip
        return Outcome(status, steps, reason, done)

    def _assisted(self, task: Task, name: str, sg: Subgoal, t0: float, steps: int, why: str) -> int:
        assert self.assist is not None
        v = view(self.observe(), task, sg, self.policy)
        why = why if why.startswith(STUCK) else f"{STUCK}: {why}"
        proposal = self.assist.next_subgoal(build_state(v, task, sg, [], why))
        self.log.write(event="llm_subgoal", host_ms=_now(), task=task.template.task, subgoal=name, goal=proposal.goal,
                       why=why)  # fmt: skip
        goal = proposal.goal.replace("{", "{{").replace("}", "}}")
        helper = Subgoal(goal=goal, launch=sg.launch, steps=6, done=Done(ask=f"Is this done on the screen: {goal}"))
        budget = min(helper.steps, task.template.budget_steps - steps)
        return self._subgoal(task, f"{name}/detour", helper, budget=budget, t0=t0)[1]

    def _subgoal(
        self,
        task: Task,
        name: str,
        sg: Subgoal,
        budget: int,
        t0: float,
        may_detour: bool = False,
        carry: dict[str, Any] | None = None,
    ) -> tuple[str, int, str]:
        history: list[str] = []
        verifications = 0
        last_result = "start of subgoal"
        no_change = 0
        typed_ok = pressed_ok = clicked_ok = False
        last_click: obs.Element | None = None
        recovering = False
        dead: dict[tuple[Any, ...], set[tuple[str, str]]] = {}
        snap = self.observe()
        if carry:
            typed_ok, pressed_ok, clicked_ok, last_click, task.added_base = carry["state"]
        else:
            added_baseline(sg.done, snap, task)
            if carry is not None:
                carry["state"] = (typed_ok, pressed_ok, clicked_ok, last_click, task.added_base)
        seen = {fingerprint(snap)}
        guarded = False
        escs = ladders = blocks = 0
        sat: list[str] = []
        shot_why: str | None = None
        step = 0
        waited, wait_why = 0.0, ""
        while step < budget:
            step += 1
            if time.monotonic() - t0 > task.template.budget_minutes * 60:
                return "failed", step - 1, "time budget"
            if waited > WAIT_CAP_S:
                return "failed", step - 1, f"waited too long: {wait_why}"
            if snap.locked:
                return "failed", step - 1, "session locked"
            if not self.mapping_ok:
                return "failed", step - 1, "pointer mapping drift after a display change"
            if snap.secure_desktop:
                self.agent.chord(keys.chord("Esc"))
                self.log.write(event="blocked", host_ms=_now(), task=task.template.task, subgoal=name,
                               reason="elevation required")  # fmt: skip
                return "failed", step - 1, "blocked: elevation required"
            code_done = check_done(
                sg.done, snap, task, typed_ok, pressed_ok=pressed_ok, clicked_ok=clicked_ok, last_click=last_click
            )
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
            if code_done is False and check_done(
                sg.done,
                snap,
                task,
                typed_ok,
                outside=False,
                pressed_ok=pressed_ok,
                clicked_ok=clicked_ok,
                last_click=last_click,
            ):
                self.log.write(event="wait", host_ms=_now(), task=task.template.task, subgoal=name, step=step,
                               reason="done on screen, waiting for the outside check")  # fmt: skip
                w0 = time.monotonic()
                time.sleep(BLOCKED_WAIT_S)
                snap = self.observe(timeout_ms=10000)
                step, waited, wait_why = step - 1, waited + time.monotonic() - w0, "the outside check"
                continue
            ask = sg.done.ask if sg.done else None
            if ask and self._decider_says_done(task, name, ask, snap):
                self.log.write(event="subgoal_done", host_ms=_now(), guest_ms=snap.guest_time_ms,
                               task=task.template.task, subgoal=name, by="decider", steps=step - 1)  # fmt: skip
                return "done", step - 1, ""
            if recipient_reserved(sg, task, self.policy):
                self.log.write(event="blocked", host_ms=_now(), task=task.template.task, subgoal=name,
                               reason=RESERVED_RECIPIENT)  # fmt: skip
                return "failed", step - 1, RESERVED_RECIPIENT
            if overwrite_prompt(snap):
                self.log.write(event="blocked", host_ms=_now(), task=task.template.task, subgoal=name,
                               reason=OVERWRITE_REASON)  # fmt: skip
                return "failed", step - 1, OVERWRITE_REASON
            blocked = preconditions(sg, snap, task)
            popup = self._popup_over(sg, snap, task) if blocked else None
            if popup is None:
                sat = []
            else:
                sat = [*sat, popup] if sat and sat[-1] == popup else [popup]
                if len(sat) % 3 == 0 and may_detour:
                    return "failed", step - 1, f"{STUCK}: '{popup}' is in front of the task window"
                if len(sat) % 3 == 0 and escs < 2:
                    escs += 1
                    self.agent.chord(keys.chord("Esc"))
                    self.log.write(event="dismiss", host_ms=_now(), task=task.template.task, subgoal=name, step=step,
                                   window=popup)  # fmt: skip
            blocks = blocks + 1 if blocked else 0
            if blocked and blocks >= STUCK_WAITS and may_detour:
                return "failed", step - 1, f"{STUCK}: {blocked}"
            if blocked:
                self.log.write(event="wait", host_ms=_now(), task=task.template.task, subgoal=name, step=step,
                               reason=blocked)  # fmt: skip
                w0 = time.monotonic()
                self._refocus(task, name, sg, snap)
                time.sleep(BLOCKED_WAIT_S)
                snap = self.observe(timeout_ms=10000)
                step, waited, wait_why = step - 1, waited + time.monotonic() - w0, blocked
                continue
            v = view(snap, task, sg, self.policy)
            options = self._recovery_options(v) if recovering else build_options(v, task, sg, self.policy)
            here = fingerprint(snap)
            options = [o for o in options if _ident(o) not in dead.get(here, ())] or options
            state = build_state(v, task, sg, history, last_result)
            choices = {o.key: o.text for o in options}
            image = image_reason(shot_why, snap, self.image)
            shot_why = None
            try:
                shot = self._shot(here) if image else None
                image = image if shot is not None else None
                decision = self.decider.decide(state, choices, ask_progress=not recovering, image=shot)
            except DecisionError as e:
                self.log.write(event="decision_error", host_ms=_now(), task=task.template.task, subgoal=name,
                               step=step, error=str(e)[:300])  # fmt: skip
                return "failed", step - 1, "decision error"
            chosen = self._choose(decision, options)

            decider_done = code_done is None and not ask and decision.subgoal_done >= DONE_THRESHOLD and not recovering
            if decider_done:
                if self.assist is not None and verifications < 2:
                    verifications += 1
                    verdict = self.assist.verify(state)
                    self.log.write(event="llm_verify", host_ms=_now(), task=task.template.task, subgoal=name,
                                   done=verdict.done, reason=verdict.reason)  # fmt: skip
                    if not verdict.done:
                        last_result = f"not done yet: {verdict.reason}"
                        decider_done = False
                if decider_done:
                    self.log.write(
                        event="subgoal_done",
                        host_ms=_now(),
                        guest_ms=snap.guest_time_ms,
                        task=task.template.task,
                        subgoal=name,
                        by="decision",
                        p=decision.subgoal_done,
                        steps=step - 1,
                    )
                    return "done", step - 1, ""
            if not recovering and decision.off_track >= OFF_TRACK_THRESHOLD:
                recovering = True
                last_result = "screen looked unrelated to the goal"
                self._log_step(task, name, snap, chosen, decision, "off_track -> recovery", step, options=options,
                               code_done=code_done, image=image)  # fmt: skip
                continue
            reason = self._post_check(chosen, sg, task)
            if reason:
                self._log_step(task, name, snap, chosen, decision, f"blocked: {reason}", step, options=options,
                               code_done=code_done, image=image)  # fmt: skip
                last_result = f"that action is not allowed ({reason})"
                shot_why = "refused"
                continue

            self.think()
            host_ms, w0 = _now(), time.monotonic()
            refused = self.act(chosen, task, sg)
            guarded = bool(sg.require) and not refused and chosen.kind != "rescan"
            snap = self.observe(timeout_ms=10000 if chosen.kind == "rescan" else 5000)
            if chosen.kind == "rescan":
                step, waited, wait_why = step - 1, waited + time.monotonic() - w0, "the screen to load"
            slow_window = sg.done is not None and bool(sg.done.window or sg.done.foreground)
            if (chosen.kind == "key" and not refused and chosen.keys in sg.keys
                    and (fingerprint(snap) == here or slow_window and not check_done(
                        sg.done, snap, task, typed_ok, outside=False, pressed_ok=pressed_ok,
                        clicked_ok=clicked_ok, last_click=last_click))):  # fmt: skip
                time.sleep(SLOW_KEY_WAIT_S)
                snap = self.observe(timeout_ms=10000)
            after = fingerprint(snap)
            changed, revisit = after != here, after in seen
            seen.add(after)
            if chosen.kind == "type":
                typed_ok = not refused and _typed_landed(snap, chosen, sg)
            pressed_ok = pressed_ok or (guarded and chosen.kind == "key" and chosen.keys in sg.keys)
            clicked_ok = clicked_ok or (sg.in_window is not None and not refused and chosen.kind == "click")
            if chosen.kind != "rescan":
                last_click = chosen.element if chosen.kind == "click" and not refused else None
            if carry is not None:
                carry["state"] = (typed_ok, pressed_ok, clicked_ok, last_click, task.added_base)
            outcome = "back to an earlier screen" if changed and revisit else "changed" if changed else "no change"
            last_result = "no visible change" if not changed else outcome
            if refused:
                outcome, last_result = f"not typed: {refused}", f"not typed: {refused}"
            if not snap.settled:
                last_result += " (screen still busy)"
            history.append(f"{_short(chosen)} -> {outcome}")
            self._log_step(task, name, snap, chosen, decision, last_result, step, host_ms=host_ms, options=options,
                           check=(sg, typed_ok), code_done=code_done, image=image)  # fmt: skip
            shot_why = "refused" if refused else "no_change" if not changed else None
            recovering = False

            progress = changed and not revisit
            no_change = 0 if progress else no_change + 1
            if not progress and chosen.kind != "rescan" and refused != FRONT_MOVED:
                dead.setdefault(here, set()).add(_ident(chosen))
            if no_change == 2:
                snap = self.observe(timeout_ms=10000)
                self.log.write(event="ladder", step=1, action="rescan", host_ms=_now(), task=task.template.task)
            elif no_change >= 3:
                ladders += 1
                if may_detour and ladders >= 2:
                    return "failed", step, f"{STUCK}: no progress, even after in-app recovery"
                recovering = True
                no_change = 0
                self.log.write(
                    event="ladder", step=2, action="in-app recovery", host_ms=_now(), task=task.template.task
                )
        return "failed", budget, "step budget"

    def _choose(self, d: Decision, options: list[Option]) -> Option:
        by_key = {o.key: o for o in options}
        top = by_key[d.choice]
        p_top = d.probabilities.get(d.choice, 0.0)
        ties = [o for k, p in d.probabilities.items() if (o := by_key.get(k)) is not None
                and p >= p_top - NEAR_TIE and _equivalent(o, top)]  # fmt: skip
        return self.rng.choice(ties) if len(ties) > 1 else top

    def _post_check(self, o: Option, sg: Subgoal, task: Task) -> str | None:
        if o.kind != "rescan" and recipient_reserved(sg, task, self.policy):
            return RESERVED_RECIPIENT
        if o.kind != "rescan" and (r := preconditions(sg, self.snap, task, o.element)):
            return r
        if o.element is not None:
            r = self.policy.element_allowed(o.element, sg.allow_destructive, self.snap)
            if r:
                return r
        if o.kind == "type":
            if overwrite_prompt(self.snap):
                return OVERWRITE_REASON
            if not sg.text and not sg.copy_text and not search_target(o.element, self.snap, task):
                return "template text goes into an edit field of the task's foreground app only"
            focus = next((e for e in self.snap.elements if e.focused), None)
            if sg.text == LAUNCH_TEXT and not launch_target(o.element or focus, self.snap):
                return LAUNCH_REASON
            return self.policy.text_allowed(o.literal or task.values[o.content], o.element)
        if o.kind == "key":
            return self.policy.key_allowed(o.keys, self.snap, declared_commits(task, sg))
        return None

    @staticmethod
    def _popup_over(sg: Subgoal, snap: obs.Snapshot, task: Task) -> str | None:
        fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
        if sg.in_window is None or fg is None or window_matches(fg, sg.in_window, task):
            return None
        return fg.title if fg.process_name.lower() in {p.lower() for p in task.template.processes} else None

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
        self.log.write(
            event="ladder",
            step=3,
            action=task.template.on_abandon,
            reason=reason,
            host_ms=_now(),
            task=task.template.task,
        )
        apps = {p.lower() for p in task.template.processes}
        words = profile(task.template.app).dialogs
        close = profile(task.template.app).keys["close"]
        keyed: tuple[Any, ...] | None = None
        for _ in range(6):
            snap = self.observe()
            fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
            fgi = next((i for i, w in enumerate(snap.windows) if w is fg), None)
            discard = next(
                (e for e in snap.elements
                 if re.fullmatch("|".join(words.discard), e.name.replace("&", ""), re.I)),
                None,
            )  # fmt: skip
            if discard is not None and discard.role == "radio button" and discard.selected:
                discard = next((e for e in snap.elements if e.window == discard.window and e.role == "button"
                                and e.name.replace("&", "") == words.choice_ok), None)  # fmt: skip
            if discard is None and keyed == fingerprint(snap):
                leave = "|".join(words.leave)
                discard = next((e for e in snap.elements if e.window == fgi and e.role == "button"
                                and re.fullmatch(leave, e.name.replace("&", ""), re.I)), None)  # fmt: skip
                if discard is None:
                    break
            keyed = None
            if discard is not None:
                self._move_to(discard)
                self.agent.click()
            elif fg is not None and fg.process_name.lower() in apps and alt_f4_safe(fg):
                self.agent.chord(keys.chord("Esc") if fg.kind != obs.Window.FOREGROUND else keys.chord(close))
                keyed = fingerprint(snap)
            else:
                return
            time.sleep(0.3)
        self.log.write(event="ladder", step=4, action="restart app (operator)", host_ms=_now(), task=task.template.task)

    def _log_step(
        self,
        task: Task,
        sg: str,
        snap: obs.Snapshot,
        o: Option,
        d: Decision,
        result: str,
        step: int,
        host_ms: int = 0,
        options: list[Option] | None = None,
        check: tuple[Subgoal, bool] | None = None,
        code_done: bool | None = None,
        image: str | None = None,
    ) -> None:
        fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
        pixels = fg is not None and pixels_window(fg)
        offered = (
            {"clicks": sum(x.kind == "click" for x in options), **{x.key: x.kind for x in options if x.kind != "click"}}
            if pixels and options
            else None
        )
        filename = _filename_diag(snap, task, *check) if pixels and check else None
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
            **({"trust": "pixels"} if pixels else {}),
            **({"offered": offered} if offered else {}),
            **({"filename": filename} if filename else {}),
            element={"id": e.id, "key": e.key, "role": e.role, "name": e.name} if e else None,
            action=o.kind,
            option=o.text,
            decision={
                "choice": d.choice,
                "confidence": d.confidence,
                "top": d.top(3),
                "subgoal_done": d.subgoal_done,
                "off_track": d.off_track,
                "cost": d.cost,
                "model": d.model,
                "image": image,
                "latency_ms": d.latency_ms,
                "retries_429": d.retries_429,
            },
            code_done=code_done,
            result=result,
        )


def image_reason(why: str | None, snap: obs.Snapshot, forced: bool = False) -> str | None:
    fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
    unreadable = fg is not None and (fg.no_tree or fg.elevated_unreadable or fg.unresponsive or pixels_window(fg))
    return why or ("unreadable" if unreadable else "forced" if forced else None)


def _window_id(w: obs.Window) -> tuple[Any, ...]:
    return w.process_name.lower(), w.class_name, w.kind, "" if pixels_window(w) else w.title


def _typed_landed(snap: obs.Snapshot, o: Option, sg: Subgoal) -> bool:
    if o.element is None or (sg.unreadable_ok and "value" not in o.element.actions and not o.element.value):
        return True
    target = next((e for e in snap.elements if e.key == o.element.key), None)
    return target is not None and target.value != o.element.value


FILENAME_ROWS = 6
FILENAME_TEXT = 80
_MASKED = re.compile(r"[\s*•●∙·]+")


def _filename_diag(snap: obs.Snapshot, task: Task, sg: Subgoal, typed_ok: bool) -> dict[str, Any] | None:
    pats = sg.done.pixels.present if sg.done and sg.done.pixels else []
    label_rx = next((task.pixels_re(p) for p in pats if p.startswith("^Filename:")), None)
    if label_rx is None or not any("{file_name" in p for p in pats) or not any(map(pixels_window, snap.windows)):
        return None
    rows = [e for e in snap.elements if e.window < len(snap.windows) and pixels_window(snap.windows[e.window])]
    labels = [e for e in rows if re.search(label_rx, re.sub(r"\s+", "", e.name), re.I)]
    out: dict[str, Any] = {
        "verdict": check_done(sg.done, snap, task, typed_ok),
        "typed_ok": typed_ok,
        "rows": len(rows),
        "labels": len(labels),
        "label": None,
        "geometry_only": True,
    }

    def shown(e: obs.Element) -> dict[str, Any]:
        b = e.bounds
        return {"name": e.name[:FILENAME_TEXT], "x": b.x, "y": b.y, "w": b.width, "h": b.height}

    if labels:
        lab = labels[0]
        b = lab.bounds
        near = sorted(
            (e for e in rows
             if e is not lab and e.name.strip() and not e.password and not _MASKED.fullmatch(e.name)
             and abs(e.bounds.y + e.bounds.height / 2 - (b.y + b.height / 2)) <= b.height
             and e.bounds.x + e.bounds.width > b.x + b.width),
            key=lambda e: (e.bounds.x, e.bounds.y),
        )  # fmt: skip
        out["label"] = shown(lab) if not lab.password else None
        out["candidates"] = [shown(e) for e in near[:FILENAME_ROWS]]
        out["candidates_dropped"] = max(0, len(near) - FILENAME_ROWS)
    return out


def _equivalent(a: Option, b: Option) -> bool:
    if a.kind != b.kind or a.element is None or b.element is None or a.kind not in ("click", "double_click"):
        return a is b
    name = re.sub(r"[&.\s]+", " ", a.element.name).strip().lower()
    return bool(name) and name == re.sub(r"[&.\s]+", " ", b.element.name).strip().lower()


def _ident(o: Option) -> tuple[str, str]:
    return o.kind, o.element.key if o.element is not None else o.text


def _short(o: Option) -> str:
    return o.text if len(o.text) <= 90 else o.text[:90] + "…"


def _now() -> int:
    return int(time.time() * 1000)
