from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from ludius.guestfile import MAX_BYTES, GuestFile

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,120}")


def unc(share: str, name: str = "") -> str:
    host = os.environ.get("LUDIUS_SHARE_HOST", "")
    return f"\\\\{host}\\{share}" + (f"\\{name}" if name else "") if host else ""


def read(share: str, name: str) -> GuestFile | None:
    host, addr = os.environ.get("LUDIUS_SHARE_HOST", ""), os.environ.get("LUDIUS_SRV_HOST", "")
    user, secret = os.environ.get("LUDIUS_EWS_USER", ""), os.environ.get("LUDIUS_EWS_PASSWORD_FILE", "")
    if not (host and addr and user and secret) or not _NAME.fullmatch(share) or not _NAME.fullmatch(name):
        return None
    try:
        password = Path(secret).read_text("utf-8").strip()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "f"
            r = subprocess.run(
                ["smbclient", f"//{host}/{share}", "-I", addr, "-U", user, "-m", "SMB3", "-c", f'get "{name}" {out}'],
                env=os.environ | {"PASSWD": password}, capture_output=True, text=True, timeout=60,
            )  # fmt: skip
            said = r.stdout + r.stderr
            if "NT_STATUS_OBJECT_NAME_NOT_FOUND" in said and "NT_STATUS_LOGON" not in said:
                return GuestFile(False)
            if r.returncode or not out.is_file() or out.stat().st_size > MAX_BYTES:
                return None
            return GuestFile(True, 0, out.read_bytes())
    except (OSError, subprocess.SubprocessError):
        return None
