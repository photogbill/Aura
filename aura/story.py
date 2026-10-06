# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Story mode: the album as a story, the spoiler line, and the prompts the host's story breaks are written from.

THE SPOILER LINE is enforced here, in code, not left to a model's discretion: the break after track N is
written from tracks 1..N only, plus the tease Bill wrote for track N+1 (and its title, which a station
announces anyway). known_through() is the only door to the story's material, and the prompt builders are
pure functions over what it returns, so a test can prove that nothing from a later track gets through.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

from .common import GROUNDING_RULE, NSTR_RULE, SPOKEN_RULE, atomic_write_text, read_text, slugify
from .ingest import load_transcript_text
from .library import Library, Track, parse_filename

log = logging.getLogger(__name__)

STORY_PACK_NAME = "aura_story.json"
DEPTHS = ("id", "short", "liner", "story")

CharacterSheet = Callable[[str], Optional[dict]]

_FULL_DETAIL_TRACKS = 2       # the latest songs carry lyrics and full scene text...
_MAX_LYRICS_CHARS = 4000      # ...within limits, because small local models have small context windows
_MAX_SCENE_CHARS = 2000
_MAX_BRIEF_CHARS = 600        # older songs: summary and the start of the scene only
_MAX_HISTORY_CHARS = 800


@dataclass
class StoryTrack:
    track_id: str
    title: str
    act: str = ""
    characters: list[str] = field(default_factory=list)
    scene: str = ""        # Bill's scene prose (from a lean sheet) - narration is read VERBATIM, no model
    lyrics: str = ""       # sheet lyrics, else the Whisper transcript
    lyrics_source: str = ""   # "sheet" | "transcript" | ""
    summary: str = ""      # the ingest summary (model-written; labelled as such in prompts)
    tease: str = ""        # Bill's line about what comes next - the ONLY thing from N+1 allowed into break N
    notes: str = ""        # director's notes, read verbatim
    file: str = ""         # the audio file, relative to the album folder ("Disc 2/2-01 - X.mp3"): finds the
                           # track again after the album folder is moved (track ids come from the full path)


@dataclass
class Story:
    album: str
    root: str
    tracks: list[StoryTrack]

    @property
    def slug(self) -> str:
        """The album's slug, used for its script file and break ids: from the album folder's name."""
        return story_slug(self.root, self.album)


class NeedsCharacter(Exception):
    """A named character has no sheet or no voice type, so nothing may be generated (Bill's standing rule)."""

    def __init__(self, names: list[str]):
        self.names = list(names)
        super().__init__("character sheet needed for: " + ", ".join(self.names))


# ------------------------------------------------------------------------------------------- files

def _folder_name(album_root: str) -> str:
    return re.split(r"[\\/]+", str(album_root).rstrip("\\/"))[-1] if album_root else ""


def story_slug(album_root: str, album: str = "") -> str:
    """Slug from the album folder's name (falling back to the album name): 'The Turing Accords' ->
    'the-turing-accords'. The folder, not the tag, so story_path() can find it from the folder alone."""
    return slugify(_folder_name(album_root) or album)


def story_path(data_dir: str | Path, album_root: str) -> Path:
    """data_dir/stories/<slug>.json - unless the album folder carries its own aura_story.json (a "story
    pack"), which wins, so a story can travel with its album."""
    pack = Path(album_root) / STORY_PACK_NAME
    if album_root and pack.is_file():
        return pack
    return Path(data_dir) / "stories" / f"{story_slug(album_root)}.json"


