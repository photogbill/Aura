# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Music sources: folder rules with subfolders only when asked, file types per rule, one song on its own; one
scan over all of them that removes only what a present rule no longer takes; a stick that is not plugged in
loses nothing; and the browser's working copy - what each click does and how each folder and file shows."""
from __future__ import annotations

import os
import shutil

from aura_test_support import TempDirTest, make_wav

from aura.library import Library
from aura.sources import Source, SourceSet, count, files_for, overlaps, resolve, sync


class Tree(TempDirTest):
    """music/
         Album/            01 - One.mp3, 01 - One.wav, 02 - Two.mp3, 03 - Three.flac
           Bonus/          04 - Four.mp3
         Other/            05 - Five.mp3
    """

    def setUp(self):
        super().setUp()
        self.music = self.tmp / "music"
        self.album = self.music / "Album"
        self.bonus = self.album / "Bonus"
        self.other = self.music / "Other"
        for path in (self.album / "01 - One.mp3", self.album / "01 - One.wav", self.album / "02 - Two.mp3",
                     self.album / "03 - Three.flac", self.bonus / "04 - Four.mp3", self.other / "05 - Five.mp3"):
            make_wav(path, 0.2)            # the bytes are a WAV either way; only the name matters here
        self.lib = Library(self.tmp / "data")
        self.addCleanup(self.lib.close)

    def names(self):
        return sorted(os.path.basename(t.path) for t in self.lib.tracks())


class SourceTests(Tree):
    def test_a_rule_round_trips_and_normalises(self):
        s = Source(path=str(self.album), types=("MP3", ".flac", "mp3"), one_copy="WAV", volume="MUSIC")
        self.assertEqual(s.types, (".mp3", ".flac"))
        self.assertEqual(s.one_copy, ".wav")
        self.assertFalse(s.subfolders)                       # never assumed
        self.assertEqual(Source.from_dict(s.to_dict()), s)
        with self.assertRaises(ValueError):
            Source(path=str(self.album), types=(".txt",))
        with self.assertRaises(ValueError):
            Source.from_dict({"kind": "folder"})
        one = Source(path=str(self.album / "02 - Two.mp3"), kind="file", subfolders=True, types=(".wav",))
        self.assertEqual((one.subfolders, one.types), (False, ()))
        self.assertEqual(one.describe(), "one song")
        self.assertEqual(s.describe(), "MP3 + FLAC, one copy per song (WAV first) · this folder only")

    def test_this_folder_only_unless_subfolders_are_asked_for(self):
        _found, kept = files_for(Source(path=str(self.album)))
        self.assertNotIn("04 - Four.mp3", [os.path.basename(p) for p in kept])
        _found, kept = files_for(Source(path=str(self.album), subfolders=True))
        self.assertIn("04 - Four.mp3", [os.path.basename(p) for p in kept])

    def test_mp3_only_and_one_copy_per_song(self):
        found, kept = files_for(Source(path=str(self.album), types=(".mp3",)))
        self.assertEqual(len(found), 4)
        self.assertEqual(sorted(os.path.basename(p) for p in kept), ["01 - One.mp3", "02 - Two.mp3"])
        _found, kept = files_for(Source(path=str(self.album), one_copy=".wav"))
        self.assertEqual(sorted(os.path.basename(p) for p in kept),
                         ["01 - One.wav", "02 - Two.mp3", "03 - Three.flac"])

    def test_the_deeper_rule_names_the_folder_a_song_is_filed_under(self):
        entries, found = resolve([Source(path=str(self.music), subfolders=True),
                                  Source(path=str(self.bonus))])
        roots = {os.path.basename(p): r for p, r in entries}
        self.assertEqual(roots["04 - Four.mp3"], str(self.bonus))
        self.assertEqual(roots["05 - Five.mp3"], str(self.music))
        self.assertEqual(found, 6)

    def test_counting_what_a_rule_takes(self):
        c = count(Source(path=str(self.album), subfolders=True, types=(".mp3",)))
        self.assertEqual((c.kept, c.left_out, c.folders), ({".mp3": 3}, {".wav": 1, ".flac": 1}, 2))
        self.assertGreater(c.bytes, 0)
        self.assertTrue(count(Source(path=str(self.album), subfolders=True), cancel=lambda: True).cancelled)
        self.assertEqual(count(Source(path=str(self.tmp / "gone"))).total(), 0)
        quick = count(Source(path=str(self.music), subfolders=True), stop_after=(1000, 2))
        self.assertTrue(quick.stopped_early)
        self.assertEqual(quick.folders, 2)

    def test_overlaps_are_named(self):
        outer = Source(path=str(self.music), subfolders=True)
        inner = Source(path=str(self.album), types=(".mp3",))
        flat = Source(path=str(self.music))                   # this folder only: reaches nothing below
        song = Source(path=str(self.other / "05 - Five.mp3"), kind="file")
        pairs = overlaps([outer, inner, flat, song])
        self.assertIn((inner, outer), pairs)
        self.assertIn((song, outer), pairs)
        self.assertNotIn((inner, flat), pairs)


