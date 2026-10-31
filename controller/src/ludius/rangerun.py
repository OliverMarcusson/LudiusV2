from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from ludius import calls
from ludius.planner import World

VMS = Path(__file__).resolve().parents[3] / "infra" / "ansible" / "group_vars" / "all" / "range-vms.yml"
CAPS = ("LUDIUS_CAP_DECISION", "LUDIUS_CAP_LLM", "LUDIUS_CAP_PERSONA_DECISION", "LUDIUS_CAP_PERSONA_LLM")
BACKOFF = (5.0, 300.0)
STEADY_S = 600.0


def personas(path: Path = VMS) -> list[str]:
    return [str(p["user"]) for p in yaml.safe_load(path.read_text(encoding="utf-8"))["range_personas"]]


def vmids(path: Path = VMS) -> dict[str, int]:
    return {str(p["user"]): int(p["vmid"]) for p in yaml.safe_load(path.read_text(encoding="utf-8"))["range_personas"]}


def vm_of(user: str) -> str:
    return f"ludius-{user}"


def refusals(users: list[str], env: dict[str, str], persona_files: Path) -> list[str]:
    why = [f"{k} is not set (unattended runs are budgeted, in total and per persona and day)" for k in CAPS
           if not env.get(k, "").isdigit()]  # fmt: skip
    for u in users:
        if not (persona_files / f"{u}.yaml").exists():
            why.append(f"{u}: no persona file {persona_files / f'{u}.yaml'}")
        if not password(u, env):
            why.append(f"{u}: no unlock password (LUDIUS_PERSONA_PASSWORD_{u.upper()} or LUDIUS_PERSONA_PASSWORD)")
    return why


def password(user: str, env: dict[str, str]) -> str:
    return env.get(f"LUDIUS_PERSONA_PASSWORD_{user.upper()}") or env.get("LUDIUS_PERSONA_PASSWORD", "")


@dataclass
class Spec:
    name: str
    argv: list[str]
    env: dict[str, str]


def specs(users: list[str], agent: str, temporal: str, model: str, env: dict[str, str]) -> list[Spec]:
    # LUDIUS_WORKER_CMD: run the workers from the Rust `ludius` binary (same argv), e.g. for the warp-day comparison
    rust = env.get("LUDIUS_WORKER_CMD")
    cli = [rust, "--agent", agent] if rust else [sys.executable, "-m", "ludius.cli", "--agent", agent]
    base = {k: v for k, v in env.items() if k not in ("LUDIUS_PERSONA", "LUDIUS_VM")}
    out = [Spec("plan", [*cli, "plan-worker", "--temporal", temporal], base)]
    for u in users:
        own = {"LUDIUS_PERSONA": u, "LUDIUS_VM": vm_of(u), "LUDIUS_PERSONA_PASSWORD": password(u, env)}
        argv = [*cli, "--vm", vm_of(u), "worker", "--temporal", temporal, "--persona", u, "--model", model]
        out.append(Spec(u, argv, base | own))
    return out


