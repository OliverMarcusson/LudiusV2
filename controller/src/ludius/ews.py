from __future__ import annotations

import datetime as dt
import hashlib
import os
import ssl
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict
from xml.sax.saxutils import escape, quoteattr

import httpx

ROOT = Path(__file__).resolve().parents[3]
T = "http://schemas.microsoft.com/exchange/services/2006/types"
M = "http://schemas.microsoft.com/exchange/services/2006/messages"
FOLDER_IDS = {"inbox": "inbox", "sent": "sentitems", "calendar": "calendar"}
PAGE = 1000
LISTED = 20
EXTENDED = ("0x0E1D", "0x003D")
FIELDS = ("item:ItemClass", "item:Subject", "item:DateTimeReceived", "item:DateTimeSent", "item:DateTimeCreated",
          "item:TextBody", "item:InReplyTo", "message:Sender", "message:ToRecipients", "message:CcRecipients",
          "message:BccRecipients", "message:InternetMessageId", "message:IsRead", "calendar:Start", "calendar:End",
          "calendar:Organizer", "calendar:RequiredAttendees", "calendar:OptionalAttendees",
          "calendar:Resources")  # fmt: skip


class Item(TypedDict):
    sender: str
    submit_ms: int
    delivery_ms: int
    body: str
    recipients: tuple[str, ...]
    id: int
    cls: str
    start_ms: int
    end_ms: int
    created_ms: int
    organizer: str
    subject: str
    prefix: str
    msgid: str
    in_reply_to: str
    read: bool


def _settings() -> tuple[str, str, str, str] | None:
    url = os.environ.get("LUDIUS_EWS_URL", "")
    user = os.environ.get("LUDIUS_EWS_USER", "")
    secret = os.environ.get("LUDIUS_EWS_PASSWORD_FILE", "")
    ca = os.environ.get("LUDIUS_EWS_CA", str(ROOT / "infra" / "ansible" / "build" / "range-ca.crt"))
    if not (url.startswith("https://") and user and secret):
        return None
    return url, user, secret, ca


def call(mailbox: str, body: str) -> ET.Element | None:
    s = _settings()
    if s is None:
        return None
    url, user, secret, ca = s
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:t="{T}" xmlns:m="{M}"><soap:Header><t:RequestServerVersion Version="Exchange2016"/>'
        f"<t:ExchangeImpersonation><t:ConnectingSID><t:PrimarySmtpAddress>{escape(mailbox)}</t:PrimarySmtpAddress>"
        f"</t:ConnectingSID></t:ExchangeImpersonation></soap:Header><soap:Body>{body}</soap:Body></soap:Envelope>"
    )
    try:
        password = Path(secret).read_text(encoding="utf-8").strip()
        ctx = ssl.create_default_context(cafile=ca)
        with httpx.Client(verify=ctx, auth=(user, password), timeout=30) as client:
            r = client.post(url, content=envelope.encode(), headers={"Content-Type": "text/xml; charset=utf-8"})
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
    except (OSError, ssl.SSLError, httpx.HTTPError, ET.ParseError):
        return None
    msgs = root.find(f".//{{{M}}}ResponseMessages")
    if msgs is None or not len(msgs) or any(m.get("ResponseClass") != "Success" for m in msgs):
        return None
    return msgs


def _find(
    mailbox: str, folder: str, subject: str | None, fields: str = "", whole: bool = False
) -> list[ET.Element] | None:
    where = (
        ""
        if subject is None
        else (
            '<m:Restriction><t:Contains ContainmentMode="Substring" ContainmentComparison="IgnoreCase">'
            f'<t:FieldURI FieldURI="item:Subject"/><t:Constant Value={quoteattr(subject)}/>'
            "</t:Contains></m:Restriction>"
        )
    )
    whole = whole or subject is not None
    size = PAGE if whole else LISTED
    msgs = call(mailbox, (
        f'<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>{fields}</m:ItemShape>'
        f'<m:IndexedPageItemView MaxEntriesReturned="{size}" Offset="0" BasePoint="Beginning"/>{where}'
        '<m:SortOrder><t:FieldOrder Order="Descending"><t:FieldURI FieldURI="item:DateTimeReceived"/></t:FieldOrder>'
        f'</m:SortOrder><m:ParentFolderIds><t:DistinguishedFolderId Id="{FOLDER_IDS[folder]}"/></m:ParentFolderIds>'
        "</m:FindItem>"))  # fmt: skip
    root = None if msgs is None else msgs[0].find(f"{{{M}}}RootFolder")
    if root is None:
        return None
    if whole and root.get("IncludesLastItemInRange") != "true":
        return None
    items = root.find(f"{{{T}}}Items")
    return [] if items is None else list(items)


def _key(item_id: str) -> int:
    return int(hashlib.sha256(item_id.encode()).hexdigest()[:15], 16)


def _id(el: ET.Element) -> str:
    i = el.find(f"{{{T}}}ItemId")
    return "" if i is None else i.get("Id", "")


def _ms(text: str) -> int:
    try:
        return int(dt.datetime.fromisoformat(text).timestamp() * 1000) if text else 0
    except ValueError:
        return 0


