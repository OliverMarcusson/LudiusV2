from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from ludius import calls, rangerun
from ludius.planner import World


class FakeProc:
    pids = iter(range(100, 1000))

    def __init__(self) -> None:
        self.pid = next(self.pids)
        self.code: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self.hang = False

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.code = None if self.hang else 0

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise subprocess.TimeoutExpired("worker", timeout or 0)
        return self.code

    def kill(self) -> None:
        self.killed, self.code = True, -9


def supervisor(tmp_path: Path, names: list[str], fail: set[str] | None = None):  # type: ignore[no-untyped-def]
    now = [0.0]
    procs: dict[str, list[FakeProc]] = {n: [] for n in names}

    def spawn(spec: rangerun.Spec, log: Path) -> FakeProc:
        if spec.name in (fail or set()):
            raise OSError("no such file")
        p = FakeProc()
        procs[spec.name].append(p)
        return p

    kids = [rangerun.Child(rangerun.Spec(n, ["x"], {})) for n in names]
    sup = rangerun.Supervisor(kids, tmp_path, spawn, now=lambda: now[0])  # type: ignore[arg-type]
    return sup, procs, now


def test_a_dying_worker_is_restarted_with_backoff_and_the_others_are_untouched(tmp_path: Path) -> None:
    sup, procs, now = supervisor(tmp_path, ["plan", "aberg", "lnord"])
    sup.step()
    assert [len(p) for p in procs.values()] == [1, 1, 1]
    procs["aberg"][-1].code = 1
    now[0] = 1
    sup.step()
    assert len(procs["aberg"]) == 1
    now[0] = 6
    sup.step()
    assert len(procs["aberg"]) == 2
    procs["aberg"][-1].code = 1
    now[0] = 7
    sup.step()
    now[0] = 7 + 9
    sup.step()
    assert len(procs["aberg"]) == 2
    now[0] = 7 + 10
    sup.step()
    assert len(procs["aberg"]) == 3
    now[0] += rangerun.STEADY_S
    procs["aberg"][-1].code = 3
    sup.step()
    now[0] += rangerun.BACKOFF[0]
    sup.step()
    assert len(procs["aberg"]) == 4
    assert [len(procs["plan"]), len(procs["lnord"])] == [1, 1]
    assert all(p.signals == [] for p in procs["plan"] + procs["lnord"])
    state = json.loads((tmp_path / "state.json").read_text())["workers"]
    assert state["aberg"]["starts"] == 4 and state["aberg"]["last_exit"] == 3 and state["lnord"]["starts"] == 1
    events = [json.loads(x) for x in (tmp_path / "supervisor.jsonl").read_text().splitlines()]
    assert [e["code"] for e in events if e["event"] == "exit"] == [1, 1, 3]


def test_a_worker_that_cannot_start_is_retried_and_does_not_block_the_others(tmp_path: Path) -> None:
    sup, procs, now = supervisor(tmp_path, ["aberg", "lnord"], fail={"aberg"})
    sup.step()
    now[0] = 5
    sup.step()
    assert len(procs["lnord"]) == 1
    fails = [json.loads(x)["event"] for x in (tmp_path / "supervisor.jsonl").read_text().splitlines()]
    assert fails.count("spawn_failed") == 2


def test_stop_interrupts_every_worker_and_kills_one_that_hangs(tmp_path: Path) -> None:
    sup, procs, _ = supervisor(tmp_path, ["aberg", "lnord"])
    sup.step()
    procs["lnord"][0].hang = True
    sup.stop(grace_s=0)
    if rangerun.os.name != "nt":
        assert procs["aberg"][0].signals and not procs["aberg"][0].killed
    assert procs["lnord"][0].killed
    sup.step()
    assert [len(p) for p in procs.values()] == [1, 1]


def test_each_persona_worker_gets_its_own_vm_budget_scope_and_password() -> None:
    env = {"LUDIUS_PERSONA_PASSWORD": "shared", "LUDIUS_PERSONA_PASSWORD_LNORD": "own", "LUDIUS_PERSONA": "stale"}
    out = {s.name: s for s in rangerun.specs(["aberg", "lnord"], "127.0.0.1:7451", "t:7233", "m", env)}
    assert "LUDIUS_PERSONA" not in out["plan"].env and "plan-worker" in out["plan"].argv
    assert (out["aberg"].env["LUDIUS_PERSONA"], out["aberg"].env["LUDIUS_VM"]) == ("aberg", "ludius-aberg")
    assert out["aberg"].argv[out["aberg"].argv.index("--vm") + 1] == "ludius-aberg"
    assert out["aberg"].env["LUDIUS_PERSONA_PASSWORD"] == "shared"
    assert out["lnord"].env["LUDIUS_PERSONA_PASSWORD"] == "own"


