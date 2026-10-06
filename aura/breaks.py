# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Breaks: writing the host's words (in a batch, ahead of time), rendering them to audio, and saying which
ones may be aired.

The host is pre-recorded. Scripts are written and rendered long before play time; at play time the engine
only hands back files, and a break that is not there is skipped, never generated on the spot. Bill's own
recorded take for a break (a file in data_dir/overrides/) always wins, and a break whose speech fell back to
Piper is never aired.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

from .common import (GROUNDING_RULE, NSTR_RULE, SPOKEN_RULE, atomic_write_text, clean_reply, is_cancelled,
                     is_nstr, is_under, read_text, say)
from .library import AUDIO_EXTS, Library, Track, measure_loudness
from .script import (CASUAL_KINDS, KINDS, STATUSES, STORY_KINDS, Break, back_id, clean_text, intro_id, kind_of,
                     narration_id, parse_script, recap_id, refresh_status, render_script, segue_id, text_hash)
from .story import NeedsCharacter, Story, recap_prompt, segue_prompt

log = logging.getLogger(__name__)

OVERRIDE_EXTS = (".wav", ".mp3", ".flac") + tuple(e for e in AUDIO_EXTS if e not in (".wav", ".mp3", ".flac"))
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _lock_for(data_dir: Path) -> threading.RLock:
    """One lock per data folder, shared by every BreakStore on it in this process.

    The host may run a story Write, a Render and an Approve click on different threads, possibly through
    different BreakStore objects; each read-modify-write of a script must finish before the next starts, or
    one of them silently loses the other's change.
    """
    key = os.path.normcase(os.path.abspath(data_dir))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


