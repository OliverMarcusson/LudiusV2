"""Visual fallback (spec section 3): screenshot rows in the observer's element format. The OCR checks use the recorded
Save As screenshot, with the dialog's UIA rows as ground truth; they skip without the visual dependency group."""

import importlib.util
from typing import Any

import pytest
from google.protobuf import json_format

from ludius import visual
from ludius.cli import ROOT
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

SAVE_AS = ROOT / "fixtures" / "notepad-save-as"
HAS_OCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None


def test_rows_map_to_guest_pixels_in_reading_order_with_repeatable_keys() -> None:
    boxes = [
        visual.Box(50, 10, 20, 10, "Cancel", 0.95),
        visual.Box(10, 12, 20, 10, "Save", 0.99),
        visual.Box(10, 40, 20, 10, "Save", 0.9),
        visual.Box(10, 60, 20, 10, "noise", 0.3),
    ]
    rows = visual.elements(boxes, window=2, dx=100, dy=200, scale=2.0)
    assert [r.name for r in rows] == ["Save", "Cancel", "Save"]  # same line first, low score dropped
    assert (rows[0].bounds.x, rows[0].bounds.y, rows[0].bounds.width) == (120, 224, 40)
    assert all(r.framework_id == "visual" and r.window == 2 and visual.HINT in r.hints for r in rows)
    assert rows[0].key != rows[2].key  # two "Save" on screen stay distinct
    assert [r.key for r in visual.elements(boxes, window=0)] == [r.key for r in rows]


class FakeScreens:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = frames
        self.shots = 0

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        raise AssertionError("pixels-only mode never asks the observer")

    def screenshot(self) -> bytes:
        self.shots += 1
        return self.frames[min(self.shots, len(self.frames)) - 1]

    def click(self) -> str:
        return "clicked"


def _png(width: int, height: int, tag: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + tag


def test_pixels_only_settles_on_two_equal_frames_and_passes_input_through() -> None:
    a, b = _png(1920, 1080, b"a"), _png(1920, 1080, b"b")
    screens = FakeScreens([a, b, b])
    agent = visual.VisualAgent(screens, "pixels", read=lambda png: [visual.Box(1, 2, 3, 4, "Start", 0.9)])
    snap = agent.observe(quiet_ms=1).snapshot
    assert snap.settled and screens.shots == 3
    assert (snap.screen_width, snap.screen_height) == (1920, 1080)
    assert snap.windows[0].no_tree and snap.windows[0].process_name == ""  # pixels carry no process identity
    assert [e.name for e in snap.elements] == ["Start"]
    assert agent.click() == "clicked"

    moving = FakeScreens([_png(8, 8, bytes([i])) for i in range(50)])
    ticks = iter(range(0, 10_000, 1))
    agent = visual.VisualAgent(moving, "pixels", read=lambda png: [], clock=lambda: next(ticks) / 1000)
    assert not agent.observe(quiet_ms=1, timeout_ms=5).snapshot.settled


def _save_as_without_tree() -> tuple[obs.Snapshot, dict[str, Any]]:
    snap = json_format.Parse((SAVE_AS / "snapshot.json").read_text(encoding="utf-8"), obs.Snapshot())
    truth = {e.name: e.bounds for e in snap.elements if e.window == 0}
    kept = [e for e in snap.elements if e.window != 0]
    del snap.elements[:]
    snap.elements.extend(kept)
    snap.windows[0].no_tree = True
    return snap, truth


@pytest.mark.skipif(not HAS_OCR, reason="visual group not installed (uv sync --group visual)")
def test_ocr_fallback_finds_the_dialog_controls_where_uia_had_them() -> None:
    snap, truth = _save_as_without_tree()
    added = visual.fill_no_tree(snap, (SAVE_AS / "screen.png").read_bytes(), visual.ocr_reader())
    rows = [e for e in snap.elements if e.framework_id == "visual"]
    assert added == len(rows) > 10 and all(e.window == 0 for e in rows)
    assert all(e.id >= visual.ID_BASE for e in rows)
    for name in ["Save", "Cancel", "New folder", "Hide Folders"]:
        t = truth[name]
        hit = [e for e in rows if e.name == name]
        assert hit, f"OCR missed {name!r}; got {[e.name for e in rows]}"
        cx, cy = hit[0].bounds.x + hit[0].bounds.width // 2, hit[0].bounds.y + hit[0].bounds.height // 2
        assert t.x <= cx <= t.x + t.width and t.y <= cy <= t.y + t.height, f"{name!r} at {cx},{cy}, UIA {t}"
    # Rows stay inside the dialog: nothing from the desktop behind it.
    w = snap.windows[0].bounds
    assert all(w.x <= e.bounds.x and e.bounds.x + e.bounds.width <= w.x + w.width + 1 for e in rows)


def test_password_masks_are_dropped_and_ids_stay_stable_across_snapshots() -> None:
    frames = [_png(100, 50, b"a")] * 6
    words = [[visual.Box(1, 1, 5, 5, "Save", 0.9), visual.Box(1, 20, 5, 5, "•••• ••", 0.99)]]
    agent = visual.VisualAgent(FakeScreens(frames), "pixels", read=lambda png: words[0])
    first = agent.observe(quiet_ms=1).snapshot
    assert [e.name for e in first.elements] == ["Save"]  # masked password text never becomes a row
    words[0] = [visual.Box(1, 1, 5, 5, "Cancel", 0.9), visual.Box(9, 1, 5, 5, "Save", 0.9)]
    second = agent.observe(quiet_ms=1).snapshot
    ids = {e.name: e.id for e in second.elements}
    assert ids["Save"] == first.elements[0].id and ids["Cancel"] != ids["Save"]


@pytest.mark.skipif(not HAS_OCR, reason="visual group not installed (uv sync --group visual)")
def test_fallback_reads_only_the_on_screen_part_of_a_window() -> None:
    snap, _ = _save_as_without_tree()
    w = snap.windows[0].bounds
    w.x, w.width = -300, w.width + 300  # hangs 300 px off the left edge
    seen: list[tuple[int, int]] = []

    def read(png: bytes) -> list[visual.Box]:
        seen.append(visual.png_size(png))
        return [visual.Box(0, 0, 10, 10, "Edge", 0.9)]

    visual.fill_no_tree(snap, (SAVE_AS / "screen.png").read_bytes(), read)
    assert seen == [(w.width - 300, w.height)]
    row = next(e for e in snap.elements if e.framework_id == "visual")
    assert (row.bounds.x, row.bounds.y) == (0, w.y)  # the crop's origin is the screen edge, not the window's
