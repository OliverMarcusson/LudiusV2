"""PowerPoint deck template: planner validation of the slides, the .pptx reader and the guest-side done check
(offline)."""

from __future__ import annotations

import io
import random
import zipfile
from typing import Any

import pytest

from ludius import cli, guestfile, tasks
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, check_done, element_matches, file_baseline
from ludius.v1 import observation_pb2 as obs

DECK = {"title": "q4 close status", "slide_title": "open points", "bullets": "accruals by friday\nvat return filed",
        "slide2_title": "next steps", "bullets2": "review on 31/10"}  # fmt: skip
NS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')  # fmt: skip
REL = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def pptx(*slides: list[str], order: list[int] | None = None) -> bytes:
    """A minimal deck as PowerPoint writes it: slide i is slides/slide{i}.xml, listed in presentation.xml in `order`;
    each slide's first line is its title placeholder (written after the body, as tree order may be), the rest
    bullets, and every slide has an empty subtitle-like shape."""

    def sp(ph: str, lines: list[str]) -> str:
        paras = "".join(f"<a:p><a:r><a:t>{x[:2]}</a:t></a:r><a:r><a:t>{x[2:]}</a:t></a:r></a:p>" for x in lines)
        ph = f'<p:nvSpPr><p:nvPr><p:ph type="{ph}"/></p:nvPr></p:nvSpPr>'
        return f"<p:sp>{ph}<p:txBody>{paras}<a:p/></p:txBody></p:sp>"

    order = order or list(range(1, len(slides) + 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i}"/>' for i in order)
        z.writestr("ppt/presentation.xml", f"<p:presentation {NS}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>")
        rels = "".join(f'<Relationship Id="rId{i}" Target="slides/slide{i}.xml"/>' for i in range(1, len(slides) + 1))
        z.writestr("ppt/_rels/presentation.xml.rels", f"<Relationships {REL}>{rels}</Relationships>")
        for i, s in enumerate(slides, start=1):
            title = "ctrTitle" if i == 1 else "title"
            tree = sp("body", s[1:]) + sp(title, s[:1]) + sp("subTitle", [])
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld {NS}><p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sld>")
    return buf.getvalue()


SAVED = pptx(["Q4 close status"], ["Open points", "Accruals by friday", "Vat return filed"],
             ["Next steps", "Review on 31/10"])  # fmt: skip


def check(**content: str) -> list[str]:
    it = PlanItem.model_validate({"at": "09:00", "template": "powerpoint_short_deck", "context": "status deck",
                                  "params": {"file_name": "q4 close status"}, "content": DECK | content})  # fmt: skip
    return [why for _, why in validate([it], cli.plan_sources().templates, [], Policy())]


def test_planner_slides() -> None:
    assert check() == [] and check(slide2_title="", bullets2="") == []  # one or two bullet slides
    assert check(bullets2="") and check(slide2_title="")  # a slide is planned whole or not at all
    assert check(bullets="a point\n" * 7)  # bounded bullets
    assert check(slide_title="two\nlines") and check(title="it's q4")  # one-line titles, no smart quotes
    assert check(bullets="fine\n1. numbered") and check(bullets2="q4 - close")  # AutoFormat would change these


def test_pptx_reader() -> None:
    assert guestfile.pptx_slides(SAVED) == [["Q4 close status"], ["Open points", "Accruals by friday",
                                            "Vat return filed"], ["Next steps", "Review on 31/10"]]  # fmt: skip
    moved = pptx(["a title"], ["b title", "b point"], order=[2, 1])  # presentation order, not file names
    assert guestfile.pptx_slides(moved) == [["b title", "b point"], ["a title"]]
    assert guestfile.pptx_slides(b"not a zip") is None


@pytest.fixture
def task(monkeypatch: pytest.MonkeyPatch) -> tuple[Task, dict[str, Any]]:
    disk: dict[str, Any] = {}

    def read(user: str, rel: str) -> guestfile.GuestFile | None:
        return disk.get("file")

    monkeypatch.setattr(tasks.guestfile, "read", read)
    return _task(DECK), disk


def _task(content: dict[str, str]) -> Task:
    t = cli.plan_sources().templates["powerpoint_short_deck"]
    inst = TaskInstance(template=t.task, params={"file_name": "q4 close status"}, content=content, variant=0)
    tk = Task(t, inst, random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    return tk


def saved_snap() -> obs.Snapshot:
    s = obs.Snapshot()
    s.windows.add(process_name="POWERPNT.EXE", title="q4 close status - PowerPoint", kind=obs.Window.FOREGROUND)
    return s


def test_file_done_needs_a_new_deck_written_in_the_task(task: tuple[Task, dict[str, Any]]) -> None:
    tk, disk = task
    done = tk.template.subgoals["confirm_save"].done
    disk["file"] = guestfile.GuestFile(True, 2_000_000, SAVED)
    assert check_done(done, saved_snap(), tk, False) is False  # no baseline taken: could be pre-existing
    assert file_baseline(tk, Policy()) == "Documents\\q4 close status.pptx: already exists, never overwritten"
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, SAVED)
    assert check_done(done, saved_snap(), tk, False) is True
    disk["file"] = guestfile.GuestFile(True, 999_999, SAVED)
    assert check_done(done, saved_snap(), tk, False) is False  # written before the task started
    for wrong in (pptx(["Q4 close status"], ["Open points", "Accruals by friday", "Vat return filed"]),
                  pptx(["Q4 close status"], ["Open points Accruals by friday", "Vat return filed"],
                       ["Next steps", "Review on 31/10"]),  # bullets typed into the title
                  pptx(["Q4 close status"], ["Open points", "Accruals by friday", "Vat return filed"],
                       ["Next steps", "Review on 31/10"], [])):  # a stray empty slide # fmt: skip
        disk["file"] = guestfile.GuestFile(True, 2_000_000, wrong)
        assert check_done(done, saved_snap(), tk, False) is False


def test_one_bullet_slide_skips_the_second(task: tuple[Task, dict[str, Any]]) -> None:
    _, disk = task
    tk = _task(DECK | {"slide2_title": "", "bullets2": ""})
    assert not {"new_slide2", "write_slide2_title", "focus_bullets2", "write_bullets2"} & set(tk.subgoal_names)
    assert "write_bullets" in tk.subgoal_names
    disk["file"] = guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) is None
    disk["file"] = guestfile.GuestFile(True, 2_000_000, pptx(["Q4 close status"], ["Open points",
                                       "Accruals by friday", "Vat return filed"]))  # fmt: skip
    assert check_done(tk.template.subgoals["confirm_save"].done, saved_snap(), tk, False) is True


