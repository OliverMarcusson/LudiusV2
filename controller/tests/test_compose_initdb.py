from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "compose" / "initdb" / "10-ludius.sh"
BASH = shutil.which("bash")
PW = "p'w\"x$y;z"  # quoting stress: the compose passwords are URL-safe, the script must not rely on it


def _run(env: dict[str, str], bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    path = os.pathsep.join(filter(None, [str(bin_dir) if bin_dir else None, os.environ["PATH"]]))
    base = {k: v for k, v in os.environ.items() if k != "LUDIUS_DB_PASSWORD"}
    return subprocess.run([BASH, str(SCRIPT)], env={**base, "PATH": path, **env},  # noqa: S603
                          capture_output=True, text=True, timeout=60)  # fmt: skip


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("env", [{}, {"LUDIUS_DB_PASSWORD": ""}])
def test_missing_password_fails_before_psql(env: dict[str, str], tmp_path: Path) -> None:
    stub = tmp_path / "psql"
    stub.write_text(f"#!/bin/sh\necho called > '{(tmp_path / 'called').as_posix()}'\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {"POSTGRES_USER": "temporal", **env}
    done = _run(env, tmp_path)
    assert done.returncode != 0
    assert "LUDIUS_DB_PASSWORD" in done.stderr
    assert not (tmp_path / "called").exists()


@pytest.mark.service
@pytest.mark.skipif(BASH is None, reason="no bash")
def test_init_script_against_real_postgres(tmp_path: Path) -> None:
    """The init script on a scram-auth server like the image's: role + database exist, the role owns it and can work in it."""
    if not (bin_env := os.environ.get("LUDIUS_PG_BIN")):
        pytest.skip("set LUDIUS_PG_BIN to a PostgreSQL bin directory")
    bin_dir = Path(bin_env)
    exe = ".exe" if os.name == "nt" else ""
    data = tmp_path / "data"
    pwfile = tmp_path / "pw"
    pwfile.write_text("superuser-pw", encoding="utf-8")
    subprocess.run([bin_dir / f"initdb{exe}", "-D", data, "-U", "temporal", "-A", "scram-sha-256", f"--pwfile={pwfile}",
                    "-E", "UTF8", "--no-locale"], check=True, capture_output=True, timeout=120)  # fmt: skip
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = str(s.getsockname()[1])
    ctl = [bin_dir / f"pg_ctl{exe}", "-D", data, "-w", "-t", "30"]
    subprocess.run([*ctl, "-o", f"-p {port} -h 127.0.0.1", "-l", data / "server.log", "start"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)  # fmt: skip
    try:
        conn = {"PGHOST": "127.0.0.1", "PGPORT": port}
        done = _run({**conn, "POSTGRES_USER": "temporal", "PGPASSWORD": "superuser-pw", "LUDIUS_DB_PASSWORD": PW}, bin_dir)
        assert done.returncode == 0, done.stderr.replace(PW, "***")

        def psql(sql: str, user: str, password: str, db: str = "ludius") -> subprocess.CompletedProcess[str]:
            cmd = [bin_dir / f"psql{exe}", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db, "-c", sql]
            return subprocess.run(cmd, env={**os.environ, **conn, "PGPASSWORD": password},  # noqa: S603
                                  capture_output=True, text=True, timeout=30)  # fmt: skip

        out = psql("select current_user, (select pg_get_userbyid(datdba) from pg_database where datname='ludius')", "ludius", PW)
        assert out.stdout.strip() == "ludius|ludius", out.stderr
        assert psql("create table t(x int); insert into t values (1); select count(*) from t", "ludius", PW).returncode == 0
        assert psql("select 1", "ludius", "wrong").returncode != 0
        # least privilege: the store role is not a superuser
        assert psql("select rolsuper from pg_roles where rolname='ludius'", "ludius", PW).stdout.strip() == "f"
        # rerun on the same server fails (role exists): the script is first-start-only by design
        assert _run({**conn, "POSTGRES_USER": "temporal", "PGPASSWORD": "superuser-pw", "LUDIUS_DB_PASSWORD": PW}, bin_dir).returncode != 0
    finally:
        subprocess.run([*ctl, "-m", "fast", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