def safe_id(break_id: str) -> str:
    """A break id as a file name: 'segue:the-turing-accords:3' -> 'segue_the-turing-accords_3'.

    Bill names his own recorded takes this way: data_dir/overrides/segue_the-turing-accords_3.wav.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", break_id).strip("._") or "break"


class BreakStore:
    """Script files in data_dir/scripts (casual.txt and <album_slug>.txt), rendered audio in data_dir/breaks,
    Bill's own takes in data_dir/overrides/<safe_id>.wav|.mp3|.flac (dropped in by hand)."""

    def __init__(self, data_dir: str | Path, ffmpeg: str | None = None):
        self.data_dir = Path(data_dir)
        self.ffmpeg = ffmpeg or None
        self.scripts_dir = self.data_dir / "scripts"
        self.breaks_dir = self.data_dir / "breaks"
        self.overrides_dir = self.data_dir / "overrides"
        for folder in (self.scripts_dir, self.breaks_dir, self.overrides_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, tuple[tuple[int, int], list[Break]]] = {}
        # Held across every load-modify-save. Re-entrant, so a caller may hold it around several calls.
        self.lock = _lock_for(self.data_dir)

    # ------------------------------------------------------------------------------------------ files

    def script_path(self, script: str) -> Path:
        """data_dir/scripts/<script>.txt; `script` is "casual" or an album slug (never a path)."""
        if not _SCRIPT_NAME_RE.match(script or ""):
            raise ValueError(f"not a script name: {script!r} (use 'casual' or an album slug)")
        return self.scripts_dir / f"{script}.txt"

    def scripts(self) -> list[Path]:
        return sorted(self.scripts_dir.glob("*.txt"), key=lambda p: p.name.casefold())

    def load(self, script: str) -> list[Break]:
        """The breaks in one script ([] when it does not exist yet). Re-read whenever Bill saves the file."""
        path = self.script_path(script)
        with self.lock:
            try:
                st = path.stat()
            except FileNotFoundError:
                return []
            stamp = (st.st_mtime_ns, st.st_size)
            cached = self._cache.get(script)
            if cached is None or cached[0] != stamp:
                cached = (stamp, parse_script(read_text(path)))
                self._cache[script] = cached
            return [replace(b) for b in cached[1]]  # copies: callers may change them freely

    def save(self, script: str, breaks: list[Break]) -> Path:
        with self.lock:
            path = atomic_write_text(self.script_path(script), render_script(breaks))
            self._cache.pop(script, None)
            return path

    def put(self, script: str, b: Break, order: Optional[list[str]] = None) -> Path:
        """Insert or replace one break, re-reading the file first (under the lock) so an edit Bill saved, or
        another thread made, meanwhile is kept. A new break goes before the first break that comes after it
        in `order` (else at the end)."""
        with self.lock:
            breaks = self.load(script)
            for i, existing in enumerate(breaks):
                if existing.id == b.id:
                    breaks[i] = b
                    return self.save(script, breaks)
            position = len(breaks)
            if order and b.id in order:
                rank = {bid: k for k, bid in enumerate(order)}
                mine = rank[b.id]
                for i, existing in enumerate(breaks):
                    if rank.get(existing.id, -1) > mine:
                        position = i
                        break
            breaks.insert(position, b)
            return self.save(script, breaks)

    def put_new(self, script: str, b: Break, order: Optional[list[str]] = None) -> bool:
        """Like put(), but only when no break with this id exists yet; False (and nothing written) otherwise,
        so a batch writer never replaces a break another thread or Bill created while the model was busy."""
        with self.lock:
            if any(existing.id == b.id for existing in self.load(script)):
                return False
            self.put(script, b, order)
            return True

    def update_fields(self, script: str, break_id: str, **fields: Any) -> Break | None:
        """Re-read the script, change some fields of one break and save, all under the lock. None (nothing
        saved) when the break is no longer in the script."""
        with self.lock:
            breaks = self.load(script)
            for i, existing in enumerate(breaks):
                if existing.id == break_id:
                    breaks[i] = replace(existing, **fields)
                    self.save(script, breaks)
                    return breaks[i]
            return None

    def script_for(self, break_id: str) -> str:
        """Which script a break id belongs in: casual kinds in "casual", story kinds in their album's script."""
        kind = kind_of(break_id)
        if kind in CASUAL_KINDS:
            return "casual"
        parts = break_id.split(":")
        if kind in STORY_KINDS and len(parts) >= 3:
            return parts[1]
        return ""

    def _locate(self, break_id: str) -> tuple[str, list[Break], int] | None:
        names = [self.script_for(break_id)]
        names += [p.stem for p in self.scripts() if p.stem not in names]
        for name in names:
            if not name or not _SCRIPT_NAME_RE.match(name):
                continue
            breaks = self.load(name)
            for i, b in enumerate(breaks):
                if b.id == break_id:
                    return name, breaks, i
        return None

    def get(self, break_id: str) -> Break | None:
        found = self._locate(break_id)
        return found[1][found[2]] if found else None

    def set_status(self, break_id: str, status: str) -> Break:
        """Change one break's status (ATK's Approve button). KeyError for an unknown id."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self.lock:
            found = self._locate(break_id)
            if found is None:
                raise KeyError(break_id)
            name, breaks, i = found
            breaks[i] = replace(breaks[i], status=status)
            self.save(name, breaks)
            return breaks[i]

    def override_path(self, break_id: str) -> Path | None:
        """Bill's own recorded take for this break, if he dropped one into data_dir/overrides/."""
        stem = safe_id(break_id)
        for ext in OVERRIDE_EXTS:
            candidate = self.overrides_dir / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def audio_path(self, b: Break) -> Path | None:
        """The rendered audio file (stored relative to data_dir when it lives inside it)."""
        if not b.audio:
            return None
        path = Path(b.audio)
        return path if path.is_absolute() else self.data_dir / path

    def playable(self, break_id: str, current_voice: str) -> str | None:
        """The file to air for this break, or None.

        Bill's own take first; else a break that is rendered, did not fall back, is not stale (text and
        voice unchanged since the render) and whose audio file exists. Nothing is ever generated here.
        """
        own = self.override_path(break_id)
        if own is not None:
            return str(own)
        b = self.get(break_id)
        if b is None or b.kind not in KINDS or b.kind == "notes":
            return None
        b = refresh_status(b, current_voice)
        if b.status != "rendered" or b.fell_back:
            return None
        audio = self.audio_path(b)
        return str(audio) if audio is not None and audio.is_file() else None

    def own_station_ids(self) -> list[str]:
        """"station_id:<k>" for every station ID Bill recorded himself as overrides/station_id_<k>.wav (etc.),
        so his own takes air even when no script mentions them."""
        ids: set[str] = set()
        if self.overrides_dir.is_dir():
            for path in self.overrides_dir.iterdir():
                if path.is_file() and path.suffix.lower() in OVERRIDE_EXTS and path.stem.startswith("station_id_"):
                    k = path.stem[len("station_id_"):]
                    if k:
                        ids.add(f"station_id:{k}")
        return sorted(ids)


