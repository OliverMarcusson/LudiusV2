from __future__ import annotations

import random
from typing import Any

import pytest

from ludius import keys
from ludius.cli import DATA
from ludius.jev import JevError, build_request, parse_response
from ludius.policy import Policy
from ludius.tasks import Done, Persona, Task, TaskInstance, WindowCheck, check_done, load_template
from ludius.v1 import observation_pb2 as obs

OPTIONS = {"o1": "click [1] button 'Save'", "o2": "press Esc"}


def response(**over: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "model": "typesafe/jev-1.14-20261001",
        "answers": {
            "action": {"type": "choice", "choice": "o1", "confidence": 0.8, "probabilities": {"o1": 0.9, "o2": 0.1}},
            "subgoal_done": {"type": "noul", "noul": 0.1},
            "off_track": {"type": "noul", "noul": 0.05},
        },
        "usage": {"input_tokens": 100, "output_tokens": 3, "cost": 0.0001},
    }
    r.update(over)
    return r


def test_parse_valid() -> None:
    d = parse_response(response(), "typesafe/jev-1.14", OPTIONS)
    assert (d.choice, d.subgoal_done, d.off_track) == ("o1", 0.1, 0.05)


@pytest.mark.parametrize(
    "answers",
    [
        {"action": {"type": "choice", "choice": "o3", "confidence": 0.5, "probabilities": {"o1": 0.5, "o2": 0.5}}},
        {"action": {"type": "choice", "choice": "o2", "confidence": 0.5, "probabilities": {"o1": 0.9, "o2": 0.1}}},
        {"action": {"type": "choice", "choice": "o1", "confidence": 0.5, "probabilities": {"o1": 0.9}}},
        {"action": {"type": "choice", "choice": "o1", "confidence": 0.5, "probabilities": {"o1": 0.9, "o2": 0.5}}},
    ],
)
def test_parse_rejects_inconsistent_answers(answers: dict[str, Any]) -> None:
    full = response()["answers"] | answers
    with pytest.raises(JevError):
        parse_response(response(answers=full), "typesafe/jev-1.14", OPTIONS)


def test_parse_rejects_model_drift() -> None:
    with pytest.raises(JevError):
        parse_response(response(model="other/model"), "typesafe/jev-1.14", OPTIONS)


def test_request_limits() -> None:
    with pytest.raises(JevError):
        build_request("m", {}, {f"o{i}": "x" for i in range(256)})
    with pytest.raises(JevError):
        build_request("m", {}, {"bad key!": "x"})
    q = build_request("m", {}, OPTIONS)["questions"]
    assert set(q) == {"action", "subgoal_done", "off_track"} and q["subgoal_done"]["type"] == "noul"


def test_chords() -> None:
    assert keys.chord("Ctrl+S") == [0x11, ord("S")]
    assert keys.chord("Win+R") == [0x5B, ord("R")]
    assert keys.chord("Alt+F4") == [0x12, 0x73]


def notepad_task(**params: str) -> Task:
    t = load_template(DATA / "templates" / "notepad_write_note.yaml")
    inst = TaskInstance(template=t.task, params={"file_name": "q3 notes", "launch_query": "notepad", **params},
                        content={"body": "x"})  # fmt: skip
    return Task(t, inst, random.Random(0))


def test_title_check_tolerates_hidden_extension() -> None:
    task = notepad_task()
    done = task.template.subgoals["confirm_save"].done
    for title, ok in [("q3 notes - Notepad", True), ("q3 notes.txt - Notepad", True), ("*q3 notes - Notepad", False)]:
        snap = obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND, process_name="notepad.exe", title=title)])
        assert check_done(done, snap, task, typed_ok=False) is ok, title


def test_absent_window_check() -> None:
    task = notepad_task()
    done = Done(window=WindowCheck(process="notepad.exe", absent=True))
    assert check_done(done, obs.Snapshot(), task, False) is True
    open_np = obs.Snapshot(windows=[obs.Window(process_name="Notepad.exe")])
    assert check_done(done, open_np, task, False) is False
    assert check_done(None, open_np, task, False) is None


def test_policy() -> None:
    p = Policy(reserved_paths=[r"\\finance\\payroll"])
    save = obs.Element(name="Don't Save", hints=["destructive"])
    assert p.element_allowed(save, []) is not None
    assert p.element_allowed(save, ["Don't Save"]) is None
    assert p.element_allowed(obs.Element(name="Empty Recycle Bin"), [".*"]) is not None
    assert p.element_allowed(obs.Element(name="pw", password=True), []) is not None
    assert p.text_allowed(r"C:\finance\payroll\x.txt", None) is not None
    assert p.text_allowed("notes.txt", None) is None


def test_all_templates_load() -> None:
    for path in (DATA / "templates").glob("*.yaml"):
        load_template(path)


