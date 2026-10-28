"""Word document template: planner validation of the text, the .docx reader and the guest-side done check (offline)."""

from __future__ import annotations

import io
import random
import zipfile
from typing import Any

import pytest

from ludius import cli, guestfile, tasks
from ludius.options import build_options, view
from ludius.planner import PlanItem, catalog, validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, Template, check_done, file_baseline
from ludius.v1 import observation_pb2 as obs

TITLE, BODY = "q4 close memo", "accruals are booked by friday\nopen points go to the review on 31/10"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _zip(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, x in parts.items():
            z.writestr(n, x)
    return buf.getvalue()


def docx(*paragraphs: str, table: str = "") -> bytes:
    """A minimal document as Word writes it: a paragraph's text split over runs."""

    def p(text: str) -> str:
        runs = "".join(f'<w:r><w:t xml:space="preserve">{x}</w:t></w:r>' for x in (text[:3], text[3:]))
        return f"<w:p>{runs}</w:p>"

    cells = f"<w:tbl><w:tr><w:tc>{p(table)}</w:tc></w:tr></w:tbl>" if table else ""
    xml = f"<w:document {W}><w:body>{''.join(p(x) for x in paragraphs)}{cells}<w:sectPr/></w:body></w:document>"
    return _zip({"word/document.xml": xml})


SAVED = docx("Q4 close memo", "", "Accruals are booked by friday", "Open points go to the review on 31/10")


def item(file_name: str = "q4 close memo", title: str = TITLE, body: str = BODY) -> PlanItem:
    return PlanItem.model_validate(
        {
            "at": "09:00",
            "template": "word_short_document",
            "context": "close memo",
            "params": {"file_name": file_name},
            "content": {"title": title, "body": body},
        }
    )


def check(**params: str) -> list[str]:
    return [why for _, why in validate([item(**params)], cli.plan_sources().templates, [], Policy())]


def test_planner_text() -> None:
    assert check() == []
    assert check(body="a line\n" * 11)  # bounded
    assert check(title="two\nlines")  # the title is one paragraph
    for bad in ('say "hi"', "it's done", "q4 - close", "q4 -- close", "half 1/2 done", "1. first", "a) first",
                "- first", "*bold* word", "under_score", "bl\u00e5", " leading space", "close by octo"):  # fmt: skip
        assert check(body=f"fine line\n{bad}"), bad
    assert check(body="q3/q4 split and 12% left\nfriday 31/10") == []  # slashes, percent and a full day name
    assert check(file_name="a/b")  # file names stay under the shared rules


def test_docx_reader() -> None:
    assert guestfile.docx_paragraphs(SAVED) == ["Q4 close memo", "Accruals are booked by friday",
                                                "Open points go to the review on 31/10"]  # fmt: skip
    assert guestfile.docx_paragraphs(docx("a b", table="in a table")) == ["a b", "in a table"]
    assert guestfile.docx_paragraphs(b"not a zip") is None
    assert guestfile.docx_paragraphs(_zip({"word/other.xml": "<x/>"})) is None


@pytest.fixture
def task(monkeypatch: pytest.MonkeyPatch) -> tuple[Task, dict[str, Any]]:
    disk: dict[str, Any] = {}  # what guestfile.read sees: None = unreadable

    def read(user: str, rel: str) -> guestfile.GuestFile | None:
        return disk.get("file")

    monkeypatch.setattr(tasks.guestfile, "read", read)
    t = cli.plan_sources().templates["word_short_document"]
    inst = TaskInstance(template=t.task, params={"file_name": "q4 close memo"}, content={"title": TITLE, "body": BODY},
                        variant=0)  # fmt: skip
    tk = Task(t, inst, random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    return tk, disk


def saved_snap() -> obs.Snapshot:
    s = obs.Snapshot()
    s.windows.add(process_name="WINWORD.EXE", title="q4 close memo - Word", kind=obs.Window.FOREGROUND)
    return s


def test_file_done_needs_a_new_file_written_in_the_task(task: tuple[Task, dict[str, Any]]) -> None:
    tk, disk = task
    done = tk.template.subgoals["confirm_save"].done
    assert tk.values["title_enter"] == TITLE + "\n"  # the title ends in Enter, so the body starts a new paragraph
    disk["file"] = guestfile.GuestFile(True, 2_000_000, SAVED)
    assert check_done(done, saved_snap(), tk, False) is False  # no baseline taken: could be pre-existing
    assert file_baseline(tk, Policy()) == "Documents\\q4 close memo.docx: already exists, never overwritten"
    disk["file"] = None
    assert file_baseline(tk, Policy()) == "Documents\\q4 close memo.docx: unreadable on the guest"
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, SAVED)
    assert check_done(done, saved_snap(), tk, False) is True  # AutoCorrect's capitals and empty paragraphs fold
    assert check_done(done, obs.Snapshot(), tk, False) is False  # the title check still applies
    disk["file"] = guestfile.GuestFile(True, 999_999, SAVED)
    assert check_done(done, saved_snap(), tk, False) is False  # written before the task started
    for wrong in (docx("Q4 close memo", "Accruals are booked by friday"),  # a line missing
                  docx("Accruals are booked by friday", "Q4 close memo", "Open points go to the review on 31/10"),
                  docx("Q4 close memoAccruals are booked by friday", "Open points go to the review on 31/10"),
                  docx("Q4 close memo", "Accruals are booked by friday", "Open points go to the review on 31/10",
                       "stray"), b"garbage"):  # fmt: skip
        disk["file"] = guestfile.GuestFile(True, 2_000_000, wrong)
        assert check_done(done, saved_snap(), tk, False) is False


def test_collisions_and_reserved_paths(task: tuple[Task, dict[str, Any]], tmp_path: Any) -> None:
    tk, _ = task
    templates = cli.plan_sources().templates
    clash = [w for _, w in validate([item()], templates, [], Policy(), existing=["Documents\\Q4 Close Memo.docx"])]
    assert clash == ["file_name: 'q4 close memo' already exists or is used by another item, choose a different name"]
    two = [item(), item(title="another memo", body="something else to say")]
    assert [i for i, _ in validate(two, templates, [], Policy())] == [1]  # the second would overwrite the first
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(r"reserved: [{path: 'C:\Users\*\Documents\q4 close memo.docx'}]", encoding="utf-8")
    p = Policy.load(scenario)
    why = [w for _, w in validate([item()], templates, [], p, user="aberg")]
    assert why == [r"Documents\q4 close memo.docx: scenario-reserved file, choose a different name"]
    assert file_baseline(tk, p) == r"Documents\q4 close memo.docx: scenario-reserved file"


def test_overwrite_confirmation_offers_neither_yes_nor_enter(task: tuple[Task, dict[str, Any]]) -> None:
    tk, _ = task
    s, p = obs.Snapshot(), Policy()
    s.windows.add(process_name="WINWORD.EXE", title="Confirm Save As", kind=obs.Window.FOREGROUND)
    for i, name in enumerate(("&Yes", "&No"), start=1):
        e = s.elements.add(id=i, role="button", name=name, window=0)
        e.bounds.width = e.bounds.height = 20
    sg = tk.subgoal("confirm_save")
    opts = [o.text for o in build_options(view(s, tk, sg, p), tk, sg, p)]
    assert not any("Yes" in o or o == "press Enter" for o in opts) and any("'&No'" in o for o in opts)


def test_backstage_never_offers_onedrive_or_sign_in(task: tuple[Task, dict[str, Any]]) -> None:
    tk, _ = task
    s, p = obs.Snapshot(), Policy()
    s.windows.add(process_name="WINWORD.EXE", title="Document1 - Word", kind=obs.Window.FOREGROUND)
    for i, (role, name) in enumerate([("tab item", "OneDrive"), ("hyperlink", "Sign in"), ("button", "Sign in"),
                                      ("button", "Browse")], start=1):  # fmt: skip
        e = s.elements.add(id=i, role=role, name=name, window=0, enabled=True)
        e.bounds.width = e.bounds.height = 20
    sg = tk.subgoal("browse")
    opts = [o.text for o in build_options(view(s, tk, sg, p), tk, sg, p)]
    assert any("'Browse'" in o for o in opts) and not any("OneDrive" in o or "Sign in" in o for o in opts)


def test_template_declares_its_write_and_wordpad_is_planned_less() -> None:
    templates = cli.plan_sources().templates
    t = templates["word_short_document"].model_dump()
    with pytest.raises(ValueError, match="saves .docx|not a declared write"):
        Template.model_validate({**t, "writes": [r"Documents\{file_name}.rtf"]})
    with pytest.raises(ValueError, match="needs paragraphs"):
        tasks.FileCheck(path=r"Documents\{file_name}.docx", cells="{rows}")
    lines = {ln.split(":")[0]: ln for ln in catalog(templates).splitlines()}
    assert "rarely" in lines["- wordpad_formatted_note"] and "rarely" not in lines["- word_short_document"]
