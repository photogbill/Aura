# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Music sources: what the library is made of, as the rules Bill picks in the music browser - a folder, with or
without the folders below it (chosen per folder, never assumed), the file types to take from it, every copy of a
song or one copy per song; or one song on its own.

Why rules and not a list of files: a folder rule picks up the songs dropped into that folder later, at the next
scan. Why subfolders per folder and OFF by default: Bill, 2026-10-12 - pick a whole folder "and not accidentally
select subfolders if i don't want to". Why the file types travel with each rule: some of his folders hold the
same songs as MP3, FLAC and WAV, and only the MP3s are wanted from those.

Why a drive's label is kept: in the field, music arrives on USB sticks, and Windows gives one stick E: today and
F: tomorrow. The host (ATK) reads the label and moves the rule to the new letter; the engine only carries it.

A source that is not there right now (the stick is not plugged in) is SKIPPED by sync, and none of its songs
leave the library: their transcripts, notes and intros wait for it. Overlapping rules are allowed - a song is
indexed once, by the union of what the rules take - and overlaps() names them so the browser can say so.

SourceSet is the browser's working copy: what each click does and what colour each folder and file is shown in,
as plain logic, so the tests drive exactly what the mouse drives.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional

from .common import is_cancelled, is_under, say
from .library import AUDIO_EXTS, Library, ScanSummary, norm_ext, preference, select_files, walk_audio

log = logging.getLogger(__name__)

KINDS = ("folder", "file")

#: How a folder shows in the browser, by how it was selected (the host picks the colours).
FOLDER_STATES = ("rule", "rule_sub", "overlap", "reached", "picked", "contains", "")
#: How a file shows: taken by a folder rule, in a rule's folder but left out by its file types, picked on its
#: own, or not selected.
FILE_STATES = ("by_rule", "left_out", "picked", "")


def _norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


@dataclass
class Source:
    path: str
    kind: str = "folder"
    subfolders: bool = False          # folder rules only: also every folder below it
    types: tuple[str, ...] = ()       # extensions to take, e.g. (".mp3",); () = every audio type
    one_copy: str = ""                # "" = every copy; ".mp3" = one copy per song, the MP3 when there is one
    volume: str = ""                  # the drive's label when the rule was made (filled in by the host)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"a source is a folder or a file, not {self.kind!r}")
        self.path = os.path.abspath(os.fspath(self.path))
        types = tuple(dict.fromkeys(norm_ext(t) for t in (self.types or ()) if t))
        bad = [t for t in types if t not in AUDIO_EXTS]
        if bad:
            raise ValueError(f"not audio file types: {', '.join(bad)}")
        self.types = types
        self.one_copy = norm_ext(self.one_copy) if self.one_copy else ""
        if self.one_copy and self.one_copy not in AUDIO_EXTS:
            raise ValueError(f"not an audio file type: {self.one_copy}")
        if self.kind == "file":
            self.subfolders, self.types, self.one_copy = False, (), ""

    # -- storage ---------------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "subfolders": self.subfolders, "types": list(self.types),
                "one_copy": self.one_copy, "volume": self.volume}

    @classmethod
    def from_dict(cls, data: Any) -> "Source":
        """From settings JSON. Raises ValueError for anything that is not a usable source."""
        if not isinstance(data, dict) or not data.get("path"):
            raise ValueError(f"not a music source: {data!r}")
        return cls(path=str(data["path"]), kind=str(data.get("kind") or "folder"),
                   subfolders=bool(data.get("subfolders")), types=tuple(data.get("types") or ()),
                   one_copy=str(data.get("one_copy") or ""), volume=str(data.get("volume") or ""))

    # -- where it reaches ------------------------------------------------------------------------------
    def key(self) -> tuple[str, str]:
        return self.kind, _norm(self.path)

    def available(self) -> bool:
        return os.path.isdir(self.path) if self.kind == "folder" else os.path.isfile(self.path)

    def folder(self) -> str:
        """The folder its songs are filed under (Track.root): itself, or a file's own folder."""
        return self.path if self.kind == "folder" else os.path.dirname(self.path)

    def in_scope(self, path: str | os.PathLike) -> bool:
        """Is `path` in the place this rule looks (whatever its file types)?"""
        if self.kind == "file":
            return _norm(path) == _norm(self.path)
        if self.subfolders:
            return is_under(path, self.path)
        return _norm(os.path.dirname(os.path.abspath(os.fspath(path)))) == _norm(self.path)

    def reaches_folder(self, folder: str | os.PathLike) -> bool:
        """Does this rule take songs from `folder` itself (as opposed to a folder above or beside it)?"""
        if self.kind != "folder":
            return False
        return is_under(folder, self.path) if self.subfolders else _norm(folder) == _norm(self.path)

    def takes_type(self, path: str | os.PathLike) -> bool:
        return not self.types or os.path.splitext(os.fspath(path))[1].lower() in self.types

    def filters(self) -> dict:
        """The select_files() arguments for this rule's file types and one-copy choice."""
        return {"only": self.types or None, "prefer": preference(self.one_copy) if self.one_copy else None}

    def describe(self) -> str:
        """'MP3 · this folder only', 'every type, one copy (MP3 first) · with subfolders', 'one song'."""
        if self.kind == "file":
            return "one song"
        kinds = " + ".join(t.lstrip(".").upper() for t in self.types) if self.types else "every type"
        if self.one_copy:
            kinds += f", one copy per song ({self.one_copy.lstrip('.').upper()} first)"
        return f"{kinds} · {'with subfolders' if self.subfolders else 'this folder only'}"


