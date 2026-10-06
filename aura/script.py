# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""The script file: every break the host will say, as plain text Bill reads, edits and approves in Notepad.

One file per album (story breaks) plus one for casual breaks. Each break starts with a "===" line carrying
its id, a human label and its status; Bill approves a break by changing "status: draft" to
"status: approved". After a render, a second line records how it was rendered. The parser is forgiving
(any case, CRLF, a BOM, a blank line before the render line) because a typo in Notepad must never cost Bill
his approvals, and render_script(parse_script(...)) gives every field back unchanged.

    === segue:the-turing-accords:3 | after 4 "Title A" -> 5 "Title B" | status: approved
    voice: host-cut · audio: breaks/segue_the-turing-accords_3.wav · hash: 1a2b... · engine: chatterbox · lufs: -18.2
    The text of the break, as many lines as it needs.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)

STATUSES = ("draft", "approved", "rendered", "stale", "needs_rerender")
KINDS = ("intro", "back", "segue", "recap", "station_id", "narration", "notes")
CASUAL_KINDS = ("intro", "back", "station_id")
STORY_KINDS = ("segue", "recap", "narration")
SPOKEN_KINDS = CASUAL_KINDS + STORY_KINDS   # "notes" blocks are Bill's own notes: never rendered or aired

PREAMBLE = (
    "# AURA script. Each break starts with a line beginning ===.\n"
    "# To approve a break, change  status: draft  to  status: approved  on its === line.\n"
    "# Edit the words under the === line freely. The line starting  voice:  is written by the renderer.\n"
    "# Lines above the first === line (like these) are ignored; keep notes in a  === notes:<name>  block.\n"
)

_META_SEP = " · "     # " · " between render fields
_EMPTY = "-"               # how an empty render field is written


@dataclass
class Break:
    id: str            # "intro:<track_id>" | "back:<track_id>" | "segue:<album_slug>:<n>" | "recap:<album_slug>:<n>" | "station_id:<k>"
    kind: str
    text: str
    status: str = "draft"
    title: str = ""    # human label, e.g. 'after 3 "A" -> 4 "B"'
    voice: str = ""    # registry voice name it was rendered with
    audio: str = ""
    text_hash: str = ""      # sha1 of the text it was RENDERED from
    fell_back: bool = False
    engine: str = ""
    lufs: float | None = None
    note: str = ""


# ------------------------------------------------------------------------------------------- ids

def intro_id(track_id: str) -> str:
    return f"intro:{track_id}"


def back_id(track_id: str) -> str:
    return f"back:{track_id}"


def segue_id(album_slug: str, n: int) -> str:
    """The segue after story track n (0-based, the same n as story.known_through)."""
    return f"segue:{album_slug}:{n}"


def recap_id(album_slug: str, n: int) -> str:
    return f"recap:{album_slug}:{n}"


def narration_id(album_slug: str, n: int) -> str:
    return f"narration:{album_slug}:{n}"


def station_id_break(k: int | str) -> str:
    return f"station_id:{k}"


def kind_of(break_id: str) -> str:
    """The kind is the part of the id before the first colon."""
    return break_id.split(":", 1)[0].strip().lower()


# ------------------------------------------------------------------------------------------- text

def clean_text(text: str) -> str:
    """Line endings to LF, trailing spaces off, no blank lines at either end.

    Notepad may save CRLF or leave stray spaces; neither is a real edit, so neither may make a rendered
    break stale.
    """
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def text_hash(text: str) -> str:
    """sha1 (first 16 hex) of the cleaned text, recorded at render time to notice later edits."""
    return hashlib.sha1(clean_text(text).encode("utf-8")).hexdigest()[:16]


def _looks_like_meta(line: str) -> bool:
    return line.startswith("voice:") and f"{_META_SEP}audio:" in line


def _escape(lines: list[str]) -> list[str]:
    """Protect text lines that would otherwise read as structure: a leading backslash is added (and removed
    again by the parser) to lines starting with "===", with a backslash, or a first line that looks like
    the render line."""
    out = []
    for i, line in enumerate(lines):
        if line.startswith(("===", "\\")) or (i == 0 and _looks_like_meta(line)):
            line = "\\" + line
        out.append(line)
    return out


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _field(value: str) -> str:
    return _one_line(value) or _EMPTY


def _meta_line(b: Break) -> str:
    """The render line; "" for a break that has never been rendered."""
    if not (b.voice or b.audio or b.text_hash or b.engine or b.lufs is not None or b.fell_back or b.note):
        return ""
    parts = [f"voice: {_field(b.voice)}", f"audio: {_field(b.audio)}", f"hash: {_field(b.text_hash)}",
             f"engine: {_field(b.engine)}", f"lufs: {_EMPTY if b.lufs is None else repr(float(b.lufs))}"]
    if b.fell_back:
        parts.append("fell back: yes")
    if b.note:
        parts.append(f"note: {_one_line(b.note)}")  # last, so a note may itself contain " · "
    return _META_SEP.join(parts)


