"""Artifact coverage, volume and survival (spec section 8) from trainee-toolchain timeline exports.

Input is a Plaso ``psort`` CSV export; nothing here runs Plaso or touches a VM. Two layouts are read,
as written by Plaso's output modules (plaso/output/dynamic.py, l2t_csv.py, shared_dsv.py):

- ``dynamic`` (psort's default CSV): header ``datetime,timestamp_desc,source,source_long,message,parser,
  display_name,tag``. ``datetime`` is ISO 8601 with offset (``2026-10-27T08:20:01.123456+00:00``); Plaso
  writes ``0000-00-00T00:00:00.000000+00:00`` for a zero timestamp and ``Invalid`` on error. Extra columns
  from ``--fields``/``--additional_fields`` are kept; ``username``/``user``, ``hostname``/``host`` and
  ``filename`` are used when present.
- ``l2tcsv``: header ``date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,
  inode,notes,format,extra``; ``date`` is MM/DD/YYYY, ``time`` HH:MM:SS in ``timezone``. ``desc`` is read as
  the message, ``filename`` as the display name, ``format`` as the parser chain, ``sourcetype`` as
  ``source_long``.

Plaso writes ``-`` for an empty field and prefixes values starting with ``= + - @`` with an apostrophe;
both are undone here. Any other header is rejected rather than guessed.

Account attribution: the ``username``/``user`` column when it is not ``-``, else the profile folder in the
artifact path (``\\Users\\<account>\\``). Rows with neither stay unattributed; Prefetch is host-wide and is
counted per host, never credited to a persona.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DYNAMIC_REQUIRED = {"datetime", "parser", "display_name", "message"}
L2T_REQUIRED = {"date", "time", "timezone", "MACB", "user", "host", "desc", "filename", "format"}
L2T_RENAME = {"desc": "message", "filename": "display_name", "format": "parser", "sourcetype": "source_long"}

# Expected per-persona types (spec section 8), classified from the parser chain, first match wins.
# Jump lists come before LNK: automatic destinations embed LNK data.
USER_TYPES: list[tuple[str, re.Pattern[str]]] = [
    ("jump_list", re.compile(r"automatic_destinations|custom_destinations", re.I)),
    ("userassist", re.compile(r"userassist", re.I)),
    ("shellbags", re.compile(r"bagmru|shellbag", re.I)),
    ("office_mru", re.compile(r"office_mru", re.I)),
    ("mail", re.compile(r"outlook|\bpst\b|\bost\b|mail", re.I)),
    ("browser_history", re.compile(r"history|webcache", re.I)),
    ("lnk", re.compile(r"(^|/)lnk\b", re.I)),
]
HOST_TYPES: list[tuple[str, re.Pattern[str]]] = [("prefetch", re.compile(r"prefetch", re.I))]
MATCH_FIELDS = ("display_name", "message", "parser", "source_long", "user", "host")
_PROFILE = re.compile(r"[\\/]Users[\\/]([^\\/]+)[\\/]", re.I)


def _clean(v: str | None) -> str:
    v = (v or "").strip()
    if len(v) > 2 and v[0] == "'" and v[1] in "=+-@":
        v = v[1:]
    return "" if v == "-" else v


def _time_dynamic(v: str) -> dt.datetime | None:
    if not v or v.startswith("0000-00-00") or v == "Invalid":
        return None
    try:
        t = dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else None  # no offset: the zone is unknown, so the instant is too


def _time_l2t(date: str, time: str, zone: str) -> dt.datetime | None:
    if date in ("", "00/00/0000") or time in ("", "--:--:--"):
        return None
    try:
        tz = dt.UTC if zone.upper() == "UTC" else ZoneInfo(zone)
        return dt.datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M:%S").replace(tzinfo=tz)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def read_timeline(path: Path) -> list[dict[str, Any]]:
    """Rows normalized to time (aware datetime or None), user, host, parser, source_long, message, display_name."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = set(reader.fieldnames or [])
        if header >= DYNAMIC_REQUIRED:
            layout = "dynamic"
        elif header >= L2T_REQUIRED:
            layout = "l2tcsv"
        else:
            raise ValueError(
                f"{path}: unsupported header {sorted(header)}; expected psort dynamic "
                f"{sorted(DYNAMIC_REQUIRED)} or l2tcsv {sorted(L2T_REQUIRED)}"
            )
        rows: list[dict[str, Any]] = []
        for raw in reader:
            r = {k: _clean(v) for k, v in raw.items() if k}
            if layout == "l2tcsv":
                when = _time_l2t(r.get("date", ""), r.get("time", ""), r.get("timezone", ""))
                r = {L2T_RENAME.get(k, k): v for k, v in r.items()}
            else:
                when = _time_dynamic(r.get("datetime", ""))
            row: dict[str, Any] = {f: r.get(f, "") for f in MATCH_FIELDS}
            row["user"] = r.get("username") or r.get("user") or ""
            row["host"] = r.get("hostname") or r.get("host") or ""
            if not row["display_name"] and r.get("filename"):
                row["display_name"] = r["filename"]
            row["time"] = when
            rows.append(row)
    return rows


def account(row: dict[str, Any]) -> str:
    if row["user"]:
        return str(row["user"]).split("\\")[-1].lower()
    m = _PROFILE.search(row["display_name"]) or _PROFILE.search(row["message"])
    return m[1].lower() if m else ""


