# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Command line, for using and checking the engine without ATK:

    python -m aura scan <folder> [--data DIR] [--ffmpeg PATH] [--no-measure] [--only wav | --prefer wav]
    python -m aura tracks [--album NAME]
    python -m aura playlist <out.m3u8> [--album NAME]
    python -m aura story-draft <album_root>
    python -m aura script-check <script> [--voice NAME]

Everything is kept in <repo>/data unless --data says otherwise: never in the user profile.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .common import read_text
from .library import Library, preference
from .playlist import playlists_dir, write_m3u8
from .script import STATUSES, lint_script, parse_script, refresh_status
from .story import STORY_PACK_NAME, attach_sheets, draft_story, save_story, story_path


def default_data_dir() -> Path:
    """<repo>/data - the folder beside the aura package, because Bill's data lives beside his code."""
    return Path(__file__).resolve().parent.parent / "data"


def _ffmpeg(args: argparse.Namespace) -> str | None:
    """ffmpeg from --ffmpeg, else from PATH (the engine itself never goes looking; the host passes it)."""
    if args.no_ffmpeg:
        return None
    return args.ffmpeg or shutil.which("ffmpeg")


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds or 0)), 60)
    return f"{minutes}:{secs:02d}"


def cmd_scan(args: argparse.Namespace, data: Path) -> int:
    ffmpeg = _ffmpeg(args)
    if not ffmpeg:
        print("ffmpeg not found: no loudness measurement; lengths come from tags or WAV headers only.")
    with Library(data, ffmpeg) as lib:
        only = (args.only,) if args.only else None
        prefer = preference(args.prefer) if args.prefer else None
        summary = lib.scan(args.folder, progress=print, measure=not args.no_measure, only=only, prefer=prefer)
        if summary.carried:
            from .breaks import BreakStore, relink_breaks
            relink_breaks(BreakStore(data), summary.carried)
    for failure in summary.failed:
        print(f"FAILED: {failure}")
    return 0


def cmd_tracks(args: argparse.Namespace, data: Path) -> int:
    with Library(data) as lib:
        tracks = lib.tracks(root=args.root, album=args.album)
    if not tracks:
        print("No tracks. Scan a folder first: python -m aura scan <folder>")
        return 1
    for t in tracks:
        number = "" if t.track_no is None else (f"{t.disc_no}-{t.track_no:02d}" if t.disc_no else f"{t.track_no:02d}")
        loud = "-" if t.lufs is None else f"{t.lufs:.1f} LUFS"
        sung = ", ".join(t.performers) or "-"
        print(f"{t.id}  {t.album} | {number or '--'} | {t.title} | {sung} | {_clock(t.duration_s)} | {loud}")
    print(f"{len(tracks)} track(s)")
    return 0


def cmd_playlist(args: argparse.Namespace, data: Path) -> int:
    with Library(data) as lib:
        tracks = lib.tracks(album=args.album)
    if not tracks:
        print("No tracks to put in a playlist" + (f" for album {args.album!r}." if args.album else "."))
        return 1
    out = Path(args.out)
    if not out.is_absolute() and out.parent == Path("."):
        out = playlists_dir(data) / out  # a bare file name goes to data/playlists
    write_m3u8(out, tracks, relative=not args.absolute)
    print(f"Wrote {len(tracks)} track(s) to {out}")
    return 0


def cmd_story_draft(args: argparse.Namespace, data: Path) -> int:
    target = story_path(data, args.album_root)
    if target.name == STORY_PACK_NAME:
        print(f"This album carries its own story pack: {target}\nEdit that file; it is never overwritten.")
        return 1
    if target.exists() and not args.force:
        print(f"A story already exists: {target}\nUse --force to replace it with a fresh draft.")
        return 1
    with Library(data) as lib:
        try:
            story = draft_story(lib, args.album_root)
        except ValueError as exc:
            print(exc)
            return 1
    matched = attach_sheets(story, args.album_root)
    save_story(story, target)
    characters = sorted({c for t in story.tracks for c in t.characters}, key=str.casefold)
    print(f"Drafted {len(story.tracks)} track(s) of {story.album!r}; {matched} lean sheet(s) attached.")
    print("Characters: " + (", ".join(characters) or "none"))
    print(f"Saved to {target} - fill in acts, scenes and teases there.")
    return 0


