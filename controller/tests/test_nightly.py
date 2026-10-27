from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import ScheduleActionStartWorkflow, ScheduleOverlapPolicy
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes
from temporalio.exceptions import ApplicationError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from ludius import cli, durable, nightly
from ludius.planner import DayPlan, Narrative, Person, World

DAY = dt.date(2026, 10, 28)
ITEM = {"at": "09:00", "template": "edge_web_lookup", "params": {"search_query": "ifrs 16 lease accounting"},
        "context": "Checks the lease rules before the close."}  # fmt: skip


class ScriptedLlm:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers, self.prompts, self.cost = list(answers), list[str](), 0.0

    def json(self, cls: type[Any], system: str, user: str, temperature: float = 0.9) -> Any:
        self.prompts.append(user)
        return cls.model_validate(self.answers.pop(0))

    def close(self) -> None:
        pass


def no_llm() -> Any:
    raise AssertionError("an existing plan must never reach the model")


def _world(tmp_path: Path) -> World:
    world = World(tmp_path)
    people = [Person(name="Anna Berg", role="Finance controller", department="Finance", user="aberg")]
    n = Narrative(company="Harbourline", city="Rotterdam", summary="Packaging.", people=people, projects=[], events=[])
    world.save_narrative(n)
    return world  # unrevised and nothing logged: the weekly revision makes no model call


def _setup(tmp_path: Path) -> tuple[World, Any, nightly.Sources]:
    src = cli.plan_sources()
    return _world(tmp_path), next(p for p in src.personas if p.user == "aberg"), src


def test_planning_date_is_the_next_local_day_of_the_intended_occurrence() -> None:
    utc = dt.UTC
    cases = {
        dt.datetime(2026, 10, 27, 21, 0, tzinfo=utc): "2026-10-28",  # 22:00 CET
        dt.datetime(2026, 10, 27, 23, 30, tzinfo=utc): "2026-10-29",  # past local midnight: its local day + 1
        dt.datetime(2026, 10, 25, 0, 30, tzinfo=utc): "2026-10-26",  # repeated 02:30 at the autumn change...
        dt.datetime(2026, 10, 25, 1, 30, tzinfo=utc): "2026-10-26",  # ...is the same local day both times
        dt.datetime(2026, 3, 29, 1, 30, tzinfo=utc): "2026-03-30",  # 03:30 CEST after the spring gap
        dt.datetime(2026, 10, 30, 21, 0, tzinfo=utc): "2026-10-31",  # Friday evening plans Saturday too
    }
    for at, want in cases.items():
        assert nightly.planning_date(at, "Europe/Stockholm").isoformat() == want
    with pytest.raises(ValueError, match="aware"):
        nightly.planning_date(dt.datetime(2026, 10, 27, 22), "UTC")


def test_nightly_schedule_is_separate_paused_and_never_overlaps() -> None:
    s = nightly.schedule_for("aberg", "22:00", "Europe/Stockholm")
    assert s.state.paused and s.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert s.policy.catchup_window == dt.timedelta(hours=8) and s.spec.time_zone_name == "Europe/Stockholm"
    assert s.spec.calendars[0].hour[0].start == 22 and s.spec.calendars[0].minute[0].start == 0
    assert isinstance(s.action, ScheduleActionStartWorkflow)
    assert s.action.workflow == "NightlyPlan" and s.action.task_queue == "ludius-plan"
    assert s.action.args == [nightly.NightlyInput(persona="aberg", tz="Europe/Stockholm")]
    assert nightly.schedule_id("aberg") == "plan-aberg" != durable.schedule_id("aberg")
    assert not nightly.schedule_for("aberg", "22:00", "UTC", paused=False).state.paused
    for persona, at in (("aberg", "22:00:00"), ("aberg", "9:00"), ("../aberg", "22:00")):
        with pytest.raises(ValueError):
            nightly.schedule_for(persona, at, "UTC")
    args = argparse.Namespace(nightly=True, canary=True, clock="realtime", persona="aberg", at=None,
                              timezone="UTC", unpause=False, temporal="unused:1", what="create")  # fmt: skip
    with pytest.raises(ValueError, match="day schedule"):
        cli.cmd_schedule(args)  # refused before any connection


