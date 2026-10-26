from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from ludius.cli import load_templates
from ludius.planner import PlanItem, validate
from ludius.policy import Policy
from ludius.v1 import observation_pb2 as obs

SCENARIO = r"""
reserved:
  - path: 'C:\Users\aberg\Documents\Q3 invoice*.xlsm'
  - path: 'C:\Users\*\Documents\handover'
  - path: 'C:\Users\aberg\Documents\budget.xlsx'
    red_herring: true
mailboxes: [cfo@harbourline.example]
"""


def _policy(tmp_path: Path) -> Policy:
    f = tmp_path / "scenario.yaml"
    f.write_text(SCENARIO, encoding="utf-8")
    return Policy.load(f)


def el(name: str, value: str = "", role: str = "list item") -> obs.Element:
    return obs.Element(id=1, key="k", role=role, name=name, value=value)


def test_reserved_files_folders_and_mailboxes_are_refused(tmp_path: Path) -> None:
    p = _policy(tmp_path)
    assert p.element_allowed(el("Q3 invoice final.xlsm"), []) == "scenario-reserved path"
    assert p.element_allowed(el("handover"), []) == "scenario-reserved path"
    assert p.element_allowed(el("budget.xlsx"), []) is None  # intended red herring
    assert p.element_allowed(el("Q3 notes.txt"), []) is None
    address = el("Address", r"C:\Users\aberg\Documents\Q3 invoice 2.xlsm", role="edit")
    assert p.element_allowed(address, []) == "scenario-reserved path"
    assert p.text_allowed(r"c:\users\LNORD\documents\handover\list.txt", None) == "scenario-reserved path"
    assert p.text_allowed(r"C:\Users\a\b\Documents\handover", None) is None  # * stays in one folder level
    assert p.text_allowed("Q3 invoice v2.xlsm", None) == "scenario-reserved path"
    assert p.text_allowed("mail cfo@harbourline.example today", None) == "scenario-reserved path"
    assert p.text_allowed("q3 invoice notes", None) is None
    # The planner rejects plan content that would touch a reserved item.
    item = PlanItem(at="09:00", template="explorer_new_folder", params={"folder_name": "handover"})
    assert "scenario-reserved" in validate([item], load_templates(), [], p)[0][1]


def test_scenario_file_is_strict_and_default_policy_is_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text("reserved:\n  - pth: x\n", encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        Policy.load(f)
    assert Policy().element_allowed(el("Q3 invoice final.xlsm"), []) is None
