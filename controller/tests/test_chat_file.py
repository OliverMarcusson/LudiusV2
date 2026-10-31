from __future__ import annotations

import hashlib
import random
import subprocess
from typing import Any

import pytest

from ludius import cli, react
from ludius.tasks import Task, TaskInstance, _chat_file_ok, _chat_post_ok  # pyright: ignore[reportPrivateUsage]

DATA = b"accruals"
SHA = hashlib.sha256(DATA).hexdigest()


def chat_task(to: str = "mvellinga@ludius.internal") -> Task:
    t = cli.load_templates()["chat_share_file"]
    params = {"to": to, "to_name": "Marcus Vellinga", "file_name": "q4 accruals.xlsx", "message": "here it is"}
    task = Task(t, TaskInstance(template=t.task, params=params, variant=0), random.Random(0))
    task.owner, task.started_ms = "aberg", 1000
    task.sources_before["Documents\\q4 accruals.xlsx"] = DATA
    return task


def test_template_attaches_the_documents_file() -> None:
    assert cli.load_templates()["chat_share_file"].attaches == ["Documents\\{file_name}"]


def test_chat_file_needs_exactly_one_new_upload_with_the_same_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    for got, ok in [([SHA], True), ([], False), ([SHA, SHA], False), (["0" * 64], False), (None, False)]:

        def fake(*a: Any, got: list[str] | None = got) -> list[str] | None:
            calls.append(a)
            return got

        monkeypatch.setattr(react, "shared", fake)
        task = chat_task()
        assert _chat_file_ok(task.template.subgoals["send_message"].done.chat_file, task) is ok  # type: ignore[union-attr, arg-type]
    assert calls[0] == ("aberg", "mvellinga", "q4 accruals.xlsx", 1000, True)
    monkeypatch.setattr(react, "shared", lambda *a: calls.append(a) or [SHA])
    task = chat_task()
    assert _chat_file_ok(task.template.subgoals["upload_file"].done.chat_file, task)  # type: ignore[union-attr, arg-type]
    assert calls[-1][-1] is False
    monkeypatch.setattr(react, "shared", lambda *_: [SHA])
    task = chat_task("mvellinga@example.com")
    assert not _chat_file_ok(task.template.subgoals["send_message"].done.chat_file, task)  # type: ignore[union-attr, arg-type]
    task = chat_task()
    task.sources_before.clear()
    assert not _chat_file_ok(task.template.subgoals["send_message"].done.chat_file, task)  # type: ignore[union-attr, arg-type]


