"""Manager requests (ludius.requests): org chart, request validation and idempotent server-side sending, the persona's
Jev mapping (fail closed), and the outcome check against the request (offline)."""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path
from typing import Any

import pytest

from ludius import cli, guestfile, mail, requests
from ludius.jev import Decision
from ludius.planner import Narrative, Person, PlanItem, World
from ludius.policy import Policy
from ludius.tasks import Persona, Task

ANNA = Persona(name="Anna Berg", user="aberg", role="Finance controller", department="Finance")
LARS = Persona(name="Lars Nord", user="lnord", role="IT administrator", department="IT")
STORY = Narrative(company="Harbourline", city="Rotterdam", summary="s", projects=["q4 close"], events=[], people=[
    Person(name="Marcus Vellinga", role="Managing director", department="Executive", user="mvellinga"),
    Person(name="Anna Berg", role="Finance controller", department="Finance", user="aberg", manager="mvellinga"),
    Person(name="Priya Nanda", role="IT administrator", department="IT", user="pnanda", manager="mvellinga"),
    Person(name="Lars Nord", role="IT administrator", department="IT", user="lnord", manager="aberg"),
])  # fmt: skip
BOSS = STORY.people[0]
THU = dt.date(2026, 10, 29)
BODY = 'Hi Anna,\ncould you put the lund accruals in a note "lund accruals q4" with the totals?\nThanks, Marcus'
ASK: dict[str, Any] = {"to": "aberg", "task": "notepad_write_note", "subject": "lund accruals note", "body": BODY,
       "file_name": "lund accruals q4", "include": ["totals"]}  # fmt: skip


class FakeLlm:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers, self.calls = list(answers), 0

    def json(self, cls: Any, system: str, user: str, temperature: float = 0.9, attempts: int = 2) -> Any:
        self.calls += 1
        return cls.model_validate(self.answers.pop(0))


class FakeJev:
    def __init__(self, choice: str, p: float = 0.9) -> None:
        self.choice, self.p = choice, p

    def decide(self, state: dict[str, Any], options: dict[str, str], *, ask_progress: bool = True) -> Decision:
        assert "none" in options and set(options) <= {*requests.requestable(cli.load_templates()), "none"}
        return Decision(self.choice, self.p, {self.choice: self.p}, 0, 0, "jev")


def ensure(tmp_path: Path, llm: FakeLlm, date: dt.date = THU, inbox: list[mail.Message] | None = None) -> Any:
    llm_: Any = llm
    return requests.ensure(llm_, World(tmp_path), STORY, {"aberg": "- nothing"}, [ANNA, LARS], cli.load_templates(),
                           Policy(), date, BOSS, {"aberg": inbox or []}, {"aberg": []})  # fmt: skip


def test_org_chart_managers() -> None:
    assert requests.manager_of(STORY, "aberg", [ANNA, LARS]) == BOSS
    assert "is a persona" in str(requests.manager_of(STORY, "lnord", [ANNA, LARS]))  # has a VM: never sends
    assert "no manager" in str(requests.manager_of(STORY, "mvellinga", [ANNA]))


def test_requests_are_validated_sent_once_and_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(mail, "deliver", lambda *a: sent.append(a) or True)  # type: ignore[func-returns-value]
    bad = [{**ASK, "to": "lnord"}, {**ASK, "body": BODY.replace('"', "")}, {**ASK, "include": ["budget"]},
           {**ASK, "task": "edge_web_lookup"}, {**ASK, "subject": "RE: lund"}]  # fmt: skip
    llm = FakeLlm({"requests": [*bad, ASK, {**ASK, "subject": "second today"}]})
    day = ensure(tmp_path, llm)
    assert [r.subject for r in day.requests] == ["lund accruals note"] and day.requests[0].status == "sent"
    assert len(day.rejects) == 6 and "not a report with requests left" in day.rejects[-1]
    assert sent[0][:5] == ("mvellinga@corp.ludius.test", "Marcus Vellinga", "aberg@corp.ludius.test", "Anna Berg",
                           "lund accruals note")  # fmt: skip
    assert ensure(tmp_path, FakeLlm()).requests == day.requests and len(sent) == 1  # no second call, no resend
    assert ensure(tmp_path, FakeLlm(), dt.date(2026, 10, 31)).rejects == ["no request: weekend"]
    for d in (27, 28):  # two more earlier this week: the weekly budget is used
        (tmp_path / "world" / "requests" / f"2026-10-{d}-mvellinga.yaml").write_text(
            (tmp_path / "world" / "requests" / "2026-10-29-mvellinga.yaml").read_text().replace("10-29", f"10-{d}")
        )
    assert ensure(tmp_path, FakeLlm(), dt.date(2026, 10, 30)).rejects == ["no request: weekly budget used"]
    dup = [mail.Message("x", 0, 0, "", subject="Lund accruals note")]
    assert "already in aberg's Inbox" in ensure(tmp_path, FakeLlm({"requests": [ASK]}), THU + dt.timedelta(7), dup
                                                ).rejects[0]  # fmt: skip
    reserved = requests.problem(requests.Ask(**ASK), {"aberg": 1}, {"aberg": cli.load_templates()}, {}, BOSS,
                                Policy(reserved_paths=["aberg@"]), {"aberg": set()}, {"aberg": []})  # fmt: skip
    assert reserved == "aberg@corp.ludius.test is scenario-reserved"
    assert requests.Ask.model_validate({**ASK, "heading": "x"}).subject == ASK["subject"]  # extra keys dropped


