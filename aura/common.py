# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Small pieces every part of the engine shares: Bill's NSTR rule, cancel checks, safe file writes, slugs.

They live in one place so that the rules a model is given are word-for-word the same in every prompt, and so
that a crash in the middle of a save can never leave Bill with a half-written script or story file.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

NSTR = "NSTR"
NSTR_RULE = "If the material doesn't give you something true to say, answer exactly NSTR."
GROUNDING_RULE = (
    "Do not invent facts: no studios, producers, recording dates, years, chart positions, band history, "
    "influences, or anything else the material does not state."
)
SPOKEN_RULE = (
    "Write plain spoken words only: no stage directions, sound-effect notes, speaker labels, markdown or emojis."
)

_QUOTE_PAIRS = {'"': '"', "“": "”", "'": "'", "‘": "’"}


def is_nstr(reply: Optional[str]) -> bool:
    """True when a model reply is exactly NSTR (trimmed, any case).

    NSTR ("nothing significant to report") is a real answer, not an error: the model is saying the material
    held nothing true to say. The caller makes no break and counts it, instead of retrying until the model
    invents something.
    """
    return isinstance(reply, str) and reply.strip().upper() == NSTR


def clean_reply(reply: Optional[str]) -> str:
    """Trim a model reply and drop one pair of quotes wrapped around the whole of it.

    Small models like to put a spoken line in quotes; left in, the quotes would be shown to Bill as part of
    the script and could be read aloud by the speech engine.
    """
    text = (reply or "").strip()
    if len(text) >= 2:
        close = _QUOTE_PAIRS.get(text[0])
        inner = text[1:-1]
        if close and text.endswith(close) and close not in inner and text[0] not in inner:
            text = inner.strip()
    return text


def is_cancelled(cancel: Any) -> bool:
    """True when the host asked a long job to stop.

    `cancel` may be None, anything with is_set() (a threading.Event), or a zero-argument callable, whichever
    is easiest for the host to pass from its worker thread.
    """
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel):
        return bool(cancel())
    return bool(cancel)


def say(progress: Optional[Callable[[str], Any]], message: str) -> None:
    """Send one progress line to the host (and to the debug log).

    A broken progress callback (a window that was closed, say) must never kill a two-hour ingest, so its
    errors are logged here, in one visible place, and the work carries on.
    """
    log.debug("%s", message)
    if progress is None:
        return
    try:
        progress(message)
    except Exception as exc:  # the host's callback failed, not our work: report it and carry on
        log.warning("progress callback failed: %s", exc)


def temp_path_for(target: Path) -> Path:
    """The temporary file beside `target` for one write: unique per process AND thread, so two threads saving
    the same file never write into (or rename away) each other's temporary file."""
    return target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def atomic_write_text(path: str | os.PathLike, text: str) -> Path:
    """Write UTF-8 text with LF line endings so that a crash leaves the old file or the new one, never half.

    The temporary file sits beside the target (never in the system temp folder) and is renamed over it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(target)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp, target)
    except OSError as exc:
        # Windows refuses the rename while another program holds the target open; write in place instead.
        log.warning("could not replace %s in one step (%s); writing it in place", target, exc)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            tmp.unlink()
        except OSError as exc2:
            log.warning("could not remove the temporary file %s: %s", tmp, exc2)
    return target


def read_text(path: str | os.PathLike) -> str:
    """Read a text file Bill may have saved from Notepad: UTF-8 with or without a BOM, else Windows-1252."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.warning("%s is not UTF-8; reading it as Windows-1252", path)
        return raw.decode("cp1252", errors="replace")


def slugify(text: str, default: str = "album") -> str:
    """Lower-case ASCII words joined by hyphens: 'The Turing Accords' -> 'the-turing-accords'.

    Slugs name script files and appear in break ids, so they must come out the same on every machine and be
    safe in a file name.
    """
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug or default


def is_under(path: str | os.PathLike, folder: str | os.PathLike) -> bool:
    """True when `path` is `folder` or lies inside it (case-insensitive on Windows, like its file system)."""
    p = os.path.normcase(os.path.abspath(os.fspath(path)))
    f = os.path.normcase(os.path.abspath(os.fspath(folder)))
    try:
        return os.path.commonpath([p, f]) == f
    except ValueError:  # different drives on Windows have no common path
        return False