def draft_story(library: Library, album_root: str) -> Story:
    """A first draft of an album's story from the library: track order, characters from the performers,
    lyrics from the transcript, summary and notes from the track. Bill fills in acts, scenes and teases."""
    tracks = library.tracks(root=album_root)
    if not tracks:
        raise ValueError(f"no scanned tracks under {album_root}; scan the folder first")
    tracks.sort(key=lambda t: (t.disc_no or 0, t.track_no is None, t.track_no or 0,
                               os.path.basename(t.path).casefold()))
    albums = Counter(t.album for t in tracks if t.album)
    album = albums.most_common(1)[0][0] if albums else _folder_name(album_root)
    story_tracks = []
    for t in tracks:
        lyrics = load_transcript_text(library.data_dir, t)
        story_tracks.append(StoryTrack(
            track_id=t.id, title=t.title, characters=list(t.performers), lyrics=lyrics,
            lyrics_source="transcript" if lyrics else "", summary=t.summary, notes=t.notes,
            file=_relative_file(t.path, album_root)))
    return Story(album=album, root=os.path.abspath(album_root), tracks=story_tracks)


def _relative_file(path: str, folder: str) -> str:
    try:
        return Path(os.path.relpath(os.path.abspath(path), os.path.abspath(folder))).as_posix()
    except ValueError:  # another drive on Windows
        return os.path.basename(path)


def _ancestor_named(path: str, name_key: str) -> str | None:
    """The nearest folder above `path` whose name is `name_key` (case-insensitive), or None."""
    folder = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.basename(folder).casefold() == name_key:
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def _unique(candidates: list) -> Any:
    return candidates[0] if len(candidates) == 1 else None


def story_relinks(story: Story, library: Library) -> tuple[dict[str, str], str | None]:
    """({old track id: new track id}, new album folder or None) for story tracks whose id is no longer in
    the library. Reads only: neither the story nor the library is changed.

    Track ids are the sha1 of the full path, so moving or renaming an album folder changes every id. A lost
    track is found again by, in order: the album folder's name plus its file path inside it; its file name
    when that is unique in the library; the album folder's name plus its title when that is unique. Anything
    ambiguous is left alone (and logged) rather than guessed.
    """
    tracks = library.tracks()
    known = {t.id for t in tracks}
    lost = [st for st in story.tracks if st.track_id not in known]
    if not lost:
        return {}, None
    folder_key = _folder_name(story.root).casefold()
    by_file: dict[str, list[tuple[Track, str]]] = {}
    by_title: dict[str, list[tuple[Track, str]]] = {}
    by_name: dict[str, list[Track]] = {}
    for t in tracks:
        by_name.setdefault(os.path.basename(t.path).casefold(), []).append(t)
        album_dir = _ancestor_named(t.path, folder_key) if folder_key else None
        if album_dir:
            by_file.setdefault(_relative_file(t.path, album_dir).casefold(), []).append((t, album_dir))
            by_title.setdefault(_match_key(t.title), []).append((t, album_dir))
    taken = {st.track_id for st in story.tracks if st.track_id in known}
    mapping: dict[str, str] = {}
    new_dirs: set[str] = set()
    for st in lost:
        hit = _unique(by_file.get(st.file.casefold(), [])) if st.file else None
        if hit is None and st.file:
            only = _unique(by_name.get(os.path.basename(st.file).casefold(), []))
            hit = (only, "") if only is not None else None
        if hit is None:
            hit = _unique(by_title.get(_match_key(st.title), []))
        if hit is None or hit[0].id in taken:
            log.warning("story track %r (%s) is not in the library and could not be found again", st.title, st.file)
            continue
        mapping[st.track_id] = hit[0].id
        taken.add(hit[0].id)
        if hit[1]:
            new_dirs.add(hit[1])
    return mapping, (new_dirs.pop() if len(new_dirs) == 1 else None)


def apply_relinks(story: Story, mapping: dict[str, str], new_root: str | None) -> None:
    """Rewrite the story's track ids by `mapping`; follow a moved album folder only if it kept its name."""
    for st in story.tracks:
        st.track_id = mapping.get(st.track_id, st.track_id)
    if new_root and os.path.basename(new_root).casefold() == _folder_name(story.root).casefold():
        story.root = new_root


