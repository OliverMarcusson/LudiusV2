from __future__ import annotations

import ssl
import xml.etree.ElementTree as ET

import pytest

from ludius import ews, mail

ITEM = f"""<t:Message xmlns:t="{ews.T}">
  <t:ItemId Id="AAMk1" ChangeKey="CQ"/><t:ItemClass>IPM.Note</t:ItemClass><t:Subject>RE: Q3 draft</t:Subject>
  <t:DateTimeReceived>2026-10-28T09:01:30Z</t:DateTimeReceived><t:DateTimeSent>2026-10-28T09:01:00Z</t:DateTimeSent>
  <t:DateTimeCreated>2026-10-28T09:00:00Z</t:DateTimeCreated><t:TextBody>Hi Anna</t:TextBody>
  <t:InReplyTo>&lt;a@ludius.internal&gt;</t:InReplyTo>
  <t:Sender><t:Mailbox><t:Name>Lars</t:Name><t:EmailAddress>LNord@ludius.internal</t:EmailAddress>
    <t:RoutingType>SMTP</t:RoutingType></t:Mailbox></t:Sender>
  <t:ToRecipients><t:Mailbox><t:EmailAddress>aberg@ludius.internal</t:EmailAddress></t:Mailbox>
    <t:Mailbox><t:EmailAddress>/o=Ludius/cn=x</t:EmailAddress><t:RoutingType>EX</t:RoutingType></t:Mailbox>
  </t:ToRecipients>
  <t:InternetMessageId>&lt;b@ludius.internal&gt;</t:InternetMessageId><t:IsRead>false</t:IsRead>
  <t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0xe1d" PropertyType="String"/><t:Value>Q3 draft</t:Value>
  </t:ExtendedProperty>
  <t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x3d" PropertyType="String"/><t:Value>RE: </t:Value>
  </t:ExtendedProperty>
</t:Message>"""


def unexpected(*_: object, **__: object) -> None:
    pytest.fail("grommunio must not be asked")


def post(*_: object, **__: object) -> object:
    return Response()


class Response:
    status_code = 200
    content = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="{ews.M}"><s:Body>'
               '<m:FindItemResponse><m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Error"/>'
               "</m:ResponseMessages></m:FindItemResponse></s:Body></s:Envelope>").encode()  # fmt: skip


def test_item_maps_to_message_and_non_smtp_recipients_never_match() -> None:
    m = mail.Message(**ews.parse(ET.fromstring(ITEM)))
    assert (m.sender, m.subject, m.prefix, m.cls, m.body) == ("lnord@ludius.internal", "Q3 draft", "RE: ", "IPM.Note",
                                                             "Hi Anna")  # fmt: skip
    assert m.recipients == ("?", "aberg@ludius.internal")
    assert (m.msgid, m.in_reply_to, m.read) == ("<b@ludius.internal>", "<a@ludius.internal>", False)
    assert m.submit_ms + 30_000 == m.delivery_ms and m.id > 0


def test_exchange_backend_without_settings_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDIUS_MAIL_BACKEND", "exchange")
    monkeypatch.delenv("LUDIUS_EWS_URL", raising=False)
    monkeypatch.setattr(mail, "_ssh", unexpected)
    assert mail.matching("aberg", "inbox", "x") is None
    assert mail.newest("aberg", "inbox") is None
    assert mail.read_state("aberg", 7) is None
    assert mail.present("aberg@ludius.internal", "inbox", "x", "lnord@ludius.internal") is None
    assert not mail.deliver("lnord@ludius.internal", "Lars", "aberg@ludius.internal", "Anna", "x", "y", "<m@x>")


def test_a_failed_ews_response_message_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ews, "_settings", lambda: ("https://x/EWS/Exchange.asmx", "svc", "pw", "ca"))
    context = ssl.create_default_context()
    monkeypatch.setattr(ews.Path, "read_text", lambda *_, **__: "pw")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(ews.ssl, "create_default_context", lambda **_: context)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(ews.httpx.Client, "post", post)
    assert ews.query("aberg@ludius.internal", "inbox", "x") is None
