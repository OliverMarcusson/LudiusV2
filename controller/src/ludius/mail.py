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
# Both times are needed: on a Sent Items copy Outlook's delivery time is cut to the minute (22:04:00 for a mail
# sent 22:04:34), and the submit time is the client's, so neither alone can date a message (see delivered()).
PR_CLIENT_SUBMIT_TIME = 0x00390040
MINUTE_MS = 60_000
PR_SMTP_ADDRESS, PR_EMAIL_ADDRESS = 0x39FE001F, 0x3003001F  # per recipient (To, Cc and, in Sent Items, Bcc)
FILETIME_UNIX_MS = 11_644_473_600_000  # 1601-01-01 to 1970-01-01, in ms
_ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


@dataclass(frozen=True)
class Message:
    sender: str  # SMTP address, lower case
    submit_ms: int  # Unix ms, client submit time; 0 when missing
    delivery_ms: int  # Unix ms, delivery time (cut to the minute on a Sent Items copy); 0 when missing
    body: str
    recipients: tuple[str, ...] = ()  # every recipient's SMTP address, lower case, sorted; "?" when unknown
    id: int = 0  # the store's message id (unique per mailbox)


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
    """Messages in `user`'s folder whose subject (without RE:/FW:) is exactly `subject`; [] on any error."""
    return _query(user, folder, subject) or []