def files_for(source: Source, *, skip_under: Any = None) -> tuple[list[str], list[str]]:
    """(found, kept): every audio file in the rule's place, and the ones its file types and one-copy choice
    keep. A rule that is not there gives ([], [])."""
    if not source.available():
        return [], []
    if source.kind == "file":
        found = [source.path] if os.path.splitext(source.path)[1].lower() in AUDIO_EXTS else []
    else:
        found = [os.path.join(folder, name) for folder, names in
                 walk_audio(source.path, subfolders=source.subfolders, skip_under=skip_under) for name in names]
    return found, select_files(found, **source.filters())


def resolve(sources: Iterable[Source], *, skip_under: Any = None) -> tuple[list[tuple[str, str]], int]:
    """([(path, root)], how many audio files the rules saw before their filters). Each file once, in the order
    the rules find them; a file two rules take is filed under the more specific (deeper) rule's folder."""
    entries: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    for source in sources:
        found, kept = files_for(source, skip_under=skip_under)
        seen.update(_norm(p) for p in found)
        root = source.folder()
        for path in kept:
            k = _norm(path)
            if k not in entries or len(root) > len(entries[k][1]):
                entries[k] = (path, root)
    return list(entries.values()), len(seen)


def sync(library: Library, sources: Iterable[Source], *, progress: Optional[Callable[[str], Any]] = None,
         cancel: Any = None, measure: bool = True) -> ScanSummary:
    """Scan every source that is there, as one run: index what the rules take; remove only what a rule that is
    there no longer takes (a deleted song, a copy its file types now leave out - their work moves to the copy
    that stays). A source that is not there is reported in summary.unavailable and nothing of it is touched."""
    sources = list(sources)
    present = [s for s in sources if s.available()]
    missing = [s for s in sources if not s.available()]
    for s in missing:
        say(progress, f"Not there right now, skipped (its songs stay in the library): {s.path}")
        log.warning("music source not available, skipped and nothing removed: %s (%s)", s.path, s.describe())
    log.info("sync: %d source(s), %d there: %s", len(sources), len(present),
             "; ".join(f"{s.path} [{s.describe()}]" for s in present) or "-")
    entries, found = resolve(present, skip_under=library.data_dir)

    def in_scope(path: str) -> bool:
        return any(s.in_scope(path) for s in present)

    summary = library.index(entries, in_scope=in_scope, found=found, progress=progress, cancel=cancel,
                            measure=measure)
    summary.unavailable = [s.path for s in missing]
    return summary


