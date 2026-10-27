"""Pixels-only scope and completion (tasks.Pixels). Row names are OCR readings from the recorded fixtures."""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest

from ludius.cli import load_templates
from ludius.jev import Decision
from ludius.loop import FILENAME_ROWS, FILENAME_TEXT, Runner
from ludius.options import Option, build_options, build_state, view
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


def test_notepad_template_pixels_checks_for_body_and_file_name_on_real_rows() -> None:
    def rows(name: str, value: str | None = None, drop: str = "") -> obs.Snapshot:
        s = real(name)
        for e in [e for e in s.elements if e.name == drop]:
            s.elements.remove(e)
        for e in s.elements:
            if value is not None and e.name == "*.txt":
                e.name = value  # the field's value row, as read live (runs/visual/20261026-230211-pixels-walk.txt)
        return s

    def done(t: Task, sg: str, snap: obs.Snapshot) -> bool | None:
        return check_done(t.subgoal(sg).done, snap, t, typed_ok=True)

    empty_ocr = real("notepad-empty")
    del empty_ocr.elements[:]
    body = task(file_name="x", body="Short note for the fixture.\nline 2")
    assert done(body, "write_body", real("notepad-unsaved-prompt")) is True
    assert done(task(file_name="x", body="Another note\nline 2"), "write_body", real("notepad-unsaved-prompt")) is False
    assert done(task(file_name="x", body="  \n "), "write_body", real("notepad-unsaved-prompt")) is False
    star_only = rows("notepad-unsaved-prompt", drop="Short note for the fixture.")
    assert done(body, "write_body", star_only) is False
    assert done(body, "write_body", real("notepad-empty")) is False
    assert done(body, "write_body", empty_ocr) is False

    name = "pixels-walk-20261026-230211"
    t = task(file_name=name, body="x")
    typo = t.values["file_name_typo"]
    assert done(t, "name_file", rows("notepad-save-as", rf"C:\Users\aberg\Documents\{name}")) is True
    assert done(t, "name_file", rows("notepad-save-as", name)) is True
    assert done(t, "name_file", real("notepad-save-as")) is False  # '*.txt' before typing
    assert done(task(file_name=name + "-other", body="x"), "name_file", rows("notepad-save-as", name)) is False
    assert done(t, "name_file", empty_ocr) is False
    assert done(t, "name_file_typo", rows("notepad-save-as", typo)) is True
    assert done(t, "fix_file_name", rows("notepad-save-as", typo)) is False
    assert done(t, "fix_file_name", rows("notepad-save-as", name)) is True
    assert done(t, "name_file_typo", rows("notepad-save-as", name)) is False
    assert done(t, "fix_file_name", empty_ocr) is False


def _step_log(snap: obs.Snapshot, t: Task, sg: str, typed_ok: bool, kind: str = "type") -> dict[str, Any]:
    """The step event Runner._log_step writes after an action, without a live loop."""
    events: list[dict[str, Any]] = []

    def write(**kw: Any) -> None:
        events.append(kw)

    me = SimpleNamespace(log=SimpleNamespace(write=write), persona=SimpleNamespace(user="u"),
                         agent=SimpleNamespace(vm="vm"))  # fmt: skip
    d = Decision(choice="o1", confidence=1.0, probabilities={"o1": 1.0}, subgoal_done=0.0, off_track=0.0, model="m")
    Runner._log_step(me, t, sg, snap, Option("o1", kind, "type the text"), d, "changed", 3,  # type: ignore[arg-type]
                   check=(t.subgoal(sg), typed_ok))  # fmt: skip
    return events[0]


def _row(i: int, name: str, x: int, y: int, w: int, h: int = 20, password: bool = False) -> obs.Element:
    return obs.Element(id=i, key=f"x{i}", role="text", name=name, framework_id="visual", password=password,
                       bounds=obs.Rect(x=x, y=y, width=w, height=h))  # fmt: skip