def test_a_recorded_request_is_checked_against_todays_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Sol on 3ccc614: interrupted after recording, then the recipient is reserved: the retry must not send.
    def boom(*_: Any) -> bool:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(mail, "deliver", boom)
    with pytest.raises(RuntimeError):
        ensure(tmp_path, FakeLlm({"requests": [ASK]}))
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(mail, "deliver", lambda *a: sent.append(a) or True)  # type: ignore[func-returns-value]
    llm: Any = FakeLlm()
    day = requests.ensure(llm, World(tmp_path), STORY, {"aberg": "- nothing"}, [ANNA, LARS], cli.load_templates(),
                          Policy(reserved_paths=["aberg@"]), THU, BOSS, {"aberg": []}, {"aberg": []})  # fmt: skip
    assert sent == [] and day.requests[0].status == "rejected" and "scenario-reserved" in day.rejects[-1]


def test_persona_maps_a_request_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "deliver", lambda *a: True)  # type: ignore[misc]
    msgid = ensure(tmp_path, FakeLlm({"requests": [ASK]})).requests[0].msgid
    at = int(dt.datetime(2026, 10, 28, tzinfo=dt.UTC).timestamp() * 1000)
    m = mail.Message("mvellinga@corp.ludius.test", 0, at, BODY, cls="IPM.Note", subject="lund accruals note",
                     msgid=msgid)  # fmt: skip
    seed = mail.Message(m.sender, 0, at, BODY, cls="IPM.Note", subject="seeded", msgid="<s1@corp.ludius.test>")
    w, ts, book = World(tmp_path), cli.load_templates(), {"aberg": "Anna Berg"}
    assert requests.pending(w, "aberg", BOSS, [m, seed], Policy(), THU) == [m]  # only mails sent as requests
    assert requests.pending(w, "aberg", BOSS, [m], Policy(reserved_subjects=["lund accruals note"]), THU) == []
    item = {"at": "09:30", "params": {"file_name": "lund accruals q4", "body": "lund totals\n- 12 400"}, "context": "c"}
    llm = FakeLlm(item)
    items, notes = requests.candidates(llm, FakeJev("notepad_write_note"), w, ANNA, BOSS, [m], ts, THU, book)  # type: ignore[arg-type]
    assert [(i.template, i.request) for i in items] == [("notepad_write_note", msgid)] and not notes
    assert requests.planned(items[0], ts, tmp_path) is None
    own = [
        PlanItem(template="notepad_write_note", at="09:00", params={"file_name": "a", "body": "Totals"}),
        PlanItem(template="notepad_write_note", at="10:00", params={"file_name": "b", "body": "sums"}),
    ]
    assert requests.duplicates(own, items[0], tmp_path) == own[:1]  # the planner wrote the requested note again
    items[0].params["body"] = "lund sums"
    assert requests.planned(items[0], ts, tmp_path) == "content lacks ['totals']"
    items[0].params["file_name"] = "other"
    assert "asked for 'lund accruals q4'" in str(requests.planned(items[0], ts, tmp_path))
    for jev in (FakeJev("none"), FakeJev("notepad_write_note", 0.4)):  # none, or too unsure: nothing, never asked again
        assert requests.candidates(llm, jev, w, ANNA, BOSS, [m], ts, THU, book)[0] == []  # type: ignore[arg-type]
        assert requests.pending(w, "aberg", BOSS, [m], Policy(), THU) == []
        (tmp_path / "world" / "requests" / "seen-aberg.yaml").unlink()
    assert llm.calls == 1


def test_fulfilled_reads_the_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "deliver", lambda *a: True)  # type: ignore[misc]
    msgid = ensure(tmp_path, FakeLlm({"requests": [ASK]})).requests[0].msgid
    ts = cli.load_templates()

    def task(name: str = "lund accruals q4", template: str = "notepad_write_note") -> Task:
        from ludius.tasks import TaskInstance

        values = {"file_name": name, "body": "lund totals", "rows": "a", "heading": "h"}
        t = Task(ts[template], TaskInstance(template=template, params=values, request=msgid), random.Random(0))
        t.owner, t.started_ms, t.files_absent = "aberg", 1000, frozenset({f"Documents\\{name}.txt"})
        return t

    files = {"lund accruals q4.txt": guestfile.GuestFile(True, 2000, b"lund totals: 12 400\r\n")}

    def read(user: str, rel: str) -> guestfile.GuestFile:
        return files.get(rel.split("\\")[-1], guestfile.GuestFile(False))

    monkeypatch.setattr(guestfile, "read", read)
    assert requests.fulfilled(task(), tmp_path) == (True, "matches the request")
    assert "asked for 'lund accruals q4'" in requests.fulfilled(task("other"), tmp_path)[1]
    assert (
        "asked for notepad_write_note, planned excel"
        in requests.fulfilled(task(template="excel_new_workbook"), tmp_path)[1]
    )
    files["lund accruals q4.txt"] = guestfile.GuestFile(True, 2000, b"lund sums")
    assert requests.fulfilled(task(), tmp_path) == (False, "file lacks ['totals']")
    files["lund accruals q4.txt"] = guestfile.GuestFile(True, 500, b"totals")  # written before the task: not ours
    assert requests.fulfilled(task(), tmp_path)[0] is False
    t = task()
    t.files_absent = frozenset()  # not proven new at start
    assert "not proven absent" in requests.fulfilled(t, tmp_path)[1]
    rtf = rb"{\rtf1\ansi{\fonttbl{\f0 Calibri;}}\b lund\b0\par totals\par}"
    assert "totals" in requests._text("x.rtf", rtf) and "fonttbl" not in requests._text("x.rtf", rtf)  # pyright: ignore[reportPrivateUsage]
