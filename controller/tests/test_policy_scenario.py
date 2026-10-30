from __future__ import annotations

import random
from pathlib import Path

import pydantic
import pytest

from ludius.cli import load_templates
from ludius.loop import Runner
from ludius.options import Option, build_options, view
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance
from ludius.v1 import observation_pb2 as obs

SCENARIO = r"""
reserved:
  - path: 'C:\Users\aberg\Documents\Q3 invoice*.xlsm'
  - path: 'C:\Users\*\Documents\handover'
  - path: 'C:\Users\aberg\Documents\budget.xlsx'
    red_herring: true
mailboxes: [cfo@harbourline.example]
"""


def _policy(tmp_path: Path) -> Policy:
    f = tmp_path / "scenario.yaml"
    f.write_text(SCENARIO, encoding="utf-8")
    return Policy.load(f)


def el(name: str, value: str = "", role: str = "list item") -> obs.Element:
    return obs.Element(id=1, key="k", role=role, name=name, value=value)


def test_reserved_files_folders_and_mailboxes_are_refused(tmp_path: Path) -> None:
    p = _policy(tmp_path)
    assert p.element_allowed(el("Q3 invoice final.xlsm"), []) == "scenario-reserved path"
    assert p.element_allowed(el("handover"), []) == "scenario-reserved path"
    assert p.element_allowed(el("budget.xlsx"), []) is None
    assert p.element_allowed(el("Q3 notes.txt"), []) is None
    address = el("Address", r"C:\Users\aberg\Documents\Q3 invoice 2.xlsm", role="edit")
    assert p.element_allowed(address, []) == "scenario-reserved path"
    assert p.text_allowed(r"c:\users\LNORD\documents\handover\list.txt", None) == "scenario-reserved path"
    assert p.text_allowed(r"C:\Users\a\b\Documents\handover", None) is None
    assert p.text_allowed("Q3 invoice v2.xlsm", None) == "scenario-reserved path"
    assert p.text_allowed("mail cfo@harbourline.example today", None) == "scenario-reserved path"
    assert p.text_allowed("q3 invoice notes", None) is None
    item = PlanItem(at="09:00", template="explorer_new_folder", params={"folder_name": "handover"})
    assert "scenario-reserved" in validate([item], load_templates(), [], p)[0][1]


def test_scenario_file_is_strict_and_default_policy_is_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text("reserved:\n  - pth: x\n", encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        Policy.load(f)
    assert Policy().element_allowed(el("Q3 invoice final.xlsm"), []) is None


def _task(name: str = "explorer_new_folder") -> Task:
    t = load_templates()[name]
    return Task(t, TaskInstance(template=t.task, params=t.example, variant=0), random.Random(0))


def _win(kind: obs.Window.Kind, process: str = "explorer.exe", modal: bool = False) -> obs.Window:
    return obs.Window(title="Documents", class_name="CabinetWClass", process_name=process, kind=kind, modal=modal)


def _row(
    i: int, name: str, w: int = 0, role: str = "list item", selected: bool = False, focused: bool = False
) -> obs.Element:
    bounds = obs.Rect(x=10, y=20 * i, width=80, height=14)
    return obs.Element(
        id=i,
        key=f"k{i}",
        role=role,
        name=name,
        window=w,
        enabled=True,
        selected=selected,
        focused=focused,
        bounds=bounds,
    )


def _keys(tmp_path: Path, windows: list[obs.Window], elements: list[obs.Element], sg: str = "name_folder",
          policy: Policy | None = None) -> list[str]:  # fmt: skip
    t, p = _task(), policy or _policy(tmp_path)
    s = t.subgoal(sg)
    snap = obs.Snapshot(windows=windows, elements=elements)
    return [o.keys for o in build_options(view(snap, t, s, p), t, s, p) if o.kind == "key"]


FG = obs.Window.FOREGROUND
RESERVED = "Q3 invoice final.xlsm"


def test_visible_selected_reserved_item_blocks_activation_keys_only(tmp_path: Path) -> None:
    for sel, foc in ((True, False), (False, True), (True, True)):
        got = _keys(tmp_path, [_win(FG)], [_row(1, RESERVED, selected=sel, focused=foc), _row(2, "notes.txt")])
        assert "Enter" not in got
        assert {"Esc", "Tab", "Up", "Down", "Left", "Right", "Alt+F4"} <= set(got)
    got = _keys(tmp_path, [_win(FG)], [_row(1, RESERVED, selected=True)], sg="rename_folder")
    assert "F2" not in got and "Enter" not in got


def test_keyboard_guard_needs_visible_reserved_evidence_in_the_keyboard_window(tmp_path: Path) -> None:
    ok = ["Enter"]
    assert _keys(tmp_path, [_win(FG)], [_row(1, "notes.txt", selected=True)])[:1] == ok
    assert _keys(tmp_path, [_win(FG)], [_row(1, RESERVED)])[:1] == ok
    off = _row(1, RESERVED, selected=True)
    off.bounds.width = 0
    assert _keys(tmp_path, [_win(FG)], [off])[:1] == ok
    bg = [_win(FG, "notepad.exe"), _win(obs.Window.KIND_UNSPECIFIED)]
    assert _keys(tmp_path, bg, [_row(1, RESERVED, w=1, selected=True)])[:1] == ok
    typing = [_row(1, RESERVED, selected=True), _row(2, "New folder", role="edit", focused=True)]
    assert _keys(tmp_path, [_win(FG)], typing)[:1] == ok
    owned = [_win(FG), _win(obs.Window.OWNED, modal=True)]
    assert _keys(tmp_path, owned, [_row(1, RESERVED, selected=True), _row(2, "OK", w=1, role="button")])[:1] == ok


def test_keyboard_guard_ignores_red_herrings_and_default_policy(tmp_path: Path) -> None:
    herring = [_row(1, "budget.xlsx", selected=True, focused=True)]
    assert "Enter" in _keys(tmp_path, [_win(FG)], herring)
    reserved = [_row(1, RESERVED, selected=True, focused=True)]
    assert "Enter" in _keys(tmp_path, [_win(FG)], reserved, policy=Policy())


def test_post_check_refuses_an_activation_key_chosen_on_a_reserved_target(tmp_path: Path) -> None:
    r = Runner.__new__(Runner)
    check = r._post_check  # pyright: ignore[reportPrivateUsage]
    r.policy = _policy(tmp_path)
    t, sg = _task(), _task().subgoal("name_folder")
    enter = Option("o1", "key", "press Enter", keys="Enter")
    r.snap = obs.Snapshot(windows=[_win(FG)], elements=[_row(1, RESERVED, selected=True)])
    for k in ("Enter", "space", "Ctrl+O", "Alt+Enter", "F2", "Delete"):
        assert check(Option("o9", "key", k, keys=k), sg, t) == "scenario-reserved path", k
    assert check(Option("o2", "key", "press Esc", keys="Esc"), sg, t) is None
    r.snap = obs.Snapshot(windows=[_win(FG)], elements=[_row(1, "notes.txt", selected=True)])
    assert check(enter, sg, t) is None
