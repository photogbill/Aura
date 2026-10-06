# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""The sequencer: casual dials, story segues only on in-order moves, recaps on resume, and gain."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from aura_test_support import TempDirTest, make_wav

from aura.breaks import BreakStore, safe_id
from aura.library import Library
from aura.script import Break, kind_of, text_hash
from aura.show import FREQUENCIES, Show, gain_db
from aura.story import Story, StoryTrack


class ShowTestBase(TempDirTest):
    def setUp(self):
        super().setUp()
        self.album = self.tmp / "music" / "TTA"
        self.paths = [make_wav(self.album / name) for name in
                      ("01 - Genesis.wav", "02 - The Velvet Lesson (Aphrodite).wav", "03 - Objection.wav",
                       "04 - Window Seat (Chloe).wav")]
        self.lib = Library(self.tmp / "data")
        self.addCleanup(self.lib.close)
        self.lib.scan(self.tmp / "music")
        self.ids = [t.id for t in self.lib.tracks()]
        self.store = BreakStore(self.tmp / "data")

    def rendered(self, script: str, break_id: str, *, voice: str = "v", lufs: float | None = None,
                 status: str = "rendered") -> None:
        make_wav(self.tmp / "data" / "breaks" / f"{safe_id(break_id)}.wav", 0.1)
        text = f"words for {break_id}"
        self.store.put(script, Break(id=break_id, kind=kind_of(break_id), text=text, status=status, voice=voice,
                                     audio=f"breaks/{safe_id(break_id)}.wav", text_hash=text_hash(text), lufs=lufs))

    @staticmethod
    def labels(items) -> list[str]:
        return [i.break_id if i.kind == "break" else f"track:{i.track_id}" for i in items]


