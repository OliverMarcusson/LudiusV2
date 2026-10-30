from test_fixtures import load

from ludius.cli import ROOT
from ludius.options import build_options, view
from ludius.policy import Policy
from ludius.v1 import observation_pb2 as obs


def _scrolls(snap: obs.Snapshot) -> list[str]:
    _, task, name, _ = load(ROOT / "fixtures" / "start-menu")
    sg = task.subgoal(name)
    return [o.text for o in build_options(view(snap, task, sg, Policy()), task, sg, Policy()) if o.kind == "scroll"]


def test_scroll_offered_only_for_a_continuing_list_or_a_document() -> None:
    snap, *_ = load(ROOT / "fixtures" / "start-menu")
    assert any("list continues" in h for e in snap.elements for h in e.hints)
    assert _scrolls(snap) == ["scroll down", "scroll up"]
    for e in snap.elements:
        if any("list continues" in h for h in e.hints):
            e.hints[:] = ["items 1–6 shown, list continues"]
    assert _scrolls(snap) == ["scroll down", "scroll up"]

    for e in snap.elements:
        e.hints[:] = [h for h in e.hints if "list continues" not in h]
    assert _scrolls(snap) == []

    page = snap.elements.add(id=999, key="doc", role="document", name="Results", enabled=True, framework_id="Chrome")
    page.bounds.x, page.bounds.y, page.bounds.width, page.bounds.height = 0, 100, 1200, 800
    assert _scrolls(snap) == ["scroll down", "scroll up"]