def relink_breaks(store: BreakStore, mapping: dict[str, str]) -> int:
    """Rename casual intro/back breaks (and Bill's own takes for them) from old track ids to new ones, after
    an album folder moved; returns how many breaks were renamed. A break already present under the new id
    is never overwritten."""
    renamed = 0
    with store.lock:
        breaks = store.load("casual")
        present = {b.id for b in breaks}
        for i, b in enumerate(breaks):
            kind, _, track_id = b.id.partition(":")
            if kind not in ("intro", "back") or track_id not in mapping:
                continue
            new_id = f"{kind}:{mapping[track_id]}"
            if new_id in present:
                continue
            own = store.override_path(b.id)
            if own is not None:
                target = own.with_name(safe_id(new_id) + own.suffix)
                if not target.exists():
                    os.replace(own, target)
            breaks[i] = replace(b, id=new_id)
            present.add(new_id)
            renamed += 1
        if renamed:
            store.save("casual", breaks)
    return renamed


# --------------------------------------------------------------------------------------- casual writing

def _track_label(t: Track) -> str:
    return f'"{t.title}"'


def casual_prompt(track: Track, kind: str, *, host: str = "the host") -> tuple[str, str]:
    """(system, user) for a casual intro (before the song) or back-announce (after it).

    PURE. Only the tags, filename performers, Bill's notes and the ingest summary go in: no transcript, no
    other song.
    """
    if kind not in ("intro", "back"):
        raise ValueError("casual breaks are 'intro' or 'back'")
    moment = "just before a song starts" if kind == "intro" else "just after a song has finished"
    length = "Length: one or two sentences, under 35 words."
    if kind == "intro" and track.intro_s:
        words = max(5, int(track.intro_s * 2.5))
        length = (f"Length: one or two sentences, under 35 words; ideally under {words} words, so it fits the "
                  f"{track.intro_s:.0f} seconds before the singing starts.")
    system = "\n".join([
        f"You are {host}, the voice of a small radio station. You are writing one short thing you will say on "
        f"air {moment}. It is recorded ahead of time and played exactly as written.",
        "Use ONLY the material in the user message: the song's tags, the singer names from its filename, the "
        "owner's notes, and a summary of the song.",
        "The summary was written by a model from machine-heard lyrics: treat it as approximate and never "
        "quote it as the song's exact words. The owner's notes are true as written.",
        GROUNDING_RULE,
        SPOKEN_RULE,
        length,
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
    if track.summary.strip():
        lines += ["", "SUMMARY (written by a model from the lyrics; approximate):", track.summary.strip()]
    task = ("Introduce the song that is about to play." if kind == "intro"
            else "Tell the listener what they just heard.")
    lines += ["", f"TASK: {task}", NSTR_RULE]
    return system, "\n".join(lines)


def write_casual(library: Library, store: BreakStore, track_ids: list[str], *, ask: Callable[[str, str], str],
                 kinds: tuple[str, ...] = ("intro", "back"), cancel: Any = None,
                 progress: Optional[Callable[[str], Any]] = None, force: bool = False,
                 host: str = "the host") -> dict:
    """Write casual intro / back-announce breaks into data_dir/scripts/casual.txt.

    Casual breaks are written with status "approved": there is no approval step for them (Bill can still edit
    the file, or set one back to draft to park it). Breaks already in the file are kept unless force - drafts
    too, because a draft there means Bill parked it. NSTR makes no break and is counted.
    Returns {"written", "nstr", "skipped", "failed"}.
    """
    bad = [k for k in kinds if k not in ("intro", "back")]
    if bad:
        raise ValueError(f"write_casual writes 'intro' and 'back' breaks, not {bad}")
    result: dict[str, Any] = {"written": 0, "nstr": 0, "skipped": 0, "failed": []}
    existing = {b.id for b in store.load("casual")}
    for i, track_id in enumerate(track_ids, 1):
        if is_cancelled(cancel):
            say(progress, f"Writing cancelled after {i - 1} of {len(track_ids)} tracks.")
            break
        track = library.track(track_id)
        if track is None:
            result["failed"].append(f"{track_id} (not in the library)")
            continue
        say(progress, f"[{i}/{len(track_ids)}] {track.title}")
        for kind in kinds:
            bid = intro_id(track.id) if kind == "intro" else back_id(track.id)
            if bid in existing and not force:
                result["skipped"] += 1
                continue
            try:
                reply = ask(*casual_prompt(track, kind, host=host))
            except Exception as exc:  # the model failed on this one: record it and go on
                log.warning("ask failed for %s: %s", bid, exc)
                result["failed"].append(f"{bid} ({exc})")
                continue
            if is_nstr(reply):
                result["nstr"] += 1
                continue
            text = clean_text(clean_reply(reply))
            if not text:
                result["failed"].append(f"{bid} (empty reply)")
                continue
            title = f"before {_track_label(track)}" if kind == "intro" else f"after {_track_label(track)}"
            new = Break(id=bid, kind=kind, text=text, status="approved", title=title)
            existing.add(bid)
            if force:
                store.put("casual", new)
            elif not store.put_new("casual", new):
                result["skipped"] += 1   # someone wrote this break while the model was busy: theirs stays
                continue
            result["written"] += 1
    return result


# ---------------------------------------------------------------------------------------- story writing

def _story_plan(story: Story) -> list[tuple[str, int, str]]:
    """(kind, n, break id) in the order the script file shows them: per track, its narration, the segue
    after it, and the recap through it."""
    slug, last = story.slug, len(story.tracks) - 1
    plan = []
    for n, t in enumerate(story.tracks):
        if t.scene.strip():
            plan.append(("narration", n, narration_id(slug, n)))
        if n < last:
            plan.append(("segue", n, segue_id(slug, n)))
        plan.append(("recap", n, recap_id(slug, n)))
    return plan


def _story_title(story: Story, kind: str, n: int) -> str:
    t = story.tracks[n]
    if kind == "segue":
        return f'after {n + 1} "{t.title}" → {n + 2} "{story.tracks[n + 1].title}"'
    if kind == "recap":
        return f'previously on - through {n + 1} "{t.title}"'
    return f'scene for {n + 1} "{t.title}"'


def _store_story_break(store: BreakStore, slug: str, b: Break, order: list[str], force: bool) -> bool:
    if force:
        store.put(slug, b, order)
        return True
    return store.put_new(slug, b, order)


def write_story(story: Story, store: BreakStore, *, ask: Callable[[str, str], str], character_sheet: Any,
                host: str = "the host", depth: str = "story", cancel: Any = None,
                progress: Optional[Callable[[str], Any]] = None, force: bool = False,
                include_next_title: bool = True) -> dict:
    """Write an album's story breaks into data_dir/scripts/<album_slug>.txt.

    One segue after every track but the last, a recap through each track (for resuming), and - without any
    model - a narration break reading Bill's scene prose verbatim for each track that has a scene. Segues and
    recaps are model-written and start as "draft": nothing a model wrote airs until Bill approves it.
    Narration is Bill's own prose read word for word, so it is written "approved" (decided 2026-10-05: his
    words need no second reading by him; he can still set one back to draft). include_next_title=False keeps
    even the next song's NAME out of the segues (the tease line is still allowed). Breaks
    already in the file are kept unless force. NeedsCharacter means nothing is generated for that break; the
    names are returned. Returns {"written", "nstr", "skipped", "needs_character": sorted names, "failed"}.
    """
    slug = story.slug
    plan = _story_plan(story)
    order = [bid for _kind, _n, bid in plan]
    existing = {b.id for b in store.load(slug)}
    result: dict[str, Any] = {"written": 0, "nstr": 0, "skipped": 0, "needs_character": [], "failed": []}
    needed: set[str] = set()
    for i, (kind, n, bid) in enumerate(plan, 1):
        if is_cancelled(cancel):
            say(progress, f"Writing cancelled after {i - 1} of {len(plan)} breaks.")
            break
        if bid in existing and not force:
            result["skipped"] += 1
            continue
        say(progress, f"[{i}/{len(plan)}] {kind} {n + 1}")
        title = _story_title(story, kind, n)
        if kind == "narration":
            narration = Break(id=bid, kind=kind, text=clean_text(story.tracks[n].scene), title=title,
                              status="approved")
            existing.add(bid)
            if _store_story_break(store, slug, narration, order, force):
                result["written"] += 1
            else:
                result["skipped"] += 1
            continue
        try:
            if kind == "segue":
                system, user = segue_prompt(story, n, character_sheet=character_sheet, host=host, depth=depth,
                                            include_next_title=include_next_title)
            else:
                system, user = recap_prompt(story, n, character_sheet=character_sheet, host=host)
        except NeedsCharacter as exc:
            needed.update(exc.names)
            continue
        try:
            reply = ask(system, user)
        except Exception as exc:  # the model failed on this one: record it and go on
            log.warning("ask failed for %s: %s", bid, exc)
            result["failed"].append(f"{bid} ({exc})")
            continue
        if is_nstr(reply):
            result["nstr"] += 1
            continue
        text = clean_text(clean_reply(reply))
        if not text:
            result["failed"].append(f"{bid} (empty reply)")
            continue
        existing.add(bid)
        if _store_story_break(store, slug, Break(id=bid, kind=kind, text=text, status="draft", title=title),
                              order, force):
            result["written"] += 1
        else:
            result["skipped"] += 1  # someone wrote this break while the model was busy: theirs stays
    result["needs_character"] = sorted(needed, key=str.casefold)
    if needed:
        say(progress, "Character sheets needed before these breaks can be written: " + ", ".join(
            result["needs_character"]))
    return result


# ---------------------------------------------------------------------------------------------- render

def _skip_reason(store: BreakStore, b: Break, voice: str, approved_only: bool, force: bool) -> str:
    """Why this break is not rendered now ("" means render it). Story breaks need Bill's approval always."""
    if b.kind not in KINDS or b.kind == "notes":
        return "not a spoken break"
    if not clean_text(b.text):
        return "no text"
    status = refresh_status(b, voice).status
    if status == "draft":
        if b.kind in STORY_KINDS:
            return "story break not approved"
        return "draft (not approved)" if approved_only else ""
    if status == "rendered" and not force:
        audio = store.audio_path(b)
        return "already rendered" if audio is not None and audio.is_file() else ""
    return ""  # approved, stale, needs_rerender, or a forced re-render


def _relative_audio(store: BreakStore, path: Path) -> str:
    """Stored relative to data_dir (with / separators) when it is inside it, so the data folder can move."""
    if is_under(path, store.data_dir):
        return Path(os.path.relpath(os.path.abspath(path), os.path.abspath(store.data_dir))).as_posix()
    return str(path)


def fell_back_reason(spoken: dict, voice: str) -> str:
    """Why this synthesis result counts as a fall-back ("" when the requested voice really spoke).

    The speech service only reports fell_back when the requested voice existed and another spoke. When the
    requested voice no longer exists it quietly speaks with its default voice (often Piper) and reports no
    fall-back - so Piper speaking, or any voice other than the one asked for, is a fall-back too (rule 6).
    """
    reasons = []
    if spoken.get("fell_back"):
        reasons.append("the speech service reported a fall-back")
    if str(spoken.get("engine") or "").strip().lower() == "piper":
        reasons.append("Piper spoke")
    actual = str(spoken.get("voice") or "")
    if actual and actual != voice:
        reasons.append(f"voice {actual!r} spoke instead of {voice!r}")
    return "; ".join(reasons)


def render(store: BreakStore, script: str, *, synthesize: Callable[[str, str, str], dict], voice: str,
           approved_only: bool = True, cancel: Any = None, progress: Optional[Callable[[str], Any]] = None,
           force: bool = False) -> dict:
    """Render a script's breaks to audio in data_dir/breaks/<safe_id>.wav.

    synthesize(text, voice, out_path) -> {"path", "engine", "voice", "fell_back", "note"}. A result that fell
    back - reported as such, spoken by Piper, or spoken by any voice other than `voice` (see
    fell_back_reason) - gets status "needs_rerender" and is never playable; otherwise "rendered" with the text
    hash, voice, engine and loudness (measured when ffmpeg is available). Drafts are skipped when
    approved_only; story breaks are rendered only once Bill approved them, whatever approved_only says.
    One failure never stops the batch. Returns {"rendered", "fell_back", "skipped", "failed"}.
    """
    result: dict[str, Any] = {"rendered": 0, "fell_back": 0, "skipped": 0, "failed": []}
    breaks = store.load(script)
    for i, b in enumerate(breaks, 1):
        if is_cancelled(cancel):
            say(progress, f"Rendering cancelled after {i - 1} of {len(breaks)} breaks.")
            break
        reason = _skip_reason(store, b, voice, approved_only, force)
        if reason:
            result["skipped"] += 1
            log.debug("not rendering %s: %s", b.id, reason)
            continue
        say(progress, f"[{i}/{len(breaks)}] rendering {b.id}")
        out = store.breaks_dir / f"{safe_id(b.id)}.wav"
        try:
            spoken = synthesize(b.text, voice, str(out))
            if not isinstance(spoken, dict):
                raise TypeError(f"synthesize() returned {type(spoken).__name__}, not a dict")
            audio = Path(str(spoken.get("path") or out))
            why = fell_back_reason(spoken, voice)
            fell_back = bool(why)
            if not fell_back and not audio.is_file():
                raise FileNotFoundError(f"synthesize reported success but wrote no file at {audio}")
        except Exception as exc:  # one break's speech failed: record it and go on
            log.warning("render failed for %s: %s", b.id, exc)
            result["failed"].append(f"{b.id} ({exc})")
            continue
        note = str(spoken.get("note") or "").strip()
        engine = str(spoken.get("engine") or "")
        fields: dict[str, Any] = {"voice": voice, "engine": engine,
                                  "audio": _relative_audio(store, audio), "text_hash": text_hash(b.text)}
        if fell_back:
            # Rule 6: speech that fell back (to Piper, or to any voice but the one asked for) is never aired;
            # it waits for a proper re-render. The note records what actually spoke.
            spoke = f"spoken by engine {engine or '?'}, voice {str(spoken.get('voice') or '?')}"
            fields.update(status="needs_rerender", fell_back=True, lufs=None,
                          note="; ".join(part for part in (f"fell back: {why}", spoke, note) if part))
            result["fell_back"] += 1
        else:
            lufs, _peak = measure_loudness(audio, store.ffmpeg) if store.ffmpeg else (None, None)
            fields.update(status="rendered", fell_back=False, note=note,
                          lufs=round(lufs, 1) if lufs is not None else None)
            result["rendered"] += 1
        # Re-read under the lock before saving: if Bill edited the text while it rendered, his text stays (and
        # reads stale); an Approve or a Write on another thread meanwhile is kept.
        if store.update_fields(script, b.id, **fields) is None:
            log.warning("break %s vanished from %s while rendering; result not recorded", b.id, script)
    return result
