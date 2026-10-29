import random
from pathlib import Path

import pytest

from ludius import intranet
from ludius.policy import Policy
from ludius.v1.observation_pb2 import Element

PAGE = "---\ntitle: T\nsection: S\nowner: O\nupdated: 2026-01-02\ntags: a, b c\n---\nOne\nline.\n\nTwo.\n"


def test_shipped_pages_and_the_rules():
    assert any(p.id == "forklift-check" and p.popular for p in intranet.load())
    p = intranet.parse("x", PAGE)
    assert (p.tags, p.body, p.popular) == (("a", "b c"), ("One line.", "Two."), False)
    assert intranet.parse("x", PAGE + "See http://intranet.corp.ludius.test/?a=x\n")
    for id, text in [("X", PAGE), ("x", PAGE.replace("owner: O\n", "")), ("x", PAGE.replace("2026-01-02", "soon")),
                     ("x", PAGE + "http://example.com/\n"), ("x", PAGE + "https://corp.ludius.test:443@example.com/\n"),
                     ("x", PAGE + "see //example.com/x\n"),
                     ("x", PAGE.replace("tags", "color")), ("x", PAGE.split("---\n")[1])]:  # fmt: skip
        with pytest.raises(ValueError):
            intranet.parse(id, text)


def test_downloads(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(PAGE.replace("tags:", "popular: true\nfiles: Q4 plan.xlsx\ntags:"), encoding="utf-8")
    with pytest.raises(ValueError, match="not in files/"):
        intranet.load(tmp_path)
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "Q4 plan.xlsx").write_bytes(b"x")
    assert intranet.load(tmp_path)[0].files == ("Q4 plan.xlsx",)
    (tmp_path / "files" / "stray.pdf").write_bytes(b"x")
    with pytest.raises(ValueError, match="not on any page"):
        intranet.load(tmp_path)
    for bad in ("run.exe", "macro.xlsm", "../up.pdf", ".hidden.pdf"):
        with pytest.raises(ValueError):
            intranet.parse("x", PAGE.replace("tags:", f"files: {bad}\ntags:"))


def test_scenario_pages_are_off_limits(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text("pages: ['Forklift daily*']\n", encoding="utf-8")
    p = Policy.load(scenario)
    link = Element(name="Forklift daily pre-use check", role="link")
    assert p.element_allowed(link, []) and not p.element_allowed(Element(name="Expenses", role="link"), [])
    assert p.text_allowed("forklift daily pre-use check", None) and not p.text_allowed("forklift", None)
    assert p.text_allowed("http://intranet.corp.ludius.test/?a=forklift-check", None)  # its address (Sol)
    assert not p.text_allowed("intranet.corp.ludius.test/?a=forklift-checklist", None)
    assert "Forklift daily" not in intranet.index(skip=p.page_reserved) and "Forklift daily" in intranet.index()


def test_download_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from ludius import cli, guestfile, tasks
    from ludius.planner import PlanItem, item_problem
    from ludius.tasks import Task, TaskInstance

    ts, policy = cli.load_templates(), Policy()
    name, page = "Close timetable October 2026.csv", "Month-end close checklist and timetable"

    def item(**params: str) -> PlanItem:
        return PlanItem(at="09:00", template="edge_download_file", params={"page": page, "file_name": name} | params)

    assert item_problem(item(), ts, policy) is None
    assert item_problem(item(page="Travel and expense policy"), ts, policy)  # a file of another page
    assert item_problem(item(file_name="Close timetable.csv"), ts, policy)  # not a download
    reserved = Policy(reserved_pages=["(?i)month-end close.*"])
    assert item_problem(item(), ts, reserved) and name not in intranet.downloads(skip=reserved.page_reserved)

    t = Task(ts["edge_download_file"], TaskInstance(template="edge_download_file", params=item().params),
             random.Random(0))  # fmt: skip
    rel = tasks.write_paths(t)[0]
    t.owner, t.started_ms, t.files_absent = "aberg", 1000, frozenset({rel})
    served = (intranet.DIR / "files" / name).read_bytes()
    check = next(sg.done.file for sg in t.template.subgoals.values() if sg.done and sg.done.file)
    for got, ok in [(guestfile.GuestFile(True, 2000, served), True), (guestfile.GuestFile(True, 2000, b"x"), False),
                    (guestfile.GuestFile(True, 500, served), True), (None, False)]:  # fmt: skip
        monkeypatch.setattr(guestfile, "read", lambda user, rel, f=got: f)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        assert tasks._file_ok(check, t) is ok  # pyright: ignore[reportPrivateUsage]
    t.files_absent = frozenset()  # there at start: never this task's download
    assert tasks._file_ok(check, t) is False  # pyright: ignore[reportPrivateUsage]
    assert guestfile.profile_path("aberg", rel) == "C:\\Users\\aberg\\Downloads\\" + name