class CasualShowTests(ShowTestBase):
    def casual(self, **dials) -> Show:
        show = Show(self.lib, self.store, voice="v", **dials)
        show.set_queue(self.ids)
        return show

    def test_every_track_gets_a_back_announce_and_an_intro_when_playable(self):
        t0, t1, t2, _t3 = self.ids
        for bid in (f"intro:{t0}", f"back:{t0}", f"intro:{t1}"):
            self.rendered("casual", bid)
        show = self.casual()
        self.assertEqual(show.position(), -1)
        self.assertIsNone(show.current())
        self.assertEqual(self.labels(show.next()), [f"intro:{t0}", f"track:{t0}"])
        self.assertEqual(self.labels(show.next()), [f"back:{t0}", f"intro:{t1}", f"track:{t1}"])
        self.assertEqual(self.labels(show.next()), [f"track:{t2}"])   # nothing rendered: nothing said
        self.assertEqual((show.position(), show.current().id), (2, t2))

    def test_off_means_music_only(self):
        self.rendered("casual", f"intro:{self.ids[0]}")
        self.assertEqual(self.labels(self.casual(frequency="off").next()), [f"track:{self.ids[0]}"])

    def test_every_n(self):
        for t in self.ids:
            self.rendered("casual", f"intro:{t}")
        show = self.casual(frequency="every_n", every_n=2)
        talked = [any(i.kind == "break" for i in show.next()) for _ in self.ids]
        self.assertEqual(talked, [True, False, True, False])

    def test_station_ids_rotate_every_n_tracks(self):
        self.rendered("casual", "station_id:1")
        self.rendered("casual", "station_id:2")
        self.rendered("casual", f"intro:{self.ids[0]}")
        show = self.casual(frequency="station_ids", every_n=2)
        said = [[i.break_id for i in show.next() if i.kind == "break"] for _ in self.ids]
        self.assertEqual(said, [["station_id:1"], [], ["station_id:2"], []])

    def test_breaks_that_are_not_playable_are_left_out(self):
        t0 = self.ids[0]
        self.rendered("casual", f"intro:{t0}", status="approved")      # approved, never rendered
        self.assertEqual(self.labels(self.casual().next()), [f"track:{t0}"])
        self.rendered("casual", f"intro:{t0}", voice="old-voice")      # rendered with another voice: stale
        self.assertEqual(self.labels(self.casual().next()), [f"track:{t0}"])

    def test_intro_talkover_uses_the_tracks_intro(self):
        t0 = self.ids[0]
        self.rendered("casual", f"intro:{t0}")
        self.lib.update(t0, intro_s=4.5)
        self.assertEqual(self.casual().next()[0].talkover_s, 4.5)
        self.assertEqual(self.casual(talk_over_intros=False).next()[0].talkover_s, 0.0)

    def test_a_jump_gets_no_back_announce(self):
        t0, t1, t2, t3 = self.ids
        for bid in (f"back:{t0}", f"intro:{t2}", f"back:{t2}"):
            self.rendered("casual", bid)
        show = self.casual()
        show.next()
        self.assertEqual(self.labels(show.jump(2)), [f"intro:{t2}", f"track:{t2}"])
        self.assertEqual(self.labels(show.jump(3)), [f"back:{t2}", f"track:{t3}"])  # 2 -> 3 is in order
        with self.assertRaises(IndexError):
            show.jump(9)

    def test_shuffle_can_pick_a_random_first_track(self):
        firsts = set()
        for seed in range(12):
            show = Show(self.lib, self.store, shuffle=True, seed=seed)
            show.set_queue(self.ids, start=None)
            self.assertEqual(sorted(show.queue()), sorted(self.ids))
            firsts.add(show.queue()[0])
            kept = Show(self.lib, self.store, shuffle=True, seed=seed)
            kept.set_queue(self.ids)                       # start=0 still means: the first id plays first
            self.assertEqual(kept.queue()[0], self.ids[0])
        self.assertGreater(len(firsts), 1)
        plain = Show(self.lib, self.store)
        plain.set_queue(self.ids, start=None)
        self.assertEqual(plain.queue(), self.ids)

    def test_shuffle_starts_where_asked_and_is_repeatable(self):
        a = Show(self.lib, self.store, shuffle=True, seed=7)
        a.set_queue(self.ids, start=2)
        b = Show(self.lib, self.store, shuffle=True, seed=7)
        b.set_queue(self.ids, start=2)
        self.assertEqual(a.queue()[0], self.ids[2])
        self.assertEqual(sorted(a.queue()), sorted(self.ids))
        self.assertEqual(a.queue(), b.queue())

    def test_end_of_queue_and_missing_files(self):
        os.remove(self.paths[1])
        show = self.casual(frequency="off")
        with self.assertLogs("aura", level="WARNING"):
            played = [self.labels(show.next()) for _ in range(3)]
        self.assertEqual(played, [[f"track:{self.ids[0]}"], [f"track:{self.ids[2]}"], [f"track:{self.ids[3]}"]])
        self.assertEqual(show.next(), [])
        self.assertEqual(show.position(), 3)

    def test_gain_comes_from_measured_loudness(self):
        t0 = self.ids[0]
        self.lib.update(t0, lufs=-20.0, true_peak_db=-3.0)
        self.rendered("casual", f"intro:{t0}", lufs=-26.0)
        intro, track = self.casual().next()
        self.assertEqual((intro.gain_db, track.gain_db), (6.0, 2.0))
        self.assertEqual(intro.path, str(self.tmp / "data" / "breaks" / f"intro_{t0}.wav"))

    def test_casual_mode_has_no_resume_recap(self):
        show = self.casual()
        show.next()
        self.assertEqual(show.resume_items(datetime.now(timezone.utc) + timedelta(days=2)), [])


class GainTests(TempDirTest):
    def test_clamps_and_true_peak_ceiling(self):
        self.assertEqual(gain_db(None, -3.0), 0.0)
        self.assertEqual(gain_db(-20.0, None), 4.0)
        self.assertEqual(gain_db(-40.0, None), 6.0)      # never more than +6
        self.assertEqual(gain_db(0.0, None), -12.0)      # never less than -12
        self.assertEqual(gain_db(-20.0, -3.0), 2.0)      # +4 wanted, but the peak may only reach -1 dBTP
        self.assertEqual(gain_db(-16.0, 0.5), -1.5)      # a hot master is brought down to -1 dBTP
        self.assertEqual(gain_db(-14.0, -0.5, target_lufs=-23.0), -9.0)
        self.assertIn("acts", FREQUENCIES)


