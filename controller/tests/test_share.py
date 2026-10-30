from __future__ import annotations

import random
from typing import Any

import pytest
from test_edit import it

from ludius import cli, guestfile, tasks
from ludius.planner import Narrative, role_templates, validate
from ludius.policy import Policy
from ludius.tasks import Persona, Task, TaskInstance, check_done, file_baseline
from ludius.v1 import observation_pb2 as obs

OWNED = {"documents\\old draft.txt": "2026-10-29"}


def test_only_an_own_file_to_an_own_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDIUS_APPS")
    monkeypatch.setenv("LUDIUS_SHARE_HOST", "files")
    templates = cli.plan_sources().templates

    def why(file_name: str = "Old Draft", share: str = "Finance", owned: dict[str, str] = OWNED) -> str:
        item = it("11:00", "explorer_copy_to_share", file_name=file_name, file_ext="txt", share=share)
        r = validate([item], templates, [], Policy(), owned=owned, day="2026-11-02", shares=["Finance"])
        return r[0][1] if r else ""

    assert why() == ""
    assert "own files" in why(file_name="someone elses")
    assert "own files" in why(owned={})
    assert "team shares" in why(share="HR")
    monkeypatch.delenv("LUDIUS_SHARE_HOST")
    assert "explorer_copy_to_share" not in role_templates(templates, Persona(name="A B", user="ab"))


def test_narrative_shares_are_plain_names_of_people() -> None:
    person = {"name": "A B", "user": "ab", "role": "r", "department": "d"}
    base = {"company": "c", "city": "c", "summary": "s", "people": [person], "projects": [], "events": []}
    assert Narrative.model_validate(base | {"shares": {"Finance": ["ab"]}}).shares == {"Finance": ["ab"]}
    bads: list[dict[str, list[str]]] = [{"Fin Ops": ["ab"]}, {"Finance": ["zz"]}, {"Finance": []}, {"SYSVOL": ["ab"]}]
    for bad in [*bads, {"C$": ["ab"]}]:
        with pytest.raises(ValueError):
            Narrative.model_validate(base | {"shares": bad})


@pytest.fixture
def task(monkeypatch: pytest.MonkeyPatch) -> tuple[Task, dict[str, Any]]:
    disk: dict[str, Any] = {}
    monkeypatch.setenv("LUDIUS_SHARE_HOST", "files")
    monkeypatch.setattr(tasks.guestfile, "read", lambda user, rel: disk.get("file"))  # pyright: ignore
    monkeypatch.setattr(tasks.share, "read", lambda share, name: disk.get(f"{share}/{name}"))  # pyright: ignore
    t = cli.plan_sources().templates["explorer_copy_to_share"]
    params = {"file_name": "old draft", "file_ext": "txt", "share": "Finance"}
    tk = Task(t, TaskInstance(template=t.task, params=params), random.Random(0))
    tk.owner, tk.started_ms = "aberg", 1_000_000
    return tk, disk


def test_the_copy_is_new_and_holds_the_own_file(task: tuple[Task, dict[str, Any]]) -> None:
    tk, disk = task
    assert tk.values["share_path"] == "\\\\files\\Finance"
    done, snap, key = tk.template.subgoals["paste_keys"].done, obs.Snapshot(), "Finance/old draft.txt"
    disk["file"] = guestfile.GuestFile(True, 500, b"draft")
    disk[key] = guestfile.GuestFile(True, 0, b"draft")
    assert check_done(done, snap, tk, False) is False
    assert file_baseline(tk, Policy()) == "\\\\files\\Finance\\old draft.txt: already exists, never overwritten"
    disk[key] = None
    assert file_baseline(tk, Policy()) == "\\\\files\\Finance\\old draft.txt: unreadable"
    disk["file"], disk[key] = guestfile.GuestFile(False), guestfile.GuestFile(False)
    assert file_baseline(tk, Policy()) == "Documents\\old draft.txt: not there, nothing to copy"
    disk["file"] = guestfile.GuestFile(True, 500, b"draft")
    assert file_baseline(tk, Policy()) is None
    assert check_done(done, snap, tk, False) is False
    for wrong in (None, guestfile.GuestFile(True, 0, b"other")):
        disk[key] = wrong
        assert check_done(done, snap, tk, False) is False
    disk[key] = guestfile.GuestFile(True, 0, b"draft")
    assert check_done(done, snap, tk, False) is True


def test_a_reserved_file_or_copy_is_never_read(
    task: tuple[Task, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tk, disk = task
    disk["file"] = disk["Finance/old draft.txt"] = guestfile.GuestFile(False)
    policy = Policy()
    monkeypatch.setattr(policy, "path_reserved", lambda p: "Finance" in str(p))  # pyright: ignore
    assert "scenario-reserved" in (file_baseline(tk, policy) or "")


def test_copy_needs_a_press_and_both_keys_a_decider_yes(task: tuple[Task, dict[str, Any]]) -> None:
    tk, _ = task
    copy, paste = tk.template.subgoals["copy_keys"], tk.template.subgoals["paste_keys"]
    assert check_done(copy.done, obs.Snapshot(), tk, False) is False
    assert check_done(copy.done, obs.Snapshot(), tk, False, pressed_ok=True) is True

    assert paste.confirm and "{share_path}" in paste.confirm and copy.confirm
