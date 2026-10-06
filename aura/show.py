# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""The sequencer: what plays next, at what gain, and where to pick up again after a break in listening.

No audio and no Qt: next() hands the host an ordered list of files and the host plays them. It never makes
anything either - a break that is not playable right now (not rendered, stale, fell back, file missing) is
simply left out, because no break is better than one generated on the spot.
"""
from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .breaks import BreakStore
from .library import TARGET_LUFS, Library, Track
from .script import back_id, intro_id, narration_id, recap_id, segue_id
from .story import DEPTHS, Story, apply_relinks, story_relinks

log = logging.getLogger(__name__)

FREQUENCIES = ("off", "station_ids", "every_n", "every", "acts")
MODES = ("casual", "story")
GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 6.0
TRUE_PEAK_CEILING_DB = -1.0

__all__ = ["PlayItem", "Show", "FREQUENCIES", "DEPTHS", "MODES", "gain_db"]


@dataclass
class PlayItem:
    kind: str           # "track" | "break"
    path: str
    title: str
    gain_db: float = 0.0          # loudness normalisation to TARGET_LUFS, clamped -12..+6, true peak kept at or below -1 dBTP
    track_id: str = ""
    break_id: str = ""
    talkover_s: float = 0.0       # an intro break over the NEXT track's intro: the seconds available (track.intro_s); 0 = play the break alone, then the track


def gain_db(lufs: float | None, true_peak_db: float | None, target_lufs: float = TARGET_LUFS) -> float:
    """Gain (dB) that brings a file to the target loudness, clamped to -12..+6 dB, and lowered further when
    needed so the file's true peak ends at or below -1 dBTP. 0.0 when the loudness was never measured."""
    if lufs is None:
        return 0.0
    gain = min(GAIN_MAX_DB, max(GAIN_MIN_DB, target_lufs - lufs))
    if true_peak_db is not None:
        gain = min(gain, TRUE_PEAK_CEILING_DB - true_peak_db)
    return round(max(GAIN_MIN_DB, gain), 2)