class StoryShowTests(ShowTestBase):
    def setUp(self):
        super().setUp()
        titles = [t.title for t in self.lib.tracks()]
        self.story = Story(album="TTA", root=str(self.album), tracks=[
            StoryTrack(track_id=tid, title=title, act=act) for tid, title, act in zip(self.ids, titles, "AABB")])
        self.slug = self.story.slug
        for n in range(3):
            self.rendered(self.slug, f"segue:{self.slug}:{n}")
        for n in range(4):
            self.rendered(self.slug, f"recap:{self.slug}:{n}")
        self.rendered(self.slug, f"narration:{self.slug}:1")

    def story_show(self, **dials) -> Show:
        return Show(self.lib, self.store, voice="v", mode="story", story=self.story, **dials)

    def test_segue_only_on_in_order_transitions(self):
        t0, t1, t2, t3 = self.ids
        show = self.story_show()
        self.assertEqual(self.labels(show.next()), [f"track:{t0}"])
        self.assertEqual(self.labels(show.next()), [f"segue:{self.slug}:0", f"narration:{self.slug}:1", f"track:{t1}"])
        self.assertEqual(self.labels(show.jump(3)), [f"track:{t3}"])        # 1 -> 3: a jump, no segue
        self.assertEqual(self.labels(show.jump(2)), [f"track:{t2}"])        # backwards: no segue
        self.assertEqual(self.labels(show.next()), [f"segue:{self.slug}:2", f"track:{t3}"])

    def test_a_jump_to_the_very_next_track_is_in_order(self):
        show = self.story_show()
        show.next()
        self.assertEqual(self.labels(show.jump(1))[0], f"segue:{self.slug}:0")

    def test_story_order_wins_over_the_given_order_and_shuffle(self):
        show = self.story_show(shuffle=True, seed=3)
        with self.assertLogs("aura", level="WARNING"):
            show.set_queue(list(reversed(self.ids)) + ["not-in-story"])
        self.assertEqual(show.queue(), self.ids)

    def test_acts_dial_talks_only_when_the_act_changes(self):
        show = self.story_show(frequency="acts")
        said = [[i.break_id for i in show.next() if i.kind == "break" and i.break_id.startswith("segue")]
                for _ in self.ids]
        self.assertEqual(said, [[], [], [f"segue:{self.slug}:1"], []])

    def test_off_silences_the_story_too(self):
        show = self.story_show(frequency="off")
        show.next()
        self.assertEqual(self.labels(show.next()), [f"track:{self.ids[1]}"])

    def test_resume_after_a_long_gap_recaps_only_the_songs_before_the_one_playing(self):
        show = self.story_show()
        show.next()
        show.next()
        show.next()          # song 3 (index 2) is playing when the show stops
        state = show.state()
        self.assertEqual((state["queue"], state["position"], state["mode"]), (self.ids, 2, "story"))
        self.assertTrue(state["last_played_at"].endswith("+00:00"))
        later = self.story_show()
        later.restore(state)
        played_at = datetime.fromisoformat(state["last_played_at"])
        # the listener never finished song 3: the recap runs through song 2 (index 1), not through song 3
        self.assertEqual(self.labels(later.resume_items(played_at + timedelta(hours=7))), [f"recap:{self.slug}:1"])
        self.assertEqual(later.resume_items(played_at + timedelta(hours=1)), [])
        naive = (played_at + timedelta(hours=7)).replace(tzinfo=None)
        self.assertEqual(len(later.resume_items(naive)), 1)
        self.assertEqual(self.labels(later.next()), [f"segue:{self.slug}:2", f"track:{self.ids[3]}"])

    def test_no_recap_when_the_first_song_was_playing(self):
        show = self.story_show()
        show.next()
        later = self.story_show()
        later.restore(show.state())
        self.assertEqual(later.resume_items(datetime.now(timezone.utc) + timedelta(days=1)), [])

    def test_a_moved_album_still_plays_its_story(self):
        moved = self.tmp / "moved"
        moved.mkdir()
        os.rename(self.album, moved / "TTA")
        self.lib.scan(self.tmp)
        self.lib.remove_missing()
        old_ids = list(self.ids)
        with self.assertLogs("aura", level="INFO"):
            show = self.story_show()
        new_ids = [t.id for t in self.lib.tracks()]
        self.assertNotEqual(new_ids, old_ids)
        self.assertEqual(show.queue(), new_ids)
        self.assertEqual([t.track_id for t in self.story.tracks], new_ids)
        show.set_queue(old_ids)                          # ids the host saved before the move still work
        self.assertEqual(show.queue(), new_ids)
        self.assertEqual(self.labels(show.next()), [f"track:{new_ids[0]}"])
        self.assertEqual(self.labels(show.next())[0], f"segue:{self.slug}:0")

    def test_restoring_story_mode_without_a_story_stays_casual(self):
        state = self.story_show().state()
        casual = Show(self.lib, self.store)
        with self.assertLogs("aura", level="WARNING"):
            casual.restore(state)
        self.assertEqual((casual.mode, casual.queue()), ("casual", self.ids))
