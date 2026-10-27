"""Mail done-checks read on the range server, never in the UI: grommunio's store on ludius-srv01 over SSH.

Read-only: one sqlite3 -readonly query on the mailbox's exchange.sqlite3 for messages in a folder with the exact
(normalised, so without RE:) subject, then their stored plain-text bodies (zstd, or plain). Fails closed: no
server, no key or any error is "not delivered".
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRV = ROOT / "infra" / "hyperv" / "build" / "srv"
DOMAIN = "corp.ludius.test"
FOLDERS = {"inbox": 13, "sent": 10}  # gromox private-store folder ids
PR_NORMALIZED_SUBJECT, PR_BODY = 0x0E1D001F, 0x1000001F
_ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


@functools.cache
def _host() -> str:
    """The server's Default Switch address (it changes across host reboots), as set-clock.ps1 finds it."""
    vm = os.environ.get("LUDIUS_SRV_VM", "ludius-srv01")
    ps = (f"(Get-VMNetworkAdapter -VMName '{vm}' | Where-Object SwitchName -eq 'Default Switch').IPAddresses"
          " | Where-Object { $_ -match '^\\d+\\.' } | Select-Object -First 1")  # fmt: skip
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=30)  # fmt: skip
    return out.stdout.strip()


def bodies(user: str, folder: str, subject: str) -> list[str]:
    """Plain-text bodies of the messages in `user`'s folder whose subject (without RE:/FW:) is exactly `subject`."""
    if not re.fullmatch(r"[a-z0-9]+", user):
        return []
    db = f"/var/lib/gromox/user/{DOMAIN}/{user}"
    # Hex literal: the subject reaches neither the shell nor SQL as text.
    sql = (f"select cast(b.propval as text) from messages m join message_properties s on s.message_id=m.message_id "
           f"and s.proptag={PR_NORMALIZED_SUBJECT} join message_properties b on b.message_id=m.message_id "
           f"and b.proptag={PR_BODY} where m.parent_fid={FOLDERS[folder]} and m.is_associated=0 "
           f"and cast(s.propval as text)=cast(x'{subject.encode().hex()}' as text)")  # fmt: skip
    remote = (f'for c in $(sudo sqlite3 -readonly {db}/exmdb/exchange.sqlite3 "{sql}"); do '
              f"sudo zstdcat -qf {db}/cid/$c; printf '\\036'; done")  # fmt: skip
    host = _host()
    if not host:
        return []
    cmd = ["ssh", "-i", str(SRV / "id_ed25519"), "-o", f"UserKnownHostsFile={SRV / 'known_hosts'}",
           "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"ludius@{host}", remote]  # fmt: skip
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode:
        return []
    return [b for b in out.stdout.decode("utf-8", "replace").split("\x1e") if b.strip()]


def letters(s: str) -> str:
    """Letters and digits only, casefolded: Outlook's AutoCorrect capitalises sentences, curls quotes and turns
    "- " lines into bullets, none of which changes what the persona wrote."""
    return re.sub(r"[^0-9a-z]", "", s.translate(_ASCII).casefold())


def delivered(address: str, folder: str, subject: str, body: str) -> bool:
    """A message with exactly this subject, whose body contains `body`, is in the mailbox's folder."""
    user, _, domain = address.strip().lower().partition("@")
    if domain != DOMAIN:
        return False
    want = letters(body)
    return any(want in letters(b) for b in bodies(user, folder, subject))