def test_filename_step_logs_the_verdict_and_bounded_rows_beside_the_label() -> None:
    name = "pixels-walk-20261026-230211"
    t = task(file_name=name, body="x")
    typed = rf"C:\Users\aberg\Documents\{name}"  # as read live (runs/visual/20261026-230211-pixels-walk.txt)
    snap = real("notepad-save-as")
    for e in snap.elements:
        if e.name == "*.txt":
            e.name = typed
    ev = _step_log(snap, t, "name_file", typed_ok=True)
    f = ev["filename"]
    assert f["verdict"] is True and f["typed_ok"] is True and f["geometry_only"] is True
    assert f["labels"] == 1 and f["label"]["name"] == "File name:"
    # Level with the label and reaching right of it: the value row only, not "Save as type:" below.
    assert [c["name"] for c in f["candidates"]] == [typed]
    assert "Save as type:" not in json.dumps(f)
    # The verdict is check_done with the loop's typed_ok; this check has no `typed` condition, so it is unaffected.
    assert _step_log(snap, t, "name_file", typed_ok=False)["filename"]["typed_ok"] is False
    assert _step_log(snap, task(file_name=name + "-other", body="x"), "name_file", True)["filename"]["verdict"] is False


def test_filename_step_before_typing_shows_the_placeholder_and_misread_rows() -> None:
    t = task(file_name="meeting notes", body="x")
    f = _step_log(real("notepad-save-as"), t, "name_file", True)["filename"]
    assert f["verdict"] is False and [c["name"] for c in f["candidates"]] == ["*.txt"]
    # A label merged with an appended-extension value: it is the label row itself, shown as read.
    snap = screen([])
    snap.elements.extend(
        [
            _row(1, "File name: meeting notes.txt.txt", 288, 688, 200),
            _row(2, "meeting notes", 700, 690, 80),
            _row(3, "meeting notes.txt", 400, 684, 200),
        ]
    )
    f = _step_log(snap, t, "name_file", True)["filename"]
    assert f["label"]["name"] == "File name: meeting notes.txt.txt" and f["labels"] == 1
    assert [c["name"] for c in f["candidates"]] == ["meeting notes.txt", "meeting notes"]  # by x


def test_filename_step_missing_label_empty_ocr_and_bounds() -> None:
    t = task(file_name="meeting notes", body="x")
    gone = real("notepad-save-as")
    gone.elements.remove(next(e for e in gone.elements if e.name == "File name:"))
    f = _step_log(gone, t, "name_file", True)["filename"]
    assert f["verdict"] is False and f["label"] is None and f["labels"] == 0 and "candidates" not in f
    empty = real("notepad-save-as")
    del empty.elements[:]
    f = _step_log(empty, t, "name_file", True)["filename"]
    assert f == {"verdict": False, "typed_ok": True, "rows": 0, "labels": 0, "label": None, "geometry_only": True}
    # Capped rows and text; masked or password rows beside the label are never listed.
    snap = real("notepad-save-as")
    label = next(e for e in snap.elements if e.name == "File name:").bounds
    snap.elements.extend(_row(900 + i, "y" * 200, 400 + i, label.y, 50) for i in range(10))
    snap.elements.extend(_row(950 + i, n, 300, label.y, 90, password=n == "hunter2")
                         for i, n in enumerate(["••••••", "***", "hunter2"]))  # fmt: skip
    f = _step_log(snap, t, "name_file", True)["filename"]
    assert len(f["candidates"]) == FILENAME_ROWS and f["candidates_dropped"] == 11 - FILENAME_ROWS  # 10 + '*.txt'
    assert all(len(c["name"]) <= FILENAME_TEXT for c in f["candidates"])
    assert not {"••••••", "***", "hunter2"} & {c["name"] for c in f["candidates"]}


def test_only_pixels_filename_steps_get_the_diagnostic() -> None:
    t = task(file_name="meeting notes", body="x")
    for sg in ("save_shortcut", "write_body", "confirm_save", "close_app"):
        assert "filename" not in _step_log(real("notepad-save-as"), t, sg, True)
    # With an accessibility tree there is no pixels diagnostic either.
    tree = obs.Snapshot(windows=[obs.Window(title="Save As", process_name="notepad.exe", kind=obs.Window.FOREGROUND)])
    assert "filename" not in _step_log(tree, t, "name_file", True)