def test_generated_plan_is_kept_on_retry_and_never_overwritten(tmp_path: Path) -> None:
    world, anna, src = _setup(tmp_path)
    llm = ScriptedLlm({"items": [ITEM]})
    first = nightly.ensure_day(lambda: llm, world, anna, src, DAY)  # type: ignore[arg-type, return-value]
    assert first.generated and first.items == 1 and first.date == "2026-10-28" and len(llm.prompts) == 1
    assert "Wednesday 2026-10-28" in llm.prompts[0]
    path = Path(first.path)
    before = path.read_bytes()
    again = nightly.ensure_day(no_llm, world, anna, src, DAY)
    assert not again.generated and again.items == 1 and path.read_bytes() == before
    plan = nightly.check_existing(world, anna, DAY, src)
    with pytest.raises(FileExistsError):  # a plan written meanwhile wins over a concurrent generation
        world.save_plan(plan.model_copy(update={"items": []}), src.templates, new=True)
    assert path.read_bytes() == before


QUERIES = ("ifrs 16 lease accounting", "supplier payment terms netherlands")
ITEMS = [{**ITEM, "params": {"search_query": QUERIES[0]}},
         {**ITEM, "at": "10:30", "params": {"search_query": QUERIES[1]}}]  # fmt: skip
PLAN = DayPlan.model_validate({"persona": "aberg", "date": DAY.isoformat(), "items": ITEMS})


def test_crash_after_publish_before_history_append_is_repaired_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _, src = _setup(tmp_path)

    def crash(*_: Any) -> None:
        raise RuntimeError("crash after publish")

    with monkeypatch.context() as m:
        m.setattr(World, "_append", crash)
        for p in src.personas:  # both personas publish the same content, then die before the append
            with pytest.raises(RuntimeError, match="crash"):
                nightly.ensure_day(lambda: ScriptedLlm({"items": ITEMS}), world, p, src, DAY)  # type: ignore[arg-type, return-value]
            assert world.plan_path(p.user, DAY.isoformat()).exists()
    assert not world.history_path.exists()
    for _ in range(2):  # the retry restores the history without the model; a repeated recovery appends nothing
        for p in src.personas:
            assert not nightly.ensure_day(no_llm, world, p, src, DAY).generated
        assert world.history("aberg") == world.history("lnord") == list(QUERIES)


@pytest.mark.parametrize("end", ["\n", ""])
def test_partial_append_keeps_every_row_and_adds_only_the_missing(tmp_path: Path, end: str) -> None:
    world, anna, src = _setup(tmp_path)
    world.save_plan(PLAN, {}, new=True)  # {} templates: no history rows of its own
    rows = [("2026-10-27", "aberg", "an earlier day"), (DAY.isoformat(), "aberg", "an overwritten manual plan"),
            (DAY.isoformat(), "lnord", QUERIES[1]), (DAY.isoformat(), "aberg", QUERIES[0])]  # fmt: skip
    old = "\n".join(json.dumps({"date": d, "persona": u, "text": t}) for d, u, t in rows) + end
    world.history_path.write_text(old, encoding="utf-8", newline="")
    for _ in range(2):
        nightly.ensure_day(no_llm, world, anna, src, DAY)
        assert world.history_path.read_text(encoding="utf-8").startswith(old)
        assert world.history("aberg") == ["an earlier day", "an overwritten manual plan", *QUERIES]
        assert world.history("lnord") == [QUERIES[1]]


@pytest.mark.parametrize("tail", ['{"date": "2026-10-28", "pers', "[1, 2]", '{"date": "2026-10-28"}'])
def test_unreadable_history_fails_closed_untouched(tmp_path: Path, tail: str) -> None:
    world, anna, src = _setup(tmp_path)
    plan = world.save_plan(PLAN, {}, new=True)
    old = json.dumps({"date": "2026-10-27", "persona": "aberg", "text": "an earlier day"}) + "\n" + tail
    world.history_path.write_text(old, encoding="utf-8", newline="")
    before = plan.read_bytes()
    with pytest.raises(nightly.PlanConflict, match="line 2 is not a history row"):
        nightly.ensure_day(no_llm, world, anna, src, DAY)
    acts = nightly.PlanActivities(tmp_path, cli.plan_sources, no_llm)
    fire = nightly.PlanFire(
        persona="aberg", tz="Europe/Stockholm", occurrence=dt.datetime(2026, 10, 27, 21, tzinfo=dt.UTC)
    )
    with pytest.raises(ApplicationError, match="cannot restore") as e:
        acts.plan(fire)
    assert e.value.non_retryable
    assert world.history_path.read_text(encoding="utf-8") == old and plan.read_bytes() == before


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("persona: lnord\ndate: '2026-10-28'\nitems: []\n", "not aberg"),
        ("persona: aberg\ndate: '2026-10-28'\nitems: [\n", "not a readable plan"),
        ("persona: aberg\ndate: '2026-10-28'\nitems:\n- {at: '09:00', template: nope}\n", "fails validation"),
    ],
)
def test_conflicting_existing_plan_fails_closed_untouched(tmp_path: Path, text: str, match: str) -> None:
    world, anna, src = _setup(tmp_path)
    path = world.plan_path("aberg", DAY.isoformat())
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(nightly.PlanConflict, match=match):
        nightly.ensure_day(no_llm, world, anna, src, DAY)
    acts = nightly.PlanActivities(tmp_path, cli.plan_sources, no_llm)
    fire = nightly.PlanFire(
        persona="aberg", tz="Europe/Stockholm", occurrence=dt.datetime(2026, 10, 27, 21, tzinfo=dt.UTC)
    )
    with pytest.raises(ApplicationError, match=match) as e:
        acts.plan(fire)
    assert e.value.non_retryable and path.read_text(encoding="utf-8") == text