def overlaps(sources: Iterable[Source]) -> list[tuple[Source, Source]]:
    """(inner, outer): a rule whose place another rule already reaches. Not an error - each song is indexed
    once, by the union of what the rules take - but an inner rule's file types then change nothing the outer
    rule does not already take, which is worth saying."""
    items = list(sources)
    pairs = []
    for inner in items:
        for outer in items:
            if inner is outer or outer.kind != "folder" or inner.key() == outer.key():
                continue
            hit = outer.reaches_folder(inner.path) if inner.kind == "folder" else outer.in_scope(inner.path)
            if hit:
                pairs.append((inner, outer))
    return pairs


# ---------------------------------------------------------------------------------------------- counting

@dataclass
class SourceCount:
    """What one rule takes, for the browser's running total."""
    kept: dict[str, int] = field(default_factory=dict)       # extension -> songs taken
    left_out: dict[str, int] = field(default_factory=dict)   # extension -> audio files its filters leave out
    folders: int = 0                                         # folders it reaches (itself and below)
    bytes: int = 0
    cancelled: bool = False
    stopped_early: bool = False                              # passed `stop_after`: at least this much

    def total(self) -> int:
        return sum(self.kept.values())


def count(source: Source, *, cancel: Any = None, skip_under: Any = None,
          stop_after: tuple[int, int] | None = None) -> SourceCount:
    """Count what a rule would take, without indexing anything (stat only, no tags, no FFmpeg).

    stop_after=(files, folders) stops walking once either is reached and sets stopped_early - the browser's
    "that is a lot, are you sure?" check, which must answer at once even for a whole drive."""
    result = SourceCount()
    if not source.available():
        return result
    if source.kind == "file":
        found = [source.path] if os.path.splitext(source.path)[1].lower() in AUDIO_EXTS else []
        result.folders = 0
    else:
        found = []
        for folder, names in walk_audio(source.path, subfolders=source.subfolders, skip_under=skip_under):
            if is_cancelled(cancel):
                result.cancelled = True
                return result
            result.folders += 1
            found.extend(os.path.join(folder, n) for n in names)
            if stop_after and (len(found) >= stop_after[0] or result.folders >= stop_after[1]):
                result.stopped_early = True
                break
    kept = select_files(found, **source.filters())
    kept_keys = {_norm(p) for p in kept}
    for path in found:
        ext = os.path.splitext(path)[1].lower()
        if _norm(path) in kept_keys:
            result.kept[ext] = result.kept.get(ext, 0) + 1
            try:
                result.bytes += os.path.getsize(path)
            except OSError:
                pass
        else:
            result.left_out[ext] = result.left_out.get(ext, 0) + 1
    return result


# ---------------------------------------------------------------------------------------------- the browser

