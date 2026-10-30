from __future__ import annotations

import json
from pathlib import Path

import pytest

from ludius import artifacts

DYN = "datetime,timestamp_desc,source,source_long,message,parser,display_name,tag\n"
L2T = "date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,inode,notes,format,extra\n"
T = "2026-10-27T08:{:02d}:00.000000+00:00"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_coverage_volume_and_unmapped(tmp_path: Path) -> None:
    after = _write(tmp_path / "after.csv", DYN + "\n".join([
        f"{T.format(10)},Executed,REG,UserAssist,'-run notepad,winreg/userassist,NTFS:\\Users\\aberg\\NTUSER.DAT,-",
        f"{T.format(11)},Creation Time,LNK,Windows Shortcut,note,lnk,NTFS:\\Users\\Aberg\\AppData\\Recent\\n.lnk,-",
        f"{T.format(12)},Executed,PE,Prefetch,NOTEPAD.EXE,prefetch,NTFS:\\Windows\\Prefetch\\NOTEPAD.EXE-1.pf,-",
        f"{T.format(13)},Creation Time,LNK,Windows Shortcut,x,lnk,NTFS:\\Users\\bob\\Desktop\\x.lnk,-",
        f"{T.format(14)},Modified,FILE,File stat,y,filestat,NTFS:\\Windows\\System32\\y.dll,-",
        "0000-00-00T00:00:00.000000+00:00,Not a time,LNK,Windows Shortcut,z,lnk,NTFS:\\Users\\aberg\\z.lnk,-",
        f"{T.format(59)},Creation Time,LNK,Windows Shortcut,late,lnk,NTFS:\\Users\\aberg\\late.lnk,-",
    ]))  # fmt: skip
    t0 = 1_793_088_000_000
    log = _write(tmp_path / "day.jsonl", "\n".join(json.dumps(r) for r in [
        {"event": "day_start", "host_ms": t0, "persona": "aberg"},
        {"event": "day_end", "host_ms": t0 + 30 * 60_000, "persona": "aberg"},
    ]))  # fmt: skip
    cov = artifacts.report(after, ["aberg", "lnord"], [log])["coverage"]
    assert cov["personas"]["aberg"]["types"] == {"userassist": 1, "lnk": 3}
    assert "shellbags" in cov["personas"]["aberg"]["missing"]
    assert cov["personas"]["lnord"]["types"] == {}
    assert cov["host_types"] == {"unknown host": {"prefetch": 1}}
    assert cov["unmapped_accounts"] == {"bob": 1}
    assert cov["unattributed_rows"] == 1 and cov["untimed_rows"] == 1
    assert cov["volume"]["aberg"] == {"events": 2, "hours": 0.5, "events_per_hour": 4.0}
    assert cov["volume"]["lnord"]["events_per_hour"] is None


def test_l2tcsv_layout_and_survival(tmp_path: Path) -> None:
    row = "10/27/2026,09:00:00,UTC,M...,REG,Registry Key,Last Written,{user},HOST1,s,{desc},2,{file},-,-,{fmt},-"
    before = _write(tmp_path / "before.csv", L2T + "\n".join([
        row.format(user="-", desc="Run key evil.exe", file="NTFS:\\Windows\\config\\SOFTWARE", fmt="winreg/run"),
        row.format(user="-", desc="EVIL.EXE run", file="NTFS:\\Windows\\Prefetch\\EVIL.EXE-1.pf", fmt="prefetch"),
    ]))  # fmt: skip
    after = _write(tmp_path / "after.csv", L2T + "\n".join([
        row.format(user="CORP\\aberg", desc="bags", file="NTFS:\\x", fmt="winreg/bagmru"),
        row.format(user="-", desc="EVIL.EXE run", file="NTFS:\\Windows\\Prefetch\\EVIL.EXE-1.pf", fmt="prefetch"),
        row.format(user="-", desc="dropper.ps1", file="NTFS:\\Users\\aberg\\dropper.ps1", fmt="filestat"),
    ]))  # fmt: skip
    rows = artifacts.read_timeline(after)
    assert rows[0]["time"] is not None and artifacts.account(rows[0]) == "aberg"
    assert artifacts.classify(rows[0]) == ("user", "shellbags")
    manifest = _write(tmp_path / "m.json", json.dumps([
        {"id": "run-key", "match": {"message": "evil\\.exe", "parser": "winreg/run"}},
        {"id": "prefetch", "match": {"display_name": "EVIL\\.EXE-.*\\.pf"}},
        {"id": "dropper", "match": {"display_name": "dropper\\.ps1"}},
        {"id": "vague"},
        {"id": "typo", "match": {"path": "x"}},
    ]))  # fmt: skip
    s = artifacts.report(after, ["aberg"], [], manifest, before)["survival"]
    status = {a["id"]: a["status"] for a in s["artifacts"]}
    assert status == {"run-key": "lost", "prefetch": "survived", "dropper": "unknown", "vague": "unknown",
                      "typo": "unknown"}  # fmt: skip
    assert s["pass"] is False
    alone = artifacts.report(after, ["aberg"], [], manifest)["survival"]
    assert {a["id"]: a["status"] for a in alone["artifacts"]}["dropper"] == "present"
    assert artifacts.report(after, ["aberg"], [])["survival"]["pass"] is None


def test_unsupported_header_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported header"):
        artifacts.read_timeline(_write(tmp_path / "x.csv", "time,what\n1,2\n"))
