# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Delivery copies for a distributor: WAV masters in, exactly the format the distributor asks for out.

First and only one so far (Bill, 2026-10-12): RouteNote - "take only the .WAV inputs and convert them, organized,
into the right flac format". RouteNote's support hub: audio "must be formatted in stereo as a FLAC or MP3 file",
16-bit, "the sample rate should be 44.1kHz"; hi-res is for its All Access tier only.

What "right" takes, beyond running FFmpeg:
* 48 kHz (or any other rate) to 44.1 kHz through the SoX resampler at high precision when the FFmpeg build has
  it, else FFmpeg's own; the summary names which.
* 24-bit or float to 16-bit WITH triangular dither, so quiet passages and fades do not turn into distortion.
* Mono to stereo by copying the one channel to both at the same level (FFmpeg's default upmix would drop it
  3 dB); more than two channels are folded down to stereo, and said so.
* A master whose true peak is over 0 dBFS will clip in the 44.1 kHz copy; that is REPORTED, never "fixed" by
  quietly turning the song down.
* Every copy is checked after it is written (FLAC, 44.1 kHz, 2 channels, 16-bit); one that is not is removed
  and reported as failed.

Organized: <output>/<album folder>/[<Disc N>/]<the WAV's name>.flac - the album folder being the WAV's folder
(or the one above a "Disc 2"-style folder). The masters are never touched. A copy already there, newer than its
WAV and passing the check, is kept, so a second run only does what is new.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .common import is_cancelled, say, temp_path_for
from .library import measure_loudness

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TIMEOUT_S = 1800
_DISC_FOLDER_RE = re.compile(r"^(disc|disk|cd)\s*\d+$", re.IGNORECASE)
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class DeliverySpec:
    name: str
    inputs: tuple[str, ...] = (".wav",)
    codec: str = "flac"
    ext: str = ".flac"
    rate: int = 44100
    channels: int = 2
    sample_fmt: str = "s16"
    bits: int = 16


ROUTENOTE = DeliverySpec(name="RouteNote")


@dataclass
class AudioInfo:
    codec: str = ""
    rate: int = 0
    channels: int = 0
    sample_fmt: str = ""
    bits: int = 0
    duration_s: float = 0.0

    def describe(self) -> str:
        chans = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} channels")
        return f"{self.codec}, {self.rate / 1000:g} kHz, {self.bits}-bit, {chans}"


@dataclass
class ConvertItem:
    src: str
    dst: str = ""
    status: str = ""                # converted | kept | failed | skipped
    source: str = ""                # what the WAV was ("pcm_s24le, 48 kHz, 24-bit, stereo")
    changes: list[str] = field(default_factory=list)
    warning: str = ""
    error: str = ""


@dataclass
class ConvertSummary:
    spec: str = ""
    out_root: str = ""
    items: list[ConvertItem] = field(default_factory=list)
    resampler: str = ""
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    def warnings(self) -> list[ConvertItem]:
        return [i for i in self.items if i.warning]


# ------------------------------------------------------------------------------------------------ probing

_CHANNEL_WORDS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8}
_FMT_BITS = {"u8": 8, "u8p": 8, "s16": 16, "s16p": 16, "s32": 32, "s32p": 32, "s64": 64, "s64p": 64,
             "flt": 32, "fltp": 32, "dbl": 64, "dblp": 64}


def parse_probe(text: str) -> AudioInfo | None:
    """AudioInfo from what `ffmpeg -i` prints, e.g.
    "Stream #0:0: Audio: pcm_s24le ([1][0][0][0] / 0x0001), 48000 Hz, stereo, s32 (24 bit), 2304 kb/s"."""
    line = next((ln for ln in text.splitlines() if "Audio:" in ln and "Stream #" in ln), "")
    if not line:
        return None
    info = AudioInfo()
    body = line.split("Audio:", 1)[1].strip()
    info.codec = re.split(r"[\s,(]", body, maxsplit=1)[0]
    m = re.search(r"(\d+) Hz", body)
    info.rate = int(m.group(1)) if m else 0
    parts = [p.strip() for p in re.sub(r"\([^)]*\)", lambda x: x.group(0).replace(",", ";"), body).split(",")]
    for part in parts[1:]:
        word = part.split()[0] if part.split() else ""
        if word in _CHANNEL_WORDS:
            info.channels = _CHANNEL_WORDS[word]
        elif re.fullmatch(r"\d+ channels", part):
            info.channels = int(part.split()[0])
        elif word in _FMT_BITS and not info.sample_fmt:
            info.sample_fmt = word
            bits = re.search(r"\((\d+) bit\)", part)
            info.bits = int(bits.group(1)) if bits else _FMT_BITS[word]
    pcm = re.match(r"pcm_[suf](\d+)", info.codec)
    if pcm:
        info.bits = int(pcm.group(1))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info.duration_s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return info


