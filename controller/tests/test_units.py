from __future__ import annotations

import random
from typing import Any

import pytest

from ludius import keys
from ludius.cli import DATA
from ludius.jev import JevError, build_request, parse_response
from ludius.policy import Policy
from ludius.tasks import Done, Task, TaskInstance, WindowCheck, check_done, load_template
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
