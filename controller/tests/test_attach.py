from __future__ import annotations

import base64
import hashlib
import random
import xml.etree.ElementTree as ET
from typing import Any

import pytest
from test_edit import it

from ludius import cli, ews, mail, tasks
from ludius.guestfile import GuestFile
from ludius.planner import validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, check_done, file_baseline

TO = {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga", "subject": "rates", "body": "rates attached"}


def test_attach_only_an_own_documents_file_of_an_allowed_type() -> None:
    templates = cli.plan_sources().templates
    owned = {r"documents\rates.xlsx": "2026-11-01", r"documents\notes.csv": "2026-11-01"}

    def why(name: str) -> str:
        item = it("11:00", "outlook_send_attachment", **TO, file_name=name)
        r = validate([item], templates, [], Policy(), colleagues={"mvellinga"},
                     people={"mvellinga": "Marcus Vellinga"}, owned=owned, day="2026-11-02")  # fmt: skip
        return r[0][1] if r else ""

    assert why("Rates.xlsx") == ""
    assert "attach only one" in why("other.xlsx")
    assert "attach only .docx" in why("notes.csv")


def test_done_needs_the_baseline_bytes_attached_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    disk: dict[str, Any] = {}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk.get(rel))
    t = cli.plan_sources().templates["outlook_send_attachment"]
    tk = Task(t, TaskInstance(template=t.task, params={**TO, "file_name": "Rates.xlsx"}), random.Random(0))
    tk.owner, tk.started_ms, tk.mail_before = "aberg", 1_000_000, frozenset()
    rel = r"Documents\Rates.xlsx"
    assert file_baseline(tk, Policy()) == f"{rel}: unreadable on the guest"
    disk[rel] = GuestFile(False)
    assert "nothing to attach" in (file_baseline(tk, Policy()) or "")
    disk[rel] = GuestFile(True, 500, b"x" * (tasks.ATTACH_MAX_BYTES + 1))
    assert "nothing to attach" in (file_baseline(tk, Policy()) or "")
    disk[rel] = GuestFile(True, 500, b"a,b")
    assert file_baseline(tk, Policy()) is None
    got: list[Any] = []
    monkeypatch.setattr(mail, "delivered", lambda *a: got.append(a[-1]) or True)
    assert check_done(t.subgoals["send_shortcut"].done, ET.Element("x"), tk, False) is True  # type: ignore[arg-type]
    assert got == [("rates.xlsx", hashlib.sha256(b"a,b").hexdigest())]


def test_ews_attachments_are_names_and_hashes_and_item_attachments_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    t = f'xmlns:t="{ews.T}"'
    item = ET.fromstring(f'<t:Message {t}><t:Attachments><t:FileAttachment><t:AttachmentId Id="A1"/>'
                         "</t:FileAttachment></t:Attachments></t:Message>")  # fmt: skip
    data = base64.b64encode(b"a,b").decode()
    reply = ET.fromstring(f'<m:R xmlns:m="{ews.M}" {t}><m:GetAttachmentResponseMessage><m:Attachments>'
                          f"<t:FileAttachment><t:Name>Rates.xlsx </t:Name><t:Content>{data}</t:Content>"
                          "</t:FileAttachment></m:Attachments></m:GetAttachmentResponseMessage></m:R>")  # fmt: skip
    monkeypatch.setattr(ews, "call", lambda mailbox, body: reply)
    assert ews._attachments("a", item) == (("rates.xlsx", hashlib.sha256(b"a,b").hexdigest()),)  # pyright: ignore[reportPrivateUsage]
    content = reply.find(f".//{{{ews.T}}}Content")
    assert content is not None
    reply.find(f".//{{{ews.T}}}FileAttachment").remove(content)  # type: ignore[union-attr]
    assert ews._attachments("a", item) is None  # pyright: ignore[reportPrivateUsage]
    assert ews._attachments("a", ET.fromstring(f"<t:Message {t}/>")) == ()  # pyright: ignore[reportPrivateUsage]
    other = ET.fromstring(f'<t:Message {t}><t:Attachments><t:ItemAttachment><t:AttachmentId Id="A2"/>'
                          "</t:ItemAttachment></t:Attachments></t:Message>")  # fmt: skip
    assert ews._attachments("a", other) is None  # pyright: ignore[reportPrivateUsage]