def _query(user: str, folder: str, subject: str) -> list[Message] | None:
    """As messages(), but None when the server could not be read."""
    if not re.fullmatch(r"[a-z0-9]+", user):
        return None
    db = f"/var/lib/gromox/user/{DOMAIN}/{user}"
    # Hex literal: the subject reaches neither the shell nor SQL as text.
    rcpt = (f"(select group_concat(coalesce(cast(a.propval as text), cast(e.propval as text), '?'), ',') "
            f"from recipients x left join recipients_properties a on a.recipient_id=x.recipient_id "
            f"and a.proptag={PR_SMTP_ADDRESS} left join recipients_properties e on e.recipient_id=x.recipient_id "
            f"and e.proptag={PR_EMAIL_ADDRESS} where x.message_id=m.message_id)")  # fmt: skip
    sql = (f"select m.message_id, coalesce(t.propval, 0), coalesce(d.propval, 0), coalesce(f.propval, r.propval, '-'), "
           f"coalesce({rcpt}, '-'), cast(b.propval as text) "
           f"from messages m join message_properties s on s.message_id=m.message_id "
           f"and s.proptag={PR_NORMALIZED_SUBJECT} join message_properties b on b.message_id=m.message_id "
           f"and b.proptag={PR_BODY} left join message_properties d on d.message_id=m.message_id "
           f"and d.proptag={PR_DELIVERY_TIME} left join message_properties t on t.message_id=m.message_id "
           f"and t.proptag={PR_CLIENT_SUBMIT_TIME} left join message_properties f on f.message_id=m.message_id "
           f"and f.proptag={PR_SENDER_SMTP} left join message_properties r on r.message_id=m.message_id "
           f"and r.proptag={PR_SENT_REPRESENTING_SMTP} where m.parent_fid={FOLDERS[folder]} "
           f"and m.is_associated=0 and cast(s.propval as text)=cast(x'{subject.encode().hex()}' as text)")  # fmt: skip
    remote = (f"sudo sqlite3 -readonly -separator ' ' {db}/exmdb/exchange.sqlite3 \"{sql}\" | "
              f"while read i t d s r c; do printf '%s %s %s %s %s\\n' \"$i\" \"$t\" \"$d\" \"$s\" \"$r\"; "
              f"sudo zstdcat -qf {db}/cid/$c; "
              f"printf '\\036'; done")  # fmt: skip
    try:
        host = _host()
        if not host:
            return None
        cmd = ["ssh", "-i", str(SRV / "id_ed25519"), "-o", f"UserKnownHostsFile={SRV / 'known_hosts'}",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"ludius@{host}", remote]  # fmt: skip
        out = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    found: list[Message] = []
    for rec in out.stdout.decode("utf-8", "replace").split("\x1e"):
        head, _, body = rec.lstrip("\r\n").partition("\n")
        i, t, d, sender, rcpt = (head.split(" ") + ["", "", "", ""])[:5]
        if i.isdigit() and t.isdigit() and d.isdigit():
            to = tuple(sorted(x.strip().lower() for x in rcpt.split(",") if rcpt.strip() not in ("", "-")))
            ms = [int(x) // 10_000 - FILETIME_UNIX_MS if int(x) else 0 for x in (t, d)]
            found.append(Message(sender.strip().lower(), ms[0], ms[1], body, to, int(i)))
    return found


def letters(s: str) -> str:
    """Letters and digits only, casefolded: Outlook's AutoCorrect capitalises sentences, curls quotes and turns
    "- " lines into bullets, none of which changes what the persona wrote."""
    return re.sub(r"[^0-9a-z]", "", s.translate(_ASCII).casefold())


QUOTE_HEADER = re.compile(r"^From:[^\n]*\r?\nSent:", re.M)  # Outlook's header above the quoted original


Seen = frozenset[tuple[str, str, int]]  # (user, folder, message id)


def present(address: str, folder: str, subject: str, sender: str) -> Seen | None:
    """Ids of the messages a done check could match that exist already (recipient's folder and sender's Sent
    Items), read at task start; None when the server could not be read (then done can never pass)."""
    user, me = address.strip().lower().partition("@")[0], sender.strip().lower().partition("@")[0]
    seen: set[tuple[str, str, int]] = set()
    for u, f in ((user, folder), (me, "sent")):
        found = _query(u, f, subject)
        if found is None:
            return None
        seen |= {(u, f, m.id) for m in found}
    return frozenset(seen)


def delivered(
    address: str,
    folder: str,
    subject: str,
    body: str,
    sender: str,
    since_ms: int,
    above_quote: bool = False,
    before: Seen | None = frozenset(),
) -> bool:
    """A message from `sender` with exactly this subject, delivered at or after `since_ms` (Unix ms) and whose
    body contains `body`, is in the mailbox's folder; and every such copy in the sender's Sent Items went to
    `address` alone (To, Cc and Bcc: a hidden Bcc shows there, not in the recipient's copy). A body with no
    letters or digits never matches. srv01's only outgoing path (gromox LDA) refuses non-local recipients.
    `above_quote`: only the text before the quoted original's header counts (a reply written above the quote).
    `before`: messages present at task start (present()) never count: the guest clock can be rewound (time-jump
    replay), so a mail from an earlier playback can look recent. None (unknown) never passes."""
    address = address.strip().lower()
    user, _, domain = address.partition("@")
    me, _, my_domain = sender.strip().lower().partition("@")
    want = letters(body)
    if domain != DOMAIN or my_domain != DOMAIN or not want or since_ms <= 0 or before is None:
        return False
    seen = before

    def recent(m: Message) -> bool:
        # The submit time (client-set) and the delivery time (server-set, minute-cut on Sent Items) must both fall
        # in the task, and agree: a forged future submit time on an old message, or an old submit time on a
        # re-delivered one, fails.
        return (m.submit_ms >= since_ms and m.delivery_ms // MINUTE_MS >= since_ms // MINUTE_MS
                and m.submit_ms <= m.delivery_ms + MINUTE_MS)  # fmt: skip

    def ours(m: Message, u: str, f: str) -> bool:
        text = QUOTE_HEADER.split(m.body, maxsplit=1)[0] if above_quote else m.body
        return (u, f, m.id) not in seen and m.sender == sender.lower() and recent(m) and want in letters(text)

    if not any(ours(m, user, folder) for m in messages(user, folder, subject)):
        return False
    sent = [m for m in messages(me, "sent", subject) if ours(m, me, "sent")]
    return bool(sent) and all(m.recipients == (address,) for m in sent)
