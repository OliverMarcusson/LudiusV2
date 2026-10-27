"""Mail done-checks read on the range server, never in the UI: grommunio's store on ludius-srv01 over SSH.

Read-only: one sqlite3 -readonly query on the mailbox's exchange.sqlite3 for messages in a folder with the exact
(normalised, so without RE:) subject, with their sender's SMTP address and delivery time, then their stored
plain-text bodies (zstd, or plain). Fails closed: no server, no key or any error is "not delivered".
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRV = ROOT / "infra" / "hyperv" / "build" / "srv"
DOMAIN = "corp.ludius.test"
FOLDERS = {"inbox": 13, "sent": 10}  # gromox private-store folder ids
PR_NORMALIZED_SUBJECT, PR_BODY = 0x0E1D001F, 0x1000001F
PR_SENDER_SMTP, PR_SENT_REPRESENTING_SMTP, PR_DELIVERY_TIME = 0x5D01001F, 0x5D02001F, 0x0E060040
FILETIME_UNIX_MS = 11_644_473_600_000  # 1601-01-01 to 1970-01-01, in ms
_ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


@dataclass(frozen=True)
class Message:
    sender: str  # SMTP address, lower case
    delivered_ms: int  # Unix ms, the server's clock
    body: str


@functools.cache
def _host() -> str:
    """The server's Default Switch address (it changes across host reboots), as set-clock.ps1 finds it."""
    vm = os.environ.get("LUDIUS_SRV_VM", "ludius-srv01")
    ps = (f"(Get-VMNetworkAdapter -VMName '{vm}' | Where-Object SwitchName -eq 'Default Switch').IPAddresses"
          " | Where-Object { $_ -match '^\\d+\\.' } | Select-Object -First 1")  # fmt: skip
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=30)  # fmt: skip
    return out.stdout.strip()


def messages(user: str, folder: str, subject: str) -> list[Message]:
    """Messages in `user`'s folder whose subject (without RE:/FW:) is exactly `subject`."""
    if not re.fullmatch(r"[a-z0-9]+", user):
        return []
    db = f"/var/lib/gromox/user/{DOMAIN}/{user}"
    # Hex literal: the subject reaches neither the shell nor SQL as text.
    sql = (f"select coalesce(d.propval, 0), coalesce(f.propval, r.propval, '-'), cast(b.propval as text) "
           f"from messages m join message_properties s on s.message_id=m.message_id "
           f"and s.proptag={PR_NORMALIZED_SUBJECT} join message_properties b on b.message_id=m.message_id "
           f"and b.proptag={PR_BODY} left join message_properties d on d.message_id=m.message_id "
           f"and d.proptag={PR_DELIVERY_TIME} left join message_properties f on f.message_id=m.message_id "
           f"and f.proptag={PR_SENDER_SMTP} left join message_properties r on r.message_id=m.message_id "
           f"and r.proptag={PR_SENT_REPRESENTING_SMTP} where m.parent_fid={FOLDERS[folder]} "
           f"and m.is_associated=0 and cast(s.propval as text)=cast(x'{subject.encode().hex()}' as text)")  # fmt: skip
    remote = (f"sudo sqlite3 -readonly -separator ' ' {db}/exmdb/exchange.sqlite3 \"{sql}\" | "
              f"while read t s c; do printf '%s %s\\n' \"$t\" \"$s\"; sudo zstdcat -qf {db}/cid/$c; "
              f"printf '\\036'; done")  # fmt: skip
    try:
        host = _host()
        if not host:
            return []
        cmd = ["ssh", "-i", str(SRV / "id_ed25519"), "-o", f"UserKnownHostsFile={SRV / 'known_hosts'}",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"ludius@{host}", remote]  # fmt: skip
        out = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode:
        return []
    found: list[Message] = []
    for rec in out.stdout.decode("utf-8", "replace").split("\x1e"):
        head, _, body = rec.lstrip("\r\n").partition("\n")
        t, _, sender = head.partition(" ")
        if t.isdigit() and int(t):
            found.append(Message(sender.strip().lower(), int(t) // 10_000 - FILETIME_UNIX_MS, body))
    return found


def letters(s: str) -> str:
    """Letters and digits only, casefolded: Outlook's AutoCorrect capitalises sentences, curls quotes and turns
    "- " lines into bullets, none of which changes what the persona wrote."""
    return re.sub(r"[^0-9a-z]", "", s.translate(_ASCII).casefold())


def delivered(address: str, folder: str, subject: str, body: str, sender: str, since_ms: int) -> bool:
    """A message from `sender` with exactly this subject, delivered at or after `since_ms` (Unix ms) and whose
    body contains `body`, is in the mailbox's folder. A body with no letters or digits never matches."""
    user, _, domain = address.strip().lower().partition("@")
    want = letters(body)
    if domain != DOMAIN or not want or not sender or since_ms <= 0:
        return False
    return any(
        m.sender == sender.lower() and m.delivered_ms >= since_ms and want in letters(m.body)
        for m in messages(user, folder, subject)
    )
