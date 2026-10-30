from __future__ import annotations

import os
import re
import time
from collections import Counter
from collections.abc import Callable

from ludius import keys
from ludius.loop import Agent, GroundTruth
from ludius.policy import SHELL_PROCESSES, shell_dialog
from ludius.tasks import pixels_window
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

DISCARD = re.compile(r"(don'?t save|no|discard)", re.I)
DISMISS = re.compile(r"(cancel|close|ok|not now|no thanks|dismiss|got it)", re.I)


class VmUnhealthy(Exception):
    pass


class EscalationDenied(Exception):
    pass


class Session:
    def __init__(
        self,
        agent: Agent,
        log: GroundTruth,
        password: str,
        lifecycle: Callable[[int], str] | None = None,
    ) -> None:
        self.agent = agent
        self.log = log
        self.password = password
        self.lifecycle = lifecycle

    def observe(self, timeout_ms: int = 5000) -> obs.Snapshot:
        return self.agent.observe(wait_settle=True, timeout_ms=timeout_ms).snapshot

    def wait_ready(self, seconds: float = 180) -> obs.Snapshot:
        deadline = time.monotonic() + seconds
        while True:
            try:
                snap = self.observe(timeout_ms=2000)
                if not snap.locked:
                    return snap
            except Exception:
                if time.monotonic() > deadline:
                    raise
            if time.monotonic() > deadline:
                raise TimeoutError("desktop not ready")
            time.sleep(2)

    def unlock(self) -> bool:
        snap = self.observe()
        if not snap.locked:
            return True
        self.log.write(event="unlock", host_ms=_now())
        return self.sign_in()

    def sign_in(self) -> bool:
        for _ in range(2):
            self.agent.ctrl_alt_del()
            time.sleep(2.0)
            self.agent.type(self.password + "\n", 90, 40)
            try:
                self.wait_ready(30)
                self.log.write(event="unlocked", host_ms=_now())
                return True
            except Exception:
                self.agent.chord(keys.chord("Esc"))
                time.sleep(1)
        return False

    def normalize(self, keep: set[str] | None = None, rounds: int = 12) -> bool:
        keep = {k.lower() for k in keep or set()}
        closed: list[str] = []
        tried: Counter[tuple[str, str]] = Counter()
        for _ in range(rounds):
            if not self.unlock():
                return False
            snap = self.observe()
            if snap.secure_desktop:
                self.agent.chord(keys.chord("Esc"))
                closed.append("UAC prompt")
                time.sleep(1.0)
                continue
            fg = _fg(snap)
            if fg is not None and pixels_window(fg):
                if any(
                    e.framework_id == "visual" and re.fullmatch(r"Shut Down Windows", e.name) for e in snap.elements
                ):
                    self.agent.chord(keys.chord("Esc"))
                    time.sleep(0.6)
                    continue
                self.log.write(event="normalized", host_ms=_now(), closed=closed, trust="pixels", verified=False)
                return True
            popup = next(
                (
                    w
                    for i, w in enumerate(snap.windows)
                    if w.kind in (obs.Window.POPUP, obs.Window.OWNED)
                    and not _inert_shell_popup(snap, i)
                    and tried[(w.title, w.class_name)] < 2
                ),
                None,
            )
            strays = [
                a
                for a in snap.apps
                if a.process_name.lower() not in keep
                and (a.process_name.lower() not in SHELL_PROCESSES or _explorer_window(a))
            ]
            if (
                fg is not None
                and (_is_shell_surface(fg) or (fg.process_name.lower() in keep and fg.class_name != "#32770"))
                and popup is None
                and not strays
            ):
                self.log.write(event="normalized", host_ms=_now(), closed=closed)
                return True
            if popup is not None or (fg is not None and fg.class_name == "#32770"):
                if popup is not None:
                    tried[(popup.title, popup.class_name)] += 1
                target = None if shell_dialog(fg) else _button(snap, DISCARD) or _button(snap, DISMISS)
                if target is not None:
                    self._click(target)
                else:
                    self.agent.chord(keys.chord("Esc"))
            elif fg is not None and not _is_shell_surface(fg) and fg.process_name.lower() not in keep:
                closed.append(f"{fg.title} ({fg.process_name})")
                self.agent.chord(keys.chord("Alt+F4"))
            elif strays:
                app = strays[0].title.split(" - ")[-1].replace("\u200b", "").lower()
                btn = next((e for e in snap.elements if e.role == "button" and app and
                            app in e.name.replace("\u200b", "").lower()), None)  # fmt: skip
                x, y = snap.screen_width * 3 // 4, snap.screen_height // 3
                if btn is not None:
                    self._click(btn)
                elif (
                    fg is not None
                    and fg.class_name == "Shell_TrayWnd"
                    and all(
                        w.bounds.width > 0
                        and w.bounds.height > 0
                        and (w.class_name in ("Progman", "WorkerW") or not _contains(w.bounds, x, y))
                        for w in [*snap.windows, *snap.apps]
                    )
                ):
                    self.agent.move(x, y, 120)
                    self.agent.click()
                else:
                    self.agent.chord(keys.chord("Alt+Tab"))
            else:
                self.agent.chord(keys.chord("Esc"))
            time.sleep(0.6)
        self.log.write(event="normalize_failed", host_ms=_now(), closed=closed)
        return False

    def _click(self, e: obs.Element) -> None:
        b = e.bounds
        self.agent.move(max(1, b.x + b.width // 2), max(1, b.y + b.height // 2), 120)
        self.agent.click()

    def escalate(self) -> int:
        if os.environ.get("LUDIUS_NO_ESCALATE") == "1":
            self.log.write(event="escalate_blocked", host_ms=_now())
            raise EscalationDenied("LUDIUS_NO_ESCALATE=1: ladder steps 4-8 denied, no input sent")
        self.log.write(event="ladder", step=4, action="restart app", host_ms=_now())
        if self.normalize():
            return 4
        self.log.write(event="ladder", step=5, action="normalize session", host_ms=_now())
        if self.normalize(rounds=24):
            return 5
        self.log.write(event="ladder", step=6, action="sign out", host_ms=_now())
        self.agent.chord(keys.chord("Win+X"))
        time.sleep(1.0)
        self.agent.chord(keys.chord("U"))
        time.sleep(0.6)
        self.agent.chord(keys.chord("I"))
        try:
            time.sleep(15)
            if self.sign_in() and self.normalize():
                return 6
        except Exception:
            pass
        self.log.write(event="ladder", step=7, action="reboot VM", host_ms=_now())
        if self.lifecycle is not None:
            try:
                self.lifecycle(pb.LifecycleRequest.STOP)
                self.lifecycle(pb.LifecycleRequest.START)
                self.wait_ready(300)
                if self.normalize():
                    return 7
            except Exception:
                pass
        self.log.write(event="ladder", step=8, action="mark VM unhealthy", host_ms=_now())
        raise VmUnhealthy("session could not be normalized")


def _fg(snap: obs.Snapshot) -> obs.Window | None:
    return next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)


def _is_shell_surface(w: obs.Window) -> bool:
    return w.class_name in (
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "OperationStatusWindow",
    ) or w.process_name.lower() in SHELL_PROCESSES - {"explorer.exe"}


def _inert_shell_popup(snap: obs.Snapshot, i: int) -> bool:
    w = snap.windows[i]
    return (
        w.process_name.lower() == "explorer.exe"
        and w.class_name in ("Worker Window", "SysShadow")
        and w.no_tree
        and not any(e.window == i for e in snap.elements)
    )


def _contains(b: obs.Rect, x: int, y: int) -> bool:
    return b.x <= x < b.x + b.width and b.y <= y < b.y + b.height


def _explorer_window(w: obs.Window) -> bool:
    return w.process_name.lower() == "explorer.exe" and w.class_name == "CabinetWClass"


def _button(snap: obs.Snapshot, pattern: re.Pattern[str]) -> obs.Element | None:
    return next(
        (e for e in snap.elements if e.role == "button" and pattern.fullmatch(e.name.replace("&", "").strip())), None
    )


def _now() -> int:
    return int(time.time() * 1000)