def _as_utc(moment: datetime) -> datetime:
    """Aware UTC datetime; a naive one is taken to be UTC already."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        log.warning("ignoring an unreadable last_played_at: %r", value)
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Show:
    """One running show: a queue of track ids, a position in it, and the dials that decide when the host talks.

    frequency: "off" (music only), "station_ids" (a station ID every every_n tracks), "every_n" (the host
    talks every every_n tracks), "every" (between every track), "acts" (story: only when the act changes;
    casual: only when the album changes).
    """

    def __init__(self, library: Library, store: BreakStore, *, voice: str = "", mode: str = "casual",
                 story: Story | None = None, frequency: str = "every", every_n: int = 3,
                 shuffle: bool = False, seed: int | None = None, target_lufs: float = TARGET_LUFS,
                 talk_over_intros: bool = True):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if mode == "story" and story is None:
            raise ValueError("story mode needs a Story")
        if frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {FREQUENCIES}")
        self.library = library
        self.store = store
        self.voice = voice
        self.mode = mode
        self.story = story
        self.frequency = frequency
        self.every_n = max(1, int(every_n))
        self.shuffle = shuffle
        self.target_lufs = target_lufs
        self.talk_over_intros = talk_over_intros
        self._rng = random.Random(seed)
        self._queue: list[str] = []
        self._start = 0
        self._pos: int | None = None          # index of the current track; None before the first
        self._plays = 0                       # tracks handed out since set_queue: drives every_n
        self._station_turn = 0
        self._last_played_at: datetime | None = None
        self._renamed: dict[str, str] = {}   # old story track id -> new, after the album folder moved
        if mode == "story" and story is not None:
            self.set_queue([t.track_id for t in story.tracks])

    # ------------------------------------------------------------------------------------------ queue

    def _story_index(self) -> dict[str, int]:
        return {t.track_id: i for i, t in enumerate(self.story.tracks)} if self.story is not None else {}

    def _link_story(self) -> None:
        """Story track ids come from file paths: if the album folder moved, find its files again (in memory
        only - the host saves the story if it wants to keep the new ids)."""
        if self.story is None:
            return
        mapping, new_root = story_relinks(self.story, self.library)
        if mapping:
            apply_relinks(self.story, mapping, new_root)
            self._renamed.update(mapping)
            log.info("relinked %d story track(s) to moved files", len(mapping))

    def set_queue(self, track_ids: list[str], start: int | None = 0) -> None:
        """Replace the queue; the next call to next() plays the track at `start`.

        Story mode keeps the story's order whatever order the ids arrive in (ids not in the story are left
        out, and shuffle is ignored). Casual mode with shuffle on plays `start` first and shuffles the rest;
        start=None asks for a random first track when shuffling (and means the first track otherwise).
        """
        random_first = start is None
        start = 0 if start is None else start
        if self.mode == "story":
            self._link_story()
        ids = [self._renamed.get(str(t), str(t)) for t in track_ids]
        if ids and not 0 <= start < len(ids):
            raise IndexError(f"start {start} is outside the queue (0..{len(ids) - 1})")
        first = ids[start] if ids else None
        if self.mode == "story":
            index = self._story_index()
            dropped = [t for t in ids if t not in index]
            if dropped:
                log.warning("%d track(s) are not in the story and were left out of the queue", len(dropped))
            ids = sorted(dict.fromkeys(t for t in ids if t in index), key=index.__getitem__)
            start = ids.index(first) if first in ids else 0
        elif self.shuffle and ids and random_first:
            ids = list(ids)
            self._rng.shuffle(ids)
        elif self.shuffle and ids:
            rest = ids[:start] + ids[start + 1:]
            self._rng.shuffle(rest)
            ids, start = [ids[start]] + rest, 0
        self._queue = ids
        self._start = start
        self._pos = None
        self._plays = 0

    def queue(self) -> list[str]:
        return list(self._queue)

    def position(self) -> int:
        """Index of the current track in the queue (the one the last next()/jump() returned), -1 before."""
        return self._pos if self._pos is not None else -1

    def current(self) -> Track | None:
        if self._pos is None or not 0 <= self._pos < len(self._queue):
            return None
        return self.library.track(self._queue[self._pos])

    # ------------------------------------------------------------------------------------------- play

    def next(self) -> list[PlayItem]:
        """Advance one track: [the breaks that belong between the previous track and this one..., the track].

        [] at the end of the queue. Casual: the back-announce of the previous track and the intro of this
        one, by the frequency dial. Story: the segue after track n only when n -> n+1 is happening in order,
        then the narration of this track's scene. Only playable breaks are ever returned.
        """
        index = self._start if self._pos is None else self._pos + 1
        return self._advance(index, jumped=False)

    def jump(self, index: int) -> list[PlayItem]:
        """Like next(), but to any queue index. A move that is not to the very next track gets no segue
        (story) and no back-announce of the track that was cut off (casual)."""
        if not 0 <= index < len(self._queue):
            raise IndexError(f"index {index} is outside the queue (0..{len(self._queue) - 1})")
        consecutive = self._pos is not None and index == self._pos + 1
        return self._advance(index, jumped=not consecutive)

    def _advance(self, index: int, jumped: bool) -> list[PlayItem]:
        previous = self.current()
        track: Track | None = None
        while index < len(self._queue):
            candidate = self.library.track(self._queue[index])
            if candidate is not None and os.path.isfile(candidate.path):
                track = candidate
                break
            log.warning("skipping %s: not in the library, or its file is missing", self._queue[index])
            index += 1
        if track is None:
            return []
        slot = self._plays % self.every_n == 0   # every_n: talk before the 1st, (n+1)th, (2n+1)th... track
        if self.mode == "story":
            items = self._story_breaks(previous, track, slot)
        else:
            items = self._casual_breaks(previous, track, slot, jumped)
        self._pos = index
        self._plays += 1
        self._last_played_at = datetime.now(timezone.utc)
        items.append(PlayItem(kind="track", path=track.path, title=track.title,
                              gain_db=self.gain_for(track.lufs, track.true_peak_db), track_id=track.id))
        return items

    def _casual_breaks(self, previous: Track | None, track: Track, slot: bool, jumped: bool) -> list[PlayItem]:
        f = self.frequency
        if f == "off":
            return []
        if f == "station_ids":
            return self._station_id() if slot else []
        if f == "every_n" and not slot:
            return []
        if f == "acts" and previous is not None and previous.album == track.album:
            return []
        items: list[PlayItem] = []
        if previous is not None and not jumped:
            back = self._break_item(back_id(previous.id))
            if back is not None:
                items.append(back)
        intro = self._break_item(intro_id(track.id))
        if intro is not None:
            if self.talk_over_intros and track.intro_s and track.intro_s > 0:
                intro.talkover_s = float(track.intro_s)
            items.append(intro)
        return items

    def _story_breaks(self, previous: Track | None, track: Track, slot: bool) -> list[PlayItem]:
        f = self.frequency
        if f == "off" or self.story is None:
            return []
        index, slug = self._story_index(), self.story.slug
        items: list[PlayItem] = []
        n = index.get(previous.id) if previous is not None else None
        here = index.get(track.id)
        if n is not None and here == n + 1:  # in order; a jump or a skipped track gets no segue
            acts_change = self.story.tracks[n].act != self.story.tracks[here].act
            if f == "every" or (f == "every_n" and slot) or (f == "acts" and acts_change):
                segue = self._break_item(segue_id(slug, n))
                if segue is not None:
                    items.append(segue)
        if f == "station_ids" and slot:
            items += self._station_id()
        if here is not None:
            narration = self._break_item(narration_id(slug, here))
            if narration is not None:
                items.append(narration)
        return items

    def _station_id(self) -> list[PlayItem]:
        """The next playable station ID, in rotation: from the casual script or Bill's own takes."""
        ids = sorted({b.id for b in self.store.load("casual") if b.kind == "station_id"}
                     | set(self.store.own_station_ids()))
        ready = [(bid, path) for bid in ids if (path := self.store.playable(bid, self.voice))]
        if not ready:
            return []
        bid, path = ready[self._station_turn % len(ready)]
        self._station_turn += 1
        return [self._make_break_item(bid, path)]

    def _break_item(self, break_id: str) -> PlayItem | None:
        path = self.store.playable(break_id, self.voice)
        return self._make_break_item(break_id, path) if path else None

    def _make_break_item(self, break_id: str, path: str) -> PlayItem:
        b = self.store.get(break_id)
        own = self.store.override_path(break_id)
        is_own = own is not None and str(own) == path
        title = (b.title if b is not None and b.title else break_id) + (" (own take)" if is_own else "")
        gain = 0.0 if (is_own or b is None) else self.gain_for(b.lufs, None)
        return PlayItem(kind="break", path=path, title=title, gain_db=gain, break_id=break_id)

    def gain_for(self, lufs: float | None, true_peak_db: float | None) -> float:
        return gain_db(lufs, true_peak_db, self.target_lufs)

    # -------------------------------------------------------------------------------------- resuming

    def state(self) -> dict:
        """Everything needed to pick the show up again: queue, position, mode, last_played_at (ISO, UTC)."""
        return {
            "queue": list(self._queue), "position": self.position(), "start": self._start, "mode": self.mode,
            "last_played_at": (self._last_played_at.isoformat(timespec="seconds")
                               if self._last_played_at is not None else None),
            "plays": self._plays, "station_turn": self._station_turn,
        }

    def restore(self, state: dict) -> None:
        """Put back a state() snapshot. The next next() plays the track after the saved position."""
        mode = str(state.get("mode") or self.mode)
        if mode not in MODES or (mode == "story" and self.story is None):
            log.warning("cannot restore %r mode here; staying in %s mode", mode, self.mode)
        else:
            self.mode = mode
        if self.mode == "story":
            self._link_story()
        self._queue = [self._renamed.get(str(t), str(t)) for t in state.get("queue") or []]
        position = _as_int(state.get("position"), -1)
        self._pos = position if 0 <= position < len(self._queue) else None
        start = _as_int(state.get("start"), 0)
        self._start = start if 0 <= start < len(self._queue) else 0
        self._plays = max(0, _as_int(state.get("plays"), 0))
        self._station_turn = max(0, _as_int(state.get("station_turn"), 0))
        self._last_played_at = _parse_time(state.get("last_played_at"))

    def resume_items(self, now: datetime, gap_hours: float = 6.0) -> list[PlayItem]:
        """The "previously on" recap to play when a story show resumes after at least gap_hours; else [].

        The saved position is the track that was PLAYING when the show stopped: the host saved the state as
        that track started and replays it from its start on resume. The listener has therefore finished
        only the songs before it, so the recap covers songs up to position-1 (the spoiler line applies to the
        listener), and there is no recap when the position is the album's first song. Play these before
        the replayed track. A naive `now` is taken as UTC."""
        if self.mode != "story" or self.story is None or self.frequency == "off":
            return []
        if self._pos is None or self._last_played_at is None or not 0 <= self._pos < len(self._queue):
            return []
        if (_as_utc(now) - self._last_played_at).total_seconds() < gap_hours * 3600:
            return []
        playing = self._story_index().get(self._queue[self._pos])
        if playing is None or playing == 0:
            return []
        recap = self._break_item(recap_id(self.story.slug, playing - 1))
        return [recap] if recap is not None else []