def relink_story(story: Story, library: Library, store: Any = None) -> int:
    """Point a story's tracks at their files again after the album folder moved; returns how many were
    relinked. Rewrites the story's track ids in place (save the story afterwards to keep it).

    When the folder was moved but kept its name, story.root follows it. A renamed folder keeps the old
    root, so the album's slug - and with it the story's script file and break ids - stays the same. With a
    BreakStore, the casual intro/back breaks (and Bill's own takes for them) are renamed to the new ids too;
    story breaks need nothing, as their ids use the album slug and the track's position, not its id.
    """
    mapping, new_root = story_relinks(story, library)
    apply_relinks(story, mapping, new_root)
    if store is not None and mapping:
        from .breaks import relink_breaks  # imported here: breaks imports this module
        relink_breaks(store, mapping)
    return len(mapping)


_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_SCENE_HEADING_RE = re.compile(r"^\s*#{1,6}\s*\**\s*scene\b\s*\**\s*:?\s*(.*)$", re.IGNORECASE)
_SCENE_LABEL_RE = re.compile(r"^\s*\**\s*scene\s*\**\s*:\s*\**\s*(.*)$", re.IGNORECASE)
_LABEL_ONLY_RE = re.compile(r"^\s*\**\s*(lyrics|lyric body|style|style prompt)\s*\**\s*:?\s*\**\s*$", re.IGNORECASE)
_STYLE_LINE_RE = re.compile(r"^\s*\**\s*style(\s+prompt)?\s*\**\s*:", re.IGNORECASE)
_RULE_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_SUNO_TAG_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")


def _tidy(lines: list[str]) -> str:
    """Trailing spaces off, runs of blank lines down to one, no blank lines at either end."""
    out: list[str] = []
    for line in (l.rstrip() for l in lines):
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def parse_lean_sheet(text: str) -> tuple[str, str]:
    """(scene, lyrics) from one of Bill's lean sheets.

    Blockquote lines ("> ") are the Suno style prompt and are dropped: they describe sound, not story. A
    "## Scene" heading or a "Scene:" line starts the scene prose, which runs until the next heading, label,
    rule or Suno [tag] line. Everything else, minus markdown headings and "Lyrics:"/"Style:" labels, is lyrics.
    """
    scene: list[str] = []
    lyrics: list[str] = []
    in_scene = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.lstrip().startswith(">"):
            continue
        m = _SCENE_HEADING_RE.match(line) or _SCENE_LABEL_RE.match(line)
        if m:
            in_scene = True
            if m.group(1).strip():
                scene.append(m.group(1).strip())
            continue
        if in_scene and (_HEADING_RE.match(line) or _LABEL_ONLY_RE.match(line) or _STYLE_LINE_RE.match(line)
                         or _RULE_RE.match(line) or _SUNO_TAG_RE.match(line)):
            in_scene = False
        if in_scene:
            scene.append(line)
            continue
        if _HEADING_RE.match(line) or _LABEL_ONLY_RE.match(line) or _STYLE_LINE_RE.match(line) or _RULE_RE.match(line):
            continue
        lyrics.append(line)
    return _tidy(scene), _tidy(lyrics)