def _addresses(el: ET.Element | None) -> Iterator[str]:
    boxes: list[ET.Element] = [] if el is None else list(el.iter(f"{{{T}}}Mailbox"))
    for mb in boxes:
        addr, kind = mb.findtext(f"{{{T}}}EmailAddress", ""), mb.findtext(f"{{{T}}}RoutingType", "SMTP")
        yield addr.strip().lower() if addr and kind == "SMTP" else "?"


def parse(el: ET.Element) -> Item:

    def text(path: str) -> str:
        return el.findtext(path, "") or ""

    ext: dict[str, str] = {}
    for x in el.findall(f"{{{T}}}ExtendedProperty"):
        tag, value = x.find(f"{{{T}}}ExtendedFieldURI"), x.findtext(f"{{{T}}}Value", "")
        if tag is not None:
            ext[f"0x{int(tag.get('PropertyTag', '0'), 16):04X}"] = value
    rcpt = [el.find(f"{{{T}}}{n}") for n in ("ToRecipients", "CcRecipients", "BccRecipients", "RequiredAttendees",
                                             "OptionalAttendees", "Resources")]  # fmt: skip
    sender = next(_addresses(el.find(f"{{{T}}}Sender")), "")
    organizer = next(_addresses(el.find(f"{{{T}}}Organizer")), "")
    return {
        "sender": "" if sender == "?" else sender,
        "submit_ms": _ms(text(f"{{{T}}}DateTimeSent")),
        "delivery_ms": _ms(text(f"{{{T}}}DateTimeReceived")),
        "body": text(f"{{{T}}}TextBody"),
        "recipients": tuple(sorted(a for r in rcpt for a in _addresses(r))),
        "id": _key(_id(el)),
        "cls": text(f"{{{T}}}ItemClass"),
        "start_ms": _ms(text(f"{{{T}}}Start")),
        "end_ms": _ms(text(f"{{{T}}}End")),
        "created_ms": _ms(text(f"{{{T}}}DateTimeCreated")),
        "organizer": "" if organizer == "?" else organizer,
        "subject": ext.get("0x0E1D", ""),
        "prefix": ext.get("0x003D", ""),
        "msgid": text(f"{{{T}}}InternetMessageId").strip(),
        "in_reply_to": text(f"{{{T}}}InReplyTo").strip(),
        "read": text(f"{{{T}}}IsRead") == "true",
    }


def query(mailbox: str, folder: str, subject: str | None) -> list[Item] | None:
    found = _find(mailbox, folder, subject)
    if found is None:
        return None
    ids = [i for i in map(_id, found) if i]
    if len(ids) != len(found):
        return None
    if not ids:
        return []
    props = "".join(f'<t:FieldURI FieldURI="{f}"/>' for f in FIELDS) + "".join(
        f'<t:ExtendedFieldURI PropertyTag="{tag}" PropertyType="String"/>' for tag in EXTENDED
    )
    msgs = call(mailbox, (
        f"<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:BodyType>Text</t:BodyType>"
        f"<t:AdditionalProperties>{props}</t:AdditionalProperties></m:ItemShape><m:ItemIds>"
        + "".join(f"<t:ItemId Id={quoteattr(i)}/>" for i in ids) + "</m:ItemIds></m:GetItem>"))  # fmt: skip
    if msgs is None or len(msgs) != len(ids):
        return None
    items: list[Item] = []
    for m in msgs:
        got = m.find(f"{{{M}}}Items")
        if got is None or len(got) != 1:
            return None
        items.append(parse(got[0]))
    return [i for i in items if subject is None or i["subject"] == subject]


def read_state(mailbox: str, key: int, folder: str) -> bool | None:
    found = _find(mailbox, folder, None, '<t:AdditionalProperties><t:FieldURI FieldURI="message:IsRead"/>'
                  "</t:AdditionalProperties>", whole=True)  # fmt: skip
    hit = [el for el in found or [] if _key(_id(el)) == key]
    if len(hit) != 1:
        return None
    return {"true": True, "false": False}.get(hit[0].findtext(f"{{{T}}}IsRead", ""))


def send(sender: str, sender_name: str, to: str, to_name: str, subject: str, body: str, msgid: str) -> bool:

    def mailbox(name: str, addr: str) -> str:
        return f"<t:Mailbox><t:Name>{escape(name)}</t:Name><t:EmailAddress>{escape(addr)}</t:EmailAddress></t:Mailbox>"

    return call(sender, (
        '<m:CreateItem MessageDisposition="SendOnly"><m:Items><t:Message>'
        f"<t:Subject>{escape(subject)}</t:Subject><t:Body BodyType=\"Text\">{escape(body)}</t:Body>"
        '<t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x1035" PropertyType="String"/>'
        f"<t:Value>{escape(msgid)}</t:Value></t:ExtendedProperty>"
        f"<t:ToRecipients>{mailbox(to_name, to)}</t:ToRecipients><t:From>{mailbox(sender_name, sender)}</t:From>"
        "</t:Message></m:Items></m:CreateItem>")) is not None  # fmt: skip
