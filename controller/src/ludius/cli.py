"""ludius: run tasks, record fixtures, run the contract test.

Configuration comes from the repo's .env (gitignored) and the environment:
OPENROUTER_API_KEY, LUDIUS_VM, LUDIUS_AGENT (host agent address).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import ExitStack, closing, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import grpc
import yaml
from google.protobuf import json_format

from ludius import admission, artifacts, canary, growth, keys, mail, metrics, planner, revision, timeline, visual
from ludius.assist import Assist
from ludius.clocklease import Lease
from ludius.day import DayRunner, run_task
from ludius.decisions import DEFAULT_MODEL, DecisionsClient
from ludius.hostagent import HostAgent
from ludius.llm import DEFAULT_MODEL as PLANNER_MODEL
from ludius.llm import LlmClient
from ludius.loop import Clock, GroundTruth, Runner
from ludius.policy import Policy
from ludius.reply import Composer
from ludius.session import Session
from ludius.tasks import Persona, Task, TaskInstance, Template, load_template, load_yaml
from ludius.v1 import agent_pb2 as pb

if TYPE_CHECKING:
    from ludius import nightly, rangeclock, rangerun

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
    decider = DecisionsClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = Path(args.log or RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{template.task}.jsonl")
    log = GroundTruth(log_path)
    try:
        runner = Runner(
            agent,
            decider,
            persona,
            log,
            clock=args.clock,
            seed=seed,
            assist=make_assist(),
            policy=make_policy(),
            composer=make_composer(),
        )
        out = run_task(runner, make_session(agent, log), task)
    finally:
        log.close()
        decider.close()
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
def _clock_lease(vm: str | None = None) -> Lease:
    vm = vm or os.environ["LUDIUS_VM"]
    return Lease(RUNS / "locks" / f"clock-{vm}.json", vm)


class ClockRefused(RuntimeError):
    """Time-jump asked of an agent that is not Hyper-V on this machine."""


@functools.cache
def require_hyperv_clock(address: str | None, vm: str) -> None:
    """Time-jump and the clock lease act through infra/hyperv on this machine, by VM name: refuse them unless the
    host agent drives Hyper-V here, so a same-named local VM is never jumped while the persona runs elsewhere."""
    address = address or os.environ.get("LUDIUS_AGENT", "127.0.0.1:7451")
    agent = HostAgent(address, vm)
    try:
        ok = agent.local_hyperv()
    finally:
        agent.close()
    if not ok:
        raise ClockRefused(
            f"time-jump refused: the host agent at {address} does not drive Hyper-V on this machine (the clock "
            f"scripts would act on a local VM named {vm}); use --clock compressed or realtime (a Proxmox range jumps "
            "range-wide only: `ludius range warp`)"
        )


def set_clock(at: dt.datetime | None, *, vm: str | None = None, agent: str | None = None) -> None:
    """Time-jump: the guest clock to a logical time (test VM only, via infra/hyperv/set-clock.ps1). Refused before
    anything is touched unless the host agent is Hyper-V on this machine."""
    require_hyperv_clock(agent, vm or os.environ["LUDIUS_VM"])
    script = str(ROOT / "infra" / "hyperv" / "set-clock.ps1")
    extra = ["-Reset"] if at is None else ["-Time", at.isoformat(timespec="seconds")]
    cmd = ["pwsh", "-NoProfile", "-NonInteractive", "-File", script, *extra]
    if vm is not None:
        cmd += ["-VM", vm]
    if at is not None:
        _clock_lease(vm).hold()  # before the jump: a controller killed mid-jump still leaves a lease to sweep
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    if at is None:
        _clock_lease(vm).release()  # only after a reset that worked; a failed one leaves the lease to the sweeper


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
    decider = DecisionsClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = RUNS / "admission" / f"{time.strftime('%Y%m%d-%H%M%S')}-{template.task}.jsonl"
    log = GroundTruth(log_path)
    try:
        try:
            runner = Runner(agent, decider, load_persona(args.persona), log, clock="compressed", seed=0,
                            assist=make_assist(), policy=make_policy())  # fmt: skip
            results, stopped = admission.validate(template, args.runs, lambda: restore(agent, args.restore), runner,
                                                  make_session(agent, log))  # fmt: skip
        finally:
            decider.close()
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
            from ludius import nightly  # the same generation the nightly planning worker runs

            persona = load_persona(args.persona)
            src = plan_sources()
            scenario = planner.Scenario.parse(args.scenario) if args.scenario else None
            try:  # weekly revision first, so this plan sees the revised story; one planner at a time
                plan, rejects, auto = nightly.generate_day(llm, world, persona, src, date, scenario)
            except revision.RevisionBusy as e:
                print(e, file=sys.stderr)
                return 2
            print(f"narrative: {auto.note}")
            for d in auto.dropped:
                print(f"  dropped {d}")
            path = world.save_plan(plan, src.templates)
            for it in plan.items:
                print(f"  {it.at} {it.template}{' (interrupt)' if it.interrupt else ''}: {it.context}")
            for r in rejects:
                print(f"  rejected {r}")
            print(f"{len(plan.items)} items -> {path}")
        print(f"planner cost ${llm.cost:.4f}")
    finally:
        llm.close()
    return 0


def plan_sources() -> nightly.Sources:
    from ludius import nightly

    personas = [load_yaml(Persona, p) for p in sorted((DATA / "personas").glob("*.yaml"))]
    key = os.environ.get("OPENROUTER_API_KEY", "")
    return nightly.Sources(personas, load_templates(), make_policy(), functools.partial(mail.newest, folder="inbox"),
                           (lambda: DecisionsClient(key)) if key else None)  # fmt: skip


def cmd_day(args: argparse.Namespace) -> int:
    if args.canary and not args.temporal:
        print("--canary needs --temporal (without it, run `ludius canary` before the day)", file=sys.stderr)
        return 2
    if getattr(args, "clock", None) == "time-jump":  # before the agent restores or touches anything
        require_hyperv_clock(getattr(args, "agent", None), args.vm or os.environ["LUDIUS_VM"])
    plan = planner.load_plan(Path(args.plan))
    persona = load_persona(args.persona or str(DATA / "personas" / f"{plan.persona}.yaml"))
    seed = args.seed if args.seed is not None else int(time.time())
    log_path = Path(args.log) if args.log else planner.World(RUNS).day_log(plan.persona, plan.date)
    templates = load_templates()
    drift = canary.drifted(RUNS / "health.json")
    skipped = [i for i in plan.items if i.template in templates and templates[i.template].app in drift]
    plan = plan.model_copy(update={"items": [i for i in plan.items if i not in skipped]})
    agent = start_agent(args)
    decider = DecisionsClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    if args.temporal:
        from ludius import durable  # needs the durable dependency group

        log: GroundTruth = durable.HeartbeatLog(log_path)  # slot heartbeats come from ground-truth writes
    else:
        log = GroundTruth(log_path)
    reset_failed = False
    try:
        for i in skipped:  # the app's drift canary failed: don't burn Decisions calls on a UI that has changed
            log.write(event="skipped_drift", host_ms=int(time.time() * 1000), at=i.at, task=i.template,
                      app=templates[i.template].app)  # fmt: skip
            print(f"  {i.at} {i.template}: skipped, {templates[i.template].app} drifted (runs/health.json)")
        day = build_day(agent, decider, persona, log, templates, args.clock, seed, args.vm or os.environ["LUDIUS_VM"])
        if args.temporal:  # durable: resumes after the last finished slot if this day was started before
            from ludius import durable

            desk = RUNS / "locks" / f"{args.vm or os.environ['LUDIUS_VM']}.lock"

            def check(apps: set[str]) -> dict[str, str]:  # the day's drift canaries, on this desktop, first
                return canary.run(
                    templates, DATA / "admission.yaml", day.runner, day.session, RUNS / "health.json", apps
                )

            done = durable.run(
                args.temporal,
                day,
                plan,
                args.clock,
                desk,
                canary=check if args.canary else None,
                locked=getattr(args, "desk_locked", False),
            )
            results = [(d.at, d.template, d.status) for d in done]
        else:
            results = [(r.item.at, r.item.template, r.status) for r in day.run(plan)]
    finally:
        if args.clock == "time-jump" and _clock_lease(args.vm or os.environ["LUDIUS_VM"]).held:
            try:  # an exception or Ctrl+C mid-day must not leave the range on a jumped clock
                set_clock(None, vm=args.vm or os.environ["LUDIUS_VM"], agent=args.agent)
            except (subprocess.CalledProcessError, OSError) as e:  # report it; never mask an exception in flight
                reset_failed = True
                detail = (getattr(e, "stderr", "") or str(e)).strip()[-500:]
                print(f"time-jump reset FAILED, range clocks may still be jumped: {detail}\n"
                      "  fix: pwsh infra/hyperv/set-clock.ps1 -Reset", file=sys.stderr)  # fmt: skip
        log.close()
        decider.close()
        agent.close()
    for at, template, status in results:
        print(f"  {at} {template}: {status}")
    done = sum(status == "done" for _, _, status in results)
    print(f"day {plan.date} {plan.persona} (seed {seed}): {done}/{len(results)} done")
    print(f"ground truth: {log_path}")
    if reset_failed:
        return 3
    return 0 if done == len(results) else 1


def build_day(
    agent: HostAgent,
    decider: DecisionsClient,
    persona: Persona,
    log: GroundTruth,
    templates: dict[str, Template],
    clock: Clock,
    seed: int,
    vm: str,
) -> DayRunner:
    """Shared runner construction for one-shot and lazy long-lived workers."""
    if clock == "time-jump":
        require_hyperv_clock(agent.address, vm)
    pacing = "compressed" if clock == "time-jump" else clock
    runner = Runner(agent, decider, persona, log, clock=pacing, seed=seed, assist=make_assist(), policy=make_policy(),
                    composer=make_composer())  # fmt: skip
    return DayRunner(
        runner,
        make_session(agent, log),
        templates,
        log,
        clock,
        seed,
        set_clock=functools.partial(set_clock, vm=vm, agent=agent.address),
    )


def cmd_schedule(args: argparse.Namespace) -> int:
    from ludius import durable, nightly

    # Validate definitions before opening a connection. Creation never triggers immediately.
    if args.nightly:
        if args.canary or args.clock != "realtime" or args.day_offset:
            raise ValueError("--clock/--canary/--day-offset configure the day schedule, not --nightly planning")
        definition = nightly.schedule_for(args.persona, args.at or "22:00", args.timezone, paused=not args.unpause)
        name = nightly.schedule_id(args.persona)
    else:
        definition = durable.schedule_for(
            args.persona, args.at or "08:30", args.timezone, args.clock, paused=not args.unpause, canary=args.canary,
            day_offset=args.day_offset,
        )  # fmt: skip
        name = durable.schedule_id(args.persona)

    async def manage() -> None:
        client = await durable.connect(durable.Target(args.temporal))
        handle = client.get_schedule_handle(name)
        if args.what == "create":
            await client.create_schedule(name, definition)
        elif args.what == "pause":
            await handle.pause(note="paused by ludius schedule")
        elif args.what == "unpause":
            await handle.unpause(note="unpaused by ludius schedule")
        elif args.what == "delete":
            await handle.delete()
            print(f"{name}: deleted (does not cancel a running day)")
            return
        desc = await handle.describe()
        print(
            json.dumps(
                {
                    "id": name,
                    "paused": desc.schedule.state.paused,
                    "next": [t.isoformat() for t in desc.info.next_action_times],
                },
                indent=2,
            )
        )

    asyncio.run(asyncio.wait_for(manage(), timeout=30))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from ludius import durable, scheduled

    durable.schedule_id(args.persona)  # validate account syntax before forming a persona-file path
    persona = load_persona(str(DATA / "personas" / f"{args.persona}.yaml"))
    if persona.user != args.persona:
        raise ValueError("persona file account differs from worker persona")
    vm = args.vm or os.environ["LUDIUS_VM"]
    desk = RUNS / "locks" / f"{vm}.lock"

    @contextmanager
    def factory(inp: durable.DayInput) -> Generator[DayRunner]:
        if inp.plan.persona != persona.user:
            raise ValueError("worker cannot drive another persona's plan")
        # No restore/start: an explicit worker only connects to the already-running desktop.
        with ExitStack() as stack:
            agent = stack.enter_context(closing(agent_from(args)))
            user = agent.observe(wait_settle=False).hello.user.rsplit("\\", 1)[-1]
            if user.casefold() != persona.user.casefold():
                raise ValueError(
                    "worker requires the configured persona to be logged on; unknown identity fails closed"
                )
            decider = stack.enter_context(
                closing(DecisionsClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model))
            )
            log = stack.enter_context(
                closing(durable.HeartbeatLog(planner.World(RUNS).day_log(persona.user, inp.plan.date)))
            )
            seed = int.from_bytes(hashlib.sha256(f"{vm}/{persona.user}/{inp.plan.date}".encode()).digest()[:4])
            try:
                yield build_day(agent, decider, persona, log, load_templates(), inp.clock, seed, vm)
            finally:
                already_failed = sys.exc_info()[0] is not None
                if inp.clock == "time-jump" and _clock_lease(vm).held:
                    try:
                        # never reset another process's lease on a failed claim/load
                        set_clock(None, vm=vm, agent=agent.address)
                    except Exception:
                        print("worker clock reset FAILED; lease/reservation retained for recovery", file=sys.stderr)
                        if not already_failed:
                            raise

    def check(day: DayRunner, apps: set[str]) -> dict[str, str]:
        return canary.run(day.templates, DATA / "admission.yaml", day.runner, day.session, RUNS / "health.json", apps)

    async def run() -> None:
        client = await durable.connect(durable.Target(args.temporal))
        await scheduled.serve(client, RUNS, persona.user, desk, factory, check)

    with suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


def cmd_plan_worker(args: argparse.Namespace) -> int:
    from ludius import durable, nightly

    async def run() -> None:
        client = await durable.connect(durable.Target(args.temporal))
        llm = functools.partial(LlmClient, os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
        await nightly.serve(client, RUNS, plan_sources, llm)

    with suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


def cmd_range(args: argparse.Namespace) -> int:
    """The multi-persona run (ludius.rangerun): `up` starts and keeps it running, `status` summarizes it."""
    import signal
    from zoneinfo import ZoneInfo

    from ludius import rangerun

    known = rangerun.personas()
    users = args.persona or known
    if unknown := [u for u in users if u not in known]:
        print(f"not in {rangerun.VMS}: {', '.join(unknown)}", file=sys.stderr)
        return 2
    today = dt.datetime.now(ZoneInfo(args.timezone)).date()
    if args.what == "unwarp":
        return _range_unwarp()
    if args.what == "summaries":
        return _range_summaries(users)
    if args.what == "status":
        print(rangerun.summary(planner.World(RUNS), users, today, RUNS / "range", dict(os.environ)))
        return 0
    if why := rangerun.refusals(users, dict(os.environ), DATA / "personas"):
        print("range not started:", *why, sep="\n  ", file=sys.stderr)
        return 2
    if args.what == "warp":
        return _range_warp(args, users, today)
    asyncio.run(asyncio.wait_for(_range_schedules(args, users, today), timeout=60 + 600 * bool(args.today)))
    agent = args.agent or os.environ.get("LUDIUS_AGENT", "127.0.0.1:7451")
    sup = rangerun.Supervisor([rangerun.Child(s) for s in rangerun.specs(users, agent, args.temporal, args.model,
                              dict(os.environ))], RUNS / "range", rangerun.popen)  # fmt: skip

    def term(*_: object) -> None:
        raise KeyboardInterrupt  # systemd's stop: the same orderly worker shutdown as Ctrl+C

    signal.signal(signal.SIGTERM, term)
    try:
        while True:
            sup.step()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        sup.stop()
    return 0


def _range_summaries(users: list[str]) -> int:
    """The monitor's activity lines (ludius.summaries), every 5 s until stopped; its own budget, not the run's."""
    import signal

    from ludius import summaries

    def term(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, term)
    llm = LlmClient(os.environ.get("OPENROUTER_API_KEY", ""), model=summaries.MODEL, timeout=90)
    s = summaries.Summariser(users, planner.World(RUNS).root / "days", llm)
    try:
        while True:
            s.step()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        llm.close()
    return 0


def _range_clock(*, strict: bool = True) -> rangeclock.RangeClock:
    """dc01 first, then ex01 and every persona VM in range-vms.yml (all of them: one DC, one clock). strict=False
    (unwarp) skips the up-front all-or-nothing check: reset checks each VM itself and resets every one that passes."""
    from ludius import rangeclock, rangerun

    if os.environ.get("LUDIUS_MAIL_BACKEND") != "exchange":
        raise ClockRefused("range clock: Proxmox Exchange range only (dc01 is its time source; LUDIUS_MAIL_BACKEND)")
    main = yaml.safe_load((rangerun.VMS.parent / "main.yml").read_text(encoding="utf-8"))
    dc, ex = int(main["srv01_vmid"]), int(main["ex01_vmid"])
    names = {dc: "ludius-dc01", ex: "ludius-ex01"} | {v: rangerun.vm_of(u) for u, v in rangerun.vmids().items()}
    # Root `qm` acts on whatever VMID it is given: every one must be a range VM before any clock is touched, checked
    # here and again (a fresh pvesh read) before every set and reset.
    if strict:
        rangeclock.confine(names, rangeclock.resources(), socket.gethostname())
    return rangeclock.RangeClock(
        dc,
        [v for v in names if v != dc],
        verify=lambda: rangeclock.violations(names, rangeclock.resources(), socket.gethostname()),
    )


def _range_unwarp() -> int:
    """Every range VM back on real time, then the range lease dropped (recovery after a failed warp reset)."""
    try:
        got = _range_clock(strict=False).reset()
    except Exception as e:  # noqa: BLE001  every VM was tried; say which failed, keep the lease
        print(f"unwarp FAILED, range clocks may still be jumped: {e}", file=sys.stderr)
        return 3
    _clock_lease("range").path.unlink(missing_ok=True)
    print(json.dumps({"offsets_s": {str(k): round(v, 1) for k, v in got.items()}}))
    return 0


def _range_warp(args: argparse.Namespace, users: list[str], today: dt.date) -> int:
    """Time-jump on a Proxmox range (spec section 7). Per simulated weekday: the range clock to the evening before,
    that day's plans (nightly.ensure_day, as `up --today`), the range clock to just before the day's first item,
    then every persona's day as a compressed durable day on its own worker; the clock runs on from there. Every
    jump goes through the range clock (rangeclock: DC first, forward only, skew verified) under the range's clock
    lease; the range goes back on real time when the warp ends or fails (then roll back to the pre-warp snapshot:
    AD and Exchange have seen the future). Workers are supervised as by `up`; no schedules are created.
    ponytail: no lease sweeper on Proxmox; a killed warp leaves the clocks jumped until `ludius range unwarp`."""
    import signal
    from zoneinfo import ZoneInfo

    from ludius import calls, durable, nightly, rangerun

    if not args.start or not 1 <= args.days <= 7:
        print("warp needs --start YYYY-MM-DD (a future date) and --days 1-7", file=sys.stderr)
        return 2
    start = dt.date.fromisoformat(args.start)
    if start <= today:
        print("warp is forward only: --start must be after today", file=sys.stderr)
        return 2
    days = [d for d in (start + dt.timedelta(n) for n in range(3 * args.days)) if d.weekday() < 5][: args.days]
    tz, clock, lease = ZoneInfo(args.timezone), _range_clock(), _clock_lease("range")
    world, src = planner.World(RUNS), plan_sources()
    llm = functools.partial(
        LlmClient, os.environ.get("OPENROUTER_API_KEY", ""), model=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL)
    )
    agent = args.agent or os.environ.get("LUDIUS_AGENT", "127.0.0.1:7451")
    sup = rangerun.Supervisor([rangerun.Child(s) for s in rangerun.specs(users, agent, args.temporal, args.model,
                              dict(os.environ))], RUNS / "range", rangerun.popen)  # fmt: skip
    journal = RUNS / "range" / "warp.jsonl"

    def note(**rec: object) -> None:
        line = json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), **rec}, default=str)
        with suppress(OSError):  # unwritable journal or output must never skip the cleanup in `finally` (stop, reset)
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        with suppress(OSError):
            print(line, flush=True)

    def offsets(got: dict[int, float]) -> dict[str, object]:
        return {"skew_s": round(max(got.values()) - min(got.values()), 1),
                "offsets_s": {str(k): round(v, 1) for k, v in got.items()}}  # fmt: skip

    def jump(at: dt.datetime) -> None:
        lease.hold()  # before the jump: a warp killed mid-jump still leaves its lease behind
        note(event="clock_set", logical=at.isoformat(), **offsets(clock.set(at)))

    def term(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, term)
    reset_failed, failure, failed, playing, open_days = False, None, 0, list[str](), list[str]()
    try:
        sup.step()
        for d in days:
            jump(dt.datetime.combine(d - dt.timedelta(days=1), dt.time.fromisoformat(args.plan_at), tz))
            plans: dict[str, planner.DayPlan] = {}
            for u in users:
                persona = next(p for p in src.personas if p.user == u)
                calls.persona = u
                try:
                    with durable.desk_lock(world.root / "locks" / "nightly.lock"):
                        plans[u] = planner.load_plan(Path(nightly.ensure_day(llm, world, persona, src, d).path))
                except Exception as e:  # noqa: BLE001  one persona without a plan leaves the others' days to run
                    note(event="no_plan", persona=u, date=d, error=str(e)[:300])
                finally:
                    calls.persona = None
            note(event="planned", date=d, items={u: len(p.items) for u, p in plans.items()})
            failed += len(users) - len(plans)
            plans = {u: p for u, p in plans.items() if p.items}  # an empty plan has no first item and no day
            if not plans:
                continue
            first = min(dt.time.fromisoformat(i.at) for p in plans.values() for i in p.items)
            jump(dt.datetime.combine(d, first, tz) - dt.timedelta(minutes=10))
            playing = [durable.workflow_id(p) for p in plans.values()]
            results = asyncio.run(_play_days(args.temporal, plans, tz, sup))
            playing = []
            for u, res in results.items():
                note(event="day", persona=u, date=d, result=res)
                failed += isinstance(res, str) and res.startswith("failed")
    except KeyboardInterrupt:
        note(event="interrupted")
    except Exception as e:  # noqa: BLE001  re-raised below unless the clock reset failed too (that exit wins)
        failure = e
        note(event="warp_failed", error=repr(e)[-1500:])
    finally:
        # Stopped mid-day: no day may run on after the warp (a SIGTERM can skip any task's own cleanup).
        if playing:
            try:
                open_days = asyncio.run(_stop_days(args.temporal, playing))
            except Exception:  # noqa: BLE001  nothing confirmed
                open_days = playing
        if open_days:
            note(event="days_not_stopped", workflows=open_days, fix="temporal workflow terminate --workflow-id")
        sup.stop(30)  # before the reset: a worker still driving its day would jump its clock again
        if lease.held:
            try:
                got = clock.reset()
                lease.release()  # only after a reset that worked
                note(event="clock_reset", **offsets(got))
            except Exception as e:  # noqa: BLE001  report it; never mask an exception in flight
                reset_failed = True
                note(event="clock_reset_failed", error=str(e)[-1500:], fix="ludius range unwarp")
    if reset_failed:  # a range left on a jumped clock outranks whatever stopped the warp
        print(f"warp: clock reset FAILED, run `ludius range unwarp`; warp error: {failure!r}", file=sys.stderr)
        return 3
    if open_days:
        print(f"warp: day workflows not confirmed stopped, terminate them: {open_days}", file=sys.stderr)
        return 4
    if failure:
        raise failure
    if failed:
        print(f"warp: {failed} persona day(s) without a plan or failed, see {journal}", file=sys.stderr)
    return 1 if failed else 0


async def _play_days(
    temporal: str, plans: dict[str, planner.DayPlan], tz: dt.tzinfo, sup: rangerun.Supervisor
) -> dict[str, object]:
    """Every persona's day as a compressed durable day on its own worker; the supervisor keeps the workers up."""
    from temporalio.client import WorkflowFailureError
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

    from ludius import durable

    client = await durable.connect(durable.Target(temporal))

    async def one(plan: planner.DayPlan) -> object:
        noon = dt.datetime.combine(dt.date.fromisoformat(plan.date), dt.time(12), tz).utcoffset() or dt.timedelta()
        inp = durable.DayInput(plan=plan, clock="compressed", utc_offset_s=int(noon.total_seconds()))
        wid = durable.workflow_id(plan)
        # A running day is picked up again; a closed one is played anew: after a rollback its result is not real.
        h = await client.start_workflow(durable.PersonaDay.run, inp, id=wid, task_queue=durable.task_queue(plan),
                                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                                        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)  # fmt: skip
        try:
            return [f"{d.at} {d.template} {d.status}" for d in await h.result()]
        except WorkflowFailureError as e:
            return f"failed: {e.cause}"

    async def keep() -> None:
        while True:
            sup.step()
            await asyncio.sleep(5)

    keeper = asyncio.create_task(keep())
    try:
        return dict(zip(plans, await asyncio.gather(*(one(p) for p in plans.values())), strict=True))
    finally:
        keeper.cancel()