def probe(ffmpeg: str, path: str | os.PathLike) -> AudioInfo | None:
    try:
        proc = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", os.fspath(path)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=60, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("FFmpeg could not read %s: %s", path, exc)
        return None
    return parse_probe(proc.stderr.decode("utf-8", errors="replace"))


def meets(info: AudioInfo | None, spec: DeliverySpec) -> bool:
    return (info is not None and info.codec == spec.codec and info.rate == spec.rate
            and info.channels == spec.channels and info.sample_fmt == spec.sample_fmt and info.bits == spec.bits)


# ------------------------------------------------------------------------------------------------ where copies go

def _safe(name: str) -> str:
    return _BAD_NAME_CHARS.sub("_", name).strip(" .") or "_"


def delivery_path(src: str | os.PathLike, out_root: str | os.PathLike, spec: DeliverySpec = ROUTENOTE) -> Path:
    """<out_root>/<album folder>/[<Disc N>/]<name>.flac."""
    path = Path(os.fspath(src))
    parent = path.parent
    if _DISC_FOLDER_RE.match(parent.name) and parent.parent.name:
        folder = Path(out_root) / _safe(parent.parent.name) / _safe(parent.name)
    else:
        folder = Path(out_root) / _safe(parent.name or "Songs")
    return folder / (_safe(path.stem) + spec.ext)


# ------------------------------------------------------------------------------------------------ converting

def _filter_chain(info: AudioInfo, spec: DeliverySpec, resampler: str) -> tuple[list[str], list[str]]:
    """(FFmpeg arguments for the audio, the changes they make in words)."""
    changes: list[str] = []
    filters: list[str] = []
    extra: list[str] = []
    if info.channels == 1:
        filters.append("pan=stereo|c0=c0|c1=c0")          # the one channel to both, at the same level
        changes.append("mono → stereo (the same signal on both sides)")
    elif info.channels > 2:
        extra += ["-ac", str(spec.channels)]
        changes.append(f"{info.channels} channels folded down to stereo")
    engine = "resampler=soxr:precision=28:" if resampler == "soxr" else ""
    filters.append(f"aresample={engine}osr={spec.rate}:osf={spec.sample_fmt}:dither_method=triangular")
    if info.rate and info.rate != spec.rate:
        changes.append(f"{info.rate / 1000:g} kHz → {spec.rate / 1000:g} kHz ({resampler})")
    if info.bits and (info.bits != spec.bits or info.sample_fmt in ("flt", "fltp", "dbl", "dblp")):
        kind = "float" if info.sample_fmt in ("flt", "fltp", "dbl", "dblp") else f"{info.bits}-bit"
        changes.append(f"{kind} → {spec.bits}-bit, with dither")
    return ["-af", ",".join(filters), *extra], changes


