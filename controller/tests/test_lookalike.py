from __future__ import annotations

from ludius.cli import DATA, load_persona, load_templates
from ludius.planner import PlanItem, Scenario, role_templates, validate
from ludius.policy import Policy
from ludius.tasks import Template


def test_lookalike_work_follows_risk_profile_and_avoids_scenario() -> None:
    base = load_templates()["explorer_new_folder"]
    ps = Template.model_validate({**base.model_dump(), "task": "powershell_admin_check", "resembles": "powershell"})
    templates = {**load_templates(), ps.task: ps}
    admin, finance = load_persona(str(DATA / "personas" / "lnord.yaml")), load_persona(None)
    assert ps.task in role_templates(templates, admin) and ps.task not in role_templates(templates, finance)

    s = Scenario.parse("2026-10-28T13:00-14:00:powershell")
    assert (s.start, s.end, s.signal) == ("13:00", "14:00", "powershell")
    near = PlanItem(at="12:15", template=ps.task, params={"folder_name": "2026-10-28 patch list"})
    inside = PlanItem(at="13:30", template=ps.task, params={"folder_name": "2026-10-28 service audit"})
    for_admin = role_templates(templates, admin)
    assert validate([near], for_admin, [], Policy(), scenario=s) == []
    assert "overlap the scenario" in validate([inside], for_admin, [], Policy(), scenario=s)[0][1]
    assert "unknown template" in validate([near], role_templates(templates, finance), [], Policy())[0][1]