async def _stop_days(temporal: str, wids: list[str]) -> list[str]:
    """Terminate the warp's day workflows; returns the ids not confirmed closed (unreachable server, any doubt)."""
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    from ludius import durable

    try:
        client = await durable.connect(durable.Target(temporal))
    except Exception:  # noqa: BLE001  nothing confirmed
        return wids
    left: list[str] = []
    for wid in wids:
        h = client.get_workflow_handle(wid)
        try:
            with suppress(RPCError):  # already closed or never started: the describe below decides
                await h.terminate("warp stopped")
            if (await h.describe()).status == WorkflowExecutionStatus.RUNNING:
                left.append(wid)
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:  # not found: its start never reached the server
                left.append(wid)
        except Exception:  # noqa: BLE001  unknown is not closed
            left.append(wid)
    return left


async def _range_schedules(args: argparse.Namespace, users: list[str], today: dt.date) -> None:
    """Every persona's nightly-plan and day schedules, active; existing ones are kept as they are. --today: plan
    today where no plan exists yet, then start today's day now (a schedule trigger) instead of tomorrow."""
    from temporalio.client import ScheduleAlreadyRunningError

    from ludius import calls, durable, nightly

    client = await durable.connect(durable.Target(args.temporal))
    for u in users:
        for sid, definition in (
            (nightly.schedule_id(u), nightly.schedule_for(u, args.plan_at, args.timezone, paused=False)),
            (durable.schedule_id(u), durable.schedule_for(u, args.day_at, args.timezone, args.clock, paused=False)),
        ):
            try:
                await client.create_schedule(sid, definition)
                print(f"{sid}: created")
            except ScheduleAlreadyRunningError:
                print(f"{sid}: exists, kept as it is")
    if not args.today:
        return
    world, src = planner.World(RUNS), plan_sources()
    llm = functools.partial(
        LlmClient, os.environ.get("OPENROUTER_API_KEY", ""), model=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL)
    )
    for u in users:
        persona = next(p for p in src.personas if p.user == u)
        calls.persona = u
        try:
            with durable.desk_lock(world.root / "locks" / "nightly.lock"):
                print(f"{u}: plan {nightly.ensure_day(llm, world, persona, src, today).path}")
        except Exception as e:  # noqa: BLE001  one persona's plan failing leaves the others' days to run
            print(f"{u}: no plan for {today}, its day will fail to load: {e}", file=sys.stderr)
            continue
        finally:
            calls.persona = None
        await client.get_schedule_handle(durable.schedule_id(u)).trigger()
        print(f"{u}: today's day started")