def _run(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=_TIMEOUT_S, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return -1, str(exc)
    return proc.returncode, proc.stderr.decode("utf-8", errors="replace")


def convert_one(ffmpeg: str, src: str | os.PathLike, dst: str | os.PathLike, spec: DeliverySpec = ROUTENOTE, *,
                tags: Optional[dict] = None, resampler: str = "soxr") -> tuple[ConvertItem, str]:
    """Write one delivery copy. Returns (the item, the resampler that was used - "soxr" falls back to "swr"
    when this FFmpeg has no SoX resampler)."""
    item = ConvertItem(src=os.fspath(src), dst=os.fspath(dst))
    info = probe(ffmpeg, src)
    if info is None or not info.rate:
        item.status, item.error = "failed", "FFmpeg could not read it as audio"
        return item, resampler
    item.source = info.describe()
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(target)
    meta = ["-map_metadata", "0"]
    for key, value in (tags or {}).items():
        if value:
            meta += ["-metadata", f"{key}={value}"]
    for attempt in ((resampler, "swr") if resampler == "soxr" else (resampler,)):
        audio, changes = _filter_chain(info, spec, attempt)
        code, err = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", os.fspath(src),
                          "-map", "0:a:0", *meta, *audio, "-c:a", spec.codec, "-compression_level", "8",
                          "-sample_fmt", spec.sample_fmt, "-ar", str(spec.rate), "-f", spec.codec, str(tmp)])
        if code == 0:
            resampler = attempt
            item.changes = changes
            break
        log.warning("FFmpeg (%s) could not convert %s: %s", attempt, src, err.strip().splitlines()[-1:] or "-")
        _remove(tmp)
    else:
        item.status, item.error = "failed", (err.strip().splitlines() or ["FFmpeg failed"])[-1]
        return item, resampler
    written = probe(ffmpeg, tmp)
    if not meets(written, spec):
        _remove(tmp)
        item.status = "failed"
        item.error = f"the copy came out as {written.describe() if written else 'unreadable'}, not the spec"
        return item, resampler
    os.replace(tmp, target)
    _lufs, peak = measure_loudness(src, ffmpeg)
    if peak is not None and peak > 0.0:
        item.warning = (f"true peak {peak:+.1f} dBTP: the {spec.rate / 1000:g} kHz copy clips slightly. "
                        "Nothing was turned down; re-master with ~1 dB headroom if it matters.")
    item.status = "converted"
    return item, resampler


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove the temporary file %s: %s", path, exc)


def convert_batch(paths: Iterable[str | os.PathLike], out_root: str | os.PathLike, *, ffmpeg: str,
                  spec: DeliverySpec = ROUTENOTE, overwrite: bool = False,
                  tags_for: Optional[Callable[[str], dict]] = None,
                  progress: Optional[Callable[[str], Any]] = None, cancel: Any = None) -> ConvertSummary:
    """Delivery copies of every input-type file in `paths` (others are listed as skipped), organized under
    `out_root` (see delivery_path). Raises ValueError without FFmpeg."""
    if not ffmpeg:
        raise ValueError("FFmpeg is needed to convert audio and was not found")
    summary = ConvertSummary(spec=spec.name, out_root=os.path.abspath(os.fspath(out_root)), resampler="soxr")
    todo = list(dict.fromkeys(os.path.abspath(os.fspath(p)) for p in paths))
    taken: dict[str, str] = {}
    log.info("%s copies: %d file(s) to %s", spec.name, len(todo), summary.out_root)
    for i, src in enumerate(todo, 1):
        if is_cancelled(cancel):
            summary.cancelled = True
            break
        if os.path.splitext(src)[1].lower() not in spec.inputs:
            summary.items.append(ConvertItem(src=src, status="skipped",
                                             error=f"not a {'/'.join(e.lstrip('.').upper() for e in spec.inputs)}"))
            continue
        dst = delivery_path(src, out_root, spec)
        n = 2
        while os.path.normcase(str(dst)) in taken and taken[os.path.normcase(str(dst))] != src:
            dst = dst.with_name(f"{dst.stem.rsplit(' (', 1)[0]} ({n}){spec.ext}")
            n += 1
        taken[os.path.normcase(str(dst))] = src
        say(progress, f"{spec.name} [{i}/{len(todo)}] {os.path.basename(src)}")
        if not overwrite and dst.is_file() and dst.stat().st_mtime >= os.path.getmtime(src) \
                and meets(probe(ffmpeg, dst), spec):
            summary.items.append(ConvertItem(src=src, dst=str(dst), status="kept"))
            log.debug("%s copy kept (newer than its WAV and to spec): %s", spec.name, dst)
            continue
        try:
            item, summary.resampler = convert_one(ffmpeg, src, dst, spec,
                                                  tags=tags_for(src) if tags_for else None,
                                                  resampler=summary.resampler)
        except OSError as exc:
            item = ConvertItem(src=src, dst=str(dst), status="failed", error=str(exc))
        summary.items.append(item)
        if item.status == "failed":
            log.warning("%s copy failed: %s - %s", spec.name, src, item.error)
        else:
            log.info("%s copy: %s -> %s (%s)%s", spec.name, src, item.dst, "; ".join(item.changes) or "as it was",
                     f" WARNING {item.warning}" if item.warning else "")
    log.info("%s copies done%s: %d converted, %d kept, %d failed, %d skipped, %d warning(s); resampler %s",
             spec.name, " (stopped)" if summary.cancelled else "", summary.count("converted"),
             summary.count("kept"), summary.count("failed"), summary.count("skipped"), len(summary.warnings()),
             summary.resampler)
    return summary
