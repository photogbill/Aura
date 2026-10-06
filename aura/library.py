# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""The music library: find audio files, learn what they are, and keep the answers in one SQLite file.

Why SQLite: a single file under data_dir that survives a crash (WAL journal) and that the host can read from
its GUI thread while a scan runs in a worker thread. Tags come from tinytag when it is installed; without it
the filename is the source (Bill's files are named "07 - Title (Singer).mp3"), and ffmpeg supplies durations
and loudness. Fields a person set by hand are never overwritten by a scan.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import wave
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable, Optional

from .common import is_cancelled, is_under, say

log = logging.getLogger(__name__)

AUDIO_EXTS = (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus")
TARGET_LUFS = -16.0
INTRO_SOURCES = ("", "whisper", "manual")
SUMMARY_SOURCES = ("", "model", "nstr", "manual")

# Keeps a console window from flashing up over the GUI host on Windows every time ffmpeg runs.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOUDNESS_TIMEOUT_S = 900
_PROBE_TIMEOUT_S = 60


@dataclass
class Track:
    id: str                 # stable: sha1 of the normalised absolute path, first 16 hex
    path: str
    root: str               # the scanned folder it was found under
    title: str
    artist: str = ""
    album: str = ""         # tag, else the parent folder name
    track_no: int | None = None
    disc_no: int | None = None
    performers: list[str] = field(default_factory=list)   # from "(Valkyrie)" / "(Valkyrie & Sarah)" / "(A, B)"
    duration_s: float = 0.0
    lufs: float | None = None
    true_peak_db: float | None = None
    intro_s: float | None = None        # seconds before the first sung word
    intro_source: str = ""              # "" | "whisper" | "manual"  (manual is never overwritten by ingest)
    transcript_path: str = ""
    summary: str = ""
    summary_source: str = ""            # "" | "model" | "nstr" | "manual"
    notes: str = ""                     # Bill's own note for the host (read verbatim)
    size: int = 0
    mtime: float = 0.0


@dataclass
class ScanSummary:
    found: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list)


# Fields the host may change through Library.update(). id, path, root, size and mtime belong to the scan.
EDITABLE_FIELDS = frozenset({
    "title", "artist", "album", "track_no", "disc_no", "performers", "duration_s", "lufs", "true_peak_db",
    "intro_s", "intro_source", "transcript_path", "summary", "summary_source", "notes",
})
# What a scan never computes, so a rescan carries it over: Bill's hand-set fields and hours of ingest work.
_CARRIED_FIELDS = ("intro_s", "intro_source", "transcript_path", "summary", "summary_source", "notes")


def track_id_for(path: str | os.PathLike) -> str:
    """Stable id for a file: the first 16 hex digits of the sha1 of its normalised absolute path.

    normcase makes D:\\Music\\x.mp3 and d:/music/X.MP3 one track on Windows, as they are one file there.
    """
    norm = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
    return hashlib.sha1(norm.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- filenames

_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{1,4}")
_DISC_TRACK_RE = re.compile(r"^(\d{1,2})-(\d{1,3})(?:\s*[-\u2013\u2014._:]\s*|\s+)(.+)$")
_TRACK_RE = re.compile(r"^(\d{1,3})(?:\s*[-\u2013\u2014._:)]\s*|\s+)(.+)$")
_LAST_GROUP_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_FEAT_RE = re.compile(r"^(?:feat\.?|ft\.?|featuring|with)\s+(.+)$", re.IGNORECASE)
_NAME_SPLIT_RE = re.compile(r"\s*(?:&|,|/|\s+and\s+)\s*", re.IGNORECASE)

# A parenthesised group containing any of these words is part of the title ("(Reprise)", "(Live at X)").
_TITLE_WORDS = frozenset("""
    remix mix live instrumental reprise acoustic demo edit version remaster remastered extended radio single
    bonus intro outro interlude part pt cover original alternate alt take explicit clean unplugged orchestral
    piano karaoke mono stereo session rehearsal prologue epilogue finale overture coda theme suite slowed sped
    nightcore bootleg redux reimagined revisited rework outtake dub vip vocal vocals cappella acapella cut
    mixdown draft reprisal medley
""".split())
# Pronouns and helper words: "(I Will Email You)" is a subtitle, not four singers.
_NOT_NAME_WORDS = frozenset("""
    i you me we us my your our it its is are was were be been am an to in on for at from by not no yes all
    this that these those what when where why how will can do does did don't won't can't i'm you're it's
    we're so if or but oh just never ever always love
""".split())
_NAME_PARTICLES = frozenset({"de", "da", "di", "del", "della", "der", "den", "van", "von", "la", "le", "du",
                             "bin", "al", "el", "of", "the"})


def _looks_like_name(part: str) -> bool:
    """One performer name: 1-4 capitalised words, no digits-only words, no title words, no possessives."""
    words = part.split()
    if not 1 <= len(words) <= 4:
        return False
    lowered = part.lower()
    if "'s" in lowered or "\u2019s" in lowered:
        return False
    for i, word in enumerate(words):
        bare = word.strip(".-'\u2019")
        low = bare.casefold()
        if not bare or bare.isdigit() or low in _TITLE_WORDS or low in _NOT_NAME_WORDS:
            return False
        if bare[0].isalpha() and bare[0].isupper():
            continue
        if i > 0 and low in _NAME_PARTICLES:
            continue
        return False
    return True


def _names_in(group: str) -> list[str]:
    """The performer names in a parenthesised group, or [] when it does not look like names."""
    parts = [p.strip() for p in _NAME_SPLIT_RE.split(group.strip()) if p.strip()]
    if parts and all(_looks_like_name(p) for p in parts):
        return parts
    return []


def split_performers(text: str) -> tuple[str, list[str]]:
    """Split "Title (Valkyrie & Sarah)" into ("Title", ["Valkyrie", "Sarah"]).

    Only the LAST parenthesised group can be performers, and only when it looks like names, so "(Reprise)",
    "(Live)" and "(Instrumental)" stay in the title. A trailing "(feat. X)" adds X and lets the group before
    it be checked too.
    """
    title = (text or "").strip()
    featured: list[str] = []
    m = _LAST_GROUP_RE.match(title)
    if m and m.group(1).strip():
        f = _FEAT_RE.match(m.group(2).strip())
        if f:
            names = _names_in(f.group(1))
            if names:
                featured = names
                title = m.group(1).strip()
                m = _LAST_GROUP_RE.match(title)
    performers: list[str] = []
    if m and m.group(1).strip():
        names = _names_in(m.group(2))
        if names:
            performers = names
            title = m.group(1).strip()
    for name in featured:
        if name.casefold() not in {p.casefold() for p in performers}:
            performers.append(name)
    title = title.strip(" -_\u2013\u2014")
    return title, performers


def _last_component(name: str) -> str:
    """The file name from a path, without mistaking "(Lex / Meg)" for a folder separator."""
    depth = 0
    for i in range(len(name) - 1, -1, -1):
        ch = name[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth = max(0, depth - 1)
        elif ch in "/\\" and depth == 0:
            return name[i + 1:]
    return name


def parse_filename(name: str) -> dict:
    """Learn what a filename says: '07 - The Velvet Lesson (Aphrodite).mp3' ->
    {"track_no": 7, "disc_no": None, "title": "The Velvet Lesson", "performers": ["Aphrodite"]}.

    Handles "07 - Title", "07. Title", "07 Title", "1-07 Title" (disc-track), no number at all, a trailing
    "(feat. X)", and performers separated by "&", ",", " and " or "/". Without tinytag this is the only
    source of a title, so it errs towards keeping text in the title rather than dropping it.
    """
    base = _last_component(str(name))
    stem, ext = os.path.splitext(base)
    if not (ext.lower() in AUDIO_EXTS or _EXT_RE.fullmatch(ext)):
        stem = base
    stem = stem.strip()
    if " " not in stem and "_" in stem:
        stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    disc_no: int | None = None
    track_no: int | None = None
    rest = stem
    m = _DISC_TRACK_RE.match(rest)
    if m:
        disc_no, track_no, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = _TRACK_RE.match(rest)
        if m:
            track_no, rest = int(m.group(1)), m.group(2)
        elif rest.isdigit() and len(rest) <= 3:
            track_no = int(rest)
    title, performers = split_performers(rest)
    return {"track_no": track_no, "disc_no": disc_no, "title": title or stem, "performers": performers}


# ------------------------------------------------------------------------------------------ tags and ffmpeg

_tinytag_class: Any = None
_tinytag_tried = False


def _tinytag() -> Any:
    """The TinyTag class when tinytag is installed, else None. Imported lazily: it is optional."""
    global _tinytag_class, _tinytag_tried
    if not _tinytag_tried:
        _tinytag_tried = True
        try:
            from tinytag import TinyTag  # optional, MIT licensed
            _tinytag_class = TinyTag
        except ImportError:
            log.info("tinytag is not installed: titles come from filenames, durations from ffmpeg")
    return _tinytag_class


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(str(value).split("/")[0].strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def read_tags(path: str | os.PathLike) -> dict:
    """Tags via tinytag: {"title", "artist", "album", "track", "disc", "duration"}; {} without tinytag."""
    tag_class = _tinytag()
    if tag_class is None:
        return {}
    try:
        tag = tag_class.get(os.fspath(path))
    except Exception as exc:  # tinytag raises its own error types for damaged or unusual files
        log.warning("tinytag could not read %s: %s", path, exc)
        return {}
    return {
        "title": str(getattr(tag, "title", None) or "").strip(),
        "artist": str(getattr(tag, "artist", None) or "").strip(),
        "album": str(getattr(tag, "album", None) or "").strip(),
        "track": _to_int(getattr(tag, "track", None)),
        "disc": _to_int(getattr(tag, "disc", None)),
        "duration": _to_float(getattr(tag, "duration", None)),
    }


def _run_ffmpeg(ffmpeg: str, args: list[str], timeout: float) -> str:
    """Run ffmpeg and return what it printed to stderr (where it reports durations and loudness)."""
    proc = subprocess.run(
        [ffmpeg, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=_NO_WINDOW,
    )
    return proc.stderr.decode("utf-8", errors="replace")


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value not in (float("inf"), float("-inf")) else None


def parse_ebur128(text: str) -> tuple[float | None, float | None]:
    """(integrated LUFS, true peak dBFS) from the Summary block ffmpeg's ebur128 filter prints at the end.

    Only the text after the last "Summary:" is read: the per-frame lines before it carry running values.
    """
    at = text.rfind("Summary:")
    if at < 0:
        return None, None
    block = text[at:]
    i = re.search(r"^\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS", block, re.MULTILINE)
    p = re.search(r"^\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dBFS", block, re.MULTILINE)
    return (_number(i.group(1)) if i else None), (_number(p.group(1)) if p else None)


def measure_loudness(path: str | os.PathLike, ffmpeg: str | None) -> tuple[float | None, float | None]:
    """Integrated loudness (LUFS) and true peak (dBFS) via ffmpeg's ebur128 filter; (None, None) without
    ffmpeg or on any failure, so a scan carries on and the show simply applies no gain to that track."""
    if not ffmpeg:
        return None, None
    args = ["-nostdin", "-hide_banner", "-nostats", "-i", os.fspath(path),
            "-filter_complex", "ebur128=peak=true", "-f", "null", "-"]
    try:
        text = _run_ffmpeg(ffmpeg, args, _LOUDNESS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("loudness measurement failed for %s: %s", path, exc)
        return None, None
    lufs, peak = parse_ebur128(text)
    if lufs is None:
        log.warning("ffmpeg gave no loudness summary for %s", path)
    return lufs, peak


def _duration_fallback(path: str | os.PathLike, ffmpeg: str | None) -> float:
    """Duration without tinytag: the stdlib wave module for .wav, else ffmpeg's "Duration:" line, else 0."""
    if os.fspath(path).lower().endswith(".wav"):
        try:
            with wave.open(os.fspath(path), "rb") as w:
                rate = w.getframerate()
                if rate:
                    return w.getnframes() / float(rate)
        except Exception as exc:  # noqa: BLE001 - a damaged header can raise RuntimeError from chunk.py
            log.debug("wave could not read %s: %s", path, exc)
    if ffmpeg:
        try:
            text = _run_ffmpeg(ffmpeg, ["-nostdin", "-hide_banner", "-i", os.fspath(path)], _PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("ffmpeg could not probe %s: %s", path, exc)
            return 0.0
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0.0


def probe_duration(path: str | os.PathLike, ffmpeg: str | None) -> float:
    """Length in seconds: tinytag first, else ffmpeg's "Duration: HH:MM:SS.xx" (plain .wav files are read
    with the stdlib wave module), else 0.0."""
    tag_class = _tinytag()
    if tag_class is not None:
        try:
            duration = _to_float(tag_class.get(os.fspath(path)).duration)
            if duration:
                return duration
        except Exception as exc:  # tinytag raises its own error types for damaged or unusual files
            log.warning("tinytag could not read the length of %s: %s", path, exc)
    return _duration_fallback(path, ffmpeg)


# ------------------------------------------------------------------------------------------------ library

_SQL_TYPES = {"track_no": "INTEGER", "disc_no": "INTEGER", "size": "INTEGER", "duration_s": "REAL",
              "lufs": "REAL", "true_peak_db": "REAL", "intro_s": "REAL", "mtime": "REAL"}
_DISC_FOLDER_RE = re.compile(r"^(disc|disk|cd)\s*\d+$", re.IGNORECASE)


def _skip_dir(name: str) -> bool:
    return name.startswith((".", "$")) or name.casefold() == "system volume information"


def _album_folder_name(path: str) -> str:
    """The album name a folder implies: the parent folder, or the one above a "Disc 2"-style folder."""
    parent = os.path.dirname(path)
    name = os.path.basename(parent)
    if _DISC_FOLDER_RE.match(name):
        name = os.path.basename(os.path.dirname(parent)) or name
    return name


def _order_key(t: Track) -> tuple:
    """Albums together, then disc, track number (unnumbered last) and filename."""
    return (t.root.casefold(), t.album.casefold(), t.disc_no or 0, t.track_no is None, t.track_no or 0,
            os.path.basename(t.path).casefold())


class Library:
    """The track index at data_dir/aura.db. Safe to share between the host's GUI thread and one worker."""

    def __init__(self, data_dir: str | Path, ffmpeg: str | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = ffmpeg or None
        self.db_path = self.data_dir / "aura.db"
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema()

    # -- context manager, so `with Library(d) as lib:` always closes the file
    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("the library has been closed")
        return self._db

    def _ensure_schema(self) -> None:
        """One tracks table; columns added in later versions are added to an older file in place."""
        names = [f.name for f in fields(Track)]
        cols = ", ".join("id TEXT PRIMARY KEY" if n == "id" else f"{n} {_SQL_TYPES.get(n, 'TEXT')}" for n in names)
        db = self._conn()
        db.execute(f"CREATE TABLE IF NOT EXISTS tracks ({cols})")
        existing = {row[1] for row in db.execute("PRAGMA table_info(tracks)")}
        for n in names:
            if n not in existing:
                db.execute(f"ALTER TABLE tracks ADD COLUMN {n} {_SQL_TYPES.get(n, 'TEXT')}")
        db.commit()

    def _save(self, track: Track) -> None:
        row = asdict(track)
        row["performers"] = json.dumps(list(track.performers), ensure_ascii=False)
        cols = list(row)
        sql = f"INSERT OR REPLACE INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        with self._lock:
            db = self._conn()
            db.execute(sql, [row[c] for c in cols])
            db.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Track:
        data: dict[str, Any] = {}
        for f in fields(Track):
            value = row[f.name]
            if value is None and f.type == "str":
                value = ""
            data[f.name] = value
        try:
            performers = json.loads(data.get("performers") or "[]")
        except json.JSONDecodeError:
            log.warning("unreadable performers for track %s; treating as none", data.get("id"))
            performers = []
        data["performers"] = [str(p) for p in performers] if isinstance(performers, list) else []
        data["duration_s"] = float(data.get("duration_s") or 0.0)
        data["size"] = int(data.get("size") or 0)
        data["mtime"] = float(data.get("mtime") or 0.0)
        return Track(**data)

    # ------------------------------------------------------------------------------------------- scanning

    def scan(self, root: str | os.PathLike, *, progress: Optional[Callable[[str], Any]] = None,
             cancel: Any = None, measure: bool = True) -> ScanSummary:
        """Index every audio file under `root` (recursively).

        A file whose size and mtime are unchanged keeps its row. Tags come from tinytag when importable.
        Fields a person set (intro_source "manual", summary_source "manual", notes) and ingest results are
        never overwritten by a scan. Rows for files under `root` that have gone are removed, but only when
        the scan ran to the end: a cancelled scan has not seen every file. `cancel` is Event-like (is_set())
        or a zero-argument callable; `progress(str)` gets one line per file.
        """
        root_abs = os.path.abspath(os.fspath(root))
        if not os.path.isdir(root_abs):
            raise NotADirectoryError(f"not a folder: {root_abs}")
        summary = ScanSummary()
        files = self._find_audio(root_abs)
        summary.found = len(files)
        seen: set[str] = set()
        for i, path in enumerate(files, 1):
            if is_cancelled(cancel):
                say(progress, f"Scan cancelled after {i - 1} of {len(files)} files; nothing was removed.")
                return summary
            say(progress, f"[{i}/{len(files)}] {os.path.basename(path)}")
            seen.add(track_id_for(path))  # seen even if it fails: a read error must not delete Bill's notes
            try:
                outcome = self._scan_one(path, root_abs, measure)
            except Exception as exc:  # noqa: BLE001 - one unreadable file never stops the scan
                log.warning("could not index %s: %s", path, exc)
                summary.failed.append(f"{path} ({exc})")
                continue
            setattr(summary, outcome, getattr(summary, outcome) + 1)
        summary.removed = self._remove_unseen(root_abs, seen)
        say(progress, f"Scan finished: {summary.found} found, {summary.added} added, {summary.updated} updated, "
                      f"{summary.unchanged} unchanged, {summary.removed} removed, {len(summary.failed)} failed.")
        return summary

    def _find_audio(self, root: str) -> list[str]:
        found: list[str] = []
        data_dir = os.path.abspath(self.data_dir)

        def on_error(err: OSError) -> None:
            log.warning("cannot read folder %s: %s", getattr(err, "filename", "?"), err)

        for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
            # never index our own rendered breaks if Bill points a scan at a folder holding data_dir
            dirnames[:] = sorted((d for d in dirnames if not _skip_dir(d)
                                  and not is_under(os.path.join(dirpath, d), data_dir)), key=str.casefold)
            for name in sorted(filenames, key=str.casefold):
                if not name.startswith("._") and os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                    found.append(os.path.join(dirpath, name))
        return found

    def _scan_one(self, path: str, root: str, measure: bool) -> str:
        """Index one file; returns "added", "updated" or "unchanged".

        Loudness measurement can take minutes, and the host may save a note, an intro or an ingest result
        for this very track meanwhile. So the slow work happens outside the lock, and the row is re-read
        under the lock right before saving: only the scan's own fields come from the scan.
        """
        st = os.stat(path)
        track_id = track_id_for(path)
        old = self.track(track_id)
        if old is not None and old.size == st.st_size and abs(old.mtime - st.st_mtime) < 0.001:
            changes: dict[str, Any] = {}
            if old.root != root:
                changes["root"] = root
            if measure and self.ffmpeg and old.lufs is None:  # e.g. the first scan ran with --no-measure
                lufs, peak = measure_loudness(path, self.ffmpeg)
                if lufs is not None:
                    changes.update(lufs=lufs, true_peak_db=peak)
            if changes:
                with self._lock:
                    latest = self.track(track_id)
                    if latest is not None:
                        self._save(replace(latest, **changes))
            return "updated" if "lufs" in changes else "unchanged"
        fresh = self._read_file(path, root, st, measure)
        with self._lock:
            latest = self.track(track_id)
            if latest is not None:
                fresh = replace(fresh, **{name: getattr(latest, name) for name in _CARRIED_FIELDS})
            self._save(fresh)
        return "updated" if old is not None else "added"

    def _read_file(self, path: str, root: str, st: os.stat_result, measure: bool) -> Track:
        parsed = parse_filename(os.path.basename(path))
        tags = read_tags(path)
        title = parsed["title"]
        performers = list(parsed["performers"])
        if tags.get("title"):
            tag_title, tag_performers = split_performers(tags["title"])
            title = tag_title or tags["title"]
            performers = performers or tag_performers
        duration = tags.get("duration") or _duration_fallback(path, self.ffmpeg)
        lufs, peak = measure_loudness(path, self.ffmpeg) if (measure and self.ffmpeg) else (None, None)
        return Track(
            id=track_id_for(path), path=path, root=root, title=title,
            artist=tags.get("artist") or "", album=tags.get("album") or _album_folder_name(path),
            track_no=tags.get("track") or parsed["track_no"], disc_no=tags.get("disc") or parsed["disc_no"],
            performers=performers, duration_s=round(float(duration), 3), lufs=lufs, true_peak_db=peak,
            size=st.st_size, mtime=st.st_mtime,
        )

    def _remove_unseen(self, root: str, seen: set[str]) -> int:
        gone = [t.id for t in self.tracks() if t.id not in seen and is_under(t.path, root)]
        with self._lock:
            db = self._conn()
            db.executemany("DELETE FROM tracks WHERE id = ?", [(i,) for i in gone])
            db.commit()
        return len(gone)

    # -------------------------------------------------------------------------------------------- reading

    def tracks(self, root: str | None = None, album: str | None = None) -> list[Track]:
        """Tracks in play order (disc, track number, filename), optionally only those under the folder
        `root` (a scanned root or any folder inside one, such as an album folder) and/or with tag `album`."""
        with self._lock:
            rows = self._conn().execute("SELECT * FROM tracks").fetchall()
        result = [self._from_row(r) for r in rows]
        if root:
            result = [t for t in result if is_under(t.path, root)]
        if album is not None:
            result = [t for t in result if t.album == album]
        result.sort(key=_order_key)
        return result

    def track(self, track_id: str) -> Track | None:
        with self._lock:
            row = self._conn().execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return self._from_row(row) if row is not None else None

    def albums(self) -> list[dict]:
        """[{"album", "root", "count"}] sorted by album. "root" is the folder holding the album's files (the
        folder to pass to story.draft_story / story_path), which is the scanned root when Bill scans one
        album folder at a time."""
        groups: dict[tuple[str, str], list[Track]] = {}
        for t in self.tracks():
            groups.setdefault((t.album, t.root), []).append(t)
        result = []
        for (album, _root), items in groups.items():
            try:
                folder = os.path.commonpath([os.path.dirname(t.path) for t in items])
            except ValueError:  # files on different drives
                folder = _root
            if _DISC_FOLDER_RE.match(os.path.basename(folder)):
                folder = os.path.dirname(folder)
            result.append({"album": album, "root": folder, "count": len(items)})
        result.sort(key=lambda a: (a["album"].casefold(), a["root"].casefold()))
        return result

    def roots(self) -> list[str]:
        return sorted({t.root for t in self.tracks()}, key=str.casefold)

    # -------------------------------------------------------------------------------------------- writing

    def update(self, track_id: str, **fields_: Any) -> Track:
        """Change editable fields of one track and return it. Raises KeyError for an unknown id and
        ValueError for a field that is not editable.

        Setting intro_s or summary without saying where it came from marks it "manual", because a value the
        host sets by hand is Bill's and must survive later ingests and scans.
        """
        bad = sorted(set(fields_) - EDITABLE_FIELDS)
        if bad:
            raise ValueError(f"not editable: {', '.join(bad)}")
        with self._lock:
            current = self.track(track_id)
            if current is None:
                raise KeyError(track_id)
            if "intro_s" in fields_ and "intro_source" not in fields_:
                fields_["intro_source"] = "manual" if fields_["intro_s"] is not None else ""
            if "summary" in fields_ and "summary_source" not in fields_:
                fields_["summary_source"] = "manual" if fields_["summary"] else ""
            if fields_.get("intro_source", "") not in INTRO_SOURCES:
                raise ValueError(f"intro_source must be one of {INTRO_SOURCES}")
            if fields_.get("summary_source", "") not in SUMMARY_SOURCES:
                raise ValueError(f"summary_source must be one of {SUMMARY_SOURCES}")
            if "performers" in fields_:
                fields_["performers"] = [str(p) for p in (fields_["performers"] or [])]
            new = replace(current, **fields_)
            self._save(new)
            return new

    def remove_missing(self) -> int:
        """Drop rows whose file no longer exists; returns how many."""
        gone = [t.id for t in self.tracks() if not os.path.isfile(t.path)]
        with self._lock:
            db = self._conn()
            db.executemany("DELETE FROM tracks WHERE id = ?", [(i,) for i in gone])
            db.commit()
        return len(gone)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
