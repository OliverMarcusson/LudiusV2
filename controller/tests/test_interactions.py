import json
import time
from pathlib import Path
from typing import Any

import yaml


def test_interactions_only_from_verified_effects_between_personas(tmp_path: Path) -> None:
    from ludius import interactions

    days = tmp_path / "days"
    days.mkdir()
    reqs = tmp_path / "world" / "requests"
    reqs.mkdir(parents=True)
    now = int(time.time() * 1000)
    out = tmp_path / "monitor-interactions.jsonl"
    users = ["aberg", "sdekker", "lnord"]

    def send(to: str, status: str, ms: int, subject: str = "Board delivery numbers") -> list[dict[str, Any]]:
        params = {"to": to, "subject": subject, "body": "typed body text"}
        return [{"event": "item_start", "template": "outlook_send_mail", "host_ms": ms},
                {"event": "item_end", "template": "outlook_send_mail", "logical": "2026-11-02T10:00", "host_ms": ms,
                 "params": params, "status": status}]  # fmt: skip

    day = [
        *send("sdekker@ludius.internal", "done", now),  # persona -> persona, verified: one interaction
        *send("mvellinga@ludius.internal", "done", now + 1),  # a narrative colleague, no VM: none
        *send("lnord@ludius.internal", "abandoned", now + 2),  # planned, never proven delivered: none
        *send("aberg@ludius.internal", "done", now + 3),  # to itself: none
        *send("sdekker@other.example", "done", now + 4),  # another domain: none
    ]
    log = days / "2026-11-02-aberg.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in day), encoding="utf-8")
    t = interactions.Tracker(days, users, out, requests_dir=reqs)
    new = t.step()
    assert [(i.personas, i.kind, i.label, i.logical) for i in new] == [
        (["aberg", "sdekker"], "mail", "Board delivery numbers", "2026-11-02T10:00")
    ]
    assert "typed body text" not in out.read_text(encoding="utf-8")
    assert t.step() == []  # nothing new
    assert interactions.Tracker(days, users, out, requests_dir=reqs).step() == []  # a restart repeats nothing

    # A manager's request counts once it is recorded as sent, persona to persona; its fulfilment counts too.
    msgid = "<1.aberg@ludius.internal>"
    request = {"to": "sdekker", "task": "excel_new_workbook", "subject": "October accruals workbook",
               "manager": "aberg", "msgid": msgid, "status": "planned"}  # fmt: skip
    (reqs / "2026-11-02-aberg.yaml").write_text(yaml.safe_dump({"manager": "aberg", "date": "2026-11-02",
                                                                "requests": [request]}), encoding="utf-8")  # fmt: skip
    assert t.step() == []  # planned, not sent
    (reqs / "2026-11-02-aberg.yaml").write_text(yaml.safe_dump({"manager": "aberg", "date": "2026-11-02",
                                                                "requests": [request | {"status": "sent"}]}),
                                                encoding="utf-8")  # fmt: skip
    check = {"event": "request_check", "host_ms": now + 9, "request": msgid, "fulfilled": True}
    (days / "2026-11-02-sdekker.jsonl").write_text(json.dumps(check) + "\n", encoding="utf-8")
    got = sorted((i.kind, i.personas) for i in t.step())
    assert got == [("request", ["aberg", "sdekker"]), ("request_done", ["sdekker", "aberg"])]
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3