class SyncTests(Tree):
    def test_one_run_over_several_rules(self):
        summary = sync(self.lib, [Source(path=str(self.album), types=(".mp3",)),
                                  Source(path=str(self.other / "05 - Five.mp3"), kind="file")], measure=False)
        self.assertEqual(self.names(), ["01 - One.mp3", "02 - Two.mp3", "05 - Five.mp3"])
        self.assertEqual((summary.found, summary.skipped, summary.added), (5, 2, 3))

    def test_a_source_that_is_not_there_loses_nothing(self):
        stick = self.tmp / "stick" / "Field Music"
        make_wav(stick / "06 - Six.mp3", 0.2)
        rules = [Source(path=str(self.other)), Source(path=str(stick), volume="FIELD")]
        sync(self.lib, rules, measure=False)
        six = next(t for t in self.lib.tracks() if t.title == "Six")
        self.lib.update(six.id, notes="Bill's note.")
        shutil.move(str(self.tmp / "stick"), str(self.tmp / "unplugged"))       # the stick is pulled
        summary = sync(self.lib, rules, measure=False)
        self.assertEqual(summary.unavailable, [str(stick)])
        self.assertEqual(summary.removed, 0)
        self.assertEqual(self.lib.track(six.id).notes, "Bill's note.")

    def test_only_what_a_present_rule_no_longer_takes_is_removed(self):
        sync(self.lib, [Source(path=str(self.album), subfolders=True)], measure=False)
        one_wav = next(t for t in self.lib.tracks() if t.path.endswith("01 - One.wav"))
        self.lib.update(one_wav.id, notes="Made on the WAV.")
        sync(self.lib, [Source(path=str(self.other))], measure=False)            # another rule: Album untouched
        self.assertIn("04 - Four.mp3", self.names())
        summary = sync(self.lib, [Source(path=str(self.album), subfolders=True, types=(".mp3",))], measure=False)
        self.assertEqual(self.names(), ["01 - One.mp3", "02 - Two.mp3", "04 - Four.mp3", "05 - Five.mp3"])
        one_mp3 = next(t for t in self.lib.tracks() if t.path.endswith("01 - One.mp3"))
        self.assertEqual(one_mp3.notes, "Made on the WAV.")                     # the work moved with the song
        self.assertEqual(summary.carried, {one_wav.id: one_mp3.id})

    def test_this_folder_only_never_removes_what_is_below_it(self):
        sync(self.lib, [Source(path=str(self.album), subfolders=True)], measure=False)
        sync(self.lib, [Source(path=str(self.album))], measure=False)
        self.assertIn("04 - Four.mp3", self.names())

    def test_scan_without_subfolders(self):
        self.lib.scan(self.album, subfolders=False, measure=False)
        self.assertNotIn("04 - Four.mp3", self.names())

    def test_a_stop_removes_nothing(self):
        sync(self.lib, [Source(path=str(self.album))], measure=False)
        os.remove(self.album / "02 - Two.mp3")
        summary = sync(self.lib, [Source(path=str(self.album))], measure=False, cancel=lambda: True)
        self.assertEqual(summary.removed, 0)
        self.assertIn("02 - Two.mp3", self.names())


class BrowserTests(Tree):
    def setUp(self):
        super().setUp()
        self.set = SourceSet()

    def test_a_tick_is_this_folder_only_and_subfolders_is_a_second_step(self):
        self.assertTrue(self.set.toggle_folder(self.album, types=(".mp3",)))
        s = self.set.find(self.album)
        self.assertEqual((s.subfolders, s.types), (False, (".mp3",)))
        self.assertEqual(self.set.folder_state(self.album), "rule")
        self.assertEqual(self.set.folder_state(self.bonus), "")              # not reached
        self.set.set_subfolders(self.album, True)
        self.assertEqual(self.set.folder_state(self.album), "rule_sub")
        self.assertEqual(self.set.folder_state(self.bonus), "reached")
        self.assertEqual(self.set.folder_state(self.music), "contains")
        self.assertFalse(self.set.toggle_folder(self.album))                 # untick
        self.assertEqual(self.set.sources, [])

    def test_a_rule_inside_another_rules_reach_is_an_overlap(self):
        self.set.toggle_folder(self.music)
        self.set.set_subfolders(self.music, True)
        self.set.toggle_folder(self.album, types=(".mp3",))
        self.assertEqual(self.set.folder_state(self.album), "overlap")
        self.assertEqual(self.set.folder_state(self.music), "rule_sub")

    def test_how_each_file_shows(self):
        self.set.toggle_folder(self.album, types=(".mp3",))
        self.assertEqual(self.set.file_state(self.album / "01 - One.mp3"), "by_rule")
        self.assertEqual(self.set.file_state(self.album / "01 - One.wav"), "left_out")
        self.assertEqual(self.set.file_state(self.other / "05 - Five.mp3"), "")
        self.assertTrue(self.set.toggle_file(self.album / "01 - One.mp3"))   # already in: not picked twice
        self.assertIsNone(self.set.find(self.album / "01 - One.mp3", "file"))
        self.assertTrue(self.set.toggle_file(self.other / "05 - Five.mp3"))
        self.assertEqual(self.set.file_state(self.other / "05 - Five.mp3"), "picked")
        self.assertEqual(self.set.folder_state(self.other), "picked")
        self.assertFalse(self.set.toggle_file(self.other / "05 - Five.mp3"))
        self.assertEqual(self.set.file_state(self.other / "05 - Five.mp3"), "")

    def test_tag_all_and_untag_all_shown(self):
        files = [str(self.album / "01 - One.wav"), str(self.album / "03 - Three.flac")]
        self.assertEqual(self.set.set_files(files, True), 2)
        self.assertEqual(self.set.set_files(files, True), 0)
        self.assertEqual(self.set.summary(), {"folders": 0, "with_subfolders": 0, "songs": 2})
        self.assertEqual(self.set.set_files(files, False), 2)
        self.assertEqual(self.set.sources, [])

    def test_duplicates_are_dropped_on_the_way_in(self):
        s = Source(path=str(self.album))
        self.assertEqual(len(SourceSet([s, Source(path=str(self.album) + os.sep)]).sources), 1)
