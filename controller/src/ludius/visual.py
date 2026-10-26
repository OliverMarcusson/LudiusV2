"""Visual fallback and pixels-only observation (spec section 3), host side: a screenshot becomes element-table rows.

OCR runs PaddleOCR's PP-OCR models on ONNX Runtime (rapidocr-onnxruntime; `uv sync --group visual`). Elements use
the observer's format with framework_id "visual" and a hint that they were read from pixels, so Jev knows there is
no accessibility data behind them. Fallback mode fills windows the observer marked no_tree; pixels-only mode builds
the whole snapshot from screenshots, settling when two frames a quiet period apart are identical.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

FRAMEWORK = "visual"
HINT = "read from pixels, no accessibility data"
ID_BASE = 50_000  # above observer ids, so visual and UIA elements never share one
MIN_SCORE = 0.6
MASK = set("•●*·∙")  # password masking: never a usable name, and never forwarded


class VisualUnavailable(Exception):
    pass


@dataclass
class Box:
    x: int
    y: int
    width: int
    height: int
    text: str
    score: float


Reader = Callable[[bytes], list[Box]]


def ocr_reader() -> Reader:
    """PP-OCR text boxes in the image's pixel coordinates."""
    try:
        engine: Any = importlib.import_module("rapidocr_onnxruntime").RapidOCR()
    except ImportError as e:
        raise VisualUnavailable(f"needs the visual group (cd controller; uv sync --group visual): {e}") from e

    def read(png: bytes) -> list[Box]:
        result: Any = engine(png)[0] or []
        boxes: list[Box] = []
        for quad, text, score in result:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            x, y = int(min(xs)), int(min(ys))
            boxes.append(Box(x, y, int(max(xs)) - x, int(max(ys)) - y, str(text).strip(), float(score)))
        return boxes

    return read


def png_size(png: bytes) -> tuple[int, int]:
    """Width and height from the PNG header (no image library needed)."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def crop(png: bytes, x: int, y: int, width: int, height: int) -> bytes:
    image: Any = importlib.import_module("PIL.Image").open(io.BytesIO(png))
    out = io.BytesIO()
    image.crop((x, y, x + width, y + height)).save(out, format="PNG")
    return out.getvalue()


def elements(boxes: list[Box], window: int, dx: int = 0, dy: int = 0, scale: float = 1.0) -> list[obs.Element]:
    """Rows for one window. Image pixels map to guest pixels by offset and scale; keys repeat across snapshots."""
    out: list[obs.Element] = []
    seen: dict[str, int] = {}
    for b in sorted(boxes, key=lambda b: (b.y // 8, b.x)):  # reading order, lines within 8 px
        if b.score < MIN_SCORE or not b.text or set(b.text.replace(" ", "")) <= MASK:
            continue
        n = seen[b.text] = seen.get(b.text, 0) + 1
        e = obs.Element(
            id=ID_BASE + len(out),
            key=hashlib.sha1(f"{FRAMEWORK}/text/{b.text}/{n}".encode()).hexdigest()[:16],
            role="text",
            name=b.text[:200],
            enabled=True,
            framework_id=FRAMEWORK,
            hints=[HINT],
            window=window,
        )
        e.bounds.x, e.bounds.y = dx + round(b.x * scale), dy + round(b.y * scale)
        e.bounds.width, e.bounds.height = round(b.width * scale), round(b.height * scale)
        out.append(e)
    return out


def fill_no_tree(snap: obs.Snapshot, png: bytes, read: Reader) -> int:
    """Add visual rows for every window the observer could not read; returns how many were added."""
    pw = png_size(png)[0]
    scale = snap.screen_width / pw if snap.screen_width else 1.0  # guest pixels per screenshot pixel
    added = 0
    for i, w in enumerate(snap.windows):
        if not w.no_tree or w.bounds.width <= 0 or w.bounds.height <= 0:
            continue
        # Only the on-screen part: a window hanging off the screen has no pixels there.
        b, (pw, ph) = w.bounds, png_size(png)
        x0, y0 = max(0, round(b.x / scale)), max(0, round(b.y / scale))
        x1, y1 = min(pw, round((b.x + b.width) / scale)), min(ph, round((b.y + b.height) / scale))
        if x1 <= x0 or y1 <= y0:
            continue
        rows = elements(read(crop(png, x0, y0, x1 - x0, y1 - y0)), i, round(x0 * scale), round(y0 * scale), scale)
        for e in rows:
            e.id = ID_BASE + added
            added += 1
        snap.elements.extend(rows)
    return added


def pixels_snapshot(png: bytes, read: Reader, settled: bool, settle_ms: int, seq: int = 0) -> obs.Snapshot:
    """The whole screen as one window. Pixels carry no process or window identity, so those fields stay empty."""
    width, height = png_size(png)
    snap = obs.Snapshot(seq=seq, screen_width=width, screen_height=height, dpi=96, settled=settled, settle_ms=settle_ms)
    w = snap.windows.add(title="screen", framework_id=FRAMEWORK, kind=obs.Window.FOREGROUND, no_tree=True)
    w.bounds.width, w.bounds.height = width, height
    snap.elements.extend(elements(read(png), 0))
    return snap


class Screens(Protocol):
    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult: ...
    def screenshot(self) -> bytes: ...


class VisualAgent:
    """Wraps a host agent. "fallback": the observer's snapshot plus visual rows for no_tree windows. "pixels": no
    observer at all, snapshots come from console screenshots. Input and lifecycle pass through unchanged."""

    def __init__(
        self, inner: Screens, mode: str, read: Reader | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if mode not in ("fallback", "pixels"):
            raise ValueError(f"unknown visual mode {mode!r}")
        self._inner, self.mode, self._clock, self._seq = inner, mode, clock, 0
        self._read = read or ocr_reader()
        self._ids: dict[str, int] = {}  # key -> id, so a control keeps its id across snapshots like UIA rows do

    def _stable_ids(self, snap: obs.Snapshot) -> None:
        for e in snap.elements:
            if e.framework_id == FRAMEWORK:
                e.id = self._ids.setdefault(e.key, ID_BASE + len(self._ids))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        if self.mode == "fallback":
            res = self._inner.observe(wait_settle, quiet_ms, timeout_ms)
            if any(w.no_tree for w in res.snapshot.windows):
                fill_no_tree(res.snapshot, self._inner.screenshot(), self._read)
                self._stable_ids(res.snapshot)
            return res
        start = self._clock()
        png, settled = self._inner.screenshot(), not wait_settle
        while wait_settle:
            time.sleep(quiet_ms / 1000)
            nxt = self._inner.screenshot()
            if nxt == png:
                settled = True
                break
            png = nxt
            if (self._clock() - start) * 1000 >= timeout_ms:
                break
        self._seq += 1
        snap = pixels_snapshot(png, self._read, settled, int((self._clock() - start) * 1000), self._seq)
        self._stable_ids(snap)
        return pb.ObserveResult(snapshot=snap, host_time_ms=int(time.time() * 1000))
