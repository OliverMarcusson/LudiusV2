from __future__ import annotations

import random

import pytest

from ludius.cli import load_templates
from ludius.tasks import Task, TaskInstance


def test_mistake_variants_type_a_typo_then_fix_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDIUS_SHARE_HOST", "share-host")
    templates = load_templates()
    note = TaskInstance(template="notepad_write_note", params={"file_name": "Lund q3", "body": "a\nb"}, variant=3)
    t = Task(templates["notepad_write_note"], note, random.Random(0))
    assert t.subgoal_names[3:5] == ["name_file_typo", "fix_file_name"]
    assert t.values["file_name_typo"] == "Lund q33"
    assert "body_typo" not in t.values
    folder = TaskInstance(template="explorer_new_folder", params={"folder_name": "Budget"}, variant=2)
    t = Task(templates["explorer_new_folder"], folder, random.Random(0))
    assert t.values["folder_name_typo"] == "Budgety"
    assert "rename_folder" in t.subgoal_names
    for tpl in templates.values():
        Task(tpl, TaskInstance(template=tpl.task, params=tpl.example), random.Random(1))
