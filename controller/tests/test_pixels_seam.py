"""Pixels-only scope and completion (tasks.Pixels). Row names are OCR readings from the recorded fixtures."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pydantic
import pytest

from ludius.cli import load_templates
from ludius.options import build_options, build_state, view
from ludius.policy import Policy
from ludius.tasks import Done, PixelsCheck, Task, TaskInstance, Template, check_done
from ludius.v1 import observation_pb2 as obs

DESKTOP = ["Recycle Bin", "ype here to search"]
NOTEPAD_EMPTY = ["Untitled-Notepad", "FileEdit Format View Help", "Ln 1, Col 1", "ype here to search"]
SAVE_AS = ["*Untitled -Notepad", "File Edit Format View Help", "Save As", "File name:", "Save as type:", "Save"]
BOUNDS = Path(__file__).parents[2] / "fixtures" / "pixels-rows-bounds.json"
UNSAVED_PROMPT = ["*Untitled -Notepad", "Do you want to save changes to Untitled?", "Save", "Don't Save", "Cancel"]


def screen(names: list[str]) -> obs.Snapshot:
    """A pixels-only snapshot: one whole-screen window, no process identity, OCR rows."""
    w = obs.Window(title="screen", framework_id="visual", kind=obs.Window.FOREGROUND, no_tree=True)
    rows = [obs.Element(id=n + 1, key=f"v{n}", role="text", name=name, framework_id="visual", window=0,
                        bounds=obs.Rect(x=10, y=20 * n, width=60, height=14))
            for n, name in enumerate(names)]  # fmt: skip
    return obs.Snapshot(screen_width=1920, screen_height=1080, dpi=96, windows=[w], elements=rows)


def real(name: str) -> obs.Snapshot:
    """A fixture screen as the production reader (OCR + detector) emits it, with bounds."""
    sc = json.loads(BOUNDS.read_text(encoding="utf-8"))["screens"][name]
    w = obs.Window(title="screen", framework_id="visual", kind=obs.Window.FOREGROUND, no_tree=True)
    rows = [obs.Element(id=n + 1, key=f"v{n}", role=r["role"], name=r["name"], framework_id="visual", window=0,
                        bounds=obs.Rect(x=r["x"], y=r["y"], width=r["w"], height=r["h"]))
            for n, r in enumerate(sc["rows"])]  # fmt: skip
    return obs.Snapshot(screen_width=sc["screen"][0], screen_height=sc["screen"][1], dpi=96, windows=[w], elements=rows)


def task(name: str = "notepad_write_note", **params: str) -> Task:
    t = load_templates()[name]
    return Task(t, TaskInstance(template=t.task, params=params or t.example, variant=0), random.Random(0))


def clicks(snap: obs.Snapshot, t: Task, sg: str, policy: Policy | None = None) -> list[str]:
    p = policy or Policy()
    s = t.subgoal(sg)
    return [o.text for o in build_options(view(snap, t, s, p), t, s, p) if o.kind == "click"]


def test_scope_needs_title_and_structural_anchor() -> None:
    t = task()
    assert clicks(screen(DESKTOP), t, "write_body") == []
    assert clicks(screen(["Untitled-Notepad", "Recycle Bin"]), t, "write_body") == []  # title alone: out
    assert clicks(screen(["notes", "FileEdit Format View Help"]), t, "write_body") == []  # anchor alone: out
    for rows in (NOTEPAD_EMPTY, SAVE_AS, UNSAVED_PROMPT):
        assert clicks(screen(rows), t, "close_app")  # all three Notepad screens are in scope
    assert any("Don't Save" in c for c in clicks(screen(UNSAVED_PROMPT), t, "close_app"))
    # Launching gets no shell rows from pixels: keys only.
    assert clicks(screen(DESKTOP), t, "open_from_start") == []


def test_template_without_pixels_block_gets_nothing() -> None:
    t = task("explorer_new_folder")
    assert t.template.pixels is None
    assert clicks(screen(["New folder", "This PC", "File Home Share View"]), t, "go_documents") == []


def test_policy_still_refuses_rows_in_scope() -> None:
    p = Policy(reserved_names=["Don't Save"])
    assert not any("Don't Save" in c for c in clicks(screen(UNSAVED_PROMPT), task(), "close_app", p))


def test_completion_is_visible_text_and_fails_closed() -> None:
    t = task(file_name="meeting notes", body="x")

    def done(sg: str, rows: list[str]) -> bool | None:
        return check_done(t.subgoal(sg).done, screen(rows), t, typed_ok=True)

    assert done("save_shortcut", SAVE_AS) is True
    assert done("save_shortcut", NOTEPAD_EMPTY) is False
    assert done("confirm_save", ["meeting notes.txt - Notepad", "File Edit Format View Help"]) is True
    assert done("confirm_save", ["meeting notes2.txt - Notepad"]) is False
    assert done("close_app", DESKTOP) is True
    assert done("close_app", [*DESKTOP, "Untitled-Notepad"]) is False
    for sg in ("open_from_start", "save_shortcut", "confirm_save", "close_app"):
        assert done(sg, []) is False  # empty or failed OCR never completes anything
    # With an accessibility tree the pixels check is ignored: the usual process checks apply.
    tree = obs.Snapshot(windows=[obs.Window(title="Untitled - Notepad", process_name="notepad.exe",
                                            kind=obs.Window.FOREGROUND)])  # fmt: skip
    assert check_done(t.subgoal("close_app").done, tree, t, typed_ok=True) is False


def test_schema_enforces_title_anchors_and_present() -> None:
    base = load_templates()["notepad_write_note"].model_dump(exclude={"pixels"})
    cases: list[dict[str, Any]] = [
        {"title": "-Notepad$", "anchors": []},  # no structural anchor
        {"anchors": ["^FileEdit"]},  # no title
        {
            "title": "-Notepad$",
            "anchors": ["^FileEdit"],
            "done": {"close_app": {"absent": ["-Notepad$"]}},
        },  # no present
    ]
    for bad in cases:
        with pytest.raises(pydantic.ValidationError):
            Template.model_validate(base | {"pixels": bad})
    with pytest.raises(pydantic.ValidationError, match="unknown subgoal"):
        Template.model_validate(
            base | {"pixels": {"title": "x", "anchors": ["y"], "done": {"nope": {"present": ["z"]}}}}
        )


def test_confirm_save_matches_the_real_saved_title_reading() -> None:
    # Guest OCR of a saved note (runs/visual/20261026-223919-saved-title.txt): the title icon reads as "I".
    real = ["I pixels-title-20261026-223919 - Notepad", "File   Edit  Format   View  Help", "Type here to search"]
    done = task(file_name="pixels-title-20261026-223919", body="x").subgoal("confirm_save").done
    assert check_done(done, screen(real), task(file_name="pixels-title-20261026-223919", body="x"), True) is True
    other = task(file_name="pixels-title-20261026-223919-other", body="x")
    assert check_done(other.subgoal("confirm_save").done, screen(real), other, True) is False


def test_unsaved_prompt_stays_in_scope_when_the_detector_merges_its_title_into_the_question() -> None:
    # Production reader (OCR + detector) on notepad-unsaved-prompt: one control box holds the dialog title and question.
    merged = ["*Untitled -Notepad", "Notepad Do you want to save changes to Untitled?", "Save", "Don't Save", "Cancel"]
    assert any("Don't Save" in c for c in clicks(screen(merged), task(), "close_app"))


def test_region_keeps_the_notepad_rows_and_drops_desktop_and_taskbar() -> None:
    t = task()
    shell = ["RecycleBin", "Recycle Bin", "Edge", "Microsoft", "Type here to search", "ENG", "12:51 26/10/2026"]
    need = {
        "notepad-empty": ["FileEdit Format View Help", "Ln 1, Col 1"],
        "notepad-unsaved-prompt": ["Save", "Don't Save", "Cancel"],
        "notepad-save-as": ["File name:", "Save as type:", "Save", "Cancel", "26/10/2026 12:42", "q3 close call notes"],
    }
    for name, rows in need.items():
        got = clicks(real(name), t, "close_app")
        assert all(any(r in c for c in got) for r in rows), (name, got)
        assert not any(any(x in c for x in shell) for c in got), (name, got)
    assert len(view(real("notepad-empty"), t, t.subgoal("close_app"), Policy()).allowed) == 17
    assert len(view(real("notepad-save-as"), t, t.subgoal("close_app"), Policy()).allowed) == 63
    for name in ("desktop", "start-menu"):
        assert clicks(real(name), t, "close_app") == []
    # Without the search row the taskbar is assumed to be its 96 dpi height (48 px) at the bottom of the screen.
    empty = real("notepad-empty")
    bar = [e for e in empty.elements if "search" in e.name]
    for e in bar:
        empty.elements.remove(e)
    assert not any("12:51" in c for c in clicks(empty, t, "close_app"))


def test_completion_on_real_rows_reads_the_whole_screen() -> None:
    t = task(file_name="meeting notes", body="x")

    def done(sg: str, name: str) -> bool | None:
        return check_done(t.subgoal(sg).done, real(name), t, typed_ok=True)

    assert done("save_shortcut", "notepad-save-as") is True
    assert done("save_shortcut", "notepad-empty") is False
    assert done("close_app", "desktop") is True
    assert done("close_app", "notepad-empty") is False


def test_pixels_focus_is_unknown_and_the_hint_is_pixels_only() -> None:
    base = load_templates()["notepad_write_note"].model_dump()
    hint = "If nothing else was clicked since opening, try typing at the keyboard focus."
    raw = base | {"pixels": base["pixels"] | {"hints": {"write_body": hint}}}
    t = Task(Template.model_validate(raw), TaskInstance(template="notepad_write_note", params=base["example"],
                                                        variant=0), random.Random(0))  # fmt: skip
    sg = t.subgoal("write_body")
    snap = real("notepad-empty")
    state = build_state(view(snap, t, sg, Policy()), t, sg, [], "")
    assert state["focused"] == "unknown (read from pixels)"  # pixels carry no focus: never "none"
    assert state["goal"].endswith(hint)
    tree = obs.Snapshot(windows=[obs.Window(title="Untitled - Notepad", process_name="notepad.exe",
                                            kind=obs.Window.FOREGROUND)])  # fmt: skip
    state = build_state(view(tree, t, sg, Policy()), t, sg, [], "")
    assert state["focused"] == "none" and hint not in state["goal"]
    with pytest.raises(pydantic.ValidationError, match="unknown subgoal"):
        Template.model_validate(base | {"pixels": base["pixels"] | {"hints": {"nope": hint}}})


def test_pixels_never_pass_on_typed_alone() -> None:
    # Typing at focus counts as landed with no element to read back: on pixels only a visible check can pass.
    t = task(file_name="meeting notes", body="x")
    assert check_done(Done(typed=True), real("notepad-empty"), t, typed_ok=True) is False
    assert check_done(t.subgoal("name_file").done, real("notepad-save-as"), t, typed_ok=True) is False


def test_head_of_a_multi_line_value_is_its_first_visible_line() -> None:
    body = Done(typed=True, pixels=PixelsCheck(present=["^{body_head}"]))
    prompt = real("notepad-unsaved-prompt")  # its body row reads "Short note for the fixture."

    def done(value: str, snap: obs.Snapshot = prompt, typed_ok: bool = True) -> bool | None:
        return check_done(body, snap, task(file_name="n", body=value), typed_ok)

    head = task(file_name="n", body="Short note for the fixture, then a long tail\nline 2").values["body_head"]
    assert head == "Short note for the fixtu"
    assert done("Short note for the fixture.\nline 2") is True
    assert done("\n\n  Short note for the fixture.") is True  # leading blank lines are skipped
    assert done("Short note for the fixture.", typed_ok=False) is False  # the typing must have landed too
    assert done("A different note\nline 2") is False
    assert done("   \n ") is False  # no visible line: no head, the check can't match
    assert done("Short note for the fixture.", real("notepad-empty")) is False
    assert done("Short note for the fixture.", screen([])) is False  # empty OCR read