class SourceSet:
    """The music browser's working copy of the sources. Every click is one of these methods."""

    def __init__(self, sources: Iterable[Source] = ()):
        self.sources: list[Source] = []
        for s in sources:
            if self.find(s.path, s.kind) is None:
                self.sources.append(s)

    def find(self, path: str | os.PathLike, kind: str = "folder") -> Source | None:
        key = (kind, _norm(path))
        return next((s for s in self.sources if s.key() == key), None)

    # -- edits -------------------------------------------------------------------------------------------
    def toggle_folder(self, folder: str | os.PathLike, *, types: Iterable[str] = (), one_copy: str = "",
                      volume: str = "") -> bool:
        """Tick or untick a folder. A tick takes THIS folder only, with the file types given; subfolders are
        a separate, deliberate step (set_subfolders). Returns whether the folder is now selected."""
        existing = self.find(folder, "folder")
        if existing is not None:
            self.sources.remove(existing)
            log.debug("browser: folder untagged %s", existing.path)
            return False
        source = Source(path=os.fspath(folder), kind="folder", subfolders=False, types=tuple(types),
                        one_copy=one_copy, volume=volume)
        self.sources.append(source)
        log.debug("browser: folder tagged %s [%s]", source.path, source.describe())
        return True

    def set_subfolders(self, folder: str | os.PathLike, on: bool) -> Source | None:
        """Turn 'and every folder below it' on or off for a ticked folder (None when it is not ticked)."""
        existing = self.find(folder, "folder")
        if existing is None:
            return None
        updated = replace(existing, subfolders=bool(on))
        self.sources[self.sources.index(existing)] = updated
        log.debug("browser: subfolders %s for %s", "on" if on else "off", updated.path)
        return updated

    def set_filters(self, folder: str | os.PathLike, *, types: Iterable[str], one_copy: str = "") -> Source | None:
        existing = self.find(folder, "folder")
        if existing is None:
            return None
        updated = replace(existing, types=tuple(types), one_copy=one_copy)
        self.sources[self.sources.index(existing)] = updated
        return updated

    def toggle_file(self, path: str | os.PathLike, volume: str = "") -> bool:
        """Pick or unpick one song on its own. A song a folder rule already takes is not picked again
        (returns True: it is in)."""
        existing = self.find(path, "file")
        if existing is not None:
            self.sources.remove(existing)
            return False
        if self.taken_by_rule(path) is not None:
            return True
        self.sources.append(Source(path=os.fspath(path), kind="file", volume=volume))
        return True

    def set_files(self, paths: Iterable[str], on: bool, volume: str = "") -> int:
        """Pick (or unpick) several songs at once - XTree's Ctrl+T / Ctrl+U. Returns how many changed."""
        changed = 0
        for path in paths:
            picked = self.find(path, "file") is not None
            if on and not picked and self.taken_by_rule(path) is None:
                self.sources.append(Source(path=os.fspath(path), kind="file", volume=volume))
                changed += 1
            elif not on and picked:
                self.sources.remove(self.find(path, "file"))
                changed += 1
        return changed

    def remove(self, source: Source) -> None:
        self.sources = [s for s in self.sources if s.key() != source.key()]

    # -- what each thing looks like ----------------------------------------------------------------------
    def taken_by_rule(self, path: str | os.PathLike) -> Source | None:
        """The folder rule that takes this file (by place and file type), if any."""
        for s in self.sources:
            if s.kind == "folder" and s.in_scope(path) and s.takes_type(path):
                return s
        return None

    def folder_state(self, folder: str | os.PathLike) -> str:
        """One of FOLDER_STATES: ticked ("rule" / "rule_sub", or "overlap" when another rule already reaches
        it), "reached" by a rule above it, holding songs picked on their own ("picked"), "contains" a rule
        somewhere below it (a trail to follow in a collapsed tree), or ""."""
        here = _norm(folder)
        own = self.find(folder, "folder")
        reached_by = [s for s in self.sources if s.kind == "folder" and s is not own and s.subfolders
                      and is_under(folder, s.path) and _norm(s.path) != here]
        if own is not None:
            return "overlap" if reached_by else ("rule_sub" if own.subfolders else "rule")
        if reached_by:
            return "reached"
        if any(s.kind == "file" and _norm(os.path.dirname(s.path)) == here for s in self.sources):
            return "picked"
        if any(is_under(s.path, folder) and _norm(s.path) != here for s in self.sources):
            return "contains"
        return ""

    def file_state(self, path: str | os.PathLike) -> str:
        """One of FILE_STATES."""
        if self.find(path, "file") is not None:
            return "picked"
        if self.taken_by_rule(path) is not None:
            return "by_rule"
        if any(s.kind == "folder" and s.in_scope(path) for s in self.sources):
            return "left_out"
        return ""

    def summary(self) -> dict:
        """{"folders", "with_subfolders", "songs"}: how many rules of each kind."""
        folders = [s for s in self.sources if s.kind == "folder"]
        return {"folders": len(folders), "with_subfolders": sum(1 for s in folders if s.subfolders),
                "songs": sum(1 for s in self.sources if s.kind == "file")}
