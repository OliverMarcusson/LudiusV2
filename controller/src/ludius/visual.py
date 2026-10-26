"""Visual fallback and pixels-only observation (spec section 3), host side: a screenshot becomes element-table rows.

OCR runs PaddleOCR's PP-OCR models on ONNX Runtime (rapidocr-onnxruntime; `uv sync --group visual`). Elements use
the observer's format with framework_id "visual" and a hint that they were read from pixels, so Jev knows there is
no accessibility data behind them. Fallback mode fills windows the observer marked no_tree; pixels-only mode builds
the whole snapshot from screenshots, settling when two frames a quiet period apart are identical.

Controls without text (icons, custom-drawn buttons) need a UI-element detector: an ONNX model with YOLOv8-style
output ([1, 4 + classes, candidates], boxes as centre/size in the letterboxed input) at LUDIUS_UI_DETECTOR.
Without one, only text is perceived. OCR words inside a detected control become its name.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ludius.hostagent import HostAgent
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
    control: bool = False  # from the detector, not OCR


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


def letterbox(width: int, height: int, size: int = 640) -> tuple[float, int, int]:
    """Scale and padding that fit an image into the detector's square input without distortion."""
    r = min(size / width, size / height)
    return r, (size - round(width * r)) // 2, (size - round(height * r)) // 2


def _iou(a: Box, b: Box) -> float:
    ix = max(0, min(a.x + a.width, b.x + b.width) - max(a.x, b.x))
    iy = max(0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))
    inter = ix * iy
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0


def decode(rows: list[list[float]], r: float, pad_x: int, pad_y: int, conf: float = 0.3, iou: float = 0.5) -> list[Box]:
    """YOLOv8-style output (rows: cx, cy, w, h, then one score per class; columns: candidates) to control boxes
    in image pixels, after confidence filtering and non-maximum suppression."""
    found: list[Box] = []
    for i in range(len(rows[0]) if rows else 0):
        score = max(rows[k][i] for k in range(4, len(rows)))
        if score < conf:
            continue
        cx, cy, w, h = ((rows[0][i] - pad_x) / r, (rows[1][i] - pad_y) / r, rows[2][i] / r, rows[3][i] / r)
        found.append(Box(round(cx - w / 2), round(cy - h / 2), round(w), round(h), "", score, control=True))
    kept: list[Box] = []
    for b in sorted(found, key=lambda b: -b.score):
        if all(_iou(b, k) < iou for k in kept):
            kept.append(b)
    return kept


def detector_reader(path: str, size: int = 640) -> Reader:
    try:
        ort: Any = importlib.import_module("onnxruntime")
        np: Any = importlib.import_module("numpy")
        image_mod: Any = importlib.import_module("PIL.Image")
    except ImportError as e:
        raise VisualUnavailable(f"needs the visual group (cd controller; uv sync --group visual): {e}") from e
    if not os.path.isfile(path):
        raise VisualUnavailable(f"LUDIUS_UI_DETECTOR: no model file at {path}")
    session: Any = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name

    def read(png: bytes) -> list[Box]:
        img: Any = image_mod.open(io.BytesIO(png)).convert("RGB")
        r, px, py = letterbox(img.width, img.height, size)
        canvas: Any = image_mod.new("RGB", (size, size), (114, 114, 114))
        canvas.paste(img.resize((round(img.width * r), round(img.height * r))), (px, py))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        return decode(session.run(None, {name: x})[0][0].tolist(), r, px, py)

    return read


def merge(texts: list[Box], controls: list[Box]) -> list[Box]:
    """Words whose centre lies in a detected control name it; the rest stay text rows."""
    left = list(texts)
    for c in controls:
        inside = [
            t for t in left if c.x <= t.x + t.width / 2 <= c.x + c.width and c.y <= t.y + t.height / 2 <= c.y + c.height
        ]
        c.text = " ".join(t.text for t in sorted(inside, key=lambda t: (t.y // 8, t.x)))
        left = [t for t in left if t not in inside]
    return controls + left


def reader() -> Reader:
    """OCR, plus the UI-element detector when LUDIUS_UI_DETECTOR names a model."""
    ocr = ocr_reader()
    path = os.environ.get("LUDIUS_UI_DETECTOR")
    if not path:
        return ocr
    detect = detector_reader(path)
    return lambda png: merge(ocr(png), detect(png))


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
        masked = set(b.text.replace(" ", "")) <= MASK
        if not b.control and (b.score < MIN_SCORE or masked):
            continue
        name = "" if masked else b.text[:200]
        role = "button" if b.control else "text"
        n = seen[role + name] = seen.get(role + name, 0) + 1
        # A control without text has only its place to be known by: keyed by a 16 px grid cell.
        ident = f"{role}/{name}/{n}" if name else f"icon/{(dx + b.x * scale) // 16:.0f}/{(dy + b.y * scale) // 16:.0f}"
        e = obs.Element(
            id=ID_BASE + len(out),
            key=hashlib.sha1(f"{FRAMEWORK}/{ident}".encode()).hexdigest()[:16],
            role=role,
            name=name,
            enabled=True,
            framework_id=FRAMEWORK,
            hints=[HINT] if name else [HINT, "icon without text"],
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
        self._read = read or reader()
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


class _Plain:
    """The wrapped agent's own observe/screenshot, for VisualHostAgent."""

    def __init__(self, agent: HostAgent) -> None:
        self._agent = agent

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        return HostAgent.observe(self._agent, wait_settle, quiet_ms, timeout_ms)

    def screenshot(self) -> bytes:
        return HostAgent.screenshot(self._agent)


class VisualHostAgent(HostAgent):
    """A host agent whose snapshots get visual rows for windows without a usable tree (LUDIUS_VISUAL=fallback)."""

    def __init__(self, address: str, vm: str, read: Reader | None = None) -> None:
        super().__init__(address, vm)
        self._visual = VisualAgent(_Plain(self), "fallback", read)

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        return self._visual.observe(wait_settle, quiet_ms, timeout_ms)


def host_agent(address: str, vm: str, mode: str) -> HostAgent:
    """The CLI's agent for LUDIUS_VISUAL: unset is the plain agent, "fallback" adds visual rows."""
    if mode == "":
        return HostAgent(address, vm)
    if mode == "fallback":
        return VisualHostAgent(address, vm)
    raise VisualUnavailable(f"LUDIUS_VISUAL={mode!r}: only 'fallback' is wired; pixels-only needs the visual task path")