def test_activity_rejects_unknown_persona_and_waits_on_a_busy_planner(tmp_path: Path) -> None:
    acts = nightly.PlanActivities(tmp_path, cli.plan_sources, no_llm)
    at = dt.datetime(2026, 10, 27, 21, tzinfo=dt.UTC)
    for persona in ("ghost", "../aberg"):
        with pytest.raises(ApplicationError) as e:
            acts.plan(nightly.PlanFire(persona=persona, tz="Europe/Stockholm", occurrence=at))
        assert e.value.non_retryable
    with durable.desk_lock(tmp_path / "locks" / "nightly.lock"), pytest.raises(RuntimeError, match="retrying"):
        acts.plan(nightly.PlanFire(persona="aberg", tz="Europe/Stockholm", occurrence=at))
    assert activity._Definition.must_from_callable(acts.plan).name == "nightly_plan"  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]


def test_cli_plan_day_and_nightly_share_one_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLlm({"items": [ITEM]})
    monkeypatch.setattr(cli, "RUNS", tmp_path / "cli")
    monkeypatch.setattr(cli, "LlmClient", lambda *_a, **_k: llm)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.delenv("LUDIUS_SCENARIO", raising=False)
    _world(tmp_path / "cli")
    args = argparse.Namespace(what="day", persona=str(cli.DATA / "personas" / "aberg.yaml"),
                              date=DAY.isoformat(), model="m", scenario=None, idea=None)  # fmt: skip
    assert cli.cmd_plan(args) == 0
    world, anna, src = _setup(tmp_path / "nightly")
    out = nightly.ensure_day(lambda: ScriptedLlm({"items": [ITEM]}), world, anna, src, DAY)  # type: ignore[arg-type, return-value]
    manual = World(tmp_path / "cli").plan_path("aberg", DAY.isoformat()).read_text(encoding="utf-8")
    assert Path(out.path).read_text(encoding="utf-8") == manual


def test_workflow_uses_the_intended_occurrence_and_passes_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    at = dt.datetime(2026, 10, 27, 21, tzinfo=dt.UTC)
    attrs = TypedSearchAttributes(
        [SearchAttributePair(SearchAttributeKey.for_datetime("TemporalScheduledStartTime"), at)]
    )
    seen: list[nightly.PlanFire] = []

    async def act(name: str, fire: nightly.PlanFire, **kw: Any) -> nightly.Planned:
        assert name == "nightly_plan" and kw["retry_policy"].maximum_attempts == 3
        seen.append(fire)
        return nightly.Planned(persona="aberg", date="2026-10-28", path="p", generated=True, items=0)

    monkeypatch.setattr(nightly.workflow, "execute_activity", act)
    inp = nightly.NightlyInput(persona="aberg", tz="Europe/Stockholm")
    monkeypatch.setattr(nightly.workflow, "info", lambda: SimpleNamespace(typed_search_attributes=attrs))
    assert asyncio.run(nightly.NightlyPlan().run(inp)).date == "2026-10-28"
    assert seen == [nightly.PlanFire(persona="aberg", tz="Europe/Stockholm", occurrence=at)]
    monkeypatch.setattr(
        nightly.workflow, "info", lambda: SimpleNamespace(typed_search_attributes=TypedSearchAttributes([]))
    )
    with pytest.raises(ApplicationError) as e:
        asyncio.run(nightly.NightlyPlan().run(inp))
    assert e.value.non_retryable
    defn = workflow._Definition.must_from_class(nightly.NightlyPlan)  # pyright: ignore[reportPrivateUsage]

    async def sandbox() -> None:  # imports the module under the workflow sandbox; needs a running loop
        SandboxedWorkflowRunner().prepare_workflow(defn)

    asyncio.run(sandbox())