def classify(row: dict[str, Any]) -> tuple[str, str]:
    """(scope, type): scope is "user", "host" or "" when the parser is not one of the expected types."""
    chain = f"{row['parser']} {row['source_long']}"
    for scope, table in (("host", HOST_TYPES), ("user", USER_TYPES)):
        for name, rx in table:
            if rx.search(chain):
                return scope, name
    return "", ""


def persona_windows(logs: list[Path]) -> dict[str, list[tuple[dt.datetime, dt.datetime]]]:
    """Per persona, the guest-time span of each ground-truth log (host time where the guest did not report)."""
    out: dict[str, list[tuple[dt.datetime, dt.datetime]]] = defaultdict(list)
    for path in logs:
        persona, ts = "", list[int]()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r: dict[str, Any] = json.loads(line)
            persona = r.get("persona") or persona
            if ms := r.get("guest_ms") or r.get("host_ms"):
                ts.append(int(ms))
        if persona and len(ts) > 1:
            out[persona.lower()].append(
                (dt.datetime.fromtimestamp(min(ts) / 1000, dt.UTC), dt.datetime.fromtimestamp(max(ts) / 1000, dt.UTC))
            )
    return out


def coverage(rows: list[dict[str, Any]], personas: list[str], logs: list[Path]) -> dict[str, Any]:
    personas = [p.lower() for p in personas]
    expected = [name for name, _ in USER_TYPES]
    per: dict[str, Counter[str]] = {p: Counter[str]() for p in personas}
    events: Counter[str] = Counter()
    hosts: dict[str, Counter[str]] = defaultdict(Counter)
    unmapped: Counter[str] = Counter()
    unattributed = untimed = 0
    windows = persona_windows(logs)
    for row in rows:
        scope, kind = classify(row)
        if scope == "host":
            hosts[row["host"] or "unknown host"][kind] += 1
            continue
        who = account(row)
        if not who:
            unattributed += 1
            continue
        if who not in per:
            unmapped[who] += 1
            continue
        if kind:
            per[who][kind] += 1
        if row["time"] is None:
            untimed += 1
        elif any(a <= row["time"] <= b for a, b in windows.get(who, [])):
            events[who] += 1
    volume: dict[str, Any] = {}
    for p in personas:
        hours = sum((b - a).total_seconds() for a, b in windows.get(p, [])) / 3600
        volume[p] = (
            {"events": events[p], "hours": round(hours, 2), "events_per_hour": round(events[p] / hours, 1)}
            if hours
            else {"events_per_hour": None, "reason": "no ground-truth log span for this persona"}
        )
    return {
        "personas": {p: {"types": dict(per[p]), "missing": [t for t in expected if not per[p][t]]} for p in personas},
        "host_types": {h: dict(c) for h, c in hosts.items()},
        "volume": volume,
        "unmapped_accounts": dict(unmapped),
        "unattributed_rows": unattributed,
        "untimed_rows": untimed,
        "rows": len(rows),
    }


def _matches(row: dict[str, Any], match: dict[str, str]) -> bool:
    return all(re.search(rx, str(row.get(f, "")), re.I) for f, rx in match.items())


def survival(
    after: list[dict[str, Any]], manifest: list[dict[str, Any]], before: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Each manifest entry: {"id": str, "match": {field: regex, ...}} over MATCH_FIELDS (all must hold).

    survived: in the before export and still in the after export. lost: in before, gone after.
    present / missing: no before export, judged against the manifest alone. unknown: not identifiable
    (no usable match) or absent from the before export, so nothing can be said about survival.
    """
    out: list[dict[str, Any]] = []
    for e in manifest:
        match: dict[str, str] = e.get("match") or {}
        bad = sorted(set(match) - set(MATCH_FIELDS))
        if not match or bad:
            why = f"unknown match fields {bad}" if bad else "no match criteria"
            out.append({"id": e.get("id", "?"), "status": "unknown", "reason": why})
            continue
        now = sum(_matches(r, match) for r in after)
        if before is None:
            status, reason = ("present", "") if now else ("missing", "")
        else:
            was = sum(_matches(r, match) for r in before)
            if not was:
                status, reason = "unknown", "not found in the before export"
            else:
                status, reason = ("survived" if now else "lost"), f"{was} rows before, {now} after"
        out.append(
            {"id": e.get("id", "?"), "status": status, "rows_after": now} | ({"reason": reason} if reason else {})
        )
    statuses = {a["status"] for a in out}
    verdict: bool | None
    if not out:
        verdict = None
    elif statuses & {"lost", "missing"}:
        verdict = False
    elif "unknown" in statuses:
        verdict = None
    else:
        verdict = True
    basis = "manifest only (no before export)" if before is None else "before/after exports"
    return {"artifacts": out, "pass": verdict, "basis": basis}


def report(
    timeline: Path,
    personas: list[str],
    logs: list[Path],
    manifest: Path | None = None,
    before: Path | None = None,
) -> dict[str, Any]:
    rows = read_timeline(timeline)
    out: dict[str, Any] = {"coverage": coverage(rows, personas, logs)}
    if manifest is None:
        out["survival"] = {"pass": None, "reason": "no scenario manifest given"}
    else:
        entries: list[dict[str, Any]] = json.loads(manifest.read_text(encoding="utf-8"))
        out["survival"] = survival(rows, entries, read_timeline(before) if before else None)
    return out
