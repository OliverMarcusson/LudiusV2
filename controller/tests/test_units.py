from __future__ import annotations

import random
from typing import Any

import httpx
import pytest

from ludius import decisions, keys
from ludius.cli import DATA
from ludius.decisions import DecisionError, build_request, parse_response
from ludius.policy import Policy
from ludius.tasks import Done, Persona, Task, TaskInstance, WindowCheck, check_done, load_template
from ludius.v1 import observation_pb2 as obs

OPTIONS = {"o1": "click [1] button 'Save'", "o2": "press Esc"}


def response(**over: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "model": "typesafe/jev-1.14-20261001",
        "answers": {
            "action": {"type": "choice", "choice": "o1", "confidence": 0.8, "probabilities": {"o1": 0.9, "o2": 0.1}},
            "subgoal_done": {"type": "noul", "noul": 0.1},
            "off_track": {"type": "noul", "noul": 0.05},
        },
        "usage": {"input_tokens": 100, "output_tokens": 3, "cost": 0.0001},
    }
    r.update(over)
    return r


def test_parse_valid() -> None:
    d = parse_response(response(), "typesafe/jev-1.14", OPTIONS)
    assert (d.choice, d.subgoal_done, d.off_track) == ("o1", 0.1, 0.05)


@pytest.mark.parametrize(
    "answers",
    [
        {"action": {"type": "choice", "choice": "o3", "confidence": 0.5, "probabilities": {"o1": 0.5, "o2": 0.5}}},
        {"action": {"type": "choice", "choice": "o2", "confidence": 0.5, "probabilities": {"o1": 0.9, "o2": 0.1}}},
        {"action": {"type": "choice", "choice": "o1", "confidence": 0.5, "probabilities": {"o1": 0.9}}},
        {"action": {"type": "choice", "choice": "o1", "confidence": 0.5, "probabilities": {"o1": 0.9, "o2": 0.5}}},
    ],
)
def test_parse_rejects_inconsistent_answers(answers: dict[str, Any]) -> None:
    full = response()["answers"] | answers
    with pytest.raises(DecisionError):
        parse_response(response(answers=full), "typesafe/jev-1.14", OPTIONS)


def test_parse_rejects_model_drift() -> None:
    with pytest.raises(DecisionError):
        parse_response(response(model="other/model"), "typesafe/jev-1.14", OPTIONS)