def test_near_duplicate() -> None:
    from ludius.planner import near_duplicate

    earlier = ["Call with Johan re Q3 close - accruals missing"]
    assert near_duplicate("call with johan, re Q3 close: accruals missing!", earlier)
    assert near_duplicate("Budget review with Maria for the Lund office move", earlier) is None


def test_plan_validation() -> None:
    from ludius.cli import load_templates
    from ludius.planner import PlanItem, validate

    ok = PlanItem(at="09:10", template="explorer_new_folder", params={"folder_name": "lund accruals q3"})
    items = [
        ok,
        PlanItem(at="12:00", template="break", minutes=45),
        PlanItem(at="13:00", template="no_such_task"),
        PlanItem(at="13:30", template="explorer_new_folder", params={"folder_name": "a/b"}),
        PlanItem(at="14:00", template="edge_web_lookup"),  # search_query missing
        PlanItem(at="15:00", template="explorer_new_folder", params={"folder_name": "Lund accruals Q3!"}),  # dup
        PlanItem(at="16:00", template="edge_web_lookup", params={"search_query": "moms på tjänster"}),
    ]
    assert [i for i, _ in validate(items, load_templates(), [], Policy())] == [2, 3, 4, 5, 6]


def test_growth_checker_accepts_catalog_templates() -> None:
    import yaml

    from ludius import growth
    from ludius.cli import load_templates

    caps = yaml.safe_load((DATA / "capabilities.yaml").read_text(encoding="utf-8"))
    templates = load_templates()
    for name, t in templates.items():
        others = {k: v for k, v in templates.items() if k != name}
        assert growth.check(t, caps, others) == [], name
    bad = templates["explorer_new_folder"].model_copy(update={"processes": ["regedit.exe"]})
    assert any("outside the catalog" in p for p in growth.check(bad, caps, {}))


def test_near_tie_only_between_equivalent_options() -> None:
    from ludius.jev import Decision
    from ludius.loop import Runner
    from ludius.options import Option

    def el(i: int, name: str) -> obs.Element:
        return obs.Element(id=i, key=f"k{i}", role="button", name=name)

    opts = [Option("o1", "click", "a", element=el(1, "Save")), Option("o2", "click", "b", element=el(2, "&Save")),
            Option("o3", "click", "c", element=el(3, "Cancel"))]  # fmt: skip
    d = Decision("o1", 0.5, {"o1": 0.45, "o2": 0.42, "o3": 0.44}, 0, 0, "m")
    runner = Runner.__new__(Runner)
    runner.rng = random.Random(1)
    picks = {runner._choose(d, opts).key for _ in range(40)}  # pyright: ignore[reportPrivateUsage]
    assert picks == {"o1", "o2"}


def test_mapping_recheck_on_display_change(tmp_path: Any) -> None:
    from ludius.loop import GroundTruth, Runner
    from ludius.v1 import agent_pb2 as pb

    class FakeAgent:
        vm = "t"

        def __init__(self) -> None:
            self.size, self.pos, self.offset = (1920, 1080), (0, 0), 0

        def move(self, x: int, y: int, duration_ms: int = 0) -> int:
            self.pos = (x + self.offset, y)
            return 0

        def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
            s = obs.Snapshot(screen_width=self.size[0], screen_height=self.size[1], dpi=96,
                             cursor_x=self.pos[0], cursor_y=self.pos[1])  # fmt: skip
            return pb.ObserveResult(snapshot=s)

    agent = FakeAgent()
    runner = Runner(agent, None, Persona(name="t", user="t"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    runner.observe()
    assert runner.mapping_ok  # first display: nothing to compare
    agent.size = (1280, 720)
    runner.observe()
    assert runner.mapping_ok
    agent.size, agent.offset = (1600, 900), 7  # pointer lands 7 px off after this change
    runner.observe()
    assert not runner.mapping_ok


def test_no_alt_f4_on_the_desktop() -> None:
    """Alt+F4 on the desktop opens Shut Down Windows; an OK there powered the test VM off once."""
    from ludius.options import build_options, view
    from ludius.policy import alt_f4_safe, shell_dialog

    t = load_template(DATA / "templates" / "notepad_write_note.yaml")
    task = Task(t, TaskInstance(template=t.task, params=t.example), random.Random(0))
    sg = task.subgoal("close_app")
    for cls, offered in (("Progman", False), ("Shell_TrayWnd", False), ("Notepad", True)):
        fg = obs.Window(kind=obs.Window.FOREGROUND, class_name=cls, process_name="explorer.exe")
        snap = obs.Snapshot(windows=[fg])
        opts = build_options(view(snap, task, sg, Policy()), task, sg, Policy())
        assert any(o.keys == "Alt+F4" for o in opts) is offered
    assert not alt_f4_safe(None)
    shutdown = obs.Window(kind=obs.Window.FOREGROUND, class_name="#32770", process_name="explorer.exe")
    assert shell_dialog(shutdown)
    assert not shell_dialog(obs.Window(kind=obs.Window.FOREGROUND, class_name="#32770", process_name="notepad.exe"))
