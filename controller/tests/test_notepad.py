from __future__ import annotations

import random
from typing import Any

import pytest

from ludius import cli, guestfile, tasks
from ludius.policy import Policy
from ludius.tasks import Task, TaskInstance, check_done, file_baseline
from ludius.v1 import observation_pb2 as obs


def test_note_is_done_only_when_the_saved_file_holds_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    disk: dict[str, Any] = {"file": guestfile.GuestFile(False)}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk["file"])
    t = cli.plan_sources().templates["notepad_write_note"]
    tk = Task(t, TaskInstance(template=t.task, params={"file_name": "desk", "body": "Call Lars\nbook room"}, variant=0),
              random.Random(0))  # fmt: skip
    tk.owner, tk.started_ms = "aberg", 1_000_000
    assert file_baseline(tk, Policy()) is None
    snap = obs.Snapshot()
    snap.windows.add(process_name="notepad.exe", title="desk - Notepad", kind=obs.Window.FOREGROUND)
    done = t.subgoals["confirm_save"].done
    assert check_done(done, snap, tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 2_000_000, b"call lars\r\n\r\nBook room\r\n")
    assert check_done(done, snap, tk, False) is True
    for wrong in (guestfile.GuestFile(True, 999_999, b"Call Lars\nbook room"),
                  guestfile.GuestFile(True, 2_000_000, b"Call Lars"), None):  # fmt: skip
        disk["file"] = wrong
        assert check_done(done, snap, tk, False) is False


def test_pixels_title_alone_does_not_prove_the_note(monkeypatch: pytest.MonkeyPatch) -> None:
    disk: dict[str, Any] = {"file": guestfile.GuestFile(False)}
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk["file"])
    t = cli.plan_sources().templates["notepad_write_note"]
    tk = Task(t, TaskInstance(template=t.task, params={"file_name": "desk", "body": "Call Lars"}, variant=0),
              random.Random(0))  # fmt: skip
    tk.owner, tk.started_ms = "aberg", 1_000_000
    assert file_baseline(tk, Policy()) is None
    done = t.subgoals["confirm_save"].done
    assert done is not None and done.pixels is not None
    snap = obs.Snapshot()
    snap.windows.add(framework_id="visual", process_name="", kind=obs.Window.FOREGROUND)
    snap.elements.add(name="desk - Notepad", window=0)
    assert check_done(done, snap, tk, False) is False
    disk["file"] = guestfile.GuestFile(True, 2_000_000, b"Call Lars\r\n")
    assert check_done(done, snap, tk, False) is True
