from pathlib import Path

import pytest

from ludius import keys, session
from ludius.loop import GroundTruth
from ludius.session import Session
from ludius.v1 import agent_pb2 as pb
from ludius.v1 import observation_pb2 as obs


def _win(
    kind: obs.Window.Kind, cls: str, x: int, y: int, w: int, h: int, title: str = "", no_tree: bool = False
) -> obs.Window:
    return obs.Window(
        kind=kind, class_name=cls, process_name="explorer.exe", title=title, framework_id="Win32", no_tree=no_tree,
        bounds=obs.Rect(x=x, y=y, width=w, height=h),
    )  # fmt: skip


def clean_desktop() -> obs.Snapshot:
    return obs.Snapshot(
        screen_width=1920, screen_height=1080, dpi=96,
        windows=[
            _win(obs.Window.FOREGROUND, "Progman", 0, 0, 1920, 1080, "Program Manager"),
            _win(obs.Window.POPUP, "Worker Window", 1593, 942, 242, 48, no_tree=True),
            _win(obs.Window.SHELL, "Shell_TrayWnd", 0, 1040, 1920, 40),
        ],
        elements=[obs.Element(id=1, role="list", name="Desktop", window=0)],
    )  # fmt: skip


class ObserveOnly:
    def __init__(self, snaps: list[obs.Snapshot]) -> None:
        self.snaps, self.chords = snaps, list[list[int]]()

    def observe(self, wait_settle: bool = True, quiet_ms: int = 300, timeout_ms: int = 5000) -> pb.ObserveResult:
        return pb.ObserveResult(snapshot=self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0])

    def __getattr__(self, name: str) -> object:
        pytest.fail(f"normalize tried {name} on a clean desktop")


class EscOnly(ObserveOnly):
    def chord(self, vks: list[int]) -> int:
        if vks != keys.chord("Esc"):
            pytest.fail(f"unexpected chord {vks}")
        self.chords.append(vks)
        return 0


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownLambdaType,reportUnknownArgumentType]

    def deny(*_a: object, **_k: object) -> int:
        pytest.fail("escalate during a normalize test")

    monkeypatch.setattr(Session, "escalate", deny)


def test_clean_desktop_with_tray_worker_window_normalizes_with_no_input(tmp_path: Path) -> None:
    log = tmp_path / "gt.jsonl"
    s = Session(ObserveOnly([clean_desktop()]), GroundTruth(log), "pw")  # pyright: ignore[reportArgumentType]
    assert s.normalize()
    assert '"normalized"' in log.read_text() and "normalize_failed" not in log.read_text()