class Proc(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


@dataclass
class Child:
    spec: Spec
    proc: Proc | None = None
    started: float = 0.0
    due: float = 0.0
    wait: float = BACKOFF[0]
    starts: int = 0
    last_exit: int | None = None


@dataclass
class Supervisor:
    children: list[Child]
    dir: Path
    spawn: Callable[[Spec, Path], Proc]
    now: Callable[[], float] = time.monotonic
    stopping: bool = False
    _events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def step(self) -> None:
        t = self.now()
        for c in self.children:
            if c.proc is not None and (code := c.proc.poll()) is not None:
                ran = t - c.started
                wait = BACKOFF[0] if ran >= STEADY_S else c.wait
                c.proc, c.last_exit, c.due, c.wait = None, code, t + wait, min(wait * 2, BACKOFF[1])
                self._event(c, "exit", code=code, ran_s=round(ran), restart_in_s=wait)
            if c.proc is None and not self.stopping and t >= c.due:
                try:
                    c.proc = self.spawn(c.spec, self.dir / f"{c.spec.name}.log")
                except OSError as e:
                    c.due, c.wait = t + c.wait, min(c.wait * 2, BACKOFF[1])
                    self._event(c, "spawn_failed", error=str(e)[:300])
                    continue
                c.started, c.starts = t, c.starts + 1
                self._event(c, "start", pid=c.proc.pid, start=c.starts)
        self._save()

    def stop(self, grace_s: float = 120) -> None:
        self.stopping = True
        live = [c for c in self.children if c.proc is not None and c.proc.poll() is None]
        for c in live:
            assert c.proc is not None
            if os.name == "nt":
                c.proc.kill()
            else:
                c.proc.send_signal(signal.SIGINT)
        deadline = time.monotonic() + grace_s
        for c in live:
            assert c.proc is not None
            try:
                c.proc.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                c.proc.kill()
                c.proc.wait()
            with suppress(OSError):
                self._event(c, "stopped")
        with suppress(OSError):
            self._save()

    def _event(self, c: Child, event: str, **rec: Any) -> None:
        line = {"at": dt.datetime.now().isoformat(timespec="seconds"), "worker": c.spec.name, "event": event, **rec}
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "supervisor.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        print(f"{line['at']} {c.spec.name}: {event} {rec or ''}".rstrip(), flush=True)

    def _save(self) -> None:
        state = {c.spec.name: {"pid": c.proc.pid if c.proc else None, "starts": c.starts, "last_exit": c.last_exit}
                 for c in self.children}  # fmt: skip
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps({"updated": dt.datetime.now().isoformat(timespec="seconds"), "workers": state}))
        os.replace(tmp, self.dir / "state.json")


def popen(spec: Spec, log: Path) -> Proc:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as out:
        return subprocess.Popen(spec.argv, env=spec.env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=os.name != "nt")  # fmt: skip


@dataclass
class Row:
    persona: str
    worker: str
    current: str
    done: int
    abandoned: int
    decision: int
    llm: int


def tracebacks(log: Path) -> str:
    """A live pid is not health: a worker that keeps failing activities stays up and only logs tracebacks."""
    try:
        n = log.read_text(encoding="utf-8", errors="replace").count("Traceback (most recent call last)")
    except FileNotFoundError:
        return ""
    except OSError:
        return ", log unreadable"
    return f", {n} tracebacks" if n else ""


def row(world: World, user: str, day: dt.date, workers: dict[str, Any], state_dir: Path) -> Row:
    current, done, abandoned = "-", 0, 0
    log = world.day_log(user, day.isoformat())
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("event")
        if ev == "item_start":
            current = f"{e.get('at')} {e.get('template')}"
        elif ev == "item_end":
            current = "-"
            done += e.get("status") == "done"
            abandoned += e.get("status") != "done"
        elif ev == "day_end":
            current = "(day ended)"
    w: dict[str, Any] = workers.get(user) or {}
    worker = f"pid {w['pid']}" if w.get("pid") else "down" if w else "-"
    worker += f", {w['starts']} starts" if w.get("starts", 0) > 1 else ""
    worker += tracebacks(state_dir / f"{user}.log")
    used = calls.persona_day(user, day)
    return Row(user, worker, current, done, abandoned, used["decision"], used["llm"])


def summary(world: World, users: list[str], day: dt.date, state_dir: Path, env: dict[str, str]) -> str:
    try:
        state: dict[str, Any] = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    workers: dict[str, Any] = state.get("workers", {})
    caps = "/".join(env.get(k, "-") for k in CAPS[2:])
    plan: dict[str, Any] = workers.get("plan") or {}
    head = f"range {day.isoformat()}  supervisor state {state.get('updated', 'none')}  plan worker " + (
        f"pid {plan['pid']}" if plan.get("pid") else "down"
    ) + tracebacks(state_dir / "plan.log")
    cols = f"{'persona':10} {'worker':30} {'current task':34} {'done':>4} {'aband':>5} {'decis/llm':>12}"
    lines = [head, f"{cols} (cap {caps})"]
    for r in (row(world, u, day, workers, state_dir) for u in users):
        lines.append(f"{r.persona:10} {r.worker:30} {r.current[:34]:34} {r.done:>4} {r.abandoned:>5} "
                     f"{f'{r.decision}/{r.llm}':>12}")  # fmt: skip
    return "\n".join(lines)
