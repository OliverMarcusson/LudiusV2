from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import psycopg
import yaml
from minio import Minio
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
create table if not exists screenshot (
    key     text        primary key,  -- path relative to the run directory, forward slashes
    sha256  text        not null,     -- the object is sha256/<sha256>.png in the bucket
    size    bigint      not null,
    updated timestamptz not null default now()
);
"""
WORLD_GLOBS = ["world/**/*.yaml", "plans/*.yaml"]


@dataclass
class Synced:
    lines: int = 0
    docs: int = 0
    shots: int = 0


@dataclass
class Bucket:
    client: Minio
    name: str


def bucket(url: str) -> Bucket:
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or not u.username or not u.password:
        raise ValueError("S3 URL must be http(s)://ACCESS:SECRET@host:port/bucket")
    name = u.path.strip("/")
    if not name:
        raise ValueError("S3 URL has no bucket")
    client = Minio(f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}", unquote(u.username),
                   unquote(u.password), secure=u.scheme == "https")  # fmt: skip
    if not client.bucket_exists(name):
        client.make_bucket(name)
    return Bucket(client, name)


def _object(sha: str) -> str:
    return f"sha256/{sha}.png"


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
    return lines[:-1]


def sync(conn: psycopg.Connection[tuple[Any, ...]], root: Path, shots: Bucket | None = None) -> Synced:
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
                (_key(root, doc), Jsonb(yaml.safe_load(raw), dumps=_dumps), sha),
            )
            out.docs += cur.rowcount
        for png in sorted(root.rglob("*.png")) if shots else []:
            assert shots is not None
            raw = png.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            key = _key(root, png)
            row = conn.execute("select sha256 from screenshot where key = %s", (key,)).fetchone()
            if row and row[0] == sha:
                continue
            shots.client.put_object(shots.name, _object(sha), io.BytesIO(raw), len(raw), content_type="image/png")
            conn.execute(
                "insert into screenshot (key, sha256, size) values (%s, %s, %s) on conflict (key) do update "
                "set sha256 = excluded.sha256, size = excluded.size, updated = now()",
                (key, sha, len(raw)),
            )
            out.shots += 1
    return out


def trace(conn: psycopg.Connection[tuple[Any, ...]], source: str) -> list[dict[str, Any]]:
    return [r[0] for r in conn.execute("select rec from trace where source = %s order by line", (source,))]


def doc(conn: psycopg.Connection[tuple[Any, ...]], key: str) -> Any:
    row = conn.execute("select body from world_doc where key = %s", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return row[0]


def screenshot(conn: psycopg.Connection[tuple[Any, ...]], shots: Bucket, key: str) -> bytes:
    row = conn.execute("select sha256 from screenshot where key = %s", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    resp = shots.client.get_object(shots.name, _object(row[0]))
    try:
        raw = resp.read()
    finally:
        resp.close()
        resp.release_conn()
    if hashlib.sha256(raw).hexdigest() != row[0]:
        raise ValueError(f"{key}: object does not match its stored sha256")
    return raw
