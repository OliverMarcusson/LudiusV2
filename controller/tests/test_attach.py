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


SAVE = {"sender": "mvellinga@ludius.internal", "subject": "rates", "file_name": "Rates.xlsx"}


def test_save_an_attachment_only_from_a_listed_mail_of_an_allowed_type() -> None:
    templates = cli.plan_sources().templates

    def why(**kw: str) -> str:
        item = it("11:00", "outlook_save_attachment", **{**SAVE, **kw})
        r = validate([item], templates, [], Policy(), colleagues={"mvellinga"},
                     people={"mvellinga": "Marcus Vellinga"}, inbox={("mvellinga", "rates")},
                     day="2026-11-02")  # fmt: skip
        return r[0][1] if r else ""

    assert why() == ""
    assert "listed under Inbox" in why(subject="other")
    assert "exact name" in why(file_name="rates.csv")


def test_saved_attachment_done_needs_the_mail_attachment_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    disk: dict[str, Any] = {}
    inbox: list[mail.Message] | None = None
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk.get(rel))
    monkeypatch.setattr(mail, "matching", lambda *a: inbox)
    t = cli.plan_sources().templates["outlook_save_attachment"]
    tk = Task(t, TaskInstance(template=t.task, params=SAVE), random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    rel, sha = r"Documents\Rates.xlsx", hashlib.sha256(b"a,b").hexdigest()

    def msg(sender: str, files: Any) -> mail.Message:
        return mail.Message(sender, 0, 0, "", subject="rates", attachments=files)

    disk[rel] = GuestFile(False)
    assert "inbox unreadable" in (file_baseline(tk, Policy()) or "")
    inbox = [msg("mvellinga@ludius.internal", None)]
    assert "not exactly one" in (file_baseline(tk, Policy()) or "")
    inbox = [
        msg("mvellinga@ludius.internal", (("other.xlsx", sha),)),
        msg("dkoster@ludius.internal", (("rates.xlsx", sha),)),
    ]
    assert "no single attachment" in (file_baseline(tk, Policy()) or "")
    inbox = [msg("mvellinga@ludius.internal", (("rates.xlsx", sha),))]
    assert "scenario-reserved mail" in (file_baseline(tk, Policy(reserved_subjects=["rates"])) or "")
    inbox = [msg("mvellinga@ludius.internal", (("rates.xlsx", sha),))]
    disk[rel] = GuestFile(True, 500, b"old")
    assert "never overwritten" in (file_baseline(tk, Policy()) or "")
    disk[rel] = GuestFile(False)
    assert file_baseline(tk, Policy()) is None
    done = t.subgoals["save_attachment"].done
    disk[rel] = GuestFile(True, 2_000_000, b"a,c")
    assert check_done(done, ET.Element("x"), tk, False) is False  # type: ignore[arg-type]
    disk[rel] = GuestFile(True, 500, b"a,b")
    assert check_done(done, ET.Element("x"), tk, False) is True  # type: ignore[arg-type]


REVISE = {**TO, "file_name": "Rates", "notes": "One more point"}


def test_revise_and_mail_attaches_the_saved_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_word import docx

    templates = cli.plan_sources().templates
    item = it("11:00", "word_revise_and_mail", **REVISE)
    assert validate([item], templates, [], Policy(), colleagues={"mvellinga"}, people={"mvellinga": "Marcus Vellinga"},
                    owned={r"documents\rates.docx": "2026-11-01"}, day="2026-11-02") == []  # fmt: skip
    rel, old, new = r"Documents\Rates.docx", docx("Rates"), docx("Rates", "One more point")
    disk = {rel: GuestFile(True, 500, old)}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, r: disk.get(r))
    got: list[Any] = []
    monkeypatch.setattr(mail, "delivered", lambda *a: got.append(a[-1]) or True)
    t = templates["word_revise_and_mail"]
    tk = Task(t, TaskInstance(template=t.task, params=REVISE), random.Random(0))
    tk.owner, tk.started_ms, tk.mail_before = "aberg", 1_000_000, frozenset()
    assert file_baseline(tk, Policy()) is None
    disk[rel] = GuestFile(True, 2_000_000, new)
    assert check_done(t.subgoals["save_keys"].done, ET.Element("x"), tk, False) is True  # type: ignore[arg-type]
    assert check_done(t.subgoals["send_shortcut"].done, ET.Element("x"), tk, False) is True  # type: ignore[arg-type]
    assert got == [("rates.docx", hashlib.sha256(new).hexdigest())]
