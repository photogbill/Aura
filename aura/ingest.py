# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Bill's item 6: a Whisper transcript of every song, and a short model-written summary built from it.

Why: the host should be able to talk about the same song differently each time without ever getting it wrong.
The transcript (what is actually sung) and a summary written once, offline, are the facts the host's prompts
may use. Ingest is slow (Whisper large-v3 on every song, which Bill accepts), so it is resumable, and one bad
file never stops the batch. Whisper and the model are injected by the host as plain callables.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .common import GROUNDING_RULE, NSTR_RULE, atomic_write_text, clean_reply, is_cancelled, is_nstr, say
from .library import Library, Track

log = logging.getLogger(__name__)

_MAX_TRANSCRIPT_CHARS = 8000  # keeps the prompt inside a small local model's context window

# Bracketed or starred stage text and music symbols that Whisper writes for instrumental passages.
_NON_VOCAL_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫♬♩\U0001F3B5\U0001F3B6]")
# Phrases Whisper is known to "hear" in silence or pure music; a segment that is only one of these is not singing.
_WHISPER_PHANTOMS = frozenset({
    "thank you", "thanks for watching", "thank you for watching", "please subscribe", "you",
    "subtitles by the amara org community",
})


@dataclass
class IngestSummary:
    done: int = 0
    skipped: int = 0
    nstr: int = 0
    failed: list[str] = field(default_factory=list)


def _seg_value(segment: Any, key: str) -> Any:
    """Segments are dicts per the contract; objects with attributes (faster-whisper) are accepted too."""
    if isinstance(segment, dict):
        return segment.get(key)
    return getattr(segment, key, None)


def _is_words(text: str) -> bool:
    bare = _NON_VOCAL_RE.sub(" ", text or "")
    if not re.search(r"[^\W\d_]", bare):  # no letters at all: "...", "♪", "[Music]"
        return False
    normalised = " ".join(re.sub(r"[^\w\s]", " ", bare).split()).casefold()
    return normalised not in _WHISPER_PHANTOMS


def first_vocal(segments: list[dict]) -> float | None:
    """Start time of the first segment that holds real words, i.e. how long the song's intro runs.

    "[Music]", "(music)", "♪", "..." and Whisper's habit of hearing "Thank you." in silence do not count.
    None when nothing is sung.
    """
    for segment in segments or []:
        if not _is_words(str(_seg_value(segment, "text") or "")):
            continue
        try:
            return max(0.0, float(_seg_value(segment, "start")))
        except (TypeError, ValueError):
            log.warning("segment with words has no usable start time: %r", segment)
    return None


def summary_prompt(track: Track, transcript_text: str) -> tuple[str, str]:
    """(system, user) asking for a 3-5 sentence factual note on what the song is about and who sings it.

    PURE. Built only from the track's tags, the performers from its filename, Bill's notes and the
    transcript, because whatever the summary says will later be repeated on air as fact.
    """
    system = "\n".join([
        "You write a short factual note about one song. A radio host will later read your note and talk about "
        "the song in their own words, so everything in it must be true.",
        "Use ONLY the material in the user message: the song's tags, the singer names taken from its filename, "
        "the owner's notes, and a transcript of the sung lyrics.",
        "The transcript is machine-heard sung lyrics: a speech recogniser listened to music, so words may be "
        "misheard, missing or wrong. Do not quote it as exact; say what the song is clearly about.",
        "Write 3 to 5 plain sentences: what the song is about, and who sings it when the material says so.",
        GROUNDING_RULE,
        "Plain text only: no headings, lists or markdown.",
        NSTR_RULE,
    ])
    lines = ["SONG", f"Title: {track.title}"]
    if track.artist:
        lines.append(f"Artist: {track.artist}")
    if track.album:
        lines.append(f"Album: {track.album}")
    if track.track_no:
        lines.append(f"Track number: {track.track_no}")
    if track.performers:
        lines.append("Sung by (from the filename): " + ", ".join(track.performers))
    if track.notes.strip():
        lines += ["", "THE OWNER'S NOTES (true as written):", track.notes.strip()]
    text = (transcript_text or "").strip()
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        text = text[:_MAX_TRANSCRIPT_CHARS].rstrip() + " ...(transcript cut short)"
    lines += ["", "TRANSCRIPT (machine-heard sung lyrics; may contain errors):", text or "(no words were heard)"]
    lines += ["", "Write the note now.", NSTR_RULE]
    return system, "\n".join(lines)


