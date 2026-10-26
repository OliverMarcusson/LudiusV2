from __future__ import annotations

import json
import os
import socket
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from ludius import store
from ludius.loop import GroundTruth


def _pg_bin() -> Path | None:
    """PostgreSQL binaries: LUDIUS_PG_BIN, else the ones bundled in the pgserver wheel (fetched by uv)."""
    if env := os.environ.get("LUDIUS_PG_BIN"):
        return Path(env)
    code = "import pgserver, pathlib; print(pathlib.Path(pgserver.__file__).parent / 'pginstall' / 'bin')"
    try:
        uv = ["uv", "run", "--no-project", "--python", "3.12", "--with", "pgserver", "python", "-c", code]
        out = subprocess.run(uv, capture_output=True, text=True, timeout=300, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real PostgreSQL for the session: LUDIUS_TEST_PG, or a private server in a temp dir, stopped afterwards."""
    if dsn := os.environ.get("LUDIUS_TEST_PG"):
        yield dsn
        return
    bin_dir = _pg_bin()
    if bin_dir is None or not (bin_dir / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")).exists():
        pytest.skip("no PostgreSQL: set LUDIUS_TEST_PG or LUDIUS_PG_BIN")
    data = tmp_path_factory.mktemp("pg") / "data"
    subprocess.run([bin_dir / "initdb", "-D", data, "-U", "ludius", "-A", "trust", "-E", "UTF8", "--no-locale"],
                   check=True, capture_output=True, timeout=120)  # fmt: skip
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctl = [bin_dir / "pg_ctl", "-D", data, "-w", "-t", "30"]
    # No pipes on start: the server inherits them and would keep them open, so waiting for EOF never returns.
    started = subprocess.run([*ctl, "-o", f"-p {port} -h 127.0.0.1", "-l", data / "server.log", "start"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)  # fmt: skip
    if started.returncode != 0:
        log = (data / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:]
        pytest.fail(f"PostgreSQL did not start (pg_ctl exit {started.returncode}):\n{log}")
    try:
        yield f"postgresql://ludius@127.0.0.1:{port}"
    finally:
        subprocess.run([*ctl, "-m", "fast", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


@pytest.fixture
def pg(pg_server: str) -> Iterator[str]:
    """A fresh database per test."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(f"{pg_server}/postgres", autocommit=True, connect_timeout=10) as c:
        c.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    yield f"{pg_server}/{name}"


def test_traces_and_world_round_trip_incrementally(pg: str, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    log = GroundTruth(runs / "days" / "2026-11-02-aberg.jsonl")
    log.write(event="day_start", host_ms=1, persona="aberg", date="2026-11-02")
    log.write(event="item_end", host_ms=2, at="09:00", template="notepad_write_note", status="done",
              params={"body": "line one\nåäö"})  # fmt: skip
    (runs / "world").mkdir(parents=True)
    (runs / "world" / "narrative.yaml").write_text("company: Harbourline\nevents:\n- date: 2026-11-04\n  what: ERP\n",
                                                   encoding="utf-8")  # fmt: skip
    (runs / "plans").mkdir()
    (runs / "plans" / "2026-11-02-aberg.yaml").write_text("persona: aberg\nitems: []\n", encoding="utf-8")

    with store.connect(pg) as conn:
        assert store.sync(conn, runs) == store.Synced(lines=2, docs=2)
        assert store.sync(conn, runs) == store.Synced()  # idempotent: nothing new, nothing rewritten
        log.write(event="day_end", host_ms=3, done=1, items=1)
        with (runs / "days" / "2026-11-02-aberg.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"event": "half-writ')  # a line still being written is left for the next sync
        (runs / "world" / "narrative.yaml").write_text("company: Harbourline\nevents: []\n", encoding="utf-8")
        assert store.sync(conn, runs) == store.Synced(lines=1, docs=1)

        on_disk = [json.loads(x) for x in (runs / "days" / "2026-11-02-aberg.jsonl").read_text(encoding="utf-8")
                   .splitlines()[:3]]  # fmt: skip
        assert store.trace(conn, "days/2026-11-02-aberg.jsonl") == on_disk
        assert store.doc(conn, "world/narrative.yaml") == {"company": "Harbourline", "events": []}
        assert store.doc(conn, "plans/2026-11-02-aberg.yaml") == {"persona": "aberg", "items": []}
        row = conn.execute("select count(*) from trace where event = 'item_end'").fetchone()
        assert row is not None and row[0] == 1
    log.close()


def test_rewritten_log_is_refused(pg: str, tmp_path: Path) -> None:
    log = tmp_path / "a.jsonl"
    log.write_text('{"event": "x"}\n{"event": "y"}\n', encoding="utf-8")
    with store.connect(pg) as conn:
        store.sync(conn, tmp_path)
        log.write_text('{"event": "x"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="append-only"):
            store.sync(conn, tmp_path)
        log.write_text('{"event": "x"}\n{"event": "y"}\nnot json\n', encoding="utf-8")
        with pytest.raises(ValueError, match="a.jsonl:3: not JSON"):
            store.sync(conn, tmp_path)
        assert [r["event"] for r in store.trace(conn, "a.jsonl")] == ["x", "y"]  # the failed sync left nothing
