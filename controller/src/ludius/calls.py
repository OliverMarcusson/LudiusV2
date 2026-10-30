from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

PATH = Path(__file__).resolve().parents[3] / "runs" / "openrouter-calls.json"
persona: str | None = os.environ.get("LUDIUS_PERSONA") or None
_local = threading.local()


@contextmanager
def for_persona(user: str) -> Generator[None]:
    _local.persona = user
    try:
        yield
    finally:
        _local.persona = None


Kind = Literal["decision", "llm"]


def _lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


class CapReached(RuntimeError):
    pass


def count(kind: Kind) -> None:
    cap = os.environ.get(f"LUDIUS_CAP_{kind.upper()}")
    who = getattr(_local, "persona", None) or persona
    mine = os.environ.get(f"LUDIUS_CAP_PERSONA_{kind.upper()}") if who else None
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(PATH.with_suffix(".lock"), os.O_RDWR | os.O_CREAT)
        try:
            _lock(fd)
            try:
                old: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if cap is not None:
                    raise
                old = {}
            if "decision" not in old and "jev" in old:
                old["decision"] = old.pop("jev")
            if cap is not None:
                total = old.get(kind)
                if type(total) is not int or total < 0:
                    raise ValueError(f"{kind} total {total!r} is not a count")
                if total >= int(cap):
                    raise CapReached(f"{kind} request cap LUDIUS_CAP_{kind.upper()}={cap} reached; nothing sent")
            if who is not None:
                _persona_count(who, kind, mine)
            now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            data = {
                "decision": int(old.get("decision", 0)) + (kind == "decision"),
                "llm": int(old.get("llm", 0)) + (kind == "llm"),
                "since": old.get("since") or now,
                "updated": now,
            }
            tmp = PATH.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(tmp, PATH)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        if cap is not None or mine is not None:
            raise e if isinstance(e, CapReached) else CapReached(f"{kind} counter unusable: {e}") from e


def persona_path(user: str) -> Path:
    return PATH.parent / "calls" / f"{user}.json"


def persona_day(user: str, day: dt.date | None = None) -> dict[str, int]:
    try:
        data: dict[str, Any] = json.loads(persona_path(user).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    got: dict[str, Any] = data.get((day or dt.date.today()).isoformat()) or {}
    return {k: int(got.get(k, 0)) for k in ("decision", "llm")}


def _persona_count(user: str, kind: Kind, cap: str | None) -> None:
    path = persona_path(user)
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        if cap is not None:
            raise
        data = {}
    day = dt.date.today().isoformat()
    counts = {"decision": 0, "llm": 0, **data.get(day, {})}
    if cap is not None and counts[kind] >= int(cap):
        raise CapReached(f"{user}: {kind} cap LUDIUS_CAP_PERSONA_{kind.upper()}={cap} for {day} reached; nothing sent")
    counts[kind] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({**data, day: counts}), encoding="utf-8")
    os.replace(tmp, path)