def test_slide_checks_on_screen(task: tuple[Task, dict[str, Any]]) -> None:
    """Thumbnails are named "Slide <title>" ("Slide" while untitled); the text box counts only with the focus."""
    tk, _ = task
    s = obs.Snapshot()
    s.windows.add(process_name="POWERPNT.EXE", title="Presentation1 - PowerPoint", kind=obs.Window.FOREGROUND)
    s.elements.add(id=1, role="list item", name="Slide Q4 close status", window=0)
    box = s.elements.add(id=2, role="image", name="Content Placeholder", window=0)
    sgs = tk.template.subgoals
    assert check_done(sgs["new_slide"].done, s, tk, False) is False
    stale = s.elements.add(id=3, role="list item", name="Slide", window=0)
    assert check_done(sgs["new_slide"].done, s, tk, False) is False  # run1: an untitled slide that isn't the new one
    stale.selected = True
    assert check_done(sgs["new_slide"].done, s, tk, False) is True
    assert check_done(sgs["focus_bullets"].done, s, tk, False) is False
    box.focused = True
    assert check_done(sgs["focus_bullets"].done, s, tk, False) is True
    s.elements[2].name = "Slide Open points"
    assert check_done(sgs["write_slide_title"].done, s, tk, True) is True
    assert check_done(sgs["write_slide_title"].done, s, tk, False) is False  # nothing typed yet
    sg = tk.subgoal("write_slide_title")
    assert any(element_matches(obs.Element(name="New Slide"), c, tk) for c in sg.avoid)  # run1: added a stray slide