def cmd_reservation(args: argparse.Namespace) -> int:
    from temporalio.client import WorkflowExecutionStatus

    from ludius import durable, scheduled

    vm = args.vm or os.environ["LUDIUS_VM"]
    confirm = None
    if args.what == "clear":
        if not (args.confirm_vm and args.confirm_owner):
            raise ValueError("clear needs --confirm-vm and --confirm-owner repeating the reservation")
        confirm = (args.confirm_vm, args.confirm_owner)

    def status(namespace: str, workflow_id: str) -> WorkflowExecutionStatus | None:
        async def describe() -> WorkflowExecutionStatus | None:
            client = await durable.connect(durable.Target(args.temporal, namespace))
            desc = await client.get_workflow_handle(workflow_id).describe()
            if desc.namespace != namespace or desc.id != workflow_id:
                raise ValueError("server described a different workflow")
            return desc.status

        return asyncio.run(asyncio.wait_for(describe(), timeout=30))

    out = scheduled.reconcile(
        RUNS / "locks" / f"{vm}.lock",
        _clock_lease(vm).path,
        status,
        functools.partial(set_clock, None, vm=vm, agent=args.agent),
        confirm=confirm,
    )
    print(json.dumps(out, indent=2))
    return 0


def cmd_canary(args: argparse.Namespace) -> int:
    """Drift canaries: one admitted task per app on the VM as it is; results to runs/health.json."""
    agent = start_agent(args)
    decider = DecisionsClient(os.environ.get("OPENROUTER_API_KEY", ""), model=args.model)
    log_path = RUNS / "canary" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    log = GroundTruth(log_path)
    try:
        runner = Runner(agent, decider, load_persona(args.persona), log, clock="compressed", seed=0,
                        assist=make_assist(), policy=make_policy())  # fmt: skip
        states = canary.run(load_templates(), DATA / "admission.yaml", runner, make_session(agent, log),
                            RUNS / "health.json", set(args.app) or None)  # fmt: skip
    finally:
        log.close()
        decider.close()
        agent.close()
    print(f"health: {RUNS / 'health.json'}\nlog: {log_path}")
    return 1 if "drift" in states.values() else 0


