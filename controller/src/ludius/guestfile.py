from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

import grpc

from ludius.hostagent import HostAgent

MAX_BYTES = 1_000_000
NOISE_FOLDERS = ("Documents", "Pictures", "Downloads")

_PS = r"""
$ErrorActionPreference = 'Stop'
$secret = ConvertTo-SecureString $env:LUDIUS_ADMIN_PASSWORD -AsPlainText -Force
$admin = [pscredential]::new($env:LUDIUS_ADMIN_USER, $secret)
$arg = $env:LUDIUS_GUEST_PATH, $env:LUDIUS_GUEST_MAX
Invoke-Command -VMName $env:LUDIUS_VM -Credential $admin -ArgumentList $arg -ScriptBlock {
    param($p, $max)
    $ErrorActionPreference = 'Stop'
    if (-not (Test-Path -LiteralPath (Split-Path -LiteralPath $p) -PathType Container)) { throw 'no such folder' }
    try { $i = Get-Item -LiteralPath $p -Force }
    catch [System.Management.Automation.ItemNotFoundException] { '{"exists":false}'; return }
    if ($i.PSIsContainer -or $i.Length -gt [int]$max) { throw 'not a file, or too large' }
    $s = [IO.File]::Open($p, 'Open', 'Read', 'ReadWrite, Delete')
    try { $m = [IO.MemoryStream]::new(); $s.CopyTo($m) } finally { $s.Dispose() }
    @{ exists = $true; written = [DateTimeOffset]::new($i.LastWriteTimeUtc).ToUnixTimeMilliseconds()
       data = [Convert]::ToBase64String($m.ToArray()) } | ConvertTo-Json -Compress
}
"""


@dataclass(frozen=True)
class GuestFile:
    exists: bool
    written_ms: int = 0
    data: bytes = b""


def profile_path(user: str, rel: str) -> str | None:
    parts = re.split(r"[\\/]", rel)
    if not re.fullmatch(r"[a-z0-9]+", user) or len(parts) < 2 or parts[0] not in NOISE_FOLDERS:
        return None
    if any(p in ("", ".", "..") or re.search(r'[<>:"|?*]', p) for p in parts):
        return None
    return "C:\\Users\\" + user + "\\" + "\\".join(parts)


def read(user: str, rel: str) -> GuestFile | None:
    path = profile_path(user, rel)
    vm = os.environ.get("LUDIUS_VM")
    if path is None or not vm:
        return None
    agent = HostAgent(os.environ.get("LUDIUS_AGENT", "127.0.0.1:7451"), vm)
    try:
        f = agent.read_file(path, MAX_BYTES)
    except grpc.RpcError as e:
        code = e.code()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        if code == grpc.StatusCode.UNIMPLEMENTED and agent.local_hyperv():
            return _read_via_powershell(path)
        return None
    finally:
        agent.close()
    if not f.exists:
        return GuestFile(False)
    if len(f.data) > MAX_BYTES:
        return None
    return GuestFile(True, f.written_ms, f.data)


def _read_via_powershell(path: str) -> GuestFile | None:
    if not all(os.environ.get(k) for k in ("LUDIUS_VM", "LUDIUS_ADMIN_USER", "LUDIUS_ADMIN_PASSWORD")):
        return None
    env = os.environ | {"LUDIUS_GUEST_PATH": path, "LUDIUS_GUEST_MAX": str(MAX_BYTES)}
    try:
        out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", _PS], env=env,
                             capture_output=True, text=True, timeout=60)  # fmt: skip
        if out.returncode:
            return None
        r = json.loads(out.stdout.strip().splitlines()[-1])
        if r.get("exists") is False:
            return GuestFile(False)
        return GuestFile(True, int(r["written"]), base64.b64decode(r["data"], validate=True))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        return None


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_SHEET = re.compile(r"xl/worksheets/sheet\d+\.xml")
_REF = re.compile(r"([A-Z]{1,3})(\d+)")
MAX_PART = 5_000_000


def xlsx_cells(data: bytes) -> dict[str, str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = {i.filename: i for i in z.infolist()}
            sheets = [n for n in parts if _SHEET.fullmatch(n)]
            if len(sheets) != 1 or any(i.file_size > MAX_PART for i in parts.values()):
                return None
            shared: list[str] = []
            if "xl/sharedStrings.xml" in parts:
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")) for si in root.findall("m:si", _NS)
                ]
            cells: dict[str, str] = {}
            for c in ET.fromstring(z.read(sheets[0])).iterfind(".//m:sheetData/m:row/m:c", _NS):
                kind, v = c.get("t"), c.find("m:v", _NS)
                if c.find("m:f", _NS) is not None:
                    return None
                if kind == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
                elif v is None:
                    continue
                elif kind == "b":
                    text = {"1": "TRUE", "0": "FALSE"}[v.text or ""]
                else:
                    text = shared[int(v.text or "")] if kind == "s" else v.text or ""
                if text != "":
                    cells[c.get("r", "")] = text
            return cells
    except (zipfile.BadZipFile, ET.ParseError, ValueError, IndexError, KeyError, OSError):
        return None


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _parts(z: zipfile.ZipFile) -> set[str] | None:
    infos = z.infolist()
    return None if any(i.file_size > MAX_PART for i in infos) else {i.filename for i in infos}


def _runs(p: ET.Element, t: str, tab: str) -> str:
    return "".join(e.text or "" if e.tag == t else "\t" for e in p.iter() if e.tag in (t, tab))


def docx_paragraphs(data: bytes) -> list[str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = _parts(z)
            if parts is None or "word/document.xml" not in parts:
                return None
            body = ET.fromstring(z.read("word/document.xml")).find(f"{_W}body")
            if body is None:
                return None
            texts = (_runs(p, f"{_W}t", f"{_W}tab") for p in body.iter(f"{_W}p"))
            return [t for t in texts if t.strip()]
    except (zipfile.BadZipFile, ET.ParseError, ValueError, KeyError, OSError):
        return None


def pptx_slides(data: bytes) -> list[list[str]] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = _parts(z)
            if parts is None or not {"ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"} <= parts:
                return None
            rels = {r.get("Id"): r.get("Target", "") for r in ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
                    .iter(f"{_PKG_REL}Relationship")}  # fmt: skip
            order = ET.fromstring(z.read("ppt/presentation.xml")).iter(f"{_P}sldId")
            slides: list[list[str]] = []
            for sid in order:
                target = rels[sid.get(f"{_REL}id")].lstrip("/")
                name = target if target.startswith("ppt/") else "ppt/" + target
                title: list[str] = []
                rest: list[str] = []
                for sp in ET.fromstring(z.read(name)).iter(f"{_P}sp"):
                    ph = sp.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
                    texts = [t for p in sp.iter(f"{_A}p") if (t := _runs(p, f"{_A}t", f"{_A}tab")).strip()]
                    (title if ph is not None and ph.get("type") in ("title", "ctrTitle") else rest).extend(texts)
                slides.append(title + rest)
            return slides
    except (zipfile.BadZipFile, ET.ParseError, ValueError, KeyError, OSError):
        return None


def grid_cells(rows: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for r, line in enumerate(rows.strip("\n").split("\n"), start=1):
        for c, cell in enumerate(line.split("\t")):
            if cell != "":
                out[f"{column(c)}{r}"] = cell
    return out


def column(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s