def test_an_unbudgeted_or_unlockable_run_is_refused(tmp_path: Path) -> None:
    (tmp_path / "aberg.yaml").write_text("x")
    caps = dict.fromkeys(rangerun.CAPS, "100")
    assert rangerun.refusals(["aberg"], {**caps, "LUDIUS_PERSONA_PASSWORD": "p"}, tmp_path) == []
    why = rangerun.refusals(["aberg", "ghost"], {**caps, "LUDIUS_CAP_PERSONA_LLM": ""}, tmp_path)
    assert any("LUDIUS_CAP_PERSONA_LLM" in w for w in why) and any("ghost: no persona file" in w for w in why)
    assert sum("no unlock password" in w for w in why) == 2


def test_the_map_names_eight_personas() -> None:
    assert len(rangerun.personas()) == 8


def test_one_runaway_persona_spends_only_its_own_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calls, "PATH", tmp_path / "calls.json")
    (tmp_path / "calls.json").write_text('{"decision": 0, "llm": 0}')
    monkeypatch.setenv("LUDIUS_CAP_DECISION", "5")
    monkeypatch.setenv("LUDIUS_CAP_PERSONA_DECISION", "2")
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    calls.persona_path("aberg").parent.mkdir(parents=True)
    calls.persona_path("aberg").write_text(json.dumps({yesterday: {"decision": 2, "llm": 0}}))
    monkeypatch.setattr(calls, "persona", "aberg")
    calls.count("decision")
    calls.count("decision")
    with pytest.raises(calls.CapReached, match="aberg"):
        calls.count("decision")
    monkeypatch.setattr(calls, "persona", "lnord")
    calls.count("decision")
    calls.count("decision")
    assert calls.persona_day("aberg") == {"decision": 2, "llm": 0}
    assert calls.persona_day("lnord") == {"decision": 2, "llm": 0}
    assert json.loads(calls.PATH.read_text())["decision"] == 4
    monkeypatch.setattr(calls, "persona", "sdekker")
    calls.count("decision")
    with pytest.raises(calls.CapReached, match="LUDIUS_CAP_DECISION"):
        calls.count("decision")
    calls.persona_path("lnord").write_text("{broken")
    monkeypatch.setattr(calls, "persona", "lnord")
    monkeypatch.setenv("LUDIUS_CAP_DECISION", "100")
    with pytest.raises(calls.CapReached, match="unusable"):
        calls.count("decision")


def test_status_reads_current_task_counts_and_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calls, "PATH", tmp_path / "calls.json")
    day = dt.date.today()
    world = World(tmp_path)
    log = world.day_log("aberg", day.isoformat())
    log.parent.mkdir(parents=True)
    events = [
        {"event": "day_start"},
        {"event": "item_start", "at": "08:10", "template": "notepad_note"},
        {"event": "item_end", "status": "done"},
        {"event": "item_start", "at": "09:00", "template": "x"},
        {"event": "item_end", "status": "failed"},
        {"event": "item_start", "at": "10:00", "template": "outlook"},
    ]
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n{cut off")
    calls.persona_path("aberg").parent.mkdir(parents=True)
    calls.persona_path("aberg").write_text(json.dumps({day.isoformat(): {"decision": 7, "llm": 1}}))
    (tmp_path / "range").mkdir()
    (tmp_path / "range" / "state.json").write_text(
        json.dumps({"updated": "t", "workers": {"aberg": {"pid": 5, "starts": 2}, "lnord": {"pid": None, "starts": 1}}})
    )
    r = rangerun.row(world, "aberg", day, {"aberg": {"pid": 5, "starts": 2}})
    assert (r.current, r.done, r.abandoned, r.decision, r.llm) == ("10:00 outlook", 1, 1, 7, 1)
    assert r.worker == "pid 5, 2 starts"
    text = rangerun.summary(world, ["aberg", "lnord"], day, tmp_path / "range", {})
    assert "10:00 outlook" in text and "down" in text and "plan worker down" in text