def test_file_operation_window_left_in_front_is_never_closed(tmp_path: Path) -> None:
    ghost = clean_desktop()
    ghost.windows[0].CopyFrom(_win(obs.Window.FOREGROUND, "OperationStatusWindow", 104, 104, 1440, 759, "Progress"))
    assert Session(ObserveOnly([ghost]), GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize()  # pyright: ignore[reportArgumentType]


def test_real_menu_over_the_desktop_still_gets_esc(tmp_path: Path) -> None:
    menu = clean_desktop()
    menu.windows.append(_win(obs.Window.POPUP, "#32768", 800, 400, 200, 300))
    menu.elements.append(obs.Element(id=2, role="menu item", name="View", window=3))
    agent = EscOnly([menu, menu, clean_desktop()])
    assert Session(agent, GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize()  # pyright: ignore[reportArgumentType]
    assert agent.chords == [keys.chord("Esc")]


def test_worker_window_with_a_readable_tree_is_still_a_popup(tmp_path: Path) -> None:
    snap = clean_desktop()
    snap.windows[1].no_tree = False
    snap.elements.append(obs.Element(id=2, role="button", name="Open", window=1))
    agent = EscOnly([snap])
    assert not Session(agent, GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize(rounds=2)  # pyright: ignore[reportArgumentType]
    assert agent.chords == [keys.chord("Esc")] * 2


def test_minimized_edge_is_brought_forward_by_its_taskbar_button(tmp_path: Path) -> None:
    edge = clean_desktop()
    edge.apps.append(
        obs.Window(
            title="Harbourline intranet - Microsoft\u200b Edge",
            process_name="msedge.exe",
            bounds=obs.Rect(x=-32000, y=-32000, width=160, height=28),
        )
    )
    edge.elements.append(obs.Element(id=79, role="button", name="Microsoft Edge – 1 running window", window=2,
                                     bounds=obs.Rect(x=500, y=1040, width=48, height=40)))  # fmt: skip
    clicks: list[str] = []

    class ClickOnly(ObserveOnly):
        def move(self, x: int, y: int, ms: int) -> int:
            clicks.append(f"move {x},{y}")
            return 0

        def click(self) -> int:
            clicks.append("click")
            return 0

    agent = ClickOnly([edge, edge, clean_desktop()])
    assert Session(agent, GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize()  # pyright: ignore[reportArgumentType]
    assert clicks == ["move 524,1060", "click"]
    tray = obs.Snapshot(screen_width=1920, screen_height=1080, apps=edge.apps,
                        windows=[_win(obs.Window.FOREGROUND, "Shell_TrayWnd", 0, 1040, 1920, 40),
                                 _win(obs.Window.SHELL, "Progman", 0, 0, 1920, 1080)],
                        elements=[obs.Element(id=15, role="button", name="Notification Chevron")])  # fmt: skip
    tray.windows.append(_win(obs.Window.POPUP, "SysShadow", 1713, 1020, 118, 25, no_tree=True))
    clicks.clear()
    agent = ClickOnly([tray, tray, edge, edge, clean_desktop()])
    assert Session(agent, GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize()  # pyright: ignore[reportArgumentType]
    assert clicks == ["move 1440,360", "click", "move 524,1060", "click"]
    for bounds in (obs.Rect(width=1920, height=1040), obs.Rect()):
        tray.apps.append(obs.Window(title="Outlook", process_name="olk.exe", bounds=bounds))
        clicks.clear()
        with pytest.raises(BaseException, match="chord"):
            Session(ClickOnly([tray]), GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize(rounds=1)  # pyright: ignore[reportArgumentType]
        assert clicks == []
        tray.apps.pop()


def test_a_hidden_task_window_is_offered_its_taskbar_button_only_from_the_desktop() -> None:
    import random

    from ludius.cli import DATA
    from ludius.options import view
    from ludius.policy import Policy
    from ludius.tasks import Task, TaskInstance, load_template

    snap = clean_desktop()
    snap.apps.append(obs.Window(title="Harbourline intranet - Microsoft\u200b Edge", process_name="msedge.exe",
                                bounds=obs.Rect(x=-32000, y=-32000, width=160, height=28)))  # fmt: skip
    snap.elements.extend([
        obs.Element(id=79, role="button", name="Microsoft Edge – 1 running window", window=2),
        obs.Element(id=80, role="button", name="File Explorer", window=2),
    ])  # fmt: skip
    tpl = load_template(DATA / "templates" / "chat_read_messages.yaml")
    task = Task(tpl, TaskInstance(template=tpl.task, params={"to": "a@b", "to_name": "A"}), random.Random(0))
    close = task.subgoal("close_edge")
    assert [e.id for e in view(snap, task, close, Policy()).allowed] == [79]
    start = obs.Snapshot()
    start.CopyFrom(snap)
    start.windows[2].class_name = "Windows.UI.Core.CoreWindow"
    assert view(start, task, close, Policy()).allowed == []
    snap.windows[0].CopyFrom(obs.Window(kind=obs.Window.FOREGROUND, title="Chat - Microsoft\u200b Edge",
                                        process_name="msedge.exe"))  # fmt: skip
    assert 79 not in [e.id for e in view(snap, task, close, Policy()).allowed]


def test_a_kept_app_may_stay_in_front(tmp_path: Path) -> None:
    chat = clean_desktop()
    edge = obs.Window(kind=obs.Window.FOREGROUND, class_name="Chrome_WidgetWin_1", process_name="msedge.exe",
                      title="Mattermost - Edge", bounds=obs.Rect(width=1920, height=1040))  # fmt: skip
    chat.windows[0].CopyFrom(edge)
    chat.apps.append(edge)
    assert Session(ObserveOnly([chat]), GroundTruth(tmp_path / "gt.jsonl"), "pw").normalize({"msedge.exe"})  # pyright: ignore[reportArgumentType]
