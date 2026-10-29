"""The range intranet's pages (dc01 role, intranet.html.j2): one Markdown file per page, `<id>.md`, in a content
folder a range admin can swap per scenario (LUDIUS_INTRANET; Ansible `intranet_dir`, the same folder).

    ---
    title: Month-end close checklist and timetable
    section: Finance
    owner: Group finance
    updated: 2026-09-30
    popular: true
    tags: month end, close, accruals
    files: Close timetable 2026.xlsx, close-checklist.pdf
    ---
    First paragraph.

    Second paragraph.

The header is `key: value` lines only (the page reads it with the same rule, in the browser); tags are comma
separated, popular pages show on the home page and when a search matches nothing; files (comma separated) are
downloads kept in the folder's files/ subfolder, each on some page (edge_download_file fetches one). The body is
plain text paragraphs, shown as text, never as HTML. A scenario's own pages go in the same folder; the scenario
file's `pages:` (policy) keeps noise off them. `ludius intranet check` validates a folder before it is deployed; the
planner gets the page and download index (without the scenario's pages) so a task asks for one that exists.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DIR = Path(os.environ.get("LUDIUS_INTRANET") or Path(__file__).resolve().parents[2] / "data" / "intranet")
FIELDS = {"title", "section", "owner", "updated", "popular", "tags", "files"}
MAX_PAGE = 64 << 10  # bytes per page file
MAX_FILE = 1_000_000  # bytes per download: guestfile.MAX_BYTES, so the download check reads it whole
MAX_FILES = 50 << 20  # all downloads together (copied to dc01 on every apply)
# Documents IIS serves as static files (its default MIME map); nothing that runs when opened, no macros.
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}\.(?:pdf|docx|xlsx|pptx|txt|csv|png|jpg)", re.I)
MAX_PAGES = 200  # the whole folder is embedded in one page
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*:)?//[^\s\"'<>()]+", re.I)  # with a scheme or scheme-relative
RANGE_DOMAIN = "corp.ludius.test"


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    section: str
    owner: str
    updated: str
    popular: bool
    tags: tuple[str, ...]
    body: tuple[str, ...]
    files: tuple[str, ...] = ()


def parse(id: str, text: str) -> Page:
    """One page file; ValueError names what is wrong."""
    if not _ID.fullmatch(id):
        raise ValueError(f"{id}.md: file name must be lowercase letters, digits and dashes")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{id}.md: must start with a --- header --- block")
    end = lines.index("---", 1)
    head: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        if not sep or key not in FIELDS or key in head:
            raise ValueError(f"{id}.md: header line {line!r} is not one of {sorted(FIELDS)} as 'key: value' once")
        head[key] = value.strip()
    if missing := {"title", "section", "owner", "updated"} - head.keys():
        raise ValueError(f"{id}.md: header misses {sorted(missing)}")
    try:
        dt.date.fromisoformat(head["updated"])
    except ValueError:
        raise ValueError(f"{id}.md: updated must be YYYY-MM-DD") from None
    if head.get("popular", "false") not in ("true", "false"):
        raise ValueError(f"{id}.md: popular must be true or false")
    body = tuple(" ".join(p.split()) for p in re.split(r"\n\s*\n", "\n".join(lines[end + 1 :])) if p.strip())
    if not body:
        raise ValueError(f"{id}.md: no body text")
    for url in _URL.findall(text):  # the range is closed: a link out would lead a persona nowhere, or outside
        try:
            host = (urlsplit(str(url)).hostname or "").rstrip(".")
        except ValueError:
            host = ""
        if host != RANGE_DOMAIN and not host.endswith("." + RANGE_DOMAIN):
            raise ValueError(f"{id}.md: link to {host} outside {RANGE_DOMAIN}")
    tags = tuple(t.strip() for t in head.get("tags", "").split(",") if t.strip())
    files = tuple(f.strip() for f in head.get("files", "").split(",") if f.strip())
    if bad := [f for f in files if not _FILE.fullmatch(f) or ".." in f]:
        raise ValueError(
            f"{id}.md: files {bad}: letters, digits, space . _ - and a .pdf/docx/xlsx/pptx/txt/csv/png/jpg end"
        )
    return Page(id, head["title"], head["section"], head["owner"], head["updated"], head.get("popular") == "true",
                tags, body, files)  # fmt: skip


def load(folder: Path = DIR) -> list[Page]:
    """Every page in the folder, by id; ValueError for the first bad file, an empty folder or one too big."""
    files = sorted(folder.glob("*.md"))
    if not files:
        raise ValueError(f"{folder}: no .md pages")
    if len(files) > MAX_PAGES:
        raise ValueError(f"{folder}: {len(files)} pages, at most {MAX_PAGES}")
    pages: list[Page] = []
    for f in files:
        if f.stat().st_size > MAX_PAGE:
            raise ValueError(f"{f.name}: over {MAX_PAGE >> 10} KiB")
        pages.append(parse(f.stem, f.read_text(encoding="utf-8")))
    if not any(p.popular for p in pages):
        raise ValueError(f"{folder}: no page is popular: true (the home page would be empty)")
    named = {f for p in pages for f in p.files}
    there = {f.name: f for f in (folder / "files").glob("*") if f.is_file()} if (folder / "files").is_dir() else {}
    if missing := sorted(named - there.keys()):
        raise ValueError(f"{folder}: files {missing} named on a page but not in files/")
    if unused := sorted(there.keys() - named):
        raise ValueError(f"{folder}: files/{unused} not on any page (it would be served unlisted)")
    for f in there.values():
        if f.stat().st_size > MAX_FILE:
            raise ValueError(f"files/{f.name}: over {MAX_FILE} bytes")
    if sum(f.stat().st_size for f in there.values()) > MAX_FILES:
        raise ValueError(f"{folder}: files/ over {MAX_FILES >> 20} MiB together")
    return pages


def index(folder: Path = DIR, skip: Callable[[str], bool] = lambda title: False) -> str:
    """The planner's list of pages, one line each: title and tags; `skip` leaves out the scenario's reserved ones."""
    return "\n".join(f"- {p.title}" + (f" ({', '.join(p.tags[:8])})" if p.tags else "")
                     for p in load(folder) if not skip(p.title))  # fmt: skip


def downloads(folder: Path = DIR, skip: Callable[[str], bool] = lambda title: False) -> dict[str, str]:
    """Each download (file name) and the title of the page that lists it; `skip` leaves out reserved pages."""
    return {f: p.title for p in load(folder) if not skip(p.title) for f in p.files}


def download_bytes(name: str, folder: Path = DIR) -> bytes | None:
    """The served content of a download listed on some page, else None."""
    return (folder / "files" / name).read_bytes() if name in downloads(folder) else None