def _parse_meta(line: str) -> dict | None:
    if not _looks_like_meta(line):
        return None
    head, _, note = line.partition(f"{_META_SEP}note: ")
    if head.startswith("note: "):
        head, note = "", head[len("note: "):]
    values: dict[str, str] = {}
    for part in head.split(_META_SEP):
        key, sep, value = part.partition(":")
        if not sep:
            return None
        values[key.strip().lower()] = value.strip()
    if not {"voice", "audio", "hash", "engine", "lufs"} <= set(values):
        return None

    def text(key: str) -> str:
        v = values.get(key, "")
        return "" if v == _EMPTY else v

    try:
        lufs = None if values["lufs"] in ("", _EMPTY) else float(values["lufs"])
    except ValueError:
        log.warning("unreadable lufs value %r in a render line; ignoring it", values["lufs"])
        lufs = None
    return {"voice": text("voice"), "audio": text("audio"), "text_hash": text("hash"), "engine": text("engine"),
            "lufs": lufs, "fell_back": values.get("fell back", "").lower() in ("yes", "true", "1"),
            "note": note.strip()}


_STATUS_RE = re.compile(r"^\s*status\s*:\s*(\S*)\s*$", re.IGNORECASE)


def _parse_header(line: str) -> tuple[str, str, str]:
    """(id, title, raw status word) from a === line; the raw status is "" when the line has none."""
    parts = [p.strip() for p in line.lstrip("=").split("|")]
    break_id = parts[0] if parts else ""
    raw_status = ""
    rest = parts[1:]
    if rest:
        m = _STATUS_RE.match(rest[-1])
        if m:
            raw_status = m.group(1)
            rest = rest[:-1]
    return break_id, " | ".join(rest), raw_status


def _status(raw: str, break_id: str) -> str:
    status = raw.strip().lower()
    if status in STATUSES:
        return status
    if raw:
        log.warning("break %s has unknown status %r; treating it as draft", break_id, raw)
    return "draft"


def parse_script(text: str) -> list[Break]:
    """Breaks from a script file's text. Lines above the first === are ignored; unknown lines inside a break
    are kept as its text. An unknown status reads as "draft", so a typo can never make a break air."""
    breaks: list[Break] = []
    current: dict | None = None
    body: list[str] = []
    meta_possible = False

    def flush() -> None:
        if current is None:
            return
        lines = [line[1:] if line.startswith("\\") else line for line in body]
        breaks.append(Break(text=clean_text("\n".join(lines)), **current))

    for line in (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("==="):
            flush()
            break_id, title, raw = _parse_header(line)
            body = []
            if not break_id:
                log.warning("a === line without an id was ignored, with the text under it")
                current, meta_possible = None, False
                continue
            current = {"id": break_id, "kind": kind_of(break_id), "title": title, "status": _status(raw, break_id)}
            meta_possible = True
            continue
        if current is None:
            continue
        if meta_possible:
            if not line.strip():
                continue
            meta_possible = False
            meta = _parse_meta(line)
            if meta is not None:
                current.update(meta)
                continue
        body.append(line)
    flush()
    return breaks


def render_script(breaks: list[Break]) -> str:
    """The text of a script file; parse_script(render_script(b)) == b for every field of every break."""
    out = [PREAMBLE]
    for b in breaks:
        if kind_of(b.id) != b.kind:
            raise ValueError(f"break id {b.id!r} must start with its kind {b.kind!r}")
        header = f"=== {b.id}"
        if b.title:
            header += f" | {_one_line(b.title)}"
        out.append(f"{header} | status: {b.status}")
        meta = _meta_line(b)
        if meta:
            out.append(meta)
        text = clean_text(b.text)
        if text:
            out.extend(_escape(text.split("\n")))
        out.append("")
    return "\n".join(out)


def refresh_status(b: Break, current_voice: str) -> Break:
    """A copy of the break, marked "stale" when it was rendered but its text has been edited since, or it
    was rendered with a different voice than `current_voice` ("" means: do not judge by voice)."""
    if b.status != "rendered":
        return replace(b)
    edited = text_hash(b.text) != b.text_hash
    revoiced = bool(current_voice) and b.voice != current_voice
    return replace(b, status="stale") if (edited or revoiced) else replace(b)


def lint_script(text: str) -> list[str]:
    """Problems worth telling Bill about: unknown statuses or kinds, duplicate ids, empty breaks."""
    problems: list[str] = []
    for line in (text or "").lstrip("﻿").replace("\r\n", "\n").split("\n"):
        if line.startswith("==="):
            break_id, _title, raw = _parse_header(line)
            if raw and raw.lower() not in STATUSES:
                problems.append(f"{break_id or '(no id)'}: unknown status {raw!r} - read as draft "
                                f"(use one of: {', '.join(STATUSES)})")
            elif not raw:
                problems.append(f"{break_id or '(no id)'}: no 'status:' on its === line - read as draft")
    seen: set[str] = set()
    for b in parse_script(text):
        if b.kind not in KINDS:
            problems.append(f"{b.id}: unknown kind {b.kind!r} - it will never be rendered or played")
        if b.id in seen:
            problems.append(f"{b.id}: appears more than once - only the first is used")
        seen.add(b.id)
        if b.kind in SPOKEN_KINDS and not b.text:
            problems.append(f"{b.id}: has no text")
        if b.status == "rendered" and b.fell_back:
            problems.append(f"{b.id}: marked rendered but its speech fell back - it will not be aired")
    return problems
