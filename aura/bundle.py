# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Playlist packages: one .zip that carries a playlist, and everything AURA made for its songs, to another
machine - transcripts, summaries, intro lengths, Bill's notes, the host's breaks WITH their audio, Bill's own
recorded takes, and the album stories. The songs themselves are NOT in it (Bill, 2026-10-12: "no songs in the
package for now"); the receiving machine must have them scanned.

Why import has to FIND the songs: a track id is the sha1 of the file's full path, so the same song on the field
machine (another drive letter, another folder, the MP3 instead of the WAV) has another id. Import therefore
starts by finding each song in the receiving library, and everything keyed by a track id (transcript files,
casual intro/back breaks, Bill's takes for them) is re-keyed on the way in. Finding a song, best first - the
import preview shows which way each one was found:

    exact       the same bytes: size and sha256 match. Loudness carries over too (it was measured on them).
    same_song   same folder name and file name, any type: the MP3 of a song whose WAV was ingested, or a file
                whose tags were edited since.
    file_name   the same file name (any type) in one folder only, wherever that folder is.
    title       the same album and title (and track number when both have one).

Anything ambiguous is left unmatched rather than guessed, and logged.

Two ways to import (MODES):
    fill        the default: keep everything already here and add only what is missing. A hand-set value
                ("manual") from the package still beats a machine-made one here - the rule a rescan follows.
    replace     the package wins for everything it carries.
Either way, Bill's own recorded takes on this machine are never replaced, and a story pack inside an album
folder (aura_story.json) is never written to.

Break audio: only audio that would air leaves this machine (rendered, did not fall back, text unchanged since
the render, file present), converted to MP3 by FFmpeg when it is there (Bill: convert what we generate; his own
takes travel untouched). On import it is written straight into data_dir/breaks under the receiving machine's
ids and linked in the script, so nothing is placed by hand. Every break's TEXT travels; one without usable
audio arrives "approved" (a draft stays a draft), ready to render there.

Nothing here writes outside data_dir except the package file, which is written to a temporary name beside
itself and renamed at the end, so a crash or a Stop never leaves half a package under the real name. A package
comes from another machine and is read as untrusted: every file it names is checked before it is read, and
sizes are capped.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from . import __version__
from .breaks import OVERRIDE_EXTS, BreakStore, safe_id
from .common import atomic_write_bytes, atomic_write_text, is_cancelled, is_under, read_text, say, temp_path_for
from .ingest import transcript_file
from .library import AUDIO_EXTS, INTRO_SOURCES, QUALITY_ORDER, SUMMARY_SOURCES, Library, Track
from .playlist import m3u8_text, playlists_dir
from .script import KINDS, SPOKEN_KINDS, STATUSES, STORY_KINDS, Break, back_id, intro_id, kind_of, refresh_status
# story's private helpers are the same rules story relinking uses; one copy of each rule
from .story import (STORY_PACK_NAME, Story, _ancestor_named, _folder_name, _match_key, apply_relinks, load_story,
                    relink_story, save_story, story_from_dict, story_path, story_to_dict)

log = logging.getLogger(__name__)

FORMAT = "aura-playlist-package"
VERSION = 1
EXTENSION = ".aura.zip"
MANIFEST = "manifest.json"
PLAYLIST_MEMBER = "playlist.m3u8"
LINER_NOTES = "liner_notes.html"

#: How a song in a package was found in this library, best first ("" = not found).
MATCH_KINDS = ("exact", "same_song", "file_name", "title")
MATCH_LABELS = {"exact": "Same file", "same_song": "Same song, another copy", "file_name": "Same file name",
                "title": "Same album and title", "": "Not in this library"}
MODES = ("fill", "replace")

_CASUAL_TRACK_KINDS = ("intro", "back")     # casual breaks keyed by a track id
_MAX_TEXT_BYTES = 64 * 1024 * 1024          # manifest, transcripts, stories
_MAX_AUDIO_BYTES = 512 * 1024 * 1024        # one break or one of Bill's takes
_ALREADY_COMPRESSED = (".mp3", ".m4a", ".ogg", ".opus", ".flac")
_HASH_CHUNK = 1024 * 1024
_CONVERT_TIMEOUT_S = 300
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flashing up over the GUI on Windows
_MEMBER_RE = re.compile(r"^(transcripts|breaks|own_takes|stories)/[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,200}$")


class _Cancelled(Exception):
    """Stop pressed during an export: the half-written package is removed."""


# ------------------------------------------------------------------------------------------------ results

@dataclass
class ExportSummary:
    path: str = ""
    songs: int = 0
    unreadable: list[str] = field(default_factory=list)   # songs whose file could not be read (no fingerprint)
    left_out: list[str] = field(default_factory=list)     # ids asked for that are not in the library
    transcripts: int = 0
    breaks: int = 0
    audio: int = 0               # break audio files packed
    converted: int = 0           # ...of which FFmpeg turned into MP3 here
    kept_as_is: int = 0          # ...of which could not be converted and travel as they were
    own_takes: int = 0
    stories: int = 0
    voices: list[str] = field(default_factory=list)       # voices of the packed audio, most used first
    bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class SongMatch:
    key: str                 # the song's id in the package (its track id on the machine that exported it)
    title: str
    album: str
    file: str                # file name on that machine
    local_id: str = ""       # its track id here, "" when not found
    local_path: str = ""
    how: str = ""            # one of MATCH_KINDS, "" when not found


@dataclass
class ImportPlan:
    """What an import would do, worked out before anything is written (the preview Bill sees)."""
    path: str
    name: str
    created: str
    aura_version: str
    songs: list[SongMatch] = field(default_factory=list)
    stories: list[dict] = field(default_factory=list)      # {"slug", "album", "tracks", "in_playlist"}
    transcripts: int = 0
    breaks: int = 0
    audio: int = 0
    own_takes: int = 0
    voices: list[str] = field(default_factory=list)       # voices the packed audio was rendered in
    cancelled: bool = False

    def found(self) -> int:
        return sum(1 for s in self.songs if s.local_id)

    def counts(self) -> dict[str, int]:
        """{match kind or "": how many songs}, every kind present (0 when none)."""
        counts = {kind: 0 for kind in (*MATCH_KINDS, "")}
        for s in self.songs:
            counts[s.how] = counts.get(s.how, 0) + 1
        return counts

    def mapping(self) -> dict[str, str]:
        """{package key: local track id} for the songs found."""
        return {s.key: s.local_id for s in self.songs if s.local_id}


@dataclass
class ImportSummary:
    mode: str = "fill"
    songs_found: int = 0
    songs_missing: list[str] = field(default_factory=list)
    songs_updated: int = 0
    transcripts_added: int = 0
    transcripts_kept: int = 0
    breaks_added: int = 0
    breaks_replaced: int = 0
    breaks_kept: int = 0
    breaks_unmatched: int = 0    # casual breaks for songs that were not found here
    audio_files: int = 0
    own_takes_added: int = 0
    own_takes_kept: int = 0
    stories_saved: int = 0
    stories_kept: list[str] = field(default_factory=list)
    playlist: str = ""
    voices: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


# ------------------------------------------------------------------------------------------------ small parts

def package_file_name(name: str) -> str:
    """'Field Set 1' -> 'Field Set 1.aura.zip'. Only letters, digits, spaces and - _ . ( ) are kept, so the
    name is legal on Windows; an empty result becomes 'playlist'."""
    safe = "".join(c for c in (name or "") if c.isalnum() or c in " -_.()").strip(" .")
    return (safe or "playlist") + EXTENSION


def playlist_stem(name: str) -> str:
    """The file name a playlist is saved under (the same rule ATK's Save-as-playlist uses)."""
    return "".join(c for c in (name or "") if c.isalnum() or c in " -_").strip() or "playlist"


def fingerprint(path: str | os.PathLike, cancel: Any = None) -> str:
    """sha256 of the whole file, hex. Raises OSError when it cannot be read; _Cancelled on Stop."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if is_cancelled(cancel):
                raise _Cancelled()
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def convert_to_mp3(ffmpeg: str, src: str | os.PathLike, dst: str | os.PathLike) -> bool:
    """One audio file to MP3 (LAME VBR quality 2: transparent for speech, about a tenth of a WAV). True when
    `dst` was written; False - with the reason logged - when FFmpeg failed, so the caller keeps the original."""
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", os.fspath(src), "-vn",
            "-map_metadata", "-1", "-codec:a", "libmp3lame", "-q:a", "2", "-f", "mp3", os.fspath(dst)]
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=_CONVERT_TIMEOUT_S, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("FFmpeg could not be run to convert %s to MP3: %s", src, exc)
        return False
    out = Path(dst)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        log.warning("FFmpeg could not convert %s to MP3 (exit %s): %s", src, proc.returncode,
                    err[-1] if err else "no message")
        return False
    log.debug("converted %s to MP3: %d -> %d bytes", src, os.path.getsize(src), out.stat().st_size)
    return True


def _parent_name(path: str) -> str:
    return os.path.basename(os.path.dirname(os.fspath(path)))


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _i(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove the temporary file %s: %s", path, exc)


def _transcript_text(data: Any) -> str:
    """The words of a transcript record (its "text", else its segments joined)."""
    if not isinstance(data, dict):
        return ""
    text = _s(data.get("text")).strip()
    if not text:
        parts = []
        for seg in data.get("segments") or []:
            parts.append(_s(seg.get("text") if isinstance(seg, dict) else "").strip())
        text = " ".join(p for p in parts if p)
    return text


def _clock(seconds: Any) -> str:
    value = _f(seconds) or 0.0
    minutes, secs = divmod(int(round(value)), 60)
    return f"{minutes}:{secs:02d}"


# ------------------------------------------------------------------------------------------------ export

class _Packer:
    """Writes members into the package being built, and turns rendered breaks into MP3 on the way."""

    def __init__(self, zf: zipfile.ZipFile, beside: Path, ffmpeg: str | None, summary: ExportSummary):
        self.zf = zf
        self.beside = beside            # FFmpeg's temporary output goes beside the package, never to %TEMP%
        self.ffmpeg = ffmpeg
        self.summary = summary
        self._n = 0
        self.voices: Counter = Counter()

    def text(self, name: str, text: str) -> None:
        self.zf.writestr(name, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

    def data(self, name: str, data: bytes) -> None:
        self.zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

    def file(self, name: str, path: Path) -> None:
        kind = zipfile.ZIP_STORED if path.suffix.lower() in _ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
        self.zf.write(path, name, compress_type=kind)

    def break_audio(self, stem: str, path: Path) -> str:
        """Pack one rendered break, as MP3 when it is not one already and FFmpeg can make it; returns the
        member name. Without FFmpeg (or when it fails) the file travels as it is, and the summary says so."""
        ext = path.suffix.lower()
        if ext != ".mp3" and self.ffmpeg:
            self._n += 1
            tmp = self.beside / f".{stem}.{os.getpid()}.{self._n}.tmp.mp3"
            try:
                if convert_to_mp3(self.ffmpeg, path, tmp):
                    name = f"breaks/{stem}.mp3"
                    self.file(name, tmp)
                    self.summary.converted += 1
                    return name
            finally:
                _remove(tmp)
        if ext != ".mp3":
            self.summary.kept_as_is += 1
            why = "FFmpeg is not available" if not self.ffmpeg else "FFmpeg could not convert it"
            log.info("break audio %s travels as %s: %s", path.name, ext, why)
        name = f"breaks/{stem}{ext}"
        self.file(name, path)
        return name


def _airable_audio(store: BreakStore, b: Break) -> Path | None:
    """The rendered audio of a break that would air on this machine in its own voice, else None."""
    if b.kind not in SPOKEN_KINDS or b.status != "rendered" or b.fell_back:
        return None
    if refresh_status(b, "").status != "rendered":      # "" = do not judge by voice: text edited since render
        return None
    audio = store.audio_path(b)
    return audio if audio is not None and audio.is_file() else None


def _export_break(packer: _Packer, store: BreakStore, b: Optional[Break], break_id: str) -> dict | None:
    """One break as package data: its text and status, its audio when it would air, and Bill's own take for it.
    None when there is neither a script entry nor a take."""
    own = store.override_path(break_id)
    if b is None and own is None:
        return None
    rec: dict[str, Any] = {"id": break_id, "kind": kind_of(break_id), "in_script": b is not None, "text": "",
                           "status": "draft", "title": "", "voice": "", "text_hash": "", "engine": "",
                           "lufs": None, "note": "", "audio": "", "own_take": ""}
    summary = packer.summary
    if b is not None:
        rec.update(text=b.text, title=b.title)
        audio = _airable_audio(store, b)
        if audio is not None:
            rec.update(status="rendered", voice=b.voice, text_hash=b.text_hash, engine=b.engine, lufs=b.lufs,
                       note=b.note, audio=packer.break_audio(safe_id(break_id), audio))
            summary.audio += 1
            packer.voices[b.voice] += 1
        elif b.kind in SPOKEN_KINDS and b.status not in ("draft", "approved"):
            # rendered/stale/needs_rerender without audio that would air: what travels is the approved text
            rec["status"] = "approved"
            log.debug("break %s travels without audio (status here: %s, fell back: %s)", break_id, b.status,
                      b.fell_back)
        else:
            rec["status"] = b.status
        summary.breaks += 1
    if own is not None:
        name = f"own_takes/{safe_id(break_id)}{own.suffix.lower()}"
        packer.file(name, own)
        rec["own_take"] = name
        summary.own_takes += 1
    return rec


def _export_song(packer: _Packer, library: Library, t: Track, cancel: Any,
                 transcripts: dict[str, str]) -> dict:
    rec: dict[str, Any] = {
        "key": t.id, "title": t.title, "artist": t.artist, "album": t.album, "track_no": t.track_no,
        "disc_no": t.disc_no, "performers": list(t.performers), "duration_s": t.duration_s,
        "file": os.path.basename(t.path), "folder": _parent_name(t.path), "path": t.path,
        "ext": os.path.splitext(t.path)[1].lower(), "size": 0, "sha256": "",
        "lufs": t.lufs, "true_peak_db": t.true_peak_db, "intro_s": t.intro_s, "intro_source": t.intro_source,
        "summary": t.summary, "summary_source": t.summary_source, "notes": t.notes, "transcript": "",
    }
    try:
        rec["size"] = os.path.getsize(t.path)
        rec["sha256"] = fingerprint(t.path, cancel)
    except OSError as exc:
        log.warning("cannot read %s for its fingerprint (it can still be found by name): %s", t.path, exc)
        packer.summary.unreadable.append(f"{t.title} ({t.path})")
    path = transcript_file(library.data_dir, t)
    if path is not None:
        try:
            raw = path.read_bytes()
            text = _transcript_text(json.loads(raw.decode("utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("transcript %s is unreadable and stays behind: %s", path, exc)
            packer.summary.warnings.append(f"The transcript of '{t.title}' could not be read and was left out.")
        else:
            name = f"transcripts/{t.id}.json"
            packer.data(name, raw)
            rec["transcript"] = name
            transcripts[t.id] = text
            packer.summary.transcripts += 1
    return rec


def _stories_for(library: Library, tracks: list[Track], summary: ExportSummary) -> list[Story]:
    """The stories of the albums these songs belong to (an album story holds every song of the album; it
    travels whole, and the receiving machine relinks the songs it has)."""
    ids = {t.id for t in tracks}
    found: list[Story] = []
    slugs: set[str] = set()
    for album in library.albums():
        root = album["root"]
        if not any(is_under(t.path, root) for t in tracks):
            continue
        path = story_path(library.data_dir, root)
        if not path.is_file():
            continue
        try:
            story = load_story(path, library=library)
        except (OSError, ValueError) as exc:
            log.warning("story %s is unreadable and stays behind: %s", path, exc)
            summary.warnings.append(f"The story for '{album['album']}' could not be read and was left out.")
            continue
        if story.slug in slugs or not any(st.track_id in ids for st in story.tracks):
            continue
        slugs.add(story.slug)
        found.append(story)
    return found


def export_bundle(library: Library, store: BreakStore, track_ids: list[str], out_path: str | os.PathLike, *,
                  name: str = "", ffmpeg: str | None = None, progress: Optional[Callable[[str], Any]] = None,
                  cancel: Any = None) -> ExportSummary:
    """Write a playlist package for `track_ids` (in that order) to `out_path` (".aura.zip" is added when the
    name does not end in .zip). Returns an ExportSummary; summary.cancelled when Stop was pressed, in which
    case no package exists. Raises ValueError when none of the ids is in the library."""
    out = Path(out_path)
    if not out.name.lower().endswith(".zip"):
        out = out.with_name(out.name + EXTENSION)
    summary = ExportSummary()
    tracks: list[Track] = []
    for tid in dict.fromkeys(track_ids):          # in order, each once
        t = library.track(tid)
        if t is None:
            log.warning("export: track %s is not in the library; left out", tid)
            summary.left_out.append(tid)
        else:
            tracks.append(t)
    if not tracks:
        raise ValueError("none of those songs are in the library - scan their folder first")
    if not name:
        name = out.name[: -len(EXTENSION)] if out.name.lower().endswith(EXTENSION) else out.stem
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(out)
    log.info("export %r: %d song(s) to %s (FFmpeg: %s)", name, len(tracks), out, ffmpeg or "not available")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            packer = _Packer(zf, out.parent, ffmpeg, summary)
            transcripts: dict[str, str] = {}
            songs = []
            for i, t in enumerate(tracks, 1):
                if is_cancelled(cancel):
                    raise _Cancelled()
                say(progress, f"Packing [{i}/{len(tracks)}] {t.title}")
                songs.append(_export_song(packer, library, t, cancel, transcripts))
            casual = {b.id: b for b in store.load("casual")}
            breaks = []
            for t in tracks:
                for bid in (intro_id(t.id), back_id(t.id)):
                    if is_cancelled(cancel):
                        raise _Cancelled()
                    rec = _export_break(packer, store, casual.get(bid), bid)
                    if rec is not None:
                        breaks.append(rec)
            stories = []
            story_data: dict[str, dict] = {}
            ids = {t.id for t in tracks}
            for story in _stories_for(library, tracks, summary):
                say(progress, f"Packing the story of '{story.album}'")
                data = story_to_dict(story)
                member = f"stories/{story.slug}.json"
                packer.text(member, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                story_breaks = []
                for b in store.load(story.slug):
                    if is_cancelled(cancel):
                        raise _Cancelled()
                    rec = _export_break(packer, store, b, b.id)
                    if rec is not None:
                        story_breaks.append(rec)
                stories.append({"slug": story.slug, "album": story.album, "root": story.root, "file": member,
                                "tracks": len(story.tracks),
                                "in_playlist": sum(1 for st in story.tracks if st.track_id in ids),
                                "breaks": story_breaks})
                story_data[story.slug] = data
                summary.stories += 1
            summary.songs = len(songs)
            summary.voices = [v for v, _n in packer.voices.most_common() if v]
            manifest = {
                "format": FORMAT, "version": VERSION, "name": name, "aura_version": __version__,
                "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "playlist": PLAYLIST_MEMBER, "liner_notes": LINER_NOTES,
                "songs": songs, "breaks": breaks, "stories": stories,
                "counts": {"songs": summary.songs, "transcripts": summary.transcripts, "breaks": summary.breaks,
                           "audio": summary.audio, "own_takes": summary.own_takes, "stories": summary.stories},
                "voices": summary.voices,
            }
            packer.text(PLAYLIST_MEMBER, m3u8_text(tracks))
            packer.text(LINER_NOTES, liner_notes_html(manifest, transcripts, story_data))
            packer.text(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    except _Cancelled:
        _remove(tmp)
        summary.cancelled = True
        log.info("export %r cancelled; the half-written package was removed", name)
        say(progress, "Export stopped - no package was written.")
        return summary
    except BaseException:
        _remove(tmp)
        raise
    os.replace(tmp, out)
    summary.path = str(out)
    summary.bytes = out.stat().st_size
    if summary.kept_as_is:
        summary.warnings.append(f"{summary.kept_as_is} break audio file(s) could not be converted to MP3 and "
                                "travel as they were (bigger, but they play the same).")
    log.info("export %r done: %s (%d bytes) - %d song(s), %d transcript(s), %d break(s), %d with audio "
             "(%d converted to MP3, %d as they were), %d own take(s), %d story(ies); voices %s",
             name, out, summary.bytes, summary.songs, summary.transcripts, summary.breaks, summary.audio,
             summary.converted, summary.kept_as_is, summary.own_takes, summary.stories, summary.voices or "-")
    for w in summary.warnings:
        log.warning("export %r: %s", name, w)
    return summary


# ------------------------------------------------------------------------------------------------ reading a package

@dataclass
class Bundle:
    path: str
    manifest: dict

    @property
    def name(self) -> str:
        return _s(self.manifest.get("name")) or Path(self.path).name

    @property
    def songs(self) -> list[dict]:
        return self.manifest.get("songs") or []

    @property
    def breaks(self) -> list[dict]:
        return self.manifest.get("breaks") or []

    @property
    def stories(self) -> list[dict]:
        return self.manifest.get("stories") or []


def _check_member(name: Any, folder: str, exts: tuple[str, ...]) -> str:
    """A file the manifest points at, checked before it is read: in the folder it belongs in, a plain name
    (no "..", no drive, no path), and of an expected type. A package is another machine's work."""
    if not isinstance(name, str) or not _MEMBER_RE.match(name) or not name.startswith(folder + "/"):
        raise ValueError(f"the package points at {name!r}, which is not a file in its {folder} folder")
    if PurePosixPath(name).suffix.lower() not in exts:
        raise ValueError(f"the package points at {name!r}, which is not an expected kind of file")
    return name


def _read(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise ValueError(f"the package names {name} but does not contain it") from None
    if info.file_size > limit:
        raise ValueError(f"{name} is {info.file_size:,} bytes; a package file may hold at most {limit:,}")
    with zf.open(info) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{name} is larger than it says; refusing it")
    return data


def _validate(manifest: Any, label: str) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError(f"{label}: the manifest is not a JSON object")
    if manifest.get("format") != FORMAT:
        raise ValueError(f"{label} is not an AURA playlist package (format {manifest.get('format')!r})")
    version = _i(manifest.get("version"))
    if version < 1:
        raise ValueError(f"{label}: unknown package version {manifest.get('version')!r}")
    if version > VERSION:
        raise ValueError(f"{label} was made by a newer AURA (package version {version}; this one reads up to "
                         f"{VERSION}). Update the Aura engine on this machine.")
    for key in ("songs", "breaks", "stories"):
        value = manifest.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise ValueError(f"{label}: '{key}' in the manifest is not a list of entries")
    for song in manifest.get("songs") or []:
        if not _KEY_RE.match(_s(song.get("key"))):
            raise ValueError(f"{label}: a song in the manifest has no usable key ({song.get('key')!r})")
    return manifest


def read_bundle(path: str | os.PathLike) -> Bundle:
    """Open a package and check its manifest. ValueError, saying why, for anything that is not one."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"no such file: {p}")
    if not zipfile.is_zipfile(p):
        raise ValueError(f"{p.name} is not a playlist package (it is not a zip file)")
    with zipfile.ZipFile(p) as zf:
        if MANIFEST not in zf.namelist():
            raise ValueError(f"{p.name} has no {MANIFEST}, so it is not an AURA playlist package")
        raw = _read(zf, MANIFEST, _MAX_TEXT_BYTES)
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{p.name}: the manifest is damaged ({exc})") from None
    bundle = Bundle(str(p), _validate(manifest, p.name))
    log.info("package %s: %r, made %s by AURA %s - %d song(s), %d casual break(s), %d story(ies)", p,
             bundle.name, manifest.get("created"), manifest.get("aura_version"), len(bundle.songs),
             len(bundle.breaks), len(bundle.stories))
    return bundle


# ------------------------------------------------------------------------------------------------ finding songs

def _copy_key(path: str) -> tuple[str, str]:
    """Copies of one song: same folder, same name, any type."""
    folder, name = os.path.split(path)
    return os.path.normcase(os.path.abspath(folder)), os.path.splitext(name)[0].casefold()


def _one_song(candidates: list[Track], ext: str) -> Track | None:
    """The candidate when they are all copies of ONE song (the copy of the same type first, else the best
    copy by QUALITY_ORDER); None when they are different songs - ambiguous is never guessed."""
    if not candidates:
        return None
    if len({_copy_key(t.path) for t in candidates}) != 1:
        return None
    same = [t for t in candidates if os.path.splitext(t.path)[1].lower() == ext]
    if same:
        return same[0]

    def rank(t: Track) -> int:
        e = os.path.splitext(t.path)[1].lower()
        return QUALITY_ORDER.index(e) if e in QUALITY_ORDER else len(QUALITY_ORDER)

    return sorted(candidates, key=rank)[0]


class _LibraryIndex:
    """The receiving library, indexed the four ways a song is looked for."""

    def __init__(self, tracks: list[Track]):
        self.by_size: dict[int, list[Track]] = {}
        self.by_song: dict[tuple[str, str], list[Track]] = {}
        self.by_stem: dict[str, list[Track]] = {}
        self.by_title: dict[tuple[str, str], list[Track]] = {}
        self._hashes: dict[str, str] = {}
        for t in tracks:
            stem = os.path.splitext(os.path.basename(t.path))[0].casefold()
            self.by_size.setdefault(int(t.size or 0), []).append(t)
            self.by_song.setdefault((_parent_name(t.path).casefold(), stem), []).append(t)
            self.by_stem.setdefault(stem, []).append(t)
            self.by_title.setdefault((t.album.casefold(), _match_key(t.title)), []).append(t)

    def _hash(self, t: Track, cancel: Any) -> str:
        if t.id not in self._hashes:
            try:
                self._hashes[t.id] = fingerprint(t.path, cancel)
            except OSError as exc:
                log.warning("cannot read %s to compare it: %s", t.path, exc)
                self._hashes[t.id] = ""
        return self._hashes[t.id]

    def find(self, song: dict, taken: set[str], cancel: Any) -> tuple[str, Track | None]:
        size, sha = _i(song.get("size")), _s(song.get("sha256")).lower()
        if size and sha:
            for t in self.by_size.get(size, []):
                if t.id not in taken and self._hash(t, cancel) == sha:
                    return "exact", t
        file = _s(song.get("file"))
        stem, ext = os.path.splitext(file)[0].casefold(), os.path.splitext(file)[1].lower()
        if stem:
            free = [t for t in self.by_song.get((_s(song.get("folder")).casefold(), stem), []) if t.id not in taken]
            hit = _one_song(free, ext)
            if hit is not None:
                return "same_song", hit
            hit = _one_song([t for t in self.by_stem.get(stem, []) if t.id not in taken], ext)
            if hit is not None:
                return "file_name", hit
        title = _match_key(_s(song.get("title")))
        if title:
            free = [t for t in self.by_title.get((_s(song.get("album")).casefold(), title), []) if t.id not in taken]
            number = _i(song.get("track_no"))
            if number:
                free = [t for t in free if t.track_no in (None, number)]
            hit = _one_song(free, ext)
            if hit is not None:
                return "title", hit
        return "", None


def plan_import(bundle: Bundle | str | os.PathLike, library: Library, *,
                progress: Optional[Callable[[str], Any]] = None, cancel: Any = None) -> ImportPlan:
    """Find the package's songs in this library and count what would come in. Writes nothing."""
    b = bundle if isinstance(bundle, Bundle) else read_bundle(bundle)
    m = b.manifest
    plan = ImportPlan(path=b.path, name=b.name, created=_s(m.get("created")), aura_version=_s(m.get("aura_version")))
    index = _LibraryIndex(library.tracks())
    taken: set[str] = set()
    for i, song in enumerate(b.songs, 1):
        if is_cancelled(cancel):
            plan.cancelled = True
            return plan
        title = _s(song.get("title"))
        say(progress, f"Looking for [{i}/{len(b.songs)}] {title}")
        try:
            how, t = index.find(song, taken, cancel)
        except _Cancelled:
            plan.cancelled = True
            return plan
        match = SongMatch(key=_s(song.get("key")), title=title, album=_s(song.get("album")), file=_s(song.get("file")))
        if t is not None:
            taken.add(t.id)
            match.local_id, match.local_path, match.how = t.id, t.path, how
            log.info("import %r: '%s' found by %s: %s", plan.name, title, how, t.path)
        else:
            log.info("import %r: '%s' (%s) is not in this library", plan.name, title, match.file)
        plan.songs.append(match)
        if song.get("transcript"):
            plan.transcripts += 1
    voices: Counter = Counter()
    every_break = list(b.breaks) + [r for s in b.stories for r in (s.get("breaks") or []) if isinstance(r, dict)]
    for rec in every_break:
        if rec.get("in_script", True):
            plan.breaks += 1
        if rec.get("audio"):
            plan.audio += 1
            voices[_s(rec.get("voice"))] += 1
        if rec.get("own_take"):
            plan.own_takes += 1
    plan.voices = [v for v, _n in voices.most_common() if v]
    for s in b.stories:
        plan.stories.append({"slug": _s(s.get("slug")), "album": _s(s.get("album")), "tracks": _i(s.get("tracks")),
                             "in_playlist": _i(s.get("in_playlist"))})
    counts = plan.counts()
    log.info("import %r planned: %d of %d song(s) found (%s)", plan.name, plan.found(), len(plan.songs),
             ", ".join(f"{k or 'not found'} {n}" for k, n in counts.items() if n))
    return plan


# ------------------------------------------------------------------------------------------------ import

def _song_changes(song: dict, local: Track, how: str, mode: str, summary: ImportSummary) -> dict[str, Any]:
    """The fields of `local` this song's package data fills in (fill) or sets (replace)."""
    changes: dict[str, Any] = {}
    fill = mode == "fill"
    intro, intro_src = _f(song.get("intro_s")), _s(song.get("intro_source"))
    if intro is not None and intro >= 0 and intro_src in INTRO_SOURCES:
        if not fill or local.intro_s is None or (intro_src == "manual" and local.intro_source != "manual"):
            changes.update(intro_s=intro, intro_source=intro_src)
    summary_src = _s(song.get("summary_source"))
    if summary_src and summary_src in SUMMARY_SOURCES:
        if not fill or not local.summary_source or (summary_src == "manual" and local.summary_source != "manual"):
            changes.update(summary=_s(song.get("summary")), summary_source=summary_src)
    elif summary_src:
        summary.warnings.append(f"'{local.title}': unknown summary source {summary_src!r}; summary not imported.")
    notes = _s(song.get("notes")).strip()
    if notes and (not fill or not local.notes):
        changes["notes"] = notes
    lufs = _f(song.get("lufs"))
    if how == "exact" and lufs is not None and local.lufs is None:   # measured on these very bytes
        changes.update(lufs=lufs, true_peak_db=_f(song.get("true_peak_db")))
    return changes


def _import_transcript(zf: zipfile.ZipFile, song: dict, local: Track, data_dir: Path, mode: str,
                       summary: ImportSummary) -> str:
    """Write the song's transcript under the local track id; returns its path ("" when nothing was written)."""
    member = song.get("transcript")
    if not member:
        return ""
    if mode == "fill" and transcript_file(data_dir, local) is not None:
        summary.transcripts_kept += 1
        return ""
    name = _check_member(member, "transcripts", (".json",))
    try:
        record = json.loads(_read(zf, name, _MAX_TEXT_BYTES).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        summary.warnings.append(f"The transcript of '{local.title}' in the package is damaged ({exc}); skipped.")
        return ""
    if not isinstance(record, dict):
        summary.warnings.append(f"The transcript of '{local.title}' in the package is not a transcript; skipped.")
        return ""
    record["track_id"], record["path"] = local.id, local.path
    out = data_dir / "transcripts" / f"{local.id}.json"
    atomic_write_text(out, json.dumps(record, ensure_ascii=False, indent=1) + "\n")
    summary.transcripts_added += 1
    log.debug("transcript for '%s' written to %s", local.title, out)
    return str(out)


def _import_break(zf: zipfile.ZipFile, store: BreakStore, script: str, rec: dict, new_id: str, mode: str,
                  summary: ImportSummary, voices: Counter) -> None:
    """One break into `script` under `new_id`: its text and status, its audio straight into data_dir/breaks
    (linked in the script), and Bill's take for it into data_dir/overrides when this machine has none."""
    kind = kind_of(new_id)
    if kind not in KINDS:
        summary.warnings.append(f"Break {new_id!r} is of an unknown kind; skipped.")
        return
    if rec.get("in_script", True):
        present = any(b.id == new_id for b in store.load(script))
        if present and mode == "fill":
            summary.breaks_kept += 1
        else:
            status = _s(rec.get("status")).lower()
            status = status if status in STATUSES else "draft"
            new = Break(id=new_id, kind=kind, text=_s(rec.get("text")), status=status, title=_s(rec.get("title")))
            member = rec.get("audio")
            if member and status == "rendered":
                name = _check_member(member, "breaks", AUDIO_EXTS)
                dst = store.breaks_dir / f"{safe_id(new_id)}{PurePosixPath(name).suffix.lower()}"
                atomic_write_bytes(dst, _read(zf, name, _MAX_AUDIO_BYTES))
                voice = _s(rec.get("voice"))
                new = replace(new, voice=voice, audio=f"breaks/{dst.name}", text_hash=_s(rec.get("text_hash")),
                              engine=_s(rec.get("engine")), lufs=_f(rec.get("lufs")), note=_s(rec.get("note")))
                summary.audio_files += 1
                voices[voice] += 1
                log.debug("break %s: audio written to %s", new_id, dst)
            elif status in ("rendered", "stale", "needs_rerender"):
                new = replace(new, status="approved")      # no audio came with it: ready to render here
            store.put(script, new)
            if present:
                summary.breaks_replaced += 1
            else:
                summary.breaks_added += 1
    own = rec.get("own_take")
    if own:
        name = _check_member(own, "own_takes", OVERRIDE_EXTS)
        if store.override_path(new_id) is not None:
            summary.own_takes_kept += 1          # Bill's own take on this machine always wins
        else:
            dst = store.overrides_dir / f"{safe_id(new_id)}{PurePosixPath(name).suffix.lower()}"
            atomic_write_bytes(dst, _read(zf, name, _MAX_AUDIO_BYTES))
            summary.own_takes_added += 1
            log.debug("own take for %s written to %s", new_id, dst)


def _import_story(zf: zipfile.ZipFile, bundle: Bundle, rec: dict, mapping: dict[str, str], library: Library,
                  store: BreakStore, mode: str, summary: ImportSummary, voices: Counter) -> None:
    slug = _s(rec.get("slug"))
    if not _SLUG_RE.match(slug):
        summary.warnings.append(f"A story in the package has an unusable name ({slug!r}); skipped.")
        return
    name = _check_member(rec.get("file"), "stories", (".json",))
    try:
        data = json.loads(_read(zf, name, _MAX_TEXT_BYTES).decode("utf-8-sig"))
        story = story_from_dict(data, f"{Path(bundle.path).name}:{name}")
    except (UnicodeDecodeError, ValueError) as exc:
        summary.warnings.append(f"The story '{rec.get('album')}' in the package is damaged ({exc}); skipped.")
        return
    for st in story.tracks:                       # the songs this import found, by its better evidence
        st.track_id = mapping.get(st.track_id, st.track_id)
    folder_key = _folder_name(story.root).casefold()
    homes = set()
    for st in story.tracks:
        t = library.track(st.track_id)
        home = _ancestor_named(t.path, folder_key) if (t is not None and folder_key) else None
        if home:
            homes.add(home)
    if len(homes) == 1:
        apply_relinks(story, {}, homes.pop())     # the album's folder on this machine, when it kept its name
    relinked = relink_story(story, library)       # the album's songs that were not in the playlist
    if relinked:
        log.info("story %s: %d more song(s) found by folder, file or title", slug, relinked)
    if story.slug != slug:
        log.warning("story %s now reads as %s on this machine; its breaks stay under %s", slug, story.slug, slug)
    target = story_path(library.data_dir, story.root)
    if target.name == STORY_PACK_NAME:
        summary.stories_kept.append(f"{story.album}: the album folder carries its own story pack")
    elif target.is_file() and mode == "fill":
        summary.stories_kept.append(f"{story.album}: a story for it was already here")
    else:
        save_story(story, target)
        summary.stories_saved += 1
        log.info("story %s saved to %s", slug, target)
    for brec in rec.get("breaks") or []:
        if not isinstance(brec, dict):
            continue
        bid = _s(brec.get("id"))
        kind = kind_of(bid)
        if not ((kind in STORY_KINDS and store.script_for(bid) == slug) or kind == "notes"):
            summary.warnings.append(f"Break {bid!r} does not belong to the story {slug}; skipped.")
            continue
        _import_break(zf, store, slug, brec, bid, mode, summary, voices)


def apply_import(bundle: Bundle | str | os.PathLike, plan: ImportPlan, library: Library, store: BreakStore, *,
                 mode: str = "fill", progress: Optional[Callable[[str], Any]] = None,
                 cancel: Any = None) -> ImportSummary:
    """Bring the package's work in for the songs `plan` found (see the module docstring for the rules).

    A Stop keeps what was already imported; importing the same package again later (after scanning the songs
    that were missing, say) is safe and fills in only what is still missing.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    b = bundle if isinstance(bundle, Bundle) else read_bundle(bundle)
    mapping = plan.mapping()
    hows = {s.key: s.how for s in plan.songs}
    summary = ImportSummary(mode=mode, songs_found=plan.found(),
                            songs_missing=[f"{s.title} ({s.file})" for s in plan.songs if not s.local_id])
    voices: Counter = Counter()
    data_dir = Path(library.data_dir)
    log.info("import %r (%s): %d song(s) found, mode %s", b.name, b.path, len(mapping), mode)
    with zipfile.ZipFile(b.path) as zf:
        for i, song in enumerate(b.songs, 1):
            if is_cancelled(cancel):
                summary.cancelled = True
                break
            key = _s(song.get("key"))
            local = library.track(mapping[key]) if key in mapping else None
            if local is None:
                if key in mapping:
                    summary.warnings.append(f"'{song.get('title')}' left the library since the preview; skipped.")
                continue
            say(progress, f"Importing [{i}/{len(b.songs)}] {local.title}")
            changes = _song_changes(song, local, hows.get(key, ""), mode, summary)
            written = _import_transcript(zf, song, local, data_dir, mode, summary)
            if written:
                changes["transcript_path"] = written
            if changes:
                library.update(local.id, **changes)
                summary.songs_updated += 1
                log.debug("'%s' updated: %s", local.title, ", ".join(sorted(changes)))
        if not summary.cancelled:
            say(progress, "Importing the host's breaks")
            for rec in b.breaks:
                bid = _s(rec.get("id"))
                kind, _sep, source = bid.partition(":")
                if kind not in _CASUAL_TRACK_KINDS:
                    summary.warnings.append(f"Break {bid!r} is not a song's intro or back-announce; skipped.")
                    continue
                if source not in mapping:
                    summary.breaks_unmatched += 1
                    continue
                _import_break(zf, store, "casual", rec, f"{kind}:{mapping[source]}", mode, summary, voices)
            for rec in b.stories:
                if is_cancelled(cancel):
                    summary.cancelled = True
                    break
                say(progress, f"Importing the story of '{rec.get('album')}'")
                _import_story(zf, b, rec, mapping, library, store, mode, summary, voices)
    if not summary.cancelled:
        summary.playlist = _import_playlist(b, plan, library, mode)
    summary.voices = [v for v, _n in voices.most_common() if v]
    log.info("import %r done%s: %d song(s) updated, transcripts %d added / %d kept, breaks %d added / %d replaced "
             "/ %d kept / %d for songs not here, %d audio file(s), own takes %d added / %d kept, %d story(ies) "
             "saved, playlist %s", b.name, " (stopped early)" if summary.cancelled else "", summary.songs_updated,
             summary.transcripts_added, summary.transcripts_kept, summary.breaks_added, summary.breaks_replaced,
             summary.breaks_kept, summary.breaks_unmatched, summary.audio_files, summary.own_takes_added,
             summary.own_takes_kept, summary.stories_saved, summary.playlist or "-")
    for w in summary.warnings:
        log.warning("import %r: %s", b.name, w)
    return summary


def _import_playlist(bundle: Bundle, plan: ImportPlan, library: Library, mode: str) -> str:
    """The playlist, as this machine's songs in the package's order, in data_dir/playlists. In fill mode an
    existing playlist of the same name that differs is kept, and this one is saved as "<name> (imported)"."""
    tracks = [t for t in (library.track(s.local_id) for s in plan.songs if s.local_id) if t is not None]
    if not tracks:
        return ""
    folder = playlists_dir(library.data_dir)
    stem = playlist_stem(bundle.name)
    target = folder / f"{stem}.m3u8"
    text = m3u8_text(tracks, folder)
    if mode == "fill" and target.is_file():
        try:
            same = read_text(target) == text
        except OSError:
            same = False
        if not same:
            target = folder / f"{stem} (imported).m3u8"
    atomic_write_text(target, text)
    log.info("playlist %r written to %s (%d song(s))", bundle.name, target, len(tracks))
    return str(target)


# ------------------------------------------------------------------------------------------------ liner notes

_CSS = """
:root{--bg:#0e1116;--card:#151a22;--raised:#1a2029;--line:#252c37;--text:#e7ecf3;--dim:#9aa4b2;--muted:#68717f;
--azure:#4c8dff;--green:#54c98a;--amber:#e6b34a;--violet:#b98cf0;--cyan:#4fd6d0;--red:#ef6f6c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif}
header{padding:40px 24px 28px;background:linear-gradient(135deg,#132036 0%,#0e1116 60%);border-bottom:1px solid var(--line)}
.wrap{max-width:920px;margin:0 auto}
.kicker{color:var(--cyan);letter-spacing:.14em;text-transform:uppercase;font-size:12px;font-weight:600}
h1{font-size:34px;line-height:1.15;margin:6px 0 4px}
.meta{color:var(--dim)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.chip{background:var(--raised);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:13px}
.chip b{color:var(--azure)}
main{padding:24px}
h2.section{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin:28px 0 12px}
.song{display:grid;grid-template-columns:56px 1fr;gap:16px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:18px;margin-bottom:12px}
.num{font-size:26px;font-weight:700;color:var(--azure);font-variant-numeric:tabular-nums}
.song h3{margin:0;font-size:19px}
.sub{color:var(--dim);font-size:13px;margin-top:2px}
.singers{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.singer{background:rgba(185,140,240,.14);color:var(--violet);border-radius:6px;padding:1px 8px;font-size:13px}
.badge{display:inline-block;border-radius:5px;padding:0 7px;font-size:11px;font-weight:700;letter-spacing:.04em;
text-transform:uppercase;margin-right:6px;vertical-align:1px}
.b-model{background:rgba(76,141,255,.16);color:var(--azure)}
.b-yours{background:rgba(230,179,74,.16);color:var(--amber)}
.b-nstr{background:rgba(104,113,127,.25);color:var(--dim)}
.b-note{background:rgba(84,201,138,.15);color:var(--green)}
.b-host{background:rgba(79,214,208,.14);color:var(--cyan)}
.b-draft{background:rgba(239,111,108,.14);color:var(--red)}
p{margin:10px 0 0}
.host{margin-top:10px;border-left:3px solid var(--cyan);padding:2px 0 2px 12px;color:var(--text)}
.host .line+.line{margin-top:6px}
details{margin-top:10px}
summary{cursor:pointer;color:var(--dim);font-size:13px}
pre{white-space:pre-wrap;font:13px/1.5 Consolas,ui-monospace,monospace;background:var(--input,#0c0f14);
border:1px solid var(--line);border-radius:8px;padding:12px;margin:8px 0 0;color:var(--dim)}
.story{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:12px}
.story h3{margin:0 0 8px}
.story ol{margin:0;padding-left:22px}
.story li{margin:4px 0}
.act{color:var(--amber);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.tease{color:var(--dim);font-style:italic}
footer{color:var(--muted);font-size:13px;text-align:center;padding:24px 24px 40px}
@media (max-width:600px){.song{grid-template-columns:1fr}.num{font-size:20px}h1{font-size:26px}}
@media print{body{background:#fff;color:#000}header{background:none;border-color:#ccc}
.song,.story{background:#fff;border-color:#ccc;break-inside:avoid}.meta,.sub,summary,pre{color:#333}
pre{background:#f6f6f6;border-color:#ddd}details{display:none}}
"""

_SUMMARY_BADGES = {"model": ("b-model", "Model-written"), "manual": ("b-yours", "Yours"),
                   "nstr": ("b-nstr", "NSTR")}


def liner_notes_html(manifest: dict, transcripts: dict[str, str], stories: dict[str, dict]) -> str:
    """A page anyone can open in a browser, ATK or not: the playlist, who sings what, the summaries, Bill's
    notes, what the host says around each song, the stories, and the transcripts folded away."""
    e = html.escape
    songs = manifest.get("songs") or []
    host_lines: dict[str, list[tuple[str, dict]]] = {}
    for rec in manifest.get("breaks") or []:
        kind, _sep, key = _s(rec.get("id")).partition(":")
        host_lines.setdefault(key, []).append((kind, rec))
    total = sum(_f(s.get("duration_s")) or 0.0 for s in songs)
    hours, rest = divmod(int(round(total)), 3600)
    length = f"{hours} h {rest // 60} min" if hours else f"{rest // 60} min"
    counts = manifest.get("counts") or {}
    created = _s(manifest.get("created")).replace("T", " ").replace("+00:00", " UTC")
    chips = [(("song", "songs"), counts.get("songs", len(songs))),
             (("transcript", "transcripts"), counts.get("transcripts", 0)),
             (("host break", "host breaks"), counts.get("breaks", 0)),
             (("with audio", "with audio"), counts.get("audio", 0)),
             (("story", "stories"), counts.get("stories", 0))]
    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>{e(_s(manifest.get('name')))} - liner notes</title>", f"<style>{_CSS}</style></head><body>",
           '<header><div class="wrap"><div class="kicker">AURA playlist package</div>',
           f"<h1>{e(_s(manifest.get('name')))}</h1>",
           f'<div class="meta">{e(created)} &middot; {len(songs)} song{"" if len(songs) == 1 else "s"} '
           f'&middot; {e(length)}</div>',
           '<div class="chips">' + "".join(f'<span class="chip"><b>{n}</b> {e(forms[n != 1])}</span>'
                                           for forms, n in chips)
           + "</div></div></header>", '<main><div class="wrap">', '<h2 class="section">The playlist</h2>']
    for i, s in enumerate(songs, 1):
        key = _s(s.get("key"))
        sub = [x for x in (_s(s.get("album")), _s(s.get("artist"))) if x]
        sub.append(_clock(s.get("duration_s")))
        intro = _f(s.get("intro_s"))
        if intro:
            sub.append(f"singing starts at {intro:.1f} s")
        out.append(f'<article class="song"><div class="num">{i:02d}</div><div>')
        out.append(f"<h3>{e(_s(s.get('title')))}</h3><div class=\"sub\">{e(' · '.join(sub))}</div>")
        performers = [p for p in (s.get("performers") or []) if p]
        if performers:
            out.append('<div class="singers">' + "".join(f'<span class="singer">{e(_s(p))}</span>'
                                                       for p in performers) + "</div>")
        src = _s(s.get("summary_source"))
        if src in _SUMMARY_BADGES and (s.get("summary") or src == "nstr"):
            css, label = _SUMMARY_BADGES[src]
            text = _s(s.get("summary")) or "The model had nothing true to say about this one."
            out.append(f'<p><span class="badge {css}">{label}</span>{e(text)}</p>')
        if s.get("notes"):
            out.append(f'<p><span class="badge b-note">Note</span>{e(_s(s.get("notes")))}</p>')
        lines = [(k, r) for k, r in sorted(host_lines.get(key, []), key=lambda kr: kr[0] != "intro") if r.get("text")]
        if lines:
            out.append('<div class="host">')
            for kind, rec in lines:
                label = "Host, before" if kind == "intro" else "Host, after"
                draft = '<span class="badge b-draft">Draft</span>' if rec.get("status") == "draft" else ""
                out.append(f'<div class="line"><span class="badge b-host">{label}</span>{draft}'
                           f'&ldquo;{e(_s(rec.get("text")))}&rdquo;</div>')
            out.append("</div>")
        if transcripts.get(key):
            out.append("<details><summary>Transcript (machine-heard, may contain errors)</summary>"
                       f"<pre>{e(transcripts[key])}</pre></details>")
        out.append("</div></article>")
    if manifest.get("stories"):
        out.append('<h2 class="section">Stories</h2>')
        for rec in manifest.get("stories") or []:
            data = stories.get(_s(rec.get("slug"))) or {}
            out.append(f'<section class="story"><h3>{e(_s(rec.get("album")))}</h3><ol>')
            last_act = None
            for st in data.get("tracks") or []:
                act = _s(st.get("act"))
                head = f'<span class="act">{e(act)}</span><br>' if act and act != last_act else ""
                last_act = act or last_act
                tease = f' <span class="tease">&mdash; {e(_s(st.get("tease")))}</span>' if st.get("tease") else ""
                chars = ", ".join(_s(c) for c in (st.get("characters") or []) if c)
                who = f' <span class="sub">({e(chars)})</span>' if chars else ""
                out.append(f"<li>{head}{e(_s(st.get('title')))}{who}{tease}</li>")
            out.append("</ol>")
            spoken = [r for r in (rec.get("breaks") or []) if r.get("text") and r.get("kind") != "notes"]
            if spoken:
                out.append(f"<details><summary>The host's story breaks ({len(spoken)})</summary>")
                for r in spoken:
                    title = _s(r.get("title")) or _s(r.get("id"))
                    out.append(f'<p><span class="badge b-host">{e(_s(r.get("kind")))}</span>{e(title)}</p>'
                               f"<pre>{e(_s(r.get('text')))}</pre>")
                out.append("</details>")
            out.append("</section>")
    out.append("</div></main>")
    out.append(f"<footer>The songs themselves are not in this package. Made by AURA {e(_s(manifest.get('aura_version')))}"
               " &middot; open it in ATK's AURA workspace with Import package.</footer></body></html>")
    return "\n".join(out) + "\n"
