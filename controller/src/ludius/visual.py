from __future__ import annotations

import hashlib
import importlib
import io
import math
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from ludius.hostagent import HostAgent
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs

FRAMEWORK = "visual"
HINT = "read from pixels, no accessibility data"
ID_BASE = 50_000
MIN_SCORE = 0.6
DETECT_SIZE = 1280
DETECT_CONF, DETECT_IOU = 0.05, 0.7
ICON_CONF, MAX_ICONS = 0.2, 40
MASK = set("•●*·∙")


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
    control: bool = False


Reader = Callable[[bytes], list[Box]]


def ocr_reader() -> Reader:
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


def letterbox(width: int, height: int, size: int = DETECT_SIZE) -> float:
    return min(size / width, size / height)


def _iou(a: Box, b: Box) -> float:
    ix = max(0, min(a.x + a.width, b.x + b.width) - max(a.x, b.x))
    iy = max(0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))
    inter = ix * iy
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0


Head = tuple[list[list[float]], list[list[list[float]]], int]


def decode(heads: list[Head], r: float, conf: float = DETECT_CONF, iou: float = DETECT_IOU) -> list[Box]:
    cut = math.log(conf / (1 - conf))
    found: list[Box] = []
    for logits, box, stride in heads:
        for i, row in enumerate(logits):
            for j, logit in enumerate(row):
                if logit < cut:
                    continue
                cx, cy = (j + 0.5) * stride, (i + 0.5) * stride
                x0, y0 = (cx - box[0][i][j] * stride) / r, (cy - box[1][i][j] * stride) / r
                x1, y1 = (cx + box[2][i][j] * stride) / r, (cy + box[3][i][j] * stride) / r
                score = 1 / (1 + math.exp(-logit))
                found.append(Box(round(x0), round(y0), round(x1 - x0), round(y1 - y0), "", score, control=True))
    kept: list[Box] = []
    for b in sorted(found, key=lambda b: -b.score):
        if all(_iou(b, k) < iou for k in kept):
            kept.append(b)
    return kept


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                pass

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


INFERENCE_LOCK = os.environ.get("LUDIUS_DETECTOR_LOCK") or os.path.join(tempfile.gettempdir(), "ludius-detector.lock")
_in_process = threading.Lock()
_detectors: dict[str, Reader] = {}


@contextmanager
def inference_slot(path: str = INFERENCE_LOCK) -> Generator[None]:
    with _in_process, open(path, "a+b") as f:
        f.seek(0)
        _lock(f.fileno())
        try:
            yield
        finally:
            f.seek(0)
            _unlock(f.fileno())


def detector_reader(path: str) -> Reader:
    if path in _detectors:
        return _detectors[path]
    try:
        ort: Any = importlib.import_module("onnxruntime")
        np: Any = importlib.import_module("numpy")
        image_mod: Any = importlib.import_module("PIL.Image")
    except ImportError as e:
        raise VisualUnavailable(f"needs the visual group (cd controller; uv sync --group visual): {e}") from e
    if not os.path.isfile(path):
        raise VisualUnavailable(f"LUDIUS_UI_DETECTOR: no model file at {path}")
    opts: Any = ort.SessionOptions()
    opts.enable_cpu_mem_arena = False
    opts.enable_mem_pattern = False
    session: Any = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name

    def read(png: bytes) -> list[Box]:
        img: Any = image_mod.open(io.BytesIO(png)).convert("RGB")
        r = letterbox(img.width, img.height)
        canvas: Any = image_mod.new("RGB", (DETECT_SIZE, DETECT_SIZE), (114, 114, 114))
        canvas.paste(img.resize((round(img.width * r), round(img.height * r))), (0, 0))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        with inference_slot():
            out: list[Any] = session.run(None, {name: x})
        heads: list[Head] = [(out[k][0][0].tolist(), out[k + 1][0].tolist(), s) for k, s in ((0, 8), (2, 16), (4, 32))]
        return decode(heads, r)

    _detectors[path] = read
    return read


