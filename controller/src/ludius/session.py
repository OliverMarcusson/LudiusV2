"""Persistent-VM hygiene and the upper failure ladder (spec sections 5 and 7).

normalize: dismiss dialogs and close apps outside the allowlist, confirm an unlocked,
responsive desktop. unlock: Ctrl+Alt+Del and the persona's password, blind, through
the virtual keyboard (real type 7 unlock logons). escalate: ladder steps 4-8.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable

from ludius import keys
from ludius.loop import Agent, GroundTruth
from ludius.policy import SHELL_PROCESSES, shell_dialog
from ludius.tasks import pixels_window
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

DISCARD = re.compile(r"(don'?t save|no|discard)", re.I)
DISMISS = re.compile(r"(cancel|close|ok|not now|no thanks|dismiss|got it)", re.I)  # got it: Office teaching callouts


class VmUnhealthy(Exception):
    """Ladder step 8: the VM leaves the pool; the trace is in the ground-truth log."""


class EscalationDenied(Exception):
    """LUDIUS_NO_ESCALATE=1: the ladder (restart, sign out, reboot) was needed but is denied; no input was sent."""


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
        """Until the observer answers from an unlocked desktop (it restarts with every logon)."""
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

    # Unlock -------------------------------------------------------------------------

    def unlock(self) -> bool:
        snap = self.observe()
        if not snap.locked:
            return True
        self.log.write(event="unlock", host_ms=_now())
        return self.sign_in()

    def sign_in(self) -> bool:
        """Blind: Ctrl+Alt+Del, then the password into the focused box. Lock screen and logon screen alike."""
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

    # Normalize ----------------------------------------------------------------------

    def normalize(self, keep: set[str] | None = None, rounds: int = 12) -> bool:
        """Close everything outside `keep` (process names). True when only kept apps remain."""
        keep = {k.lower() for k in keep or set()}
        closed: list[str] = []
        for _ in range(rounds):
            if not self.unlock():
                return False
            snap = self.observe()
            if snap.secure_desktop:  # a UAC prompt left behind: cancel it, never answer it
                self.agent.chord(keys.chord("Esc"))
                closed.append("UAC prompt")
                time.sleep(1.0)
                continue
            fg = _fg(snap)
            if fg is not None and pixels_window(fg):
                # Pixels only: nothing is known by process, so nothing is closed and no row is clicked. Shut Down
                # Windows left open is the one thing to leave, with Esc.
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
                    if w.kind in (obs.Window.POPUP, obs.Window.OWNED) and not _inert_shell_popup(snap, i)
                ),
                None,
            )
            strays = [
                a
                for a in snap.apps
                if a.process_name.lower() not in keep
                and (a.process_name.lower() not in SHELL_PROCESSES or _explorer_window(a))
            ]
            # A kept app may stay in front (a reaction's chat, ludius.react); never its dialog.
            if (
                fg is not None
                and (_is_shell_surface(fg) or (fg.process_name.lower() in keep and fg.class_name != "#32770"))
                and popup is None
                and not strays
            ):
                self.log.write(event="normalized", host_ms=_now(), closed=closed)
                return True
            if popup is not None or (fg is not None and fg.class_name == "#32770"):
                # A save prompt from closing a noise app: discard like a person giving up.
                target = None if shell_dialog(fg) else _button(snap, DISCARD) or _button(snap, DISMISS)
                if target is not None:
                    self._click(target)
                else:
                    self.agent.chord(keys.chord("Esc"))
            elif fg is not None and not _is_shell_surface(fg) and fg.process_name.lower() not in keep:
                closed.append(f"{fg.title} ({fg.process_name})")
                self.agent.chord(keys.chord("Alt+F4"))
            elif strays:
                # Bring the next stray forward through its taskbar button, else Alt+Tab (which does not restore
                # a minimized Edge). Edge's title says "Microsoft\u200b Edge", its button "Microsoft Edge".
                app = strays[0].title.split(" - ")[-1].replace("\u200b", "").lower()
                btn = next((e for e in snap.elements if e.role == "button" and app and
                            app in e.name.replace("\u200b", "").lower()), None)  # fmt: skip
                x, y = snap.screen_width * 3 // 4, snap.screen_height // 3
                if btn is not None:
                    self._click(btn)
                elif (
                    fg is not None
                    and fg.class_name == "Shell_TrayWnd"
                    and all(  # every window's geometry known, none but the desktop under the point
                        w.bounds.width > 0
                        and w.bounds.height > 0
                        and (w.class_name in ("Progman", "WorkerW") or not _contains(w.bounds, x, y))
                        for w in [*snap.windows, *snap.apps]
                    )
                ):
                    # Focus on the taskbar itself (live: its tray chevron) leaves only the focused button in the
                    # tree, and neither Esc nor Alt+Tab moves it: a click on the bare desktop does. Only where no
                    # window but the desktop is under the point (a minimized app sits at -32000); bounds unknown
                    # anywhere: no click.
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

    # Ladder 4-8 ---------------------------------------------------------------------

    def escalate(self) -> int:
        """Run ladder steps 4..8 until the desktop is clean. Returns the step that worked."""
        if os.environ.get("LUDIUS_NO_ESCALATE") == "1":  # unattended deployments: fail the task, never sign out
            self.log.write(event="escalate_blocked", host_ms=_now())
            raise EscalationDenied("LUDIUS_NO_ESCALATE=1: ladder steps 4-8 denied, no input sent")
        # 4: restart the app (close it; the next task launches it again).
        self.log.write(event="ladder", step=4, action="restart app", host_ms=_now())
        if self.normalize():
            return 4
        # 5: normalize the session harder (more rounds).
        self.log.write(event="ladder", step=5, action="normalize session", host_ms=_now())
        if self.normalize(rounds=24):
            return 5
        # 6: sign out and back in (autologon), via the Win+X menu: U, I.
        self.log.write(event="ladder", step=6, action="sign out", host_ms=_now())
        self.agent.chord(keys.chord("Win+X"))
        time.sleep(1.0)
        self.agent.chord(keys.chord("U"))
        time.sleep(0.6)
        self.agent.chord(keys.chord("I"))
        try:
            time.sleep(15)  # autologon only runs at boot: sign back in like a person
            if self.sign_in() and self.normalize():
                return 6
        except Exception:
            pass
        # 7: reboot the VM through the hypervisor.
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
        # 8: out of the pool.
        self.log.write(event="ladder", step=8, action="mark VM unhealthy", host_ms=_now())
        raise VmUnhealthy("session could not be normalized")


def _fg(snap: obs.Snapshot) -> obs.Window | None:
    return next((w for w in snap.windows if w.kind == obs.Window.FOREGROUND), None)


def _is_shell_surface(w: obs.Window) -> bool:
    return w.class_name in ("Progman", "WorkerW", "Shell_TrayWnd") or w.process_name.lower() in SHELL_PROCESSES - {
        "explorer.exe"
    }


def _inert_shell_popup(snap: obs.Snapshot, i: int) -> bool:
    """Explorer's tray "Worker Window", or a SysShadow (a taskbar tooltip's shadow, live aberg): a WS_POPUP with no
    UIA tree and no elements. Nothing to dismiss; Esc never closes it, so counting it as a dialog fails every
    normalize (live: a minimized Edge behind it was never reached)."""
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
    """File Explorer folders run in explorer.exe but are ordinary apps to close."""
    return w.process_name.lower() == "explorer.exe" and w.class_name == "CabinetWClass"


def _button(snap: obs.Snapshot, pattern: re.Pattern[str]) -> obs.Element | None:
    return next(
        (e for e in snap.elements if e.role == "button" and pattern.fullmatch(e.name.replace("&", "").strip())), None
    )


def _now() -> int:
    return int(time.time() * 1000)