def _match_key(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def attach_sheets(story: Story, folder: str | os.PathLike) -> int:
    """Attach lean sheets found in `folder` (and below it) to the story's tracks; returns how many matched.

    A .md or .txt file matches a track when its name, or the title parsed from its name the way audio files
    are parsed, equals the track's title - so "07 - The Velvet Lesson (Aphrodite).md" beside the audio file
    of the same stem matches, and so does "The Velvet Lesson.txt". The sheet's scene goes to `scene`, its
    lyrics replace the transcript (lyrics_source "sheet").
    """
    index: dict[str, Path] = {}
    for path in sorted(Path(folder).rglob("*"), key=lambda p: str(p).casefold()):
        if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
            continue
        for key in (_match_key(path.stem), _match_key(parse_filename(path.name)["title"])):
            if key and key not in index:
                index[key] = path
    matched = 0
    for track in story.tracks:
        sheet = index.get(_match_key(track.title))
        if sheet is None:
            continue
        try:
            scene, lyrics = parse_lean_sheet(read_text(sheet))
        except OSError as exc:
            log.warning("cannot read lean sheet %s: %s", sheet, exc)
            continue
        if scene:
            track.scene = scene
        if lyrics:
            track.lyrics, track.lyrics_source = lyrics, "sheet"
        matched += 1
    return matched


def save_story(story: Story, path: str | os.PathLike) -> None:
    """JSON, UTF-8, indent 2 - readable and editable by hand."""
    data = {"format": "aura-story", "version": 1, "album": story.album, "root": story.root,
            "tracks": [asdict(t) for t in story.tracks]}
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_story(path: str | os.PathLike, library: Optional[Library] = None) -> Story:
    """Read a story file. Unknown keys are ignored (and logged); a story pack without a root gets its folder.
    With a library, tracks whose files have moved are relinked (see relink_story)."""
    data = json.loads(read_text(path))
    known = {f.name for f in fields(StoryTrack)}
    tracks: list[StoryTrack] = []
    for i, entry in enumerate(data.get("tracks") or []):
        extra = sorted(set(entry) - known)
        if extra:
            log.warning("%s track %d: ignoring unknown keys %s", path, i + 1, ", ".join(extra))
        if not entry.get("track_id") or "title" not in entry:
            raise ValueError(f"{path}: track {i + 1} needs a track_id and a title")
        kwargs = {k: entry[k] for k in known if k in entry}
        chars = kwargs.get("characters") or []
        kwargs["characters"] = [chars] if isinstance(chars, str) else [str(c) for c in chars]
        for key in known - {"characters"}:
            if key in kwargs:
                kwargs[key] = "" if kwargs[key] is None else str(kwargs[key])
        tracks.append(StoryTrack(**kwargs))
    root = str(data.get("root") or "")
    if not root and Path(path).name == STORY_PACK_NAME:
        root = os.path.dirname(os.path.abspath(path))
    story = Story(album=str(data.get("album") or ""), root=root, tracks=tracks)
    if library is not None:
        relinked = relink_story(story, library)
        if relinked:
            log.info("%s: %d track(s) relinked to moved files; save the story to keep it", path, relinked)
    return story


# ---------------------------------------------------------------------------------- the spoiler line

def known_through(story: Story, n: int, include_next_title: bool = True) -> dict:
    """PURE - THE SPOILER LINE. Everything the host may know after track n (0-based) has finished:

    {"heard": [tracks 0..n as dicts], "tease": tracks[n+1].tease or "", "next_title": tracks[n+1].title or ""}

    Nothing else from n+1 onward: not its scene, lyrics, summary, characters, notes or act. The title is
    allowed because a station says what is coming next by name; include_next_title=False withholds it.
    """
    if not 0 <= n < len(story.tracks):
        raise IndexError(f"track index {n} is outside this story (0..{len(story.tracks) - 1})")
    heard = [asdict(t) for t in story.tracks[: n + 1]]  # copies: a prompt builder cannot touch the story
    following = story.tracks[n + 1] if n + 1 < len(story.tracks) else None
    return {
        "heard": heard,
        "tease": following.tease if following is not None else "",
        "next_title": following.title if (following is not None and include_next_title) else "",
    }


def _lookup_sheet(character_sheet: Optional[CharacterSheet], name: str) -> dict | None:
    if character_sheet is None:
        return None
    try:
        sheet = character_sheet(name)
    except Exception as exc:  # the host's lookup failed: treat as "no sheet", which blocks generation
        log.warning("character sheet lookup failed for %s: %s", name, exc)
        return None
    return sheet if isinstance(sheet, dict) else None


def _sheets_and_missing(heard: list[dict], character_sheet: Optional[CharacterSheet]) -> tuple[dict, list[str]]:
    names: list[str] = []
    seen: set[str] = set()
    for t in heard:
        for name in t.get("characters") or []:
            name = str(name).strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
    sheets: dict[str, dict] = {}
    missing: list[str] = []
    for name in names:
        sheet = _lookup_sheet(character_sheet, name)
        if sheet is None or not str(sheet.get("voice_type") or "").strip():
            missing.append(name)
        else:
            sheets[name] = sheet
    return sheets, missing


def characters_needed(story: Story, n: int, character_sheet: Optional[CharacterSheet]) -> list[str]:
    """Names appearing in tracks 0..n whose sheet is missing or has no voice_type (first-appearance order)."""
    return _sheets_and_missing(known_through(story, n)["heard"], character_sheet)[1]


# ------------------------------------------------------------------------------------------ prompts

_DEPTH_GUIDE = {
    "id": "Length: one short line, under 15 words: name what just played and, if given, what is coming up. "
          "No story.",
    "short": "Length: one or two sentences, under 35 words.",
    "liner": "Length: two or three sentences, under 60 words, like a liner note: one true detail from the "
             "songs heard so far.",
    "story": "Length: a short spoken bridge, under 120 words, that carries the listener from the song that "
             "just ended towards the next one, drawing on the story so far.",
}
_APPROX_RULE = (
    "Summaries were written by a model from machine-heard lyrics, and lyrics marked as a machine transcript "
    "were machine-heard: both may contain mistakes, so never quote them as exact words. The owner's scene "
    "prose, lyric sheets, notes, teases and character sheets are true as written."
)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ...(cut short)"


def _song_lines(position: int, t: dict, full: bool) -> list[str]:
    lines = [f'Song {position}: "{t.get("title", "")}"']
    if t.get("act"):
        lines.append(f"Act: {t['act']}")
    if t.get("characters"):
        lines.append("Characters: " + ", ".join(str(c) for c in t["characters"]))
    if t.get("scene"):
        lines.append("Scene (the owner's own words): "
                     + _clip(t["scene"], _MAX_SCENE_CHARS if full else _MAX_BRIEF_CHARS))
    if full and t.get("lyrics"):
        source = ("from the owner's lyric sheet" if t.get("lyrics_source") == "sheet"
                  else "a machine transcript; words may be misheard")
        lines += [f"Lyrics ({source}):", _clip(t["lyrics"], _MAX_LYRICS_CHARS)]
    if t.get("summary"):
        lines.append("Summary (written by a model from the transcript; approximate): "
                     + _clip(t["summary"], _MAX_SCENE_CHARS if full else _MAX_BRIEF_CHARS))
    if t.get("notes"):
        lines.append("Director's notes (the owner's, true as written): " + _clip(t["notes"], _MAX_SCENE_CHARS))
    return lines


def _heard_lines(heard: list[dict]) -> list[str]:
    lines: list[str] = []
    for i, t in enumerate(heard):
        lines += _song_lines(i + 1, t, full=i >= len(heard) - _FULL_DETAIL_TRACKS)
        lines.append("")
    return lines


def _character_lines(sheets: dict) -> list[str]:
    if not sheets:
        return []
    lines = ["CHARACTERS (from the owner's character sheets):"]
    for name, sheet in sheets.items():
        line = f"- {name}: voice type: {str(sheet.get('voice_type') or '').strip()}."
        history = str(sheet.get("history") or "").strip()
        if history:
            line += " History: " + _clip(history, _MAX_HISTORY_CHARS)
        lines.append(line)
    return lines + [""]


def segue_prompt(story: Story, n: int, *, character_sheet: Optional[CharacterSheet], host: str = "the host",
                 depth: str = "story", include_next_title: bool = True) -> tuple[str, str]:
    """(system, user) for the segue the host says after track n (0-based) and before track n+1.

    PURE except the injected character_sheet lookup. Built ONLY from known_through(story, n) and the sheets
    of the characters in tracks 0..n. Raises NeedsCharacter when any of those characters lacks a sheet or a
    voice type. depth is "id" | "short" | "liner" | "story". Ends with the NSTR rule.
    """
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {DEPTHS}")
    known = known_through(story, n, include_next_title=include_next_title)
    sheets, missing = _sheets_and_missing(known["heard"], character_sheet)
    if missing:
        raise NeedsCharacter(missing)
    heard = known["heard"]
    just = heard[-1]["title"]
    has_next = n + 1 < len(story.tracks)
    system = "\n".join([
        f"You are {host}, the voice of a small radio station that is playing a story album one song at a time.",
        "You are writing the words you will say on air between two songs. They are recorded ahead of time "
        "and played back exactly as written.",
        "Use ONLY the material in the user message. It is everything you know about this story: you have "
        "heard the songs listed there and nothing after them. Never guess at, hint at or foreshadow anything "
        "later, beyond what the COMING UP lines tell you.",
        _APPROX_RULE,
        GROUNDING_RULE,
        SPOKEN_RULE,
        _DEPTH_GUIDE[depth],
        NSTR_RULE,
    ])
    lines = [f"ALBUM: {story.album or '(no name given)'}", "", "SONGS HEARD SO FAR, IN ORDER:", ""]
    lines += _heard_lines(heard)
    lines += _character_lines(sheets)
    lines.append(f'JUST FINISHED: song {n + 1}, "{just}"')
    if known["next_title"]:
        lines.append(f'COMING UP NEXT: song {n + 2}, "{known["next_title"]}"')
    elif has_next:
        lines.append(f"COMING UP NEXT: song {n + 2} (its title is not given)")
    else:
        lines.append("This was the last song of the album.")
    if known["tease"]:
        lines.append(f"THE OWNER'S TEASE FOR WHAT COMES NEXT: {known['tease']}")
    if has_next:
        lines.append("Nothing else about what comes next is known to you.")
    task = f'TASK: write what {host} says now, after "{just}"'
    if known["next_title"]:
        task += f' and before "{known["next_title"]}"'
    lines += ["", task + f". Depth: {depth}.", NSTR_RULE]
    return system, "\n".join(lines)


def recap_prompt(story: Story, n: int, *, character_sheet: Optional[CharacterSheet],
                 host: str = "the host") -> tuple[str, str]:
    """(system, user) for a "previously on..." recap of tracks 0..n, played when a listener comes back.

    Same spoiler line (and nothing at all from n+1 - not even the tease), same NeedsCharacter rule, NSTR rule.
    """
    known = known_through(story, n)  # tease and next_title deliberately unused: a recap covers what was heard
    sheets, missing = _sheets_and_missing(known["heard"], character_sheet)
    if missing:
        raise NeedsCharacter(missing)
    heard = known["heard"]
    system = "\n".join([
        f"You are {host}, the voice of a small radio station that is playing a story album one song at a time.",
        "The listener is coming back after a break. Before the next song you remind them where the story "
        "stands: a short 'previously on' recap. It is recorded ahead of time and played exactly as written.",
        "Use ONLY the material in the user message. It is everything you know about this story: you have "
        "heard the songs listed there and nothing after them. Do not guess at or hint at what comes next.",
        _APPROX_RULE,
        GROUNDING_RULE,
        SPOKEN_RULE,
        "Length: under 90 words.",
        NSTR_RULE,
    ])
    lines = [f"ALBUM: {story.album or '(no name given)'}", "", "SONGS HEARD SO FAR, IN ORDER:", ""]
    lines += _heard_lines(heard)
    lines += _character_lines(sheets)
    lines += [f'TASK: recap the story so far, through song {n + 1}, "{heard[-1]["title"]}".', NSTR_RULE]
    return system, "\n".join(lines)
