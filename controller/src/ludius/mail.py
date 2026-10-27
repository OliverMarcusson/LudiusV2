"""Mail and meeting done-checks read on the range server, never in the UI: grommunio's store on ludius-srv01 over SSH.

Read-only: one sqlite3 -readonly query on the mailbox's exchange.sqlite3 for messages in a folder with the exact
(normalised, so without RE:) subject, with their sender's SMTP address, class, times and recipients, then their
stored plain-text bodies (zstd, or plain). Fails closed: no server, no key or any error is "not delivered".
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
FOLDERS = {"inbox": 13, "sent": 10, "calendar": 15}  # gromox private-store folder ids
PR_NORMALIZED_SUBJECT, PR_BODY = 0x0E1D001F, 0x1000001F
PR_SENDER_SMTP, PR_SENT_REPRESENTING_SMTP, PR_DELIVERY_TIME = 0x5D01001F, 0x5D02001F, 0x0E060040
# Both times are needed: on a Sent Items copy Outlook's delivery time is cut to the minute (22:04:00 for a mail
# sent 22:04:34), and the submit time is the client's, so neither alone can date a message (see delivered()).
PR_CLIENT_SUBMIT_TIME = 0x00390040
# Meetings: the class tells a request from an appointment; start and end are UTC, as Outlook sets them on both.
PR_MESSAGE_CLASS, PR_START_DATE, PR_END_DATE, PR_CREATION_TIME = 0x001A001F, 0x00600040, 0x00610040, 0x30070040
MEETING_REQUEST, APPOINTMENT = "IPM.Schedule.Meeting.Request", "IPM.Appointment"
MINUTE_MS = 60_000
PR_SMTP_ADDRESS, PR_EMAIL_ADDRESS = 0x39FE001F, 0x3003001F  # per recipient (To, Cc and, in Sent Items, Bcc)
# A meeting's organiser is the recipient row flagged recipOrganizer (MS-OXOCAL); Outlook's appointment has no sender.
PR_RECIPIENT_FLAGS, RECIP_ORGANIZER = 0x5FFD0003, 0x2
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
    cls: str = ""  # message class, e.g. IPM.Note, IPM.Schedule.Meeting.Request, IPM.Appointment
    start_ms: int = 0  # Unix ms (UTC), meetings only; 0 when missing
    end_ms: int = 0
    created_ms: int = 0  # Unix ms, creation time; 0 when missing
    organizer: str = ""  # meetings: SMTP address of the one recipient flagged organiser, lower case; "" otherwise


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
    org = rcpt.replace(" where x.message_id", f" join recipients_properties g on g.recipient_id=x.recipient_id "
                       f"and g.proptag={PR_RECIPIENT_FLAGS} and (g.propval & {RECIP_ORGANIZER}) != 0 "
                       f"where x.message_id")  # fmt: skip
    prop = "(select {} from message_properties where message_id=m.message_id and proptag={})"
    extra = ", ".join([prop.format("coalesce(cast(propval as text), '-')", PR_MESSAGE_CLASS),
                       *(f"coalesce({prop.format('propval', t)}, 0)" for t in (PR_START_DATE, PR_END_DATE,
                                                                               PR_CREATION_TIME))])  # fmt: skip
    # The body is optional (a meeting without text has none): '-' then, and an empty body is read.
    sql = (f"select m.message_id, coalesce(t.propval, 0), coalesce(d.propval, 0), coalesce(f.propval, r.propval, '-'), "
           f"{extra}, coalesce({org}, '-'), coalesce({rcpt}, '-'),coalesce(cast(b.propval as text), '-') "
           f"from messages m join message_properties s on s.message_id=m.message_id "
           f"and s.proptag={PR_NORMALIZED_SUBJECT} left join message_properties b on b.message_id=m.message_id "
           f"and b.proptag={PR_BODY} left join message_properties d on d.message_id=m.message_id "
           f"and d.proptag={PR_DELIVERY_TIME} left join message_properties t on t.message_id=m.message_id "
           f"and t.proptag={PR_CLIENT_SUBMIT_TIME} left join message_properties f on f.message_id=m.message_id "
           f"and f.proptag={PR_SENDER_SMTP} left join message_properties r on r.message_id=m.message_id "
           f"and r.proptag={PR_SENT_REPRESENTING_SMTP} where m.parent_fid={FOLDERS[folder]} "
           f"and m.is_associated=0 and cast(s.propval as text)=cast(x'{subject.encode().hex()}' as text)")  # fmt: skip
    # pipefail: a locked or failing database must fail the query, never read as "no messages" (the baseline).
    remote = (f"set -o pipefail; sudo sqlite3 -readonly -separator ' ' {db}/exmdb/exchange.sqlite3 \"{sql}\" | "
              f"while read i t d s k a e n o r c; do printf '%s %s %s %s %s %s %s %s %s %s\\n' \"$i\" \"$t\" \"$d\" "
              f"\"$s\" \"$k\" \"$a\" \"$e\" \"$n\" \"$o\" \"$r\"; "
              f"if [ \"$c\" != - ]; then sudo zstdcat -qf {db}/cid/$c; fi; "
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
        i, t, d, sender, cls, a, e, n, org, rcpt = (head.split(" ") + [""] * 9)[:10]
        if all(x.isdigit() for x in (i, t, d, a, e, n)):
            to = tuple(sorted(x.strip().lower() for x in rcpt.split(",") if rcpt.strip() not in ("", "-")))
            ms = [int(x) // 10_000 - FILETIME_UNIX_MS if int(x) else 0 for x in (t, d, a, e, n)]
            org = "" if org.strip() in ("", "-") or "," in org else org.strip().lower()  # one organiser, or none
            found.append(
                Message(sender.strip().lower(), ms[0], ms[1], body, to, int(i), cls.strip(), ms[2], ms[3], ms[4], org)
            )
    return found


def letters(s: str) -> str:
    """Letters and digits only, casefolded: Outlook's AutoCorrect capitalises sentences, curls quotes and turns
    "- " lines into bullets, none of which changes what the persona wrote."""
    return re.sub(r"[^0-9a-z]", "", s.translate(_ASCII).casefold())


QUOTE_HEADER = re.compile(r"^From:[^\n]*\r?\nSent:", re.M)  # Outlook's header above the quoted original


Seen = frozenset[tuple[str, str, int]]  # (user, folder, message id)


def present(address: str, folder: str, subject: str, sender: str, mine: str = "sent") -> Seen | None:
    """Ids of the messages a done check could match that exist already (recipient's folder and the sender's own
    `mine` folder: Sent Items, or Calendar for a meeting), read at task start; None when the server could not be
    read (then done can never pass)."""
    user, me = address.strip().lower().partition("@")[0], sender.strip().lower().partition("@")[0]
    seen: set[tuple[str, str, int]] = set()
    for u, f in ((user, folder), (me, mine)):
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

    def ours(m: Message, u: str, f: str) -> bool:
        text = QUOTE_HEADER.split(m.body, maxsplit=1)[0] if above_quote else m.body
        return (
            (u, f, m.id) not in seen and m.sender == sender.lower() and _recent(m, since_ms) and want in letters(text)
        )

    if not any(ours(m, user, folder) for m in messages(user, folder, subject)):
        return False
    sent = [m for m in messages(me, "sent", subject) if ours(m, me, "sent")]
    return bool(sent) and all(m.recipients == (address,) for m in sent)


def _recent(m: Message, since_ms: int) -> bool:
    """The submit time (client-set) and the delivery time (server-set, minute-cut on Sent Items) both fall in the
    task, and agree: a forged future submit time on an old message, or an old submit time on a re-delivered one,
    fails."""
    return (m.submit_ms >= since_ms and m.delivery_ms // MINUTE_MS >= since_ms // MINUTE_MS
            and m.submit_ms <= m.delivery_ms + MINUTE_MS)  # fmt: skip


def invited(
    address: str,
    subject: str,
    body: str,
    organizer: str,
    since_ms: int,
    start_ms: int,
    end_ms: int,
    before: Seen | None,
) -> bool:
    """A meeting `organizer` sent to `address` alone is on the server, both halves new since the task started:
    a meeting request in the attendee's Inbox from the organizer, delivered in the task (as delivered()), and an
    appointment in the organizer's Calendar created in the task; both with the organizer flagged as such on them,
    exactly this subject, start and end (Unix ms, UTC), and `address` as the only recipient besides the organizer.
    A planned `body` must be contained in the request (none planned: not checked). `before`
    (present(..., mine="calendar")) never counts; None never passes."""
    address, organizer = address.strip().lower(), organizer.strip().lower()
    user, _, domain = address.partition("@")
    me, _, my_domain = organizer.partition("@")
    want = letters(body)
    if domain != DOMAIN or my_domain != DOMAIN or user == me or since_ms <= 0 or before is None or start_ms >= end_ms:
        return False
    if body and not want:  # a planned body with no letters can't be told from an empty one: never done
        return False

    def ours(m: Message, u: str, f: str) -> bool:
        return ((u, f, m.id) not in before and (m.start_ms, m.end_ms) == (start_ms, end_ms)
                and tuple(r for r in m.recipients if r != organizer) == (address,))  # fmt: skip

    request = any(
        ours(m, user, "inbox") and m.cls == MEETING_REQUEST and organizer == m.sender == m.organizer
        and _recent(m, since_ms) and want in letters(m.body)
        for m in messages(user, "inbox", subject)
    )  # fmt: skip
    return request and any(
        ours(m, me, "calendar")
        and m.cls == APPOINTMENT
        and m.organizer == organizer
        and m.created_ms // MINUTE_MS >= since_ms // MINUTE_MS
        for m in messages(me, "calendar", subject)
    )
