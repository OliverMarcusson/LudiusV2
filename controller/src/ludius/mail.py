from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ludius import ews

ROOT = Path(__file__).resolve().parents[3]
SRV = ROOT / "infra" / "hyperv" / "build" / "srv"
_DOTENV = ROOT / ".env"
_LINES = _DOTENV.read_text(encoding="utf-8").splitlines() if _DOTENV.exists() else []
_SET = [ln.partition("=")[2].strip() for ln in _LINES if ln.partition("=")[0].strip() == "LUDIUS_MAIL_DOMAIN"]
DOMAIN = os.environ.get("LUDIUS_MAIL_DOMAIN", _SET[0] if _SET else "") or "ludius.internal"
FOLDERS = {"inbox": 13, "sent": 10, "calendar": 15}
PR_NORMALIZED_SUBJECT, PR_BODY = 0x0E1D001F, 0x1000001F
PR_SENDER_SMTP, PR_SENT_REPRESENTING_SMTP, PR_DELIVERY_TIME = 0x5D01001F, 0x5D02001F, 0x0E060040
PR_CLIENT_SUBMIT_TIME = 0x00390040
PR_MESSAGE_CLASS, PR_START_DATE, PR_END_DATE, PR_CREATION_TIME = 0x001A001F, 0x00600040, 0x00610040, 0x30070040
MEETING_REQUEST, APPOINTMENT = "IPM.Schedule.Meeting.Request", "IPM.Appointment"
MINUTE_MS = 60_000
PR_SMTP_ADDRESS, PR_EMAIL_ADDRESS = 0x39FE001F, 0x3003001F
PR_RECIPIENT_FLAGS, RECIP_ORGANIZER = 0x5FFD0003, 0x2
PR_SUBJECT_PREFIX, PR_INTERNET_MESSAGE_ID, PR_IN_REPLY_TO_ID = 0x003D001F, 0x1035001F, 0x1042001F
LISTED = 20
FILETIME_UNIX_MS = 11_644_473_600_000
_ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


@dataclass(frozen=True)
class Message:
    sender: str
    submit_ms: int
    delivery_ms: int
    body: str
    recipients: tuple[str, ...] = ()
    id: int = 0
    cls: str = ""
    start_ms: int = 0
    end_ms: int = 0
    created_ms: int = 0
    organizer: str = ""
    subject: str = ""
    prefix: str = ""
    msgid: str = ""
    in_reply_to: str = ""
    read: bool = False
    attachments: tuple[tuple[str, str], ...] | None = None
    files: tuple[str, ...] = ()


def _exchange() -> bool:
    return os.environ.get("LUDIUS_MAIL_BACKEND", "grommunio") == "exchange"


@functools.cache
def _host() -> str:
    if fixed := os.environ.get("LUDIUS_SRV_HOST"):
        return fixed
    vm = os.environ.get("LUDIUS_SRV_VM", "ludius-srv01")
    ps = (f"(Get-VMNetworkAdapter -VMName '{vm}' | Where-Object SwitchName -eq 'Default Switch').IPAddresses"
          " | Where-Object { $_ -match '^\\d+\\.' } | Select-Object -First 1")  # fmt: skip
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=30)  # fmt: skip
    return out.stdout.strip()


