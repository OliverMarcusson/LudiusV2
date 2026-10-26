"""ludius: run tasks, record fixtures, run the contract test.

Configuration comes from the repo's .env (gitignored) and the environment:
OPENROUTER_API_KEY, LUDIUS_VM, LUDIUS_AGENT (host agent address).
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import yaml
from google.protobuf import json_format

from ludius import admission, artifacts, canary, growth, keys, metrics, planner, revision, timeline, visual, world_state
from ludius.assist import Assist
from ludius.clocklease import Lease
from ludius.day import DayRunner, run_task
from ludius.hostagent import HostAgent
from ludius.jev import DEFAULT_MODEL, JevClient
from ludius.llm import DEFAULT_MODEL as PLANNER_MODEL
from ludius.llm import LlmClient
from ludius.loop import GroundTruth, Runner
from ludius.policy import Policy
from ludius.session import Session
from ludius.tasks import Persona, Task, TaskInstance, Template, load_template, load_yaml
from ludius.v1 import agent_pb2 as pb

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "controller" / "data"
RUNS = Path(os.environ.get("LUDIUS_RUNS") or ROOT / "runs")  # set it when running from a worktree


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


def agent_from(args: argparse.Namespace) -> HostAgent:
    address = args.agent or os.environ.get("LUDIUS_AGENT", "127.0.0.1:7451")
    return visual.host_agent(address, args.vm or os.environ["LUDIUS_VM"], os.environ.get("LUDIUS_VISUAL", ""))


def cmd_run(args: argparse.Namespace) -> int:
    inst = load_yaml(TaskInstance, Path(args.task))
    if args.variant is not None:
        inst.variant = args.variant
    template = load_template(DATA / "templates" / f"{inst.template}.yaml")
    persona = load_persona(args.persona)
    seed = args.seed if args.seed is not None else int(time.time())
    task = Task(template, inst, random.Random(seed))
    agent = start_agent(args)
    jev = JevClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = Path(args.log or RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{template.task}.jsonl")
    log = GroundTruth(log_path)
    try:
        runner = Runner(
            agent, jev, persona, log, clock=args.clock, seed=seed, assist=make_assist(), policy=make_policy()
        )
        out = run_task(runner, make_session(agent, log), task)
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


def start_agent(args: argparse.Namespace) -> HostAgent:
    agent = agent_from(args)
    if args.restore:
        restore(agent, args.restore)
    return agent


@functools.cache
def _clock_lease() -> Lease:
    vm = os.environ["LUDIUS_VM"]  # set-clock.ps1 jumps .env's LUDIUS_VM
    return Lease(RUNS / "locks" / f"clock-{vm}.json", vm)


def set_clock(at: dt.datetime | None) -> None:
    """Time-jump: the guest clock to a logical time (test VM only, via infra/hyperv/set-clock.ps1)."""
    script = str(ROOT / "infra" / "hyperv" / "set-clock.ps1")
    extra = ["-Reset"] if at is None else ["-Time", at.isoformat(timespec="seconds")]
    cmd = ["pwsh", "-NoProfile", "-NonInteractive", "-File", script, *extra]
    if at is not None:
        _clock_lease().hold()  # before the jump: a controller killed mid-jump still leaves a lease to sweep
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    if at is None:
        _clock_lease().release()  # only after a reset that worked; a failed one leaves the lease to the sweeper


def restore(agent: HostAgent, checkpoint: str) -> None:
    agent.lifecycle(pb.LifecycleRequest.RESTORE, checkpoint)
    agent.lifecycle(pb.LifecycleRequest.START)
    _wait_for_observer(agent)


def cmd_validate(args: argparse.Namespace) -> int:
    """Admission gate: every variant `--runs` times from the checkpoint; reliable variants are admitted."""
    source = Path(args.template)
    if not source.exists():
        source = next(p for p in (DATA / "templates" / f"{args.template}.yaml",
                                  DATA / "templates" / "proposed" / f"{args.template}.yaml") if p.exists())  # fmt: skip
    template = load_template(source)
    agent = agent_from(args)
    jev = JevClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = RUNS / "admission" / f"{time.strftime('%Y%m%d-%H%M%S')}-{template.task}.jsonl"
    log = GroundTruth(log_path)
    try:
        try:
            runner = Runner(agent, jev, load_persona(args.persona), log, clock="compressed", seed=0,
                            assist=make_assist(), policy=make_policy())  # fmt: skip
            results, stopped = admission.validate(template, args.runs, lambda: restore(agent, args.restore), runner,
                                                  make_session(agent, log))  # fmt: skip
        finally:
            jev.close()
            agent.close()
        variants = {str(v): f"{st.count('done')}/{len(st)}" for v, st in results.items()}
        if stopped:
            # Incomplete: too few runs to judge reliability, so nothing is recorded or admitted.
            log.write(event="admission_end", host_ms=int(time.time() * 1000), task=template.task, admitted=False,
                      variants=variants, stopped=stopped)  # fmt: skip
            print(f"{template.task}: stopped, not judged ({stopped}). Runs so far: " + ", ".join(
                f"v{v} {' '.join(st)}" for v, st in results.items()) + f"\n  log: {log_path}")  # fmt: skip
            return 2
        ok = admission.admit(template, results, source, DATA / "templates", DATA / "admission.yaml")
        log.write(event="admission_end", host_ms=int(time.time() * 1000), task=template.task, admitted=ok,
                  variants=variants)  # fmt: skip
    finally:
        log.close()
    print(f"{template.task}: {'admitted' if ok else 'rejected'} " + ", ".join(
        f"v{v} {st.count('done')}/{len(st)}" for v, st in results.items()))  # fmt: skip
    return 0 if ok else 1


def load_persona(path: str | None) -> Persona:
    return load_yaml(Persona, Path(path) if path else DATA / "personas" / "aberg.yaml")


def load_templates() -> dict[str, Template]:
    ts = [load_template(p) for p in sorted((DATA / "templates").glob("*.yaml"))]
    return {t.task: t for t in ts}


def cmd_plan(args: argparse.Namespace) -> int:
    """Narrative once per range; one validated day plan per persona and day; template proposals (spec section 6)."""
    world = planner.World(RUNS)
    date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    llm = LlmClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    try:
        if args.what == "template":
            caps = yaml.safe_load((DATA / "capabilities.yaml").read_text(encoding="utf-8"))
            t, raw = growth.propose(llm, caps, load_templates(), args.idea or "")
            path = growth.save(raw, DATA / "templates" / "proposed")
            print(f"proposed {t.task} ({len(t.variants)} variants): {t.about}\n  -> {path}")
            print(f"  admit with: ludius validate {t.task}")
        elif args.what == "revise":
            personas = [load_yaml(Persona, p) for p in sorted((DATA / "personas").glob("*.yaml"))]
            evidence = revision.happened(world, personas, date)
            n, dropped = revision.revise(llm, world.narrative(), personas, date, evidence)
            print(f"revised {n.revised}: {n.history[-1].what}")
            print(f"  projects: {', '.join(n.projects)}; {len(n.people)} people; {len(n.events)} events")
            for d in dropped:
                print(f"  dropped {d}")
            print(f"  -> {world.save_narrative(n)}")
        elif args.what == "narrative":
            personas = [load_yaml(Persona, p) for p in sorted((DATA / "personas").glob("*.yaml"))]
            n = planner.make_narrative(llm, personas, date)
            print(
                f"{n.company} ({n.city}): {len(n.people)} people, {len(n.events)} events -> {world.save_narrative(n)}"
            )
        else:
            persona = load_persona(args.persona)
            templates = load_templates()
            narrative = world.narrative()
            personas = [load_yaml(Persona, p) for p in sorted((DATA / "personas").glob("*.yaml"))]
            state = world_state.build(world, narrative, personas, date)
            world_state.save(state, world)
            context = "\n".join(x for x in (world.summary(persona.user, date),
                                            world_state.brief(state, persona.user, date)) if x)  # fmt: skip
            plan, rejects = planner.plan_day(
                llm, narrative, persona, date, templates, context, world.history(persona.user),
                policy=make_policy(), scenario=planner.Scenario.parse(args.scenario) if args.scenario else None,
                followups={f.id: f.about for f in world_state.due(state, persona.user, date)},
            )  # fmt: skip
            path = world.save_plan(plan, templates)
            for it in plan.items:
                print(f"  {it.at} {it.template}{' (interrupt)' if it.interrupt else ''}: {it.context}")
            for r in rejects:
                print(f"  rejected {r}")
            print(f"{len(plan.items)} items -> {path}")
        print(f"planner cost ${llm.cost:.4f}")
    finally:
        llm.close()
    return 0


def cmd_day(args: argparse.Namespace) -> int:
    if args.canary and not args.temporal:
        print("--canary needs --temporal (without it, run `ludius canary` before the day)", file=sys.stderr)
        return 2
    plan = planner.load_plan(Path(args.plan))
    persona = load_persona(args.persona or str(DATA / "personas" / f"{plan.persona}.yaml"))
    seed = args.seed if args.seed is not None else int(time.time())
    log_path = Path(args.log) if args.log else planner.World(RUNS).day_log(plan.persona, plan.date)
    templates = load_templates()
    drift = canary.drifted(RUNS / "health.json")
    skipped = [i for i in plan.items if i.template in templates and templates[i.template].app in drift]
    plan = plan.model_copy(update={"items": [i for i in plan.items if i not in skipped]})
    agent = start_agent(args)
    jev = JevClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    if args.temporal:
        from ludius import durable  # needs the durable dependency group

        log: GroundTruth = durable.HeartbeatLog(log_path)  # slot heartbeats come from ground-truth writes
    else:
        log = GroundTruth(log_path)
    reset_failed = False
    try:
        for i in skipped:  # the app's drift canary failed: don't burn Jev calls on a UI that has changed
            log.write(event="skipped_drift", host_ms=int(time.time() * 1000), at=i.at, task=i.template,
                      app=templates[i.template].app)  # fmt: skip
            print(f"  {i.at} {i.template}: skipped, {templates[i.template].app} drifted (runs/health.json)")
        pacing = "compressed" if args.clock == "time-jump" else args.clock
        runner = Runner(agent, jev, persona, log, clock=pacing, seed=seed, assist=make_assist(), policy=make_policy())
        day = DayRunner(runner, make_session(agent, log), templates, log, args.clock, seed, set_clock=set_clock)
        if args.temporal:  # durable: resumes after the last finished slot if this day was started before
            from ludius import durable

            desk = RUNS / "locks" / f"{args.vm or os.environ['LUDIUS_VM']}.lock"

            def check(apps: set[str]) -> dict[str, str]:  # the day's drift canaries, on this desktop, first
                return canary.run(
                    templates, DATA / "admission.yaml", day.runner, day.session, RUNS / "health.json", apps
                )

            done = durable.run(args.temporal, day, plan, args.clock, desk, canary=check if args.canary else None)
            results = [(d.at, d.template, d.status) for d in done]
        else:
            results = [(r.item.at, r.item.template, r.status) for r in day.run(plan)]
    finally:
        if args.clock == "time-jump":
            try:  # an exception or Ctrl+C mid-day must not leave the range on a jumped clock
                set_clock(None)
            except (subprocess.CalledProcessError, OSError) as e:  # report it; never mask an exception in flight
                reset_failed = True
                detail = (getattr(e, "stderr", "") or str(e)).strip()[-500:]
                print(f"time-jump reset FAILED, range clocks may still be jumped: {detail}\n"
                      "  fix: pwsh infra/hyperv/set-clock.ps1 -Reset", file=sys.stderr)  # fmt: skip
        log.close()
        jev.close()
        agent.close()
    for at, template, status in results:
        print(f"  {at} {template}: {status}")
    done = sum(status == "done" for _, _, status in results)
    print(f"day {plan.date} {plan.persona} (seed {seed}): {done}/{len(results)} done")
    print(f"ground truth: {log_path}")
    if reset_failed:
        return 3
    return 0 if done == len(results) else 1


def cmd_canary(args: argparse.Namespace) -> int:
    """Drift canaries: one admitted task per app on the VM as it is; results to runs/health.json."""
    agent = start_agent(args)
    jev = JevClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = RUNS / "canary" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    log = GroundTruth(log_path)
    try:
        runner = Runner(agent, jev, load_persona(args.persona), log, clock="compressed", seed=0,
                        assist=make_assist(), policy=make_policy())  # fmt: skip
        states = canary.run(load_templates(), DATA / "admission.yaml", runner, make_session(agent, log),
                            RUNS / "health.json", set(args.app) or None)  # fmt: skip
    finally:
        log.close()
        jev.close()
        agent.close()
    print(f"health: {RUNS / 'health.json'}\nlog: {log_path}")
    return 1 if "drift" in states.values() else 0


def cmd_store(args: argparse.Namespace) -> int:
    """Mirror the run directory (ground-truth traces, world and plan files) into PostgreSQL."""
    from ludius import store  # needs the state dependency group

    dsn = args.dsn or os.environ.get("LUDIUS_PG", "")
    if not dsn:
        print("no database: pass --dsn or set LUDIUS_PG", file=sys.stderr)
        return 2
    with store.connect(dsn) as conn:
        done = store.sync(conn, RUNS)
    print(f"{done.lines} trace lines, {done.docs} world/plan files synced from {RUNS}")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    out = Path(args.out)
    print(f"{timeline.export([Path(p) for p in args.logs], out)} rows -> {out}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    human = [Path(p) for p in args.human] if args.human else None
    logs = [Path(p) for p in args.logs]
    out = metrics.report(logs, human)
    if args.timeline:
        personas = [load_yaml(Persona, p).user for p in sorted((DATA / "personas").glob("*.yaml"))]
        out["artifacts"] = artifacts.report(
            Path(args.timeline), personas, logs,
            Path(args.manifest) if args.manifest else None, Path(args.before) if args.before else None,
        )  # fmt: skip
    print(json.dumps(out, indent=2))
    return 0


def _wait_for_observer(agent: HostAgent, seconds: float = 180) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if not agent.observe(timeout_ms=2000).snapshot.locked:
                return
        except Exception:
            if time.monotonic() > deadline:
                raise
        if time.monotonic() > deadline:
            raise TimeoutError("observer did not report an unlocked desktop")
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


def make_policy() -> Policy:
    """Scenario-reserved paths and mailboxes from the scenario file in LUDIUS_SCENARIO (none when unset)."""
    path = os.environ.get("LUDIUS_SCENARIO")
    return Policy.load(Path(path)) if path else Policy()


def make_assist() -> Assist | None:
    """Rare LLM help for open-ended subgoals; its HTTP client lives as long as the process."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    return Assist(LlmClient(key, model=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL))) if key else None


