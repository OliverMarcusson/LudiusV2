"""ludius: run tasks, record fixtures, run the contract test.

Configuration comes from the repo's .env (gitignored) and the environment:
OPENROUTER_API_KEY, LUDIUS_VM, LUDIUS_AGENT (host agent address).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

from google.protobuf import json_format

from ludius.hostagent import HostAgent
from ludius.jev import DEFAULT_MODEL, JevClient
from ludius.loop import GroundTruth, Runner
from ludius.tasks import Persona, Task, TaskInstance, load_template, load_yaml
from ludius.v1 import agent_pb2 as pb

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "controller" / "data"


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


def agent_from(args: argparse.Namespace) -> HostAgent:
    return HostAgent(args.agent or os.environ.get("LUDIUS_AGENT", "127.0.0.1:7451"), args.vm or os.environ["LUDIUS_VM"])


def cmd_run(args: argparse.Namespace) -> int:
    inst = load_yaml(TaskInstance, Path(args.task))
    if args.variant is not None:
        inst.variant = args.variant
    template = load_template(DATA / "templates" / f"{inst.template}.yaml")
    persona = load_yaml(Persona, Path(args.persona) if args.persona else DATA / "personas" / "aberg.yaml")
    seed = args.seed if args.seed is not None else int(time.time())
    task = Task(template, inst, random.Random(seed))
    agent = agent_from(args)
    if args.restore:
        agent.lifecycle(pb.LifecycleRequest.RESTORE, args.restore)
        agent.lifecycle(pb.LifecycleRequest.START)
        _wait_for_observer(agent)
    jev = JevClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = Path(args.log or ROOT / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{template.task}.jsonl")
    log = GroundTruth(log_path)
    try:
        runner = Runner(agent, jev, persona, log, clock=args.clock, seed=seed)
        out = runner.run(task)
    finally:
        log.close()
        jev.close()
        agent.close()
    print(
        f"{template.task} (variant {task.variant}, seed {seed}): {out.status} in {out.steps} steps"
        + (f" - {out.reason}" if out.reason else "")
    )
    print(f"ground truth: {log_path}")
    return 0 if out.status == "done" else 1


def _wait_for_observer(agent: HostAgent, seconds: float = 180) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if not agent.observe(timeout_ms=2000).snapshot.locked:
                return
        except Exception:
            if time.monotonic() > deadline:
                raise
        time.sleep(2)


def cmd_observe(args: argparse.Namespace) -> int:
    """Print one snapshot, or record it (plus the console frame) as a fixture."""
    agent = agent_from(args)
    res = agent.observe()
    if args.record:
        d = ROOT / "fixtures" / args.record
        d.mkdir(parents=True, exist_ok=True)
        (d / "snapshot.json").write_text(json_format.MessageToJson(res.snapshot), encoding="utf-8")
        (d / "screen.png").write_bytes(agent.screenshot())
        print(f"recorded {d}")
    else:
        s = res.snapshot
        print(
            f"settled={s.settled} {s.settle_ms}ms locked={s.locked} elements={len(s.elements)} "
            f"ui_access={res.hello.ui_access}"
        )
        for w in s.windows:
            print(f"  window {pb_kind(w.kind)} {w.process_name} {w.title!r}")
        for e in s.elements[: args.limit]:
            from ludius.options import describe

            print("  " + describe(e))
    agent.close()
    return 0


def pb_kind(k: int) -> str:
    from ludius.v1 import observation_pb2 as obs

    return obs.Window.Kind.Name(k).lower()  # pyright: ignore[reportArgumentType]


def cmd_contract(args: argparse.Namespace) -> int:
    """Pointer mapping and typing round trip, readable from the guest (spec section 4)."""
    agent = agent_from(args)
    failures = 0
    s = agent.observe().snapshot
    w, h = s.screen_width, s.screen_height
    # Hyper-V's absolute pointer rounds to within 1 px (measured: x off by one below ~40).
    for x, y in [(1, 1), (w // 2, h // 2), (w - 2, h - 2), (137, 911 % h), (w // 3, h // 5)]:
        agent.move(x, y)
        got = agent.observe(wait_settle=False).snapshot
        ok = abs(got.cursor_x - x) <= 1 and abs(got.cursor_y - y) <= 1
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} pointer {(x, y)} -> {(got.cursor_x, got.cursor_y)}")
    # Typing: Run dialog (Win+R) field, read back via UIA, then cancel.
    probe = "Ludius contract 0123 ~!@#$%^&*()_+{}|:<>?"
    agent.chord([0x5B, 0x52])
    snap = agent.observe().snapshot
    field = next((e for e in snap.elements if e.focused and e.role in ("edit", "combo box")), None)
    if field is None:
        print("FAIL typing: no focused field in the Run dialog")
        failures += 1
    else:
        agent.chord([0x11, 0x41])  # select all, typing replaces it
        agent.type(probe, 15, 5)
        snap = agent.observe().snapshot
        got = next((e.value for e in snap.elements if e.key == field.key), "")
        ok = got == probe
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} typing {probe!r} -> {got!r}")
        agent.chord([0x1B])  # Esc closes the Run dialog
    agent.close()
    print("contract: " + ("PASS" if failures == 0 else f"{failures} failure(s)"))
    return 0 if failures == 0 else 1


def cmd_vm(args: argparse.Namespace) -> int:
    agent = agent_from(args)
    op = {
        "start": pb.LifecycleRequest.START,
        "stop": pb.LifecycleRequest.STOP,
        "checkpoint": pb.LifecycleRequest.CHECKPOINT,
        "restore": pb.LifecycleRequest.RESTORE,
        "state": pb.LifecycleRequest.STATE,
    }[args.op]
    print(agent.lifecycle(op, args.checkpoint))
    agent.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    p = argparse.ArgumentParser(prog="ludius")
    p.add_argument("--agent", help="host agent address (default LUDIUS_AGENT or 127.0.0.1:7451)")
    p.add_argument("--vm", help="VM name (default LUDIUS_VM)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one task instance")
    r.add_argument("task", help="task instance YAML")
    r.add_argument("--persona")
    r.add_argument("--clock", choices=["realtime", "compressed"], default="compressed")
    r.add_argument("--seed", type=int)
    r.add_argument("--variant", type=int, help="force a template variant")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--log")
    r.add_argument("--restore", metavar="CHECKPOINT", help="restore this checkpoint first")
    r.set_defaults(fn=cmd_run)

    o = sub.add_parser("observe", help="print a snapshot or record a fixture")
    o.add_argument("--record", metavar="NAME")
    o.add_argument("--limit", type=int, default=60)
    o.set_defaults(fn=cmd_observe)

    c = sub.add_parser("contract", help="pointer and typing contract test")
    c.set_defaults(fn=cmd_contract)

    v = sub.add_parser("vm", help="VM lifecycle through the host agent")
    v.add_argument("op", choices=["start", "stop", "checkpoint", "restore", "state"])
    v.add_argument("checkpoint", nargs="?", default="")
    v.set_defaults(fn=cmd_vm)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