def _ssh(remote: str, stdin: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[bytes] | None:
    try:
        host = _host()
        if not host:
            return None
        cmd = ["ssh", "-i", str(SRV / "id_ed25519"), "-o", f"UserKnownHostsFile={SRV / 'known_hosts'}",
               "-o", "HostKeyAlias=ludius-srv01", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=10", f"ludius@{host}", remote]  # fmt: skip
        data = None if stdin is None else stdin.encode()
        return subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def read_state(user: str, message_id: int, folder: str = "inbox") -> bool | None:
    if not re.fullmatch(r"[a-z0-9]+", user) or message_id <= 0:
        return None
    if _exchange():
        return ews.read_state(f"{user}@{DOMAIN}", message_id, folder)
    db = f"/var/lib/gromox/user/{DOMAIN}/{user}/exmdb/exchange.sqlite3"
    sql = (f"select read_state from messages where message_id={int(message_id)} and parent_fid={FOLDERS[folder]} "
           f"and is_associated=0 and coalesce(is_deleted, 0)=0")  # fmt: skip
    out = _ssh(f'sudo sqlite3 -readonly {db} "{sql}"')
    if out is None or out.returncode:
        return None
    return {"0": False, "1": True}.get(out.stdout.decode("ascii", "replace").strip())


def messages(user: str, folder: str, subject: str, attachments: bool = False) -> list[Message]:
    return _query(user, folder, subject, attachments) or []


def matching(user: str, folder: str, subject: str, attachments: bool = False) -> list[Message] | None:
    return _query(user, folder, subject, attachments)


def newest(user: str, folder: str) -> list[Message] | None:
    return _query(user, folder, None)


def _query(user: str, folder: str, subject: str | None, attachments: bool = False) -> list[Message] | None:
    if not re.fullmatch(r"[a-z0-9]+", user):
        return None
    if _exchange():
        items = ews.query(f"{user}@{DOMAIN}", folder, subject, attachments)
        return None if items is None else [Message(**i) for i in items]
    db = f"/var/lib/gromox/user/{DOMAIN}/{user}"
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
    texts = ", ".join(f"coalesce(nullif(hex(cast({v} as text)), ''), '-')" for v in (
        "s.propval", *(prop.format("propval", t) for t in (PR_SUBJECT_PREFIX, PR_INTERNET_MESSAGE_ID,
                                                           PR_IN_REPLY_TO_ID))))  # fmt: skip
    where = "" if subject is None else f"and cast(s.propval as text)=cast(x'{subject.encode().hex()}' as text)"
    order = f" order by coalesce(d.propval, 0) desc limit {LISTED}" if subject is None else ""
    sql = (f"select m.message_id, coalesce(m.read_state, 0), coalesce(t.propval, 0), coalesce(d.propval, 0), "
           f"coalesce(f.propval, r.propval, '-'), "
           f"{extra}, coalesce({org}, '-'), coalesce({rcpt}, '-'), {texts}, coalesce(cast(b.propval as text), '-') "
           f"from messages m join message_properties s on s.message_id=m.message_id "
           f"and s.proptag={PR_NORMALIZED_SUBJECT} left join message_properties b on b.message_id=m.message_id "
           f"and b.proptag={PR_BODY} left join message_properties d on d.message_id=m.message_id "
           f"and d.proptag={PR_DELIVERY_TIME} left join message_properties t on t.message_id=m.message_id "
           f"and t.proptag={PR_CLIENT_SUBMIT_TIME} left join message_properties f on f.message_id=m.message_id "
           f"and f.proptag={PR_SENDER_SMTP} left join message_properties r on r.message_id=m.message_id "
           f"and r.proptag={PR_SENT_REPRESENTING_SMTP} where m.parent_fid={FOLDERS[folder]} "
           f"and m.is_associated=0 {where}{order}")  # fmt: skip
    fields = "i u t d s k a e n o r h p x y"
    remote = (f"set -o pipefail; sudo sqlite3 -readonly -separator ' ' {db}/exmdb/exchange.sqlite3 \"{sql}\" | "
              f"while read {fields} c; do printf '{' '.join(['%s'] * 15)}\\n' "
              + " ".join(f'\"${v}\"' for v in fields.split()) + "; "
              f"if [ \"$c\" != - ]; then sudo zstdcat -qf {db}/cid/$c || exit 1; fi; "
              f"printf '\\036'; done")  # fmt: skip
    out = _ssh(remote)
    if out is None or out.returncode:
        return None
    found: list[Message] = []
    for rec in out.stdout.decode("utf-8", "replace").split("\x1e"):
        head, _, body = rec.lstrip("\r\n").partition("\n")
        i, u, t, d, sender, cls, a, e, n, org, rcpt, *hexes = (head.split(" ") + [""] * 14)[:15]
        subj, prefix, msgid, irt = (_unhex(x) for x in hexes)
        if all(x.isdigit() for x in (i, u, t, d, a, e, n)):
            to = tuple(sorted(x.strip().lower() for x in rcpt.split(",") if rcpt.strip() not in ("", "-")))
            ms = [int(x) // 10_000 - FILETIME_UNIX_MS if int(x) else 0 for x in (t, d, a, e, n)]
            org = "" if org.strip() in ("", "-") or "," in org else org.strip().lower()
            found.append(
                Message(
                    sender.strip().lower(),
                    ms[0],
                    ms[1],
                    body,
                    to,
                    int(i),
                    cls.strip(),
                    ms[2],
                    ms[3],
                    ms[4],
                    org,
                    subj,
                    prefix,
                    msgid.strip(),
                    irt.strip(),
                    u == "1",
                )  # fmt: skip
            )
    return found


def _unhex(x: str) -> str:
    try:
        return "" if x.strip() in ("", "-") else bytes.fromhex(x.strip()).decode("utf-8", "replace")
    except ValueError:
        return ""


def letters(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.translate(_ASCII).casefold())


QUOTE_HEADER = re.compile(r"^From:[^\n]*\r?\nSent:", re.M)


Seen = frozenset[tuple[str, str, int]]


def present(address: str, folder: str, subject: str, sender: str, mine: str = "sent") -> Seen | None:
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
    reply_to: str = "",
    attachment: tuple[str, str] | None = None,
) -> bool:
    address = address.strip().lower()
    user, _, domain = address.partition("@")
    me, _, my_domain = sender.strip().lower().partition("@")
    want = letters(body)
    if domain != DOMAIN or my_domain != DOMAIN or not want or since_ms <= 0 or before is None:
        return False
    seen = before

    def ours(m: Message, u: str, f: str) -> bool:
        attached = attachment is None or (u, f) != (user, folder) or m.attachments == (attachment,)
        text = QUOTE_HEADER.split(m.body, maxsplit=1)[0] if above_quote else m.body
        answers = not reply_to or (m.in_reply_to == reply_to and m.prefix.strip().upper() == "RE:")
        return (
            (u, f, m.id) not in seen
            and m.sender == sender.lower()
            and _recent(m, since_ms)
            and want in letters(text)
            and answers
            and attached
        )

    if not any(ours(m, user, folder) for m in messages(user, folder, subject, attachment is not None)):
        return False
    sent = [m for m in messages(me, "sent", subject) if ours(m, me, "sent")]
    return bool(sent) and all(m.recipients == (address,) for m in sent)


def _recent(m: Message, since_ms: int) -> bool:
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
    address, organizer = address.strip().lower(), organizer.strip().lower()
    user, _, domain = address.partition("@")
    me, _, my_domain = organizer.partition("@")
    want = letters(body)
    if domain != DOMAIN or my_domain != DOMAIN or user == me or since_ms <= 0 or before is None or start_ms >= end_ms:
        return False
    if body and not want:
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


def deliver(sender: str, sender_name: str, to: str, to_name: str, subject: str, body: str, msgid: str) -> bool:
    addrs = [a.strip().lower() for a in (sender, to)]
    if any(not re.fullmatch(rf"[a-z0-9]+@{re.escape(DOMAIN)}", a) for a in addrs) or addrs[0] == addrs[1]:
        return False
    have = matching(addrs[1].partition("@")[0], "inbox", subject)
    if have is None:
        return False
    if any(m.msgid == msgid for m in have):
        return True
    if _exchange():
        return ews.send(addrs[0], sender_name, addrs[1], to_name, subject, body, msgid)

    script = (
        "import json, smtplib, sys\nfrom email.message import EmailMessage\nfrom email.utils import formatdate\n"
        "v = json.loads(sys.stdin.readline())\nm = EmailMessage()\n"
        "m['From'] = f\"{v['sender_name']} <{v['sender']}>\"\nm['To'] = f\"{v['to_name']} <{v['to']}>\"\n"
        "m['Subject'] = v['subject']\nm['Date'] = formatdate(localtime=True)\nm['Message-ID'] = v['msgid']\n"
        "m.set_content(v['body'])\nwith smtplib.SMTP('::1', 25, timeout=20) as s:\n"
        "    s.send_message(m, from_addr=v['sender'], to_addrs=[v['to']])\nprint('sent')\n"
    )
    payload = json.dumps(dict(sender=addrs[0], sender_name=sender_name, to=addrs[1], to_name=to_name,
                              subject=subject, body=body, msgid=msgid))  # fmt: skip
    out = _ssh("python3 -c " + shlex.quote(script), payload + "\n", 60)
    return out is not None and out.returncode == 0 and out.stdout.decode("utf-8", "replace").strip() == "sent"