def test_request_limits() -> None:
    with pytest.raises(DecisionError):
        build_request("m", {}, {f"o{i}": "x" for i in range(256)})
    with pytest.raises(DecisionError):
        build_request("m", {}, {"bad key!": "x"})
    q = build_request("m", {}, OPTIONS)["questions"]
    assert set(q) == {"action", "subgoal_done", "off_track"} and q["subgoal_done"]["type"] == "noul"
    # A screenshot for image-reading Decisions models (liquid/d1.1): the JSON state as text, then the image part.
    state = build_request("liquid/d1.1", {"goal": "g"}, OPTIONS, image=b"\x89PNG...")["state"]
    assert state[0] == '{"goal": "g"}' and state[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_rate_limit_retried_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(decisions.calls, "count", sent.append)
    waits: list[float] = []
    monkeypatch.setattr(decisions.time, "sleep", waits.append)

    def client(codes: list[int]) -> decisions.DecisionsClient:
        c = decisions.DecisionsClient("k", model="typesafe/jev-1.14")
        replies = iter(codes)

        def reply(_: httpx.Request) -> httpx.Response:
            code = next(replies)
            return httpx.Response(code, json=response() if code == 200 else {})

        c._http = httpx.Client(transport=httpx.MockTransport(reply))  # pyright: ignore[reportPrivateUsage]
        return c

    d = client([429, 200]).decide({}, OPTIONS)
    assert (
        d.choice == "o1" and d.retries_429 == 1 and sent == ["decision", "decision"]
    )  # the retry counts against the cap
    sent.clear()
    with pytest.raises(DecisionError, match="429"):  # 3 retries, then the fourth 429 raises
        client([429] * 4).decide({}, OPTIONS)
    assert len(sent) == 4 and waits[-3:] == [1, 2, 4]
    sent.clear()
    with pytest.raises(DecisionError, match="400"):  # any other 4xx raises at once
        client([400]).decide({}, OPTIONS)
    assert len(sent) == 1


def test_chords() -> None:
    assert keys.chord("Ctrl+S") == [0x11, ord("S")]
    assert keys.chord("Win+R") == [0x5B, ord("R")]
    assert keys.chord("Alt+F4") == [0x12, 0x73]


def notepad_task(**params: str) -> Task:
    t = load_template(DATA / "templates" / "notepad_write_note.yaml")
    inst = TaskInstance(template=t.task, params={"file_name": "q3 notes", "launch_query": "notepad", **params},
                        content={"body": "x"})  # fmt: skip
    return Task(t, inst, random.Random(0))


def test_title_check_tolerates_hidden_extension() -> None:
    task = notepad_task()
    done = task.template.subgoals["confirm_save"].done
    for title, ok in [("q3 notes - Notepad", True), ("q3 notes.txt - Notepad", True), ("*q3 notes - Notepad", False)]:
        snap = obs.Snapshot(windows=[obs.Window(kind=obs.Window.FOREGROUND, process_name="notepad.exe", title=title)])
        assert check_done(done, snap, task, typed_ok=False) is ok, title


def test_absent_window_check() -> None:
    task = notepad_task()
    done = Done(window=WindowCheck(process="notepad.exe", absent=True))
    assert check_done(done, obs.Snapshot(), task, False) is True
    open_np = obs.Snapshot(windows=[obs.Window(process_name="Notepad.exe")])
    assert check_done(done, open_np, task, False) is False
    assert check_done(None, open_np, task, False) is None


def test_wordpad_body_rejects_a_split_heading() -> None:
    """Acceptance 20261027-1255: a click after the heading put the caret inside it, so Enter split the heading
    and the body landed in the gap. Every keystroke landed (typed_ok), but the document is wrong: never done."""
    t = load_template(DATA / "templates" / "wordpad_formatted_note.yaml")
    heading = "finance team - erp test results 28/10"
    body = "vendor master mapping - dupes?\nbank rec test run - matched ok?\nwho signs off test results"
    inst = TaskInstance(template=t.task, params={"file_name": "agenda", "heading": heading, "body": body})
    task, done = Task(t, inst, random.Random(0)), t.subgoals["bullets_body"].done

    def doc(value: str) -> obs.Snapshot:
        return obs.Snapshot(elements=[obs.Element(role="document", name="Rich Text Window", value=value)])

    split = "finance team - erp test resu\rvendor master mapping - dupes?\rbank rec test run - matched ok?\r" \
            "who signs off test resultslts 28/10\r"  # fmt: skip
    assert check_done(done, doc(split), task, typed_ok=True) is False
    assert check_done(done, doc(heading + "\r" + body.replace("\n", "\r") + "\r"), task, typed_ok=True) is True
    assert check_done(done, doc(heading + "\r" + body.replace("\n", "\r")), task, typed_ok=True) is True
    assert check_done(done, doc(heading + "\r" + body.replace("\n", "\r")), task, typed_ok=False) is False


def test_policy() -> None:
    p = Policy(reserved_paths=[r"\\finance\\payroll"])
    save = obs.Element(name="Don't Save", hints=["destructive"])
    assert p.element_allowed(save, []) is not None
    assert p.element_allowed(save, ["Don't Save"]) is None
    assert p.element_allowed(obs.Element(name="Empty Recycle Bin"), [".*"]) is not None
    assert p.element_allowed(obs.Element(name="pw", password=True), []) is not None
    assert p.text_allowed(r"C:\finance\payroll\x.txt", None) is not None
    assert p.text_allowed("notes.txt", None) is None


def test_all_templates_load() -> None:
    for path in (DATA / "templates").glob("*.yaml"):
        load_template(path)


def test_near_duplicate() -> None:
    from ludius.planner import near_duplicate

    earlier = ["Call with Johan re Q3 close - accruals missing"]
    assert near_duplicate("call with johan, re Q3 close: accruals missing!", earlier)
    assert near_duplicate("Budget review with Maria for the Lund office move", earlier) is None


def test_plan_validation() -> None:
    from ludius.cli import load_templates
    from ludius.planner import PlanItem, validate

    ok = PlanItem(at="09:10", template="explorer_new_folder", params={"folder_name": "lund accruals q3"})
    items = [
        ok,
        PlanItem(at="12:00", template="break", minutes=45),
        PlanItem(at="13:00", template="no_such_task"),
        PlanItem(at="13:30", template="explorer_new_folder", params={"folder_name": "a/b"}),
        PlanItem(at="14:00", template="edge_web_lookup"),  # search_query missing
        PlanItem(at="15:00", template="explorer_new_folder", params={"folder_name": "Lund accruals Q3!"}),  # dup
        PlanItem(at="16:00", template="edge_web_lookup", params={"search_query": "moms på tjänster"}),
    ]
    assert [i for i, _ in validate(items, load_templates(), [], Policy())] == [2, 3, 4, 5, 6]


def test_growth_checker_accepts_catalog_templates() -> None:
    import yaml

    from ludius import growth
    from ludius.cli import load_templates

    caps = yaml.safe_load((DATA / "capabilities.yaml").read_text(encoding="utf-8"))
    templates = load_templates()
    for name, t in templates.items():
        others = {k: v for k, v in templates.items() if k != name}
        problems = growth.check(t, caps, others)
        if t.app == "outlook":  # hand-written: sending is allowlisted, which proposals may never do
            problems = [p for p in problems if "not permitted for proposals" not in p]
        if t.app in ("word", "powerpoint"):  # hand-written: document text has no UIA value, the file check decides
            problems = [p for p in problems if "unreadable_ok not permitted for proposals" not in p]
        assert problems == [], name
    bad = templates["explorer_new_folder"].model_copy(update={"processes": ["regedit.exe"]})
    assert any("outside the catalog" in p for p in growth.check(bad, caps, {}))


def test_near_tie_only_between_equivalent_options() -> None:
    from ludius.decisions import Decision
    from ludius.loop import Runner
    from ludius.options import Option

    def el(i: int, name: str) -> obs.Element:
        return obs.Element(id=i, key=f"k{i}", role="button", name=name)

    opts = [Option("o1", "click", "a", element=el(1, "Save")), Option("o2", "click", "b", element=el(2, "&Save")),
            Option("o3", "click", "c", element=el(3, "Cancel"))]  # fmt: skip
    d = Decision("o1", 0.5, {"o1": 0.45, "o2": 0.42, "o3": 0.44}, 0, 0, "m")
    runner = Runner.__new__(Runner)
    runner.rng = random.Random(1)
    picks = {runner._choose(d, opts).key for _ in range(40)}  # pyright: ignore[reportPrivateUsage]
    assert picks == {"o1", "o2"}


def test_mapping_recheck_on_display_change(tmp_path: Any) -> None:
    from ludius.loop import GroundTruth, Runner
    from ludius.v1 import agent_pb2 as pb

    class FakeAgent:
        vm = "t"

        def __init__(self) -> None:
            self.size, self.pos, self.offset = (1920, 1080), (0, 0), 0

        def move(self, x: int, y: int, duration_ms: int = 0) -> int:
            self.pos = (x + self.offset, y)
            return 0

        def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
            s = obs.Snapshot(screen_width=self.size[0], screen_height=self.size[1], dpi=96,
                             cursor_x=self.pos[0], cursor_y=self.pos[1])  # fmt: skip
            return pb.ObserveResult(snapshot=s)

    agent = FakeAgent()
    runner = Runner(agent, None, Persona(name="t", user="t"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    runner.observe()
    assert runner.mapping_ok  # first display: nothing to compare
    agent.size = (1280, 720)
    runner.observe()
    assert runner.mapping_ok
    agent.size, agent.offset = (1600, 900), 7  # pointer lands 7 px off after this change
    runner.observe()
    assert not runner.mapping_ok


def test_document_click_keeps_the_caret_at_the_end(tmp_path: Any) -> None:
    """A click inside a WordPad heading moved the caret there and the next Enter split it (fidelity 20261027-1259)."""
    from ludius.loop import GroundTruth, Runner
    from ludius.options import Option

    class FakeAgent:
        vm = "t"

        def __init__(self) -> None:
            self.chords: list[list[int]] = []

        def move(self, x: int, y: int, duration_ms: int = 0) -> int:
            return 0

        def click(self, count: int = 1) -> int:
            return 0

        def chord(self, vks: list[int]) -> int:
            self.chords.append(vks)
            return 0

    agent = FakeAgent()
    runner = Runner(agent, None, Persona(name="t", user="t"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    box = obs.Rect(x=0, y=0, width=800, height=600)
    doc = obs.Element(key="d", role="document", value="heading\r", bounds=box)
    web = obs.Element(key="w", role="document", value="https://intranet/", read_only=True, bounds=box)
    button = obs.Element(key="b", role="button", bounds=box)
    for e, expected in ((doc, [keys.chord("Ctrl+End")]), (web, []), (button, [])):
        agent.chords.clear()
        runner.act(Option("o1", "click", "click", element=e), None)  # type: ignore[arg-type]
        assert agent.chords == expected, e.role


def test_no_alt_f4_on_the_desktop() -> None:
    """Alt+F4 on the desktop opens Shut Down Windows; an OK there powered the test VM off once."""
    from ludius.options import build_options, view
    from ludius.policy import alt_f4_safe, shell_dialog

    t = load_template(DATA / "templates" / "notepad_write_note.yaml")
    task = Task(t, TaskInstance(template=t.task, params=t.example), random.Random(0))
    sg = task.subgoal("close_app")
    for cls, offered in (("Progman", False), ("Shell_TrayWnd", False), ("Notepad", True)):
        fg = obs.Window(kind=obs.Window.FOREGROUND, class_name=cls, process_name="explorer.exe")
        snap = obs.Snapshot(windows=[fg])
        opts = build_options(view(snap, task, sg, Policy()), task, sg, Policy())
        assert any(o.keys == "Alt+F4" for o in opts) is offered
    assert not alt_f4_safe(None)
    shutdown = obs.Window(kind=obs.Window.FOREGROUND, class_name="#32770", process_name="explorer.exe")
    assert shell_dialog(shutdown)
    assert not shell_dialog(obs.Window(kind=obs.Window.FOREGROUND, class_name="#32770", process_name="notepad.exe"))


def test_chat_enter_only_in_the_guarded_send() -> None:
    """Enter in Mattermost's message box sends: a write step's Enter once posted a doubled leftover draft (live run)."""
    from ludius.options import build_options, view

    t = load_template(DATA / "templates" / "chat_post_message.yaml")
    task = Task(t, TaskInstance(template=t.task, params=t.example), random.Random(0))
    w = obs.Window(kind=obs.Window.FOREGROUND, class_name="Chrome_WidgetWin_1", process_name="msedge.exe")
    box = obs.Rect(x=10, y=10, width=400, height=40)
    composer = obs.Element(id=1, key="c", role="edit", name="Write to Coffee Corner", focused=True, bounds=box)
    switcher = obs.Element(id=2, key="s", role="edit", name="Find channel", focused=True, bounds=box)

    def enter(sg: str, *elements: obs.Element) -> bool:
        snap = obs.Snapshot(windows=[w], elements=list(elements))
        s = task.subgoal(sg)
        return any(o.keys == "Enter" for o in build_options(view(snap, task, s, Policy()), task, s, Policy()))

    assert not enter("write_message", composer)
    assert not enter("write_message")  # focus unknown
    assert enter("open_channel", switcher)
    assert enter("send_message", composer)  # its require checks guard the press


def test_chat_enter_rechecks_the_focus_at_the_press(tmp_path: Any) -> None:
    """The focus can move to the message box between the choice and the press: Enter is refused there, not sent."""
    from ludius.loop import GroundTruth, Runner
    from ludius.options import Option

    t = load_template(DATA / "templates" / "chat_post_message.yaml")
    task = Task(t, TaskInstance(template=t.task, params=t.example), random.Random(0))
    w = obs.Window(kind=obs.Window.FOREGROUND, process_name="msedge.exe")
    box = obs.Element(role="edit", name="Write to Coffee Corner", value="hi hi", focused=True)
    chords: list[list[int]] = []
    seen = type("R", (), {"snapshot": obs.Snapshot(windows=[w], elements=[box])})()
    agent = type("A", (), {"vm": "t", "chord": lambda self, vks: chords.append(vks) or 0,
                           "observe": lambda self, **_: seen})()  # fmt: skip
    r = Runner(agent, None, Persona(name="t", user="t"), GroundTruth(tmp_path / "gt.jsonl"))  # type: ignore[arg-type]
    assert r.act(Option("o1", "key", "press Enter", keys="Enter"), task, task.subgoal("open_channel")) and not chords
    assert r.act(Option("o1", "key", "press Esc", keys="Esc"), task, task.subgoal("open_channel")) is None and chords


def test_visual_words_are_click_targets() -> None:
    from ludius.options import build_options, view

    t = load_template(DATA / "templates" / "notepad_write_note.yaml")
    task = Task(t, TaskInstance(template=t.task, params=t.example), random.Random(0))
    sg = task.subgoal("close_app")
    w = obs.Window(kind=obs.Window.FOREGROUND, class_name="Notepad", process_name="notepad.exe", no_tree=True)
    box = obs.Rect(x=10, y=10, width=40, height=12)
    snap = obs.Snapshot(windows=[w], elements=[
        obs.Element(id=1, role="text", name="Save", framework_id="visual", bounds=box),
        obs.Element(id=2, role="text", name="Label", framework_id="Win32", bounds=box),
    ])  # fmt: skip
    texts = [o.text for o in build_options(view(snap, task, sg, Policy()), task, sg, Policy()) if o.kind == "click"]
    assert any("'Save'" in x for x in texts) and not any("'Label'" in x for x in texts)