def make_session(agent: HostAgent, log: GroundTruth) -> Session:
    return Session(agent, log, os.environ.get("LUDIUS_PERSONA_PASSWORD", ""), lifecycle=lambda op: agent.lifecycle(op))


def cmd_session(args: argparse.Namespace) -> int:
    agent = agent_from(args)
    log = GroundTruth(RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-session.jsonl")
    s = make_session(agent, log)
    if args.op == "lock":
        agent.chord(keys.chord("Win+L"))
        time.sleep(3)
        print("locked" if s.observe().locked else "not locked")
    elif args.op == "unlock":
        print("unlocked" if s.unlock() else "unlock failed")
    else:
        print("normalized" if s.normalize() else "normalize failed")
    log.close()
    agent.close()
    return 0


def cmd_vm(args: argparse.Namespace) -> int:
    agent = agent_from(args)
    op = {
        "start": pb.LifecycleRequest.START,
        "stop": pb.LifecycleRequest.STOP,
        "checkpoint": pb.LifecycleRequest.CHECKPOINT,
        "restore": pb.LifecycleRequest.RESTORE,
        "state": pb.LifecycleRequest.STATE,
        "clone": pb.LifecycleRequest.CLONE,
        "delete": pb.LifecycleRequest.DELETE,
    }[args.op]
    if args.op == "clone" and not (args.checkpoint and args.to):
        raise SystemExit("clone needs a checkpoint and --to NEW_NAME")
    if args.op == "delete" and args.confirm != agent.vm:
        raise SystemExit(f"delete removes {agent.vm} and its disks: repeat its name with --confirm {agent.vm}")
    print(agent.lifecycle(op, args.checkpoint, args.to if args.op == "clone" else args.confirm))
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

    pl = sub.add_parser("plan", help="LLM planner: company narrative or a persona's day plan")
    pl.add_argument("what", choices=["narrative", "revise", "day", "template"])
    pl.add_argument("--idea", help="template growth: what kind of task to propose")
    pl.add_argument("--persona", help="persona YAML (day plans)")
    pl.add_argument("--date", help="YYYY-MM-DD (default today)")
    pl.add_argument("--scenario", help="scenario window for look-alike work, e.g. 2026-10-28T13:00-14:00:powershell")
    pl.add_argument("--model", default=PLANNER_MODEL)
    pl.set_defaults(fn=cmd_plan)

    d = sub.add_parser("day", help="play a day plan on its logical timeline")
    d.add_argument("plan", help="day plan YAML (runs/plans/...)")
    d.add_argument("--persona")
    d.add_argument("--clock", choices=["realtime", "compressed", "time-jump"], default="compressed")
    d.add_argument("--seed", type=int)
    d.add_argument("--model", default=DEFAULT_MODEL)
    d.add_argument("--log")
    d.add_argument("--restore", metavar="CHECKPOINT", help="restore this checkpoint first")
    d.add_argument("--temporal", metavar="HOST:PORT", help="run as a durable Temporal workflow on this server")
    d.add_argument("--canary", action="store_true", help="with --temporal: run the day's drift canaries first")
    d.set_defaults(fn=cmd_day)

    ca = sub.add_parser("canary", help="drift canaries: one admitted task per app, results to runs/health.json")
    ca.add_argument("app", nargs="*", help="only these apps (default: every app with an admitted template)")
    ca.add_argument("--persona")
    ca.add_argument("--model", default=DEFAULT_MODEL)
    ca.add_argument("--restore", metavar="CHECKPOINT", help="restore this checkpoint first")
    ca.set_defaults(fn=cmd_canary)

    st = sub.add_parser("store", help="mirror runs/ (traces, world, plans) into PostgreSQL")
    st.add_argument("action", choices=["sync"])
    st.add_argument("--dsn", help="PostgreSQL connection string (default LUDIUS_PG)")
    st.set_defaults(fn=cmd_store)

    tl = sub.add_parser("timeline", help="export ground-truth logs as an instructor timeline CSV")
    tl.add_argument("logs", nargs="+")
    tl.add_argument("--out", default=str(RUNS / "timeline.csv"))
    tl.set_defaults(fn=cmd_timeline)

    me = sub.add_parser("metrics", help="range test metrics and filterability check from ground-truth logs")
    me.add_argument("logs", nargs="+")
    me.add_argument("--human", nargs="+", metavar="LOG", help="human baseline logs for the filterability check")
    me.add_argument("--timeline", metavar="CSV", help="psort CSV export (dynamic or l2tcsv) after the noise")
    me.add_argument("--before", metavar="CSV", help="psort CSV export before the noise, for survival")
    me.add_argument("--manifest", metavar="JSON", help='scenario artifacts: [{"id": ..., "match": {field: regex}}]')
    me.set_defaults(fn=cmd_metrics)

    va = sub.add_parser("validate", help="admission gate: run every template variant from a clean checkpoint")
    va.add_argument("template", help="template name (catalog or proposed) or YAML path")
    va.add_argument("--runs", type=int, default=2, help="runs per variant")
    va.add_argument("--persona")
    va.add_argument("--model", default=DEFAULT_MODEL)
    va.add_argument("--restore", default="clean", metavar="CHECKPOINT")
    va.set_defaults(fn=cmd_validate)

    o = sub.add_parser("observe", help="print a snapshot or record a fixture")
    o.add_argument("--record", metavar="NAME")
    o.add_argument("--limit", type=int, default=60)
    o.set_defaults(fn=cmd_observe)

    c = sub.add_parser("contract", help="pointer and typing contract test")
    c.set_defaults(fn=cmd_contract)

    s = sub.add_parser("session", help="session hygiene: lock (test), unlock, normalize")
    s.add_argument("op", choices=["lock", "unlock", "normalize"])
    s.set_defaults(fn=cmd_session)

    v = sub.add_parser("vm", help="VM lifecycle through the host agent")
    v.add_argument("op", choices=["start", "stop", "checkpoint", "restore", "state", "clone", "delete"])
    v.add_argument("checkpoint", nargs="?", default="")
    v.add_argument("--to", default="", help="clone: name of the new VM (ludius-*)")
    v.add_argument("--confirm", default="", help="delete: the VM name again")
    v.set_defaults(fn=cmd_vm)

    args = p.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]  # guest UI text (U+200E...) on a cp1252 pipe
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