def cmd_store(args: argparse.Namespace) -> int:
    """Mirror the run directory (ground-truth traces, world and plan files) into PostgreSQL, screenshots into S3."""
    from ludius import store  # needs the state dependency group

    dsn = args.dsn or os.environ.get("LUDIUS_PG", "")
    if not dsn:
        print("no database: pass --dsn or set LUDIUS_PG", file=sys.stderr)
        return 2
    s3 = args.s3 or os.environ.get("LUDIUS_S3", "")
    with store.connect(dsn) as conn:
        done = store.sync(conn, RUNS, store.bucket(s3) if s3 else None)
    shots = f", {done.shots} screenshots" if s3 else " (no S3: screenshots skipped)"
    print(f"{done.lines} trace lines, {done.docs} world/plan files{shots} synced from {RUNS}")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    out = Path(args.out)
    print(f"{timeline.export([Path(p) for p in args.logs], out)} rows -> {out}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    human = [Path(p) for p in args.human] if args.human else None
    logs = [Path(p) for p in args.logs]
    out = metrics.report(logs, human, Path(args.features) if args.features else None)
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


def contract_verdict(failures: int, skipped: int) -> str:
    """PASS only with no failure; a check an old agent can't run is reported as skipped, never as passed."""
    if failures:
        return f"FAIL: {failures} failure(s)" + (f", {skipped} skipped (old agent)" if skipped else "")
    return "PASS" + (f", {skipped} skipped (old agent)" if skipped else "")


def _contract_read_only(agent: HostAgent) -> tuple[int, int]:
    """The checks that change nothing in the guest: VM state, the observer's resolution against the console frame
    (display fixed in VM config, spec section 4), and a guest file read (absent file in an existing folder). Returns
    (failures, skipped): skipped only for UNIMPLEMENTED (agent or observer predates the call); any other error fails."""
    failures = skipped = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {what}")

    state = agent.lifecycle(pb.LifecycleRequest.STATE)
    check(state == "Running", f"state {state!r}")
    res = agent.observe(wait_settle=False)
    s = res.snapshot
    check(s.screen_width > 0 and s.screen_height > 0, f"observer {res.hello.version} as {res.hello.user}: "
          f"screen {s.screen_width}x{s.screen_height}, cursor {(s.cursor_x, s.cursor_y)}")  # fmt: skip
    png = agent.screenshot()
    size = (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")) if png[1:4] == b"PNG" else None
    check(size == (s.screen_width, s.screen_height), f"console frame {size} matches the observer's screen")
    missing = rf"C:\Users\{res.hello.user}\Documents\ludius-contract-{random.getrandbits(32):08x}.txt"
    try:
        f = agent.read_file(missing, 1024)
        check(not f.exists, f"guest file read: {missing} absent")
    except grpc.RpcError as e:
        code = e.code()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        if code == grpc.StatusCode.UNIMPLEMENTED:
            skipped += 1
            print("skip guest file read: agent or observer predates ReadGuestFile (old agent)")
        else:
            check(False, f"guest file read: {code}")
    return failures, skipped


def cmd_contract(args: argparse.Namespace) -> int:
    """Pointer mapping and typing round trip, readable from the guest (spec section 4), after the read-only checks;
    --read-only stops there (nothing typed, pointer untouched)."""
    agent = agent_from(args)
    failures, skipped = _contract_read_only(agent)
    if args.read_only:
        agent.close()
        print("contract (read-only): " + contract_verdict(failures, skipped))
        return 0 if failures == 0 else 1
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
    print("contract: " + contract_verdict(failures, skipped))
    return 0 if failures == 0 else 1


def make_policy() -> Policy:
    """Scenario-reserved paths and mailboxes from the scenario file in LUDIUS_SCENARIO (none when unset)."""
    return Policy.from_env()


def make_assist() -> Assist | None:
    """Rare LLM help for open-ended subgoals (LUDIUS_ASSIST=0: none); its HTTP client lives as long as the process."""
    key = os.environ.get("OPENROUTER_API_KEY", "") if os.environ.get("LUDIUS_ASSIST") != "0" else ""
    return Assist(LlmClient(key, model=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL))) if key else None


def make_composer() -> Composer | None:
    """Task-time reply content: one planner-LLM call per reply task, always checked by the decision model's yes/no.
    Independent of LUDIUS_ASSIST (that switches the in-loop help off, not content)."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    llm = LlmClient(key, model=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL))
    return Composer(llm, RUNS / "world" / "narrative.yaml")


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
    pl.add_argument("--model", default=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL))
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

    sc = sub.add_parser("schedule", help="explicit Temporal schedule administration (day playback or --nightly)")
    sc.add_argument("what", choices=["create", "describe", "pause", "unpause", "delete"])
    sc.add_argument("--temporal", required=True, metavar="HOST:PORT")
    sc.add_argument("--persona", required=True, help="persona account, e.g. aberg")
    sc.add_argument("--nightly", action="store_true", help="the plan-generation schedule (plan-<persona>)")
    sc.add_argument("--at", help="daily HH:MM (create only; default 08:30, --nightly 22:00)")
    sc.add_argument("--timezone", default="Europe/Stockholm", help="IANA zone (create only)")
    sc.add_argument("--clock", choices=["realtime", "compressed", "time-jump"], default="realtime")
    sc.add_argument("--canary", action="store_true", help="run drift canaries before each scheduled day")
    sc.add_argument("--day-offset", type=int, default=0, help="play the plan N days after the fire's date")
    sc.add_argument("--unpause", action="store_true", help="create active; default is paused")
    sc.set_defaults(fn=cmd_schedule)

    wk = sub.add_parser("worker", help="explicit long-lived Temporal desktop worker; does not start/restore VM")
    wk.add_argument("--temporal", required=True, metavar="HOST:PORT")
    wk.add_argument("--persona", required=True, help="persona account, e.g. aberg")
    wk.add_argument("--vm", default=argparse.SUPPRESS, help="already-running VM (or global --vm/LUDIUS_VM)")
    wk.add_argument("--model", default=DEFAULT_MODEL)
    wk.set_defaults(fn=cmd_worker)

    pw = sub.add_parser("plan-worker", help="explicit Temporal nightly planning worker (host only, LLM calls)")
    pw.add_argument("--temporal", required=True, metavar="HOST:PORT")
    pw.add_argument("--model", default=os.environ.get("LUDIUS_PLANNER_MODEL", PLANNER_MODEL))
    pw.set_defaults(fn=cmd_plan_worker)

    rg = sub.add_parser("range", help="multi-persona run: start and supervise every persona's worker, or summarize")
    rg.add_argument(
        "what",
        choices=["up", "status", "warp", "unwarp", "summaries"],
        help="warp: time-jump test over --days simulated weekdays (Proxmox); unwarp: clocks to real time; "
        "summaries: keep the monitor's per-persona activity lines current (ludius.summaries)",
    )
    rg.add_argument("--start", help="warp: first simulated date YYYY-MM-DD (after today)")
    rg.add_argument("--days", type=int, default=3, help="warp: simulated weekdays to play")
    rg.add_argument("--temporal", default="127.0.0.1:7233", metavar="HOST:PORT")
    rg.add_argument("--persona", action="append", help="only this persona (repeatable; default: all in range-vms.yml)")
    rg.add_argument("--clock", choices=["realtime", "compressed"], default="realtime", help="for new day schedules")
    rg.add_argument("--day-at", default="06:30", help="daily day-schedule fire HH:MM (new schedules)")
    rg.add_argument("--plan-at", default="22:00", help="nightly planning HH:MM for the next day (new schedules)")
    rg.add_argument("--timezone", default="Europe/Stockholm", help="IANA zone of the personas' days")
    rg.add_argument("--today", action="store_true", help="up: plan today where missing and start today's days now")
    rg.add_argument("--model", default=DEFAULT_MODEL, help="Decisions model for the desktop workers")
    rg.set_defaults(fn=cmd_range)

    rv = sub.add_parser("reservation", help="inspect (default, dry run) or clear a closed workflow's day reservation")
    rv.add_argument("what", nargs="?", choices=["inspect", "clear"], default="inspect")
    rv.add_argument("--temporal", required=True, metavar="HOST:PORT")
    rv.add_argument("--confirm-vm", default="", help="clear: this VM's name again")
    rv.add_argument("--confirm-owner", default="", help="clear: the exact namespace/workflow-id owner again")
    rv.set_defaults(fn=cmd_reservation)

    ca = sub.add_parser("canary", help="drift canaries: one admitted task per app, results to runs/health.json")
    ca.add_argument("app", nargs="*", help="only these apps (default: every app with an admitted template)")
    ca.add_argument("--persona")
    ca.add_argument("--model", default=DEFAULT_MODEL)
    ca.add_argument("--restore", metavar="CHECKPOINT", help="restore this checkpoint first")
    ca.set_defaults(fn=cmd_canary)

    st = sub.add_parser("store", help="mirror runs/ (traces, world, plans) into PostgreSQL, screenshots into S3")
    st.add_argument("action", choices=["sync"])
    st.add_argument("--dsn", help="PostgreSQL connection string (default LUDIUS_PG)")
    st.add_argument("--s3", help="screenshot bucket, http://KEY:SECRET@host:9000/bucket (default LUDIUS_S3)")
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
    me.add_argument("--features", metavar="CSV", help="per-instance features with label, held-out split, provenance")
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
    c.add_argument("--read-only", action="store_true", help="only the checks that change nothing in the guest")
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
    if args.cmd in {"run", "day", "validate", "canary", "contract", "session"}:
        from ludius import durable, scheduled

        vm = args.vm or os.environ["LUDIUS_VM"]
        desk = RUNS / "locks" / f"{vm}.lock"
        with durable.desk_lock(desk):  # before restore, input or clock cleanup; all controller entry points fence
            rec = scheduled.DayOwner(desk).read()
            if rec:
                if args.cmd != "day" or not args.temporal:
                    raise durable.DeskBusy(f"desktop reserved by {rec.owner}; resume that Temporal day first")
                plan = planner.load_plan(Path(args.plan))
                scheduled.DayOwner(desk).check(f"default/{durable.workflow_id(plan)}")
                if rec.day.plan != plan or rec.day.clock != args.clock:
                    raise ValueError("reserved day plan/clock changed")
            args.desk_locked = True
            return int(args.fn(args))
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
