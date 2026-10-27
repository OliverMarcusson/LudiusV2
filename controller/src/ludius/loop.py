"""The controller step loop: observe, ask Jev, enforce policy, act, verify.

Failure ladder steps 1-3 (rescan, in-app recovery, human-like abandonment) live here.
Steps 4+ (restart app ... mark VM unhealthy) are logged as escalations for the operator.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ludius import keys
from ludius.assist import Assist
from ludius.jev import Decision
from ludius.options import Option, StepView, build_options, build_state, describe, view
from ludius.policy import Policy, alt_f4_safe
from ludius.tasks import Persona, Subgoal, Task, check_done, pixels_window, preconditions
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

Clock = Literal["realtime", "compressed", "time-jump"]
DONE_THRESHOLD = 0.85
OFF_TRACK_THRESHOLD = 0.8
NEAR_TIE = 0.05  # probability gap within which equivalent options may be sampled
BLOCKED_WAIT_S = 1.5  # a step with no input allowed: wait this long, then look again (no Jev call)


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
        assist: Assist | None = None,
    ) -> None:
        self.agent = agent
        self.assist = assist  # rare LLM help for open-ended subgoals; None disables it
        self.jev = jev
        self.persona = persona
        self.log = log
        self.clock: Clock = clock
        self.policy = policy or Policy()
        self.rng = random.Random(seed)
        self.snap: obs.Snapshot = obs.Snapshot()
        self.display: tuple[int, int, int] | None = None  # (width, height, dpi) the pointer mapping was checked at
        self.mapping_ok = True

    # Observation and pacing -------------------------------------------------------

    def observe(self, timeout_ms: int = 5000) -> obs.Snapshot:
        self.snap = self.agent.observe(wait_settle=True, timeout_ms=timeout_ms).snapshot
        display = (self.snap.screen_width, self.snap.screen_height, self.snap.dpi)
        if not self.snap.locked and display[0] > 0 and display != self.display:
            if self.display is not None:
                self.mapping_ok = self.recheck_mapping(display)
            self.display = display
        return self.snap

    def recheck_mapping(self, display: tuple[int, int, int]) -> bool:
        """Spec section 4: after a resolution or scaling change, confirm a pointer move lands where intended."""
        w, h, _ = display
        misses: list[tuple[int, int, int, int]] = []
        for x, y in ((w // 2, h // 2), (w // 5, h // 4)):
            self.agent.move(x, y, 0)
            s = self.agent.observe(wait_settle=False).snapshot
            if abs(s.cursor_x - x) > 1 or abs(s.cursor_y - y) > 1:  # Hyper-V rounds by up to 1 px
                misses.append((x, y, s.cursor_x, s.cursor_y))
        self.log.write(event="mapping_recheck", host_ms=_now(), display=list(display), ok=not misses, misses=misses)
        return not misses

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

    def _caret_to_end(self, e: obs.Element, sg: Subgoal | None = None) -> None:
        # A click lands at a random spot, so in a text document it moved the caret into earlier text and the next
        # Enter split a heading (fidelity 20261027-1259). Writers append, so put the caret back at the end.
        # A reply is written above the quoted mail (caret: start): Outlook's Word body has no value, so a click in
        # the quote left the caret there (fidelity 20261027 reply-r6); Ctrl+Home puts it back at the top.
        if sg is not None and sg.caret == "start":
            if e.role == "document" and not e.read_only:
                self.agent.chord(keys.chord("Ctrl+Home"))
        elif e.role == "document" and not e.read_only and e.value:
            self.agent.chord(keys.chord("Ctrl+End"))

    def act(self, o: Option, task: Task, sg: Subgoal | None = None) -> None:
        e = o.element
        if o.kind in ("click", "double_click") and e is not None:
            self._move_to(e)
            self.agent.click(count=2 if o.kind == "double_click" else 1)
            if o.kind == "click":
                self._caret_to_end(e, sg)
        elif o.kind == "type":
            text = o.literal or task.values[o.content]
            if e is not None and not e.focused:
                self._move_to(e)
                self.agent.click()
                time.sleep(0.15)
                self._caret_to_end(e, sg)
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

    def run(self, task: Task, between: Callable[[], None] | None = None) -> Outcome:
        """`between` runs at each subgoal boundary: the task stack's hook for interruptions."""
        t0 = time.monotonic()
        steps = 0
        done: list[str] = []
        task.owner = self.persona.user
        task.started_ms = self.observe().guest_time_ms or int(time.time() * 1000)  # the guest's clock, like the range's
        self.log.write(
            event="task_start",
            host_ms=_now(),
            persona=self.persona.user,
            vm=self.agent.vm,
            task=task.template.task,
            variant=task.variant,
            subgoals=task.subgoal_names,
        )
        assisted = False
        for name in task.subgoal_names:
            sg = task.subgoal(name)
            result = self._subgoal(task, name, sg, budget=min(sg.steps, task.template.budget_steps - steps), t0=t0)
            steps += result[1]
            if result[0] != "done" and result[2] == "step budget" and sg.done is None and self.assist and not assisted:
                # Jev is stuck in an open-ended subgoal: one LLM call proposes an intermediate subgoal.
                assisted = True
                steps += self._assisted(task, name, sg, t0, steps)
                left = task.template.budget_steps - steps
                result = self._subgoal(task, name, sg, budget=min(sg.steps // 2 + 2, left), t0=t0)
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
            if between is not None and name != task.subgoal_names[-1]:
                paused = time.monotonic()
                between()
                t0 += time.monotonic() - paused  # an interruption does not use this task's time budget
        self.log.write(event="task_end", host_ms=_now(), task=task.template.task, status="done", steps=steps)
        return Outcome("done", steps, "", done)

    def _assisted(self, task: Task, name: str, sg: Subgoal, t0: float, steps: int) -> int:
        assert self.assist is not None
        v = view(self.observe(), task, sg, self.policy)
        proposal = self.assist.next_subgoal(build_state(v, task, sg, [], "stuck: step budget used up"))
        self.log.write(event="llm_subgoal", host_ms=_now(), task=task.template.task, subgoal=name, goal=proposal.goal)
        helper = Subgoal(goal=proposal.goal, keys=sg.keys, launch=sg.launch, text=sg.text,
                         allow_destructive=sg.allow_destructive, steps=6)  # fmt: skip
        budget = min(helper.steps, task.template.budget_steps - steps)
        return self._subgoal(task, f"{name}/assist", helper, budget=budget, t0=t0)[1]

    def _subgoal(self, task: Task, name: str, sg: Subgoal, budget: int, t0: float) -> tuple[str, int, str]:
        history: list[str] = []
        verifications = 0
        last_result = "start of subgoal"
        no_change = 0
        typed_ok = False
        recovering = False
        # Progress means reaching a screen not seen before in this subgoal. An action that changed nothing,
        # or only led back to an earlier screen (open/close toggles, dialog loops), is withdrawn on the screen
        # it was tried from, for the rest of the subgoal.
        dead: dict[tuple[Any, ...], set[tuple[str, str]]] = {}
        snap = self.observe()
        seen = {fingerprint(snap)}
        for step in range(1, budget + 1):
            if time.monotonic() - t0 > task.template.budget_minutes * 60:
                return "failed", step - 1, "time budget"
            if snap.locked:
                return "failed", step - 1, "session locked"
            if not self.mapping_ok:
                return "failed", step - 1, "pointer mapping drift after a display change"
            if snap.secure_desktop:
                # A UAC prompt: noise never elevates. Cancel it like a person would and give up the task.
                self.agent.chord(keys.chord("Esc"))
                self.log.write(event="blocked", host_ms=_now(), task=task.template.task, subgoal=name,
                               reason="elevation required")  # fmt: skip
                return "failed", step - 1, "blocked: elevation required"
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
            if blocked := preconditions(sg, snap, task):
                # No input is allowed (e.g. the mail is sent, its window closed, delivery still on its way): a Jev
                # call could only pick a blocked action. Wait and look again; the step budget still bounds it.
                self.log.write(event="wait", host_ms=_now(), task=task.template.task, subgoal=name, step=step,
                               reason=blocked)  # fmt: skip
                time.sleep(BLOCKED_WAIT_S)
                snap = self.observe(timeout_ms=10000)
                continue
            v = view(snap, task, sg, self.policy)
            options = self._recovery_options(v) if recovering else build_options(v, task, sg, self.policy)
            here = fingerprint(snap)
            options = [o for o in options if _ident(o) not in dead.get(here, ())] or options
            state = build_state(v, task, sg, history, last_result)
            decision = self.jev.decide(state, {o.key: o.text for o in options}, ask_progress=not recovering)
            chosen = self._choose(decision, options)

            jev_done = code_done is None and decision.subgoal_done >= DONE_THRESHOLD and not recovering
            if jev_done:
                if self.assist is not None and verifications < 2:
                    # Open-ended completion: the LLM confirms Jev's judgment (at most twice per subgoal).
                    verifications += 1
                    verdict = self.assist.verify(state)
                    self.log.write(event="llm_verify", host_ms=_now(), task=task.template.task, subgoal=name,
                                   done=verdict.done, reason=verdict.reason)  # fmt: skip
                    if not verdict.done:
                        last_result = f"not done yet: {verdict.reason}"
                        jev_done = False
                if jev_done:
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
                self._log_step(task, name, snap, chosen, decision, "off_track -> recovery", step, options=options)
                continue
            reason = self._post_check(chosen, sg, task)
            if reason:
                self._log_step(task, name, snap, chosen, decision, f"blocked: {reason}", step, options=options)
                last_result = f"that action is not allowed ({reason})"
                continue

            self.think()
            host_ms = _now()
            self.act(chosen, task, sg)
            snap = self.observe(timeout_ms=10000 if chosen.kind == "rescan" else 5000)
            after = fingerprint(snap)
            changed, revisit = after != here, after in seen
            seen.add(after)
            if chosen.kind == "type":
                typed_ok = _typed_landed(snap, chosen, sg)
            outcome = "back to an earlier screen" if changed and revisit else "changed" if changed else "no change"
            last_result = "no visible change" if not changed else outcome
            if not snap.settled:
                last_result += " (screen still busy)"
            history.append(f"{_short(chosen)} -> {outcome}")
            self._log_step(task, name, snap, chosen, decision, last_result, step, host_ms=host_ms, options=options,
                           check=(sg, typed_ok))  # fmt: skip
            recovering = False

            progress = changed and not revisit
            no_change = 0 if progress else no_change + 1
            if not progress and chosen.kind != "rescan":
                dead.setdefault(here, set()).add(_ident(chosen))
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
        """Top option. Small variation only among near-ties that code confirms are equivalent
        (two Save buttons, a menu item and a toolbar button with the same name); variety comes from planning."""
        by_key = {o.key: o for o in options}
        top = by_key[d.choice]
        p_top = d.probabilities.get(d.choice, 0.0)
        ties = [o for k, p in d.probabilities.items() if (o := by_key.get(k)) is not None
                and p >= p_top - NEAR_TIE and _equivalent(o, top)]  # fmt: skip
        return self.rng.choice(ties) if len(ties) > 1 else top

    def _post_check(self, o: Option, sg: Subgoal, task: Task) -> str | None:
        """Defense in depth: re-check the chosen option against policy and the subgoal's preconditions."""
        if o.kind != "rescan" and (r := preconditions(sg, self.snap, task, o.element)):
            return r
        if o.element is not None:
            r = self.policy.element_allowed(o.element, sg.allow_destructive)
            if r:
                return r
        if o.kind == "type":
            return self.policy.text_allowed(o.literal or task.values[o.content], o.element)
        if o.kind == "key":
            return self.policy.key_allowed(o.keys, self.snap)
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
            elif fg is not None and fg.process_name.lower() in apps and alt_f4_safe(fg):  # not the desktop
                self.agent.chord(keys.chord("Esc") if fg.kind != obs.Window.FOREGROUND else keys.chord("Alt+F4"))
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
    ) -> None:
        fg = next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)
        pixels = fg is not None and pixels_window(fg)
        # Pixels steps: which option keys were offered as what (kinds only, never option text or content), so
        # Jev's top-3 keys can be read back. Clicks are counted, not listed.
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


def _typed_landed(snap: obs.Snapshot, o: Option, sg: Subgoal) -> bool:
    if o.element is None or (sg.unreadable_ok and "value" not in o.element.actions and not o.element.value):
        # Typed at the focus, or into a control the template marks as having no readable value (Outlook's Word
        # body): no value can show the text landing, so the subgoal's own check decides.
        return True
    target = next((e for e in snap.elements if e.key == o.element.key), None)
    return target is not None and target.value != o.element.value


FILENAME_ROWS = 6  # candidate rows logged per step
FILENAME_TEXT = 80  # characters kept of a logged row
_MASKED = re.compile(r"[\s*•●∙·]+")


def _filename_diag(snap: obs.Snapshot, task: Task, sg: Subgoal, typed_ok: bool) -> dict[str, Any] | None:
    """Pixels steps of a file-name subgoal (its visible check wants a `^Filename:` label and a {file_name…} value):
    the check's verdict as the next step will see it, plus the rows beside the label, so a failed check can be
    told apart (typed elsewhere, appended text, merged or misread OCR) from the log alone.

    The candidates are geometry only: rows level with the label and reaching right of it. They say nothing about
    which control has the keyboard focus. Capped, named rows only, password and masked rows never logged.
    """
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