def cmd_script_check(args: argparse.Namespace, data: Path) -> int:
    path = Path(args.script)
    if not path.is_file() and path.suffix.lower() != ".txt":
        path = data / "scripts" / f"{args.script}.txt"
    if not path.is_file():
        print(f"No script at {path}")
        return 2
    text = read_text(path)
    breaks = parse_script(text)
    counts = {status: 0 for status in STATUSES}
    stale: list[str] = []
    missing_audio: list[str] = []
    for b in breaks:
        status = refresh_status(b, args.voice).status
        counts[status] = counts.get(status, 0) + 1
        if status == "stale" and b.status == "rendered":
            stale.append(b.id)
        if b.status == "rendered" and b.audio:
            audio = Path(b.audio) if Path(b.audio).is_absolute() else data / b.audio
            if not audio.is_file():
                missing_audio.append(b.id)
    print(f"{path}: {len(breaks)} break(s)")
    print("  " + ", ".join(f"{status}: {n}" for status, n in counts.items()))
    for bid in stale:
        print(f"  stale (text edited or voice changed since it was rendered): {bid}")
    for bid in missing_audio:
        print(f"  rendered, but its audio file is missing: {bid}")
    for problem in lint_script(text):
        print(f"  problem: {problem}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", metavar="DIR", help="data folder (default: <repo>/data)")
    common.add_argument("--ffmpeg", metavar="PATH", help="ffmpeg executable (default: the one on PATH)")
    common.add_argument("--no-ffmpeg", action="store_true", help="do not use ffmpeg at all")
    parser = argparse.ArgumentParser(prog="python -m aura", description="AURA engine: library, scripts, show.")
    parser.add_argument("--version", action="version", version=f"aura {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True
    p = sub.add_parser("scan", parents=[common], help="index the audio files under a folder")
    p.add_argument("folder")
    p.add_argument("--no-measure", action="store_true", help="skip loudness measurement (much faster)")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--only", metavar="TYPE", help="index only this file type, e.g. wav")
    kind.add_argument("--prefer", metavar="TYPE",
                      help="one copy per song: this type when a song has it, else the best other copy")
    p = sub.add_parser("tracks", parents=[common], help="list indexed tracks")
    p.add_argument("--album", help="only this album (exact name, as listed)")
    p.add_argument("--root", help="only tracks under this folder")
    p = sub.add_parser("playlist", parents=[common], help="write an .m3u8 playlist")
    p.add_argument("out", help="output file; a bare name goes to <data>/playlists/")
    p.add_argument("--album", help="only this album")
    p.add_argument("--absolute", action="store_true", help="write absolute paths only")
    p = sub.add_parser("story-draft", parents=[common], help="draft an album's story file")
    p.add_argument("album_root", help="the album's folder (scan it first)")
    p.add_argument("--force", action="store_true", help="replace an existing story file with a fresh draft")
    p = sub.add_parser("script-check", parents=[common], help="check a script file: statuses, stale, problems")
    p.add_argument("script", help="'casual', an album slug, or a path to a script .txt")
    p.add_argument("--voice", default="", help="the current host voice (breaks rendered with another read stale)")
    return parser


_COMMANDS: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "scan": cmd_scan, "tracks": cmd_tracks, "playlist": cmd_playlist,
    "story-draft": cmd_story_draft, "script-check": cmd_script_check,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # a Windows console or pipe must not crash on a non-English title
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    data = Path(args.data) if args.data else default_data_dir()
    try:
        return _COMMANDS[args.command](args, data)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
