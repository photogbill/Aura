# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Extended M3U (.m3u8) playlists: the plain, universal format every player reads.

Paths are written relative when the audio sits under the playlist's folder, so an album folder carrying its
own playlist can be copied to a USB stick and still play; anything else is written as an absolute path.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Union
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .common import atomic_write_text, is_under, read_text
from .library import Track

log = logging.getLogger(__name__)


def playlists_dir(data_dir: str | Path) -> Path:
    """Where playlists live by default: data_dir/playlists (the host may pass any other path)."""
    return Path(data_dir) / "playlists"


def _item(entry: Union[Track, dict]) -> dict:
    if isinstance(entry, Track):
        return {"path": entry.path, "title": entry.title, "artist": entry.artist, "duration_s": entry.duration_s}
    return {"path": str(entry.get("path") or ""), "title": str(entry.get("title") or ""),
            "artist": str(entry.get("artist") or ""), "duration_s": entry.get("duration_s") or 0.0}


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def write_m3u8(path: str | os.PathLike, tracks: list[Track] | list[dict], relative: bool = True) -> Path:
    """Write #EXTM3U with one "#EXTINF:<secs>,<artist - title>" line per entry, UTF-8. Returns the path."""
    out = Path(path)
    folder = os.path.abspath(out.parent)
    lines = ["#EXTM3U"]
    for entry in tracks:
        item = _item(entry)
        if not item["path"]:
            log.warning("playlist entry without a path skipped: %r", entry)
            continue
        try:
            seconds = int(round(float(item["duration_s"])))
        except (TypeError, ValueError):
            seconds = 0
        title = _one_line(item["title"]) or Path(item["path"]).stem
        label = f"{_one_line(item['artist'])} - {title}" if item["artist"] else title
        lines.append(f"#EXTINF:{seconds if seconds > 0 else -1},{label}")
        target = os.path.abspath(item["path"])
        lines.append(os.path.relpath(target, folder) if relative and is_under(target, folder) else target)
    return atomic_write_text(out, "\n".join(lines) + "\n")


def _resolve(entry: str, folder: str) -> str:
    if entry.lower().startswith("file:"):
        return os.path.abspath(url2pathname(unquote(urlparse(entry).path)))
    if "://" in entry:  # a stream URL: nothing to resolve
        return entry
    if os.sep == "/":  # a playlist written on Windows, read elsewhere
        entry = entry.replace("\\", "/")
    if not os.path.isabs(entry):
        entry = os.path.join(folder, entry)
    return os.path.normpath(os.path.abspath(entry))


def read_m3u8(path: str | os.PathLike) -> list[dict]:
    """[{"path": absolute str, "title": str, "duration_s": float}] in playlist order."""
    folder = os.path.dirname(os.path.abspath(path))
    items: list[dict] = []
    pending: tuple[str, float] | None = None
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            head, _, title = line[len("#EXTINF:"):].partition(",")
            try:
                seconds = float(head.split()[0]) if head.split() else -1.0
            except ValueError:
                seconds = -1.0
            pending = (title.strip(), seconds)
            continue
        if line.startswith("#"):
            continue
        resolved = _resolve(line, folder)
        title, seconds = pending if pending else (Path(resolved).stem, -1.0)
        items.append({"path": resolved, "title": title or Path(resolved).stem, "duration_s": max(seconds, 0.0)})
        pending = None
    return items