def test_shared_hashes_the_stored_uploads_and_refuses_odd_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_ssh(remote: str, stdin: str, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
        sent.append(remote)
        return subprocess.CompletedProcess([], 0, f"{SHA}  /opt/mattermost/data/x\n".encode())

    monkeypatch.setattr(react, "_ssh", fake_ssh)
    path = "20261030/teams/noteam/channels/c/users/u/f/q4 accruals.xlsx"
    monkeypatch.setattr(react, "_query", lambda *_, **__: [path])
    assert react.shared("aberg", "mvellinga", "q4 accruals.xlsx", 1) == [SHA]
    assert sent[0].startswith("sudo sha256sum -- '/opt/mattermost/data/20261030/")
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(react, "_query", lambda _, **kw: seen.append(kw) or [])
    react.shared("aberg", "mvellinga", "q4 accruals.xlsx", 1, posted=False)
    assert seen[0]["state"] == "staged"
    monkeypatch.setattr(react, "_query", lambda *_, **__: ["a/../../etc/shadow"])
    assert react.shared("aberg", "mvellinga", "q4 accruals.xlsx", 1) is None
    monkeypatch.setattr(react, "_query", lambda *_, **__: [])
    assert react.shared("aberg", "mvellinga", "q4 accruals.xlsx", 1) == []
    monkeypatch.setattr(react, "_query", lambda *_, **__: None)
    assert react.shared("aberg", "mvellinga", "q4 accruals.xlsx", 1) is None


def test_chat_post_needs_the_message_posted_since_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    for got, ok in [(1, True), (2, True), (0, False), (None, False)]:
        monkeypatch.setattr(react, "posted", lambda *a, got=got: calls.append(a) or got)
        for name, to in [("chat_post_message", "~Coffee Corner"), ("chat_direct_message", "mvellinga")]:
            t = cli.load_templates()[name]
            params = {"channel": "Coffee Corner", "to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga",
                      "message": " soup day "}  # fmt: skip
            task = Task(t, TaskInstance(template=name, params=params, variant=0), random.Random(0))
            task.owner, task.started_ms = "aberg", 1000
            assert _chat_post_ok(t.subgoals["send_message"].done.chat_post, task) is ok  # type: ignore[union-attr, arg-type]
            assert calls[-1] == ("aberg", to, " soup day ", 1000)
    task = Task(t, TaskInstance(template=t.task, params={**params, "to": "mvellinga@example.com"}, variant=0),
                random.Random(0))  # fmt: skip
    task.owner = "aberg"
    assert not _chat_post_ok(t.subgoals["send_message"].done.chat_post, task)  # type: ignore[union-attr, arg-type]


def test_a_name_is_never_typed_into_the_field_of_another_step() -> None:
    from ludius.options import build_options, field_of_another_step, view
    from ludius.policy import Policy
    from ludius.v1 import observation_pb2 as obs

    t = cli.load_templates()["chat_direct_message"]
    params = {"to": "mvellinga@ludius.internal", "to_name": "Marcus Vellinga", "message": "soup day"}
    task = Task(t, TaskInstance(template=t.task, params=params, variant=0), random.Random(0))
    box = obs.Rect(x=10, y=10, width=50, height=9)
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="msedge.exe", title="Town Square - Mattermost")
    for name, typed in [("Address and search bar", False), ("quick switch input", True)]:
        e = obs.Element(id=1, key="k1", role="edit", name=name, enabled=True, bounds=box, window=0, focused=True)
        snap = obs.Snapshot(windows=[fg], elements=[e])
        sg = task.subgoal("open_conversation")
        opts = build_options(view(snap, task, sg, Policy()), task, sg, Policy())
        assert any(o.kind == "type" for o in opts) is typed, name
        assert field_of_another_step(e, task, task.subgoal("open_chat")) is False


def test_start_search_offers_no_store_or_web_result() -> None:
    from ludius.options import build_options, view
    from ludius.policy import Policy
    from ludius.v1 import observation_pb2 as obs

    t = cli.load_templates()["outlook_read_mail"]
    params = {"sender": "x@ludius.internal", "sender_name": "X", "subject": "Hi"}
    task = Task(t, TaskInstance(template=t.task, params=params, variant=0), random.Random(0))
    fg = obs.Window(kind=obs.Window.FOREGROUND, process_name="SearchApp.exe", title="Search")
    names = ["Outlook (classic), App", "Microsoft Store, App", "outlook - See web results"]
    els = [
        obs.Element(
            id=i + 1,
            key=f"k{i}",
            role="list item",
            name=n,
            enabled=True,
            bounds=obs.Rect(x=10, y=10 + 20 * i, width=50, height=9),
            window=0,
        )
        for i, n in enumerate(names)
    ]
    snap = obs.Snapshot(windows=[fg], elements=els)
    sg = task.subgoal("open_outlook")
    clicked = {
        o.element.name for o in build_options(view(snap, task, sg, Policy()), task, sg, Policy()) if o.kind == "click"
    }
    assert clicked == {"Outlook (classic), App"}


def test_a_file_shown_without_its_extension_can_be_selected_and_uploaded() -> None:
    from ludius.tasks import check_done, preconditions
    from ludius.v1 import observation_pb2 as obs

    task = chat_task()
    task.values["persona_name"] = "Anna Berg"
    item = obs.Element(id=1, role="list item", name="Q4 Accruals", selected=True)
    other = obs.Element(id=2, role="list item", name="q4 accruals old", selected=True)
    snap = obs.Snapshot(windows=[obs.Window(title="Open", process_name="msedge.exe", kind=obs.Window.FOREGROUND)])
    crumbs = [obs.Element(role="split button", name=n) for n in ("All locations", "This PC", "Documents")]
    snap.elements.extend([*crumbs, obs.Element(role="button", name="Refresh"), item])
    assert check_done(task.template.subgoals["select_file"].done, snap, task, False)
    upload = task.template.subgoals["upload_file"]
    assert preconditions(upload, snap, task) is None
    snap.elements.extend([other])
    assert "must not match" in (preconditions(upload, snap, task) or "")