def transcript_file(data_dir: str | Path, track: Track) -> Path | None:
    """Where a track's transcript JSON is: its recorded path, else the default place (the data folder may
    have been moved since), else None."""
    if track.transcript_path and Path(track.transcript_path).is_file():
        return Path(track.transcript_path)
    default = Path(data_dir) / "transcripts" / f"{track.id}.json"
    return default if default.is_file() else None


def load_transcript_text(data_dir: str | Path, track: Track) -> str:
    """The transcript's text ("" when there is none or it cannot be read; the reason is logged)."""
    path = transcript_file(data_dir, track)
    if path is None:
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read transcript %s: %s", path, exc)
        return ""
    text = str(data.get("text") or "").strip()
    if not text:
        text = " ".join(str(_seg_value(s, "text") or "").strip() for s in data.get("segments") or []).strip()
    return text


def _json_default(value: Any) -> Any:
    """Whisper wrappers sometimes hand back numpy numbers; store them as plain numbers or text."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _ingest_one(library: Library, track: Track, transcribe: Callable[[str], dict],
                ask: Optional[Callable[[str, str], str]], force: bool) -> str:
    """Transcribe and/or summarise one track; returns "done", "nstr" or "skipped". Raises on failure."""
    existing = transcript_file(library.data_dir, track)
    wants_summary = ask is not None and track.summary_source != "manual" and (
        force or track.summary_source not in ("model", "nstr"))
    if not force and existing is not None and not wants_summary:
        return "skipped"
    if force or existing is None:
        result = transcribe(track.path)
        if not isinstance(result, dict):
            raise TypeError(f"transcribe() returned {type(result).__name__}, not a dict")
        out = Path(library.data_dir) / "transcripts" / f"{track.id}.json"
        record = {"track_id": track.id, "path": track.path, **result}
        atomic_write_text(out, json.dumps(record, ensure_ascii=False, indent=1, default=_json_default) + "\n")
        changes: dict[str, Any] = {"transcript_path": str(out)}
        if track.intro_source != "manual":  # Bill's hand-set intro always wins
            changes["intro_s"] = first_vocal(result.get("segments") or [])
            changes["intro_source"] = "whisper"
        track = library.update(track.id, **changes)
        text = str(result.get("text") or "").strip() or load_transcript_text(library.data_dir, track)
    else:
        text = load_transcript_text(library.data_dir, track)
    if not wants_summary:
        return "done"
    system, user = summary_prompt(track, text)
    reply = ask(system, user)  # type: ignore[misc]  # wants_summary implies ask is not None
    if is_nstr(reply):
        library.update(track.id, summary="", summary_source="nstr")
        return "nstr"
    summary = clean_reply(reply)
    if not summary:
        raise ValueError("the model returned an empty summary")
    library.update(track.id, summary=summary, summary_source="model")
    return "done"


def ingest(library: Library, track_ids: list[str], *, transcribe: Callable[[str], dict],
           ask: Optional[Callable[[str, str], str]] = None, cancel: Any = None,
           progress: Optional[Callable[[str], Any]] = None, force: bool = False) -> IngestSummary:
    """Transcribe each track (JSON in data_dir/transcripts/<id>.json) and, when `ask` is given, summarise it.

    Resumable: a track that already has a transcript (and a summary, when ask is given) is skipped unless
    force. Sets transcript_path and intro_s (source "whisper", unless Bill set it by hand); the summary gets
    source "model", or "nstr" with an empty summary when the model answers NSTR. A summary Bill wrote himself
    (source "manual") is never replaced, even with force. One track failing never stops the batch.
    """
    summary = IngestSummary()
    total = len(track_ids)
    for i, track_id in enumerate(track_ids, 1):
        if is_cancelled(cancel):
            say(progress, f"Ingest cancelled after {i - 1} of {total} tracks.")
            break
        track = library.track(track_id)
        if track is None:
            summary.failed.append(f"{track_id} (not in the library)")
            continue
        say(progress, f"[{i}/{total}] {track.title}")
        try:
            outcome = _ingest_one(library, track, transcribe, ask, force)
        except Exception as exc:  # Whisper or the model failed on this song: record it, go on to the next
            log.warning("ingest failed for %s: %s", track.path, exc)
            summary.failed.append(f"{track.path} ({exc})")
            continue
        if outcome == "skipped":
            summary.skipped += 1
        else:
            summary.done += 1
            if outcome == "nstr":
                summary.nstr += 1
    say(progress, f"Ingest finished: {summary.done} done ({summary.nstr} NSTR), {summary.skipped} skipped, "
                  f"{len(summary.failed)} failed.")
    return summary
