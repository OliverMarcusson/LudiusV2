from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from ludius import mail

TEMPLATES = Path(__file__).resolve().parents[2] / "data" / "templates"
KINDS = {"mail.send": "mail", "mail.reply": "reply", "calendar.invite": "invite"}
MAX_READ = 8 << 20


@dataclass
class Interaction:
    id: str
    personas: list[str]
    kind: str
    label: str
    host: str
    logical: str | None


def kinds(templates: Path = TEMPLATES) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in templates.glob("*.yaml"):
        try:
            doc: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        cap = cast(dict[str, Any], doc).get("capability") if isinstance(doc, dict) else None
        if cap in KINDS:
            out[p.stem] = KINDS[cap]
    return out


def _persona(address: Any, personas: set[str]) -> str | None:
    local, at, domain = str(address or "").strip().lower().partition("@")
    return local if at and domain == mail.DOMAIN.lower() and local in personas else None


def _iso(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat(timespec="seconds")


def from_day_log(log: Path, sender: str, personas: set[str], sends: dict[str, str],
                 requests: dict[str, dict[str, Any]]) -> list[Interaction]:  # fmt: skip
    out: list[Interaction] = []
    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - MAX_READ))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return out
    for text in lines:
        try:
            parsed: Any = json.loads(text)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        e = cast(dict[str, Any], parsed)
        ev, ms = e.get("event"), e.get("host_ms")
        if not isinstance(ms, int):
            continue
        if ev == "item_end" and e.get("status") == "done" and e.get("template") in sends:
            params: dict[str, Any] = e.get("params") or {}
            to = _persona(params.get("to"), personas)
            if to and to != sender:
                out.append(Interaction(f"{log.name}:{ms}", [sender, to], sends[e["template"]],
                                       str(params.get("subject") or ""), _iso(ms), e.get("logical")))  # fmt: skip
        elif ev == "request_check" and e.get("fulfilled") is True:
            r = requests.get(str(e.get("request")))
            boss = str((r or {}).get("manager") or "")
            if r and boss in personas and boss != sender:
                out.append(Interaction(f"done:{e['request']}", [sender, boss], "request_done",
                                       str(r.get("subject") or ""), _iso(ms), None))  # fmt: skip
    return out


def sent_requests(requests_dir: Path) -> dict[str, tuple[dict[str, Any], float]]:
    out: dict[str, tuple[dict[str, Any], float]] = {}
    for p in requests_dir.glob("2*.yaml"):
        try:
            day: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
            mtime = p.stat().st_mtime * 1000
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(day, dict):
            continue
        for r in cast(list[Any], cast(dict[str, Any], day).get("requests") or []):
            req = cast(dict[str, Any], r) if isinstance(r, dict) else {}
            if req.get("status") == "sent" and req.get("msgid"):
                out[str(req["msgid"])] = ({**req, "date": cast(dict[str, Any], day).get("date")}, mtime)
    return out


@dataclass
class Tracker:
    days: Path
    personas: list[str]
    out: Path
    requests_dir: Path | None = None
    sends: dict[str, str] = field(default_factory=kinds)
    recent: list[Interaction] = field(default_factory=list[Interaction])
    _seen: set[str] | None = None
    _mtimes: dict[Path, float] = field(default_factory=dict[Path, float])

    def step(self) -> list[Interaction]:
        if self._seen is None:
            self._seen = set()
            try:
                for line in self.out.read_text(encoding="utf-8").splitlines():
                    try:
                        self.recent.append(Interaction(**json.loads(line)))
                    except (ValueError, TypeError):
                        continue
            except OSError:
                pass
            self._seen = {i.id for i in self.recent}
            self.recent = self.recent[-20:]
        users = set(self.personas)
        sent = sent_requests(self.requests_dir or self.days.parent / "world" / "requests")
        found: list[Interaction] = []
        for msgid, (r, mtime) in sent.items():
            boss, to = str(r.get("manager") or ""), str(r.get("to") or "")
            if boss in users and to in users and boss != to:
                found.append(Interaction(msgid, [boss, to], "request", str(r.get("subject") or ""), _iso(mtime),
                                         str(r.get("date") or "") or None))  # fmt: skip
        records = {k: v for k, (v, _) in sent.items()}
        for log in self.days.glob("????-??-??-*.jsonl"):
            user = log.stem[11:]
            try:
                mtime = log.stat().st_mtime
            except OSError:
                continue
            if user not in users or self._mtimes.get(log) == mtime:
                continue
            self._mtimes[log] = mtime
            found += from_day_log(log, user, users, self.sends, records)
        new = [i for i in found if i.id not in self._seen]
        if new:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            with self.out.open("a", encoding="utf-8") as f:
                for i in sorted(new, key=lambda i: i.host):
                    f.write(json.dumps(asdict(i)) + "\n")
                    self._seen.add(i.id)
                    self.recent.append(i)
            self.recent = sorted(self.recent, key=lambda i: i.host)[-20:]
        return new
