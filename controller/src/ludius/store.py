"""World state and traces in PostgreSQL (spec section 9).

The run directory stays the source of truth while a day runs (append-only JSONL, YAML world files), so the
local CLI keeps working without a database. sync() mirrors it into PostgreSQL, incrementally and idempotently:
- trace: one row per ground-truth line (every *.jsonl under the run directory), keyed by (source, line);
  logs are append-only, so only lines past the stored maximum are inserted, and a log that got shorter is
  an error rather than a silent divergence;
- world_doc: one row per world/plan YAML file (narrative, revisions, derived state, day plans), replaced
  when its content hash changes.
Needs the `state` dependency group and a DSN (`ludius store sync --dsn ...` or LUDIUS_PG).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

SCHEMA = """
create table if not exists trace (
    source  text   not null,  -- log path relative to the run directory, forward slashes
    line    int    not null,  -- 1-based line number in that log
    event   text,
    host_ms bigint,
    rec     jsonb  not null,
    primary key (source, line)
);
create index if not exists trace_event on trace (event);
create table if not exists world_doc (
    key     text        primary key,  -- path relative to the run directory, forward slashes
    body    jsonb       not null,
    sha256  text        not null,
    updated timestamptz not null default now()
);
"""
WORLD_GLOBS = ["world/**/*.yaml", "plans/*.yaml"]


@dataclass
class Synced:
    lines: int = 0
    docs: int = 0


def connect(dsn: str, timeout_s: int = 10) -> psycopg.Connection[tuple[Any, ...]]:
    conn = psycopg.connect(dsn, connect_timeout=timeout_s)
    with conn.transaction():
        conn.execute(SCHEMA)
    return conn


def _dumps(o: Any) -> str:
    return json.dumps(o, default=str)


def _key(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    return lines[:-1]  # the last piece is "" after a final newline, or a line still being written: skip it


def sync(conn: psycopg.Connection[tuple[Any, ...]], root: Path) -> Synced:
    out = Synced()
    with conn.transaction():
        for log in sorted(root.rglob("*.jsonl")):
            source = _key(root, log)
            row = conn.execute("select coalesce(max(line), 0) from trace where source = %s", (source,)).fetchone()
            have: int = row[0] if row else 0
            lines = _lines(log)
            if len(lines) < have:
                raise ValueError(f"{source}: {len(lines)} lines on disk but {have} stored; logs are append-only")
            rows: list[tuple[str, int, Any, Any, Jsonb]] = []
            for n, text in enumerate(lines[have:], start=have + 1):
                try:
                    rec = json.loads(text)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{source}:{n}: not JSON ({e})") from e
                rows.append((source, n, rec.get("event"), rec.get("host_ms"), Jsonb(rec)))
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into trace (source, line, event, host_ms, rec) values (%s, %s, %s, %s, %s)", rows
                )
            out.lines += len(rows)
        for doc in sorted({p for g in WORLD_GLOBS for p in root.glob(g)}):
            raw = doc.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            cur = conn.execute(
                "insert into world_doc (key, body, sha256) values (%s, %s, %s) on conflict (key) do update "
                "set body = excluded.body, sha256 = excluded.sha256, updated = now() "
                "where world_doc.sha256 <> excluded.sha256",
                (_key(root, doc), Jsonb(yaml.safe_load(raw), dumps=_dumps), sha),  # YAML dates become strings
            )
            out.docs += cur.rowcount
    return out


def trace(conn: psycopg.Connection[tuple[Any, ...]], source: str) -> list[dict[str, Any]]:
    return [r[0] for r in conn.execute("select rec from trace where source = %s order by line", (source,))]


def doc(conn: psycopg.Connection[tuple[Any, ...]], key: str) -> Any:
    row = conn.execute("select body from world_doc where key = %s", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return row[0]