def merge(texts: list[Box], controls: list[Box]) -> list[Box]:
    left = list(texts)
    for c in controls:
        inside = [
            t for t in left if c.x <= t.x + t.width / 2 <= c.x + c.width and c.y <= t.y + t.height / 2 <= c.y + c.height
        ]
        c.text = " ".join(t.text for t in sorted(inside, key=lambda t: (t.y // 8, t.x)))
        left = [t for t in left if t not in inside]
    icons = sorted((c for c in controls if not c.text and c.score >= ICON_CONF), key=lambda c: -c.score)
    return [c for c in controls if c.text] + icons[:MAX_ICONS] + left


def reader() -> Reader:
    ocr = ocr_reader()
    path = os.environ.get("LUDIUS_UI_DETECTOR")
    if not path:
        return ocr
    detect = detector_reader(path)
    return lambda png: merge(ocr(png), detect(png))


def png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def crop(png: bytes, x: int, y: int, width: int, height: int) -> bytes:
    image: Any = importlib.import_module("PIL.Image").open(io.BytesIO(png))
    out = io.BytesIO()
    image.crop((x, y, x + width, y + height)).save(out, format="PNG")
    return out.getvalue()


def elements(
    boxes: list[Box], window: int, dx: int = 0, dy: int = 0, scale: float = 1.0, seen: dict[str, int] | None = None
) -> list[obs.Element]:
    out: list[obs.Element] = []
    seen = {} if seen is None else seen
    for b in sorted(boxes, key=lambda b: (b.y // 8, b.x)):
        masked = set(b.text.replace(" ", "")) <= MASK
        if not b.control and (b.score < MIN_SCORE or masked):
            continue
        name = "" if masked else b.text[:200]
        role = "button" if b.control else "text"
        base = f"{role}/{name}" if name else f"icon/{(dx + b.x * scale) // 16:.0f}/{(dy + b.y * scale) // 16:.0f}"
        n = seen[base] = seen.get(base, 0) + 1
        ident = f"{base}/{n}" if name or n > 1 else base
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
    pw = png_size(png)[0]
    scale = snap.screen_width / pw if snap.screen_width else 1.0
    added = 0
    seen: dict[str, int] = {}
    for i, w in enumerate(snap.windows):
        if not w.no_tree or w.bounds.width <= 0 or w.bounds.height <= 0:
            continue
        b, (pw, ph) = w.bounds, png_size(png)
        x0, y0 = max(0, round(b.x / scale)), max(0, round(b.y / scale))
        x1, y1 = min(pw, round((b.x + b.width) / scale)), min(ph, round((b.y + b.height) / scale))
        if x1 <= x0 or y1 <= y0:
            continue
        crop_rows = read(crop(png, x0, y0, x1 - x0, y1 - y0))
        rows = elements(crop_rows, i, round(x0 * scale), round(y0 * scale), scale, seen)
        for e in rows:
            e.id = ID_BASE + added
            added += 1
        snap.elements.extend(rows)
    return added


def pixels_snapshot(png: bytes, read: Reader, settled: bool, settle_ms: int, seq: int = 0) -> obs.Snapshot:
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
    def __init__(
        self, inner: Screens, mode: str, read: Reader | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if mode not in ("fallback", "pixels"):
            raise ValueError(f"unknown visual mode {mode!r}")
        self._inner, self.mode, self._clock, self._seq = inner, mode, clock, 0
        self._read = read or reader()
        self._ids: dict[str, int] = {}

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
    def __init__(self, agent: HostAgent) -> None:
        self._agent = agent

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        return HostAgent.observe(self._agent, wait_settle, quiet_ms, timeout_ms)

    def screenshot(self) -> bytes:
        return HostAgent.screenshot(self._agent)


class VisualHostAgent(HostAgent):
    def __init__(self, address: str, vm: str, read: Reader | None = None, mode: str = "fallback") -> None:
        super().__init__(address, vm)
        self._visual = VisualAgent(_Plain(self), mode, read)

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        return self._visual.observe(wait_settle, quiet_ms, timeout_ms)


def host_agent(address: str, vm: str, mode: str) -> HostAgent:
    if mode == "":
        return HostAgent(address, vm)
    if mode in ("fallback", "pixels"):
        return VisualHostAgent(address, vm, mode=mode)
    raise VisualUnavailable(f"LUDIUS_VISUAL={mode!r}: expected fallback or pixels")
