"""Visual fallback (spec section 3): screenshot rows in the observer's element format. The OCR checks use the recorded
Save As screenshot, with the dialog's UIA rows as ground truth; they skip without the visual dependency group."""

import importlib.util
import subprocess
import sys
import time
from pathlib import Path
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


def _head(grid: int, stride: int, cells: dict[tuple[int, int], tuple[float, list[float]]]) -> visual.Head:
    logits = [[-20.0] * grid for _ in range(grid)]
    box = [[[0.0] * grid for _ in range(grid)] for _ in range(4)]
    for (i, j), (logit, ltrb) in cells.items():
        logits[i][j] = logit
        for k in range(4):
            box[k][i][j] = ltrb[k]
    return logits, box, stride


def test_raw_detector_head_decodes_to_image_pixels_with_duplicates_suppressed() -> None:
    r = visual.letterbox(1920, 1080)
    assert r == 1280 / 1920  # top-left letterbox: the width fills the input
    # Stride 8, cell (row 50, col 40): centre (324, 404) in input pixels; 2 cells left/up, 3 right/down.
    s8 = _head(160, 8, {(50, 40): (2.0, [2, 2, 3, 3]), (50, 41): (1.0, [3, 2, 2, 3]), (10, 10): (-4.0, [1, 1, 1, 1])})
    s32 = _head(40, 32, {(5, 30): (0.0, [0.5, 0.5, 0.5, 0.5])})  # an icon, score 0.5
    got = visual.decode([s8, s32], r)
    assert [(b.x, b.y, b.width, b.height) for b in got] == [(462, 582, 60, 60), (1440, 240, 48, 48)]
    assert all(b.control for b in got) and abs(got[0].score - 0.8808) < 1e-3  # sigmoid(2); the 0.73 twin suppressed


def test_words_inside_a_control_name_it_and_icons_keep_their_place() -> None:
    controls = [visual.Box(100, 100, 120, 30, "", 0.9, control=True), visual.Box(400, 100, 30, 30, "", 0.8, True)]
    words = [visual.Box(110, 108, 40, 14, "Save", 0.99), visual.Box(155, 108, 40, 14, "copy", 0.99)]
    words.append(visual.Box(100, 300, 60, 14, "Status: ready", 0.95))
    rows = visual.elements(visual.merge(words, controls), window=0)
    assert [(e.role, e.name) for e in rows] == [("button", "Save copy"), ("button", ""), ("text", "Status: ready")]
    assert "icon without text" in rows[1].hints
    moved = visual.elements(visual.merge([], [visual.Box(402, 101, 30, 30, "", 0.8, True)]), window=0)
    assert moved[0].key == rows[1].key  # same 16 px cell: same icon across snapshots
    faint = [visual.Box(10 * i, 500, 8, 8, "", 0.15 if i == 0 else 0.5, True) for i in range(visual.MAX_ICONS + 2)]
    kept = visual.merge([], faint)
    assert len(kept) == visual.MAX_ICONS and faint[0] not in kept  # unnamed icons: likely ones only, capped


def test_cli_agent_adds_visual_rows_only_in_fallback_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from ludius.hostagent import HostAgent

    snap = obs.Snapshot(screen_width=100, screen_height=50)
    w = snap.windows.add(process_name="mspaint.exe", kind=obs.Window.FOREGROUND, no_tree=True)
    w.bounds.width, w.bounds.height = 100, 50

    def observe(self: HostAgent, *args: object) -> pb.ObserveResult:
        return pb.ObserveResult(snapshot=snap)

    def screenshot(self: HostAgent) -> bytes:
        return _png(100, 50, b"x")

    def whole(png: bytes, x: int, y: int, width: int, height: int) -> bytes:
        return png

    monkeypatch.setattr(HostAgent, "observe", observe)
    monkeypatch.setattr(HostAgent, "screenshot", screenshot)
    monkeypatch.setattr(visual, "crop", whole)

    assert type(visual.host_agent("127.0.0.1:1", "vm", "")) is HostAgent
    with pytest.raises(visual.VisualUnavailable, match="pixels-only"):
        visual.host_agent("127.0.0.1:1", "vm", "pixels")
    agent = visual.VisualHostAgent("127.0.0.1:1", "vm", read=lambda png: [visual.Box(5, 5, 20, 10, "Colours", 0.9)])
    rows = agent.observe().snapshot.elements
    assert [(e.name, e.window, e.framework_id) for e in rows] == [("Colours", 0, "visual")]


def test_detector_inference_waits_for_another_process_holding_the_slot(tmp_path: Path) -> None:
    lock = str(tmp_path / "detector.lock")
    hold = (
        "import sys, time\nfrom ludius import visual\n"
        "with visual.inference_slot(sys.argv[1]):\n    print('held', flush=True)\n    time.sleep(1.5)\n"
    )
    other = subprocess.Popen([sys.executable, "-c", hold, lock], stdout=subprocess.PIPE, text=True)
    assert other.stdout is not None and other.stdout.readline().strip() == "held"
    start = time.monotonic()
    with visual.inference_slot(lock):
        waited = time.monotonic() - start
    other.wait(10)
    assert waited > 0.8  # the second VM's inference ran only after the first one's


def test_pixels_snapshot_never_passes_window_checks_never_gets_alt_f4_and_normalize_only_escapes(
    tmp_path: Path,
) -> None:
    from ludius import keys
    from ludius.loop import GroundTruth
    from ludius.policy import alt_f4_safe
    from ludius.session import Session
    from ludius.tasks import Done, WindowCheck, check_done, pixels_window

    def screen(*names: str) -> obs.Snapshot:
        return visual.pixels_snapshot(_png(1920, 1080, b"s"), lambda png: [
            visual.Box(10, 10 + 30 * i, 200, 20, n, 0.99) for i, n in enumerate(names)
        ], settled=True, settle_ms=0)  # fmt: skip

    snap = screen("Untitled - Notepad", "File")
    fg = snap.windows[0]
    assert pixels_window(fg) and not alt_f4_safe(fg)
    task: Any = None  # never consulted: the pixels guard decides first
    for done in [Done(window=WindowCheck(process="notepad.exe")), Done(foreground=WindowCheck(absent=True, title="x"))]:
        assert check_done(done, snap, task, typed_ok=True) is False

    class Screens:
        def __init__(self, snaps: list[obs.Snapshot]) -> None:
            self.snaps, self.chords = snaps, list[list[int]]()

        def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
            return pb.ObserveResult(snapshot=self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0])

        def chord(self, vks: list[int]) -> int:
            self.chords.append(vks)
            return 0

    agent = Screens([screen("Shut Down Windows", "OK"), screen("Shut Down Windows", "OK"), screen("Recycle Bin")])
    s = Session(agent, GroundTruth(tmp_path / "gt.jsonl"), "pw")  # pyright: ignore[reportArgumentType]
    assert s.normalize(rounds=4)
    assert agent.chords == [keys.chord("Esc")]  # Esc for Shut Down, never Alt+F4, never a click
