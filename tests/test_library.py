# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Library: filename parsing, ffmpeg loudness parsing, and scanning tiny WAV files with no ffmpeg."""
from __future__ import annotations

import hashlib
import os
import threading
import unittest
from unittest import mock

from aura_test_support import TempDirTest, make_wav

from aura import library as L
from aura.library import (Library, measure_loudness, parse_ebur128, parse_filename, probe_duration,
                          split_performers, track_id_for)

# Captured from `ffmpeg -nostats -i song.mp3 -filter_complex ebur128=peak=true -f null -` (values edited).
EBUR128_SAMPLE = """\
[Parsed_ebur128_0 @ 0x55d0c8a1b900] t: 201.79998  TARGET:-23 LUFS    M: -13.9 S: -14.0     I: -14.3 LUFS       LRA:   6.0 LU  FTPK: -0.7 dBFS  TPK: -0.6 dBFS
[Parsed_ebur128_0 @ 0x55d0c8a1b900] t: 201.89998  TARGET:-23 LUFS    M: -13.8 S: -14.0     I: -14.2 LUFS       LRA:   6.1 LU  FTPK: -0.6 dBFS  TPK: -0.5 dBFS
[out#0/null @ 0x55d0c8a1a880] video:0kB audio:34762kB subtitle:0kB other streams:0kB global headers:0kB muxing overhead: unknown
size=N/A time=00:03:21.90 bitrate=N/A speed= 171x
[Parsed_ebur128_0 @ 0x55d0c8a1b900] Summary:

  Integrated loudness:
    I:         -14.2 LUFS
    Threshold: -24.5 LUFS

  Loudness range:
    LRA:         6.1 LU
    Threshold: -34.4 LUFS
    LRA low:   -19.1 LUFS
    LRA high:  -13.0 LUFS

  True peak:
    Peak:       -0.5 dBFS
"""


class ParseFilenameTests(unittest.TestCase):
    def check(self, name, track_no, title, performers, disc_no=None):
        got = parse_filename(name)
        self.assertEqual((got["track_no"], got["disc_no"], got["title"], got["performers"]),
                         (track_no, disc_no, title, performers), name)

    def test_contract_example(self):
        self.assertEqual(parse_filename("07 - The Velvet Lesson (Aphrodite).mp3"),
                         {"track_no": 7, "disc_no": None, "title": "The Velvet Lesson", "performers": ["Aphrodite"]})

    def test_single_performer(self):
        self.check("12 - I Will Email You (Valkyrie).mp3", 12, "I Will Email You", ["Valkyrie"])

    def test_two_performers(self):
        self.check("04 - Co-Parents (Valkyrie & Sarah).mp3", 4, "Co-Parents", ["Valkyrie", "Sarah"])
        self.check("04 - Co-Parents (Valkyrie and Sarah).mp3", 4, "Co-Parents", ["Valkyrie", "Sarah"])

    def test_comma_and_slash_separators(self):
        self.check("03 - Some Song (A, B).mp3", 3, "Some Song", ["A", "B"])
        self.assertEqual(split_performers("Objection (Lex, Meg / Tessa)"), ("Objection", ["Lex", "Meg", "Tessa"]))

    def test_version_words_stay_in_the_title(self):
        for word in ("Reprise", "Remix", "Live", "Instrumental", "Live at the Courthouse", "Part 2"):
            self.check(f"09 - Did You Miss Me ({word}).mp3", 9, f"Did You Miss Me ({word})", [])

    def test_only_the_last_group_is_performers(self):
        self.check("09 - Did You Miss Me (Reprise) (Valkyrie).mp3", 9, "Did You Miss Me (Reprise)", ["Valkyrie"])

    def test_no_number_and_no_performer(self):
        self.check("The Velvet Lesson (Aphrodite).flac", None, "The Velvet Lesson", ["Aphrodite"])
        self.check("Genesis.mp3", None, "Genesis", [])
        self.check("01 - Genesis.mp3", 1, "Genesis", [])

    def test_number_separators(self):
        self.check("07. The Velvet Lesson.mp3", 7, "The Velvet Lesson", [])
        self.check("07 The Velvet Lesson.mp3", 7, "The Velvet Lesson", [])
        self.check("07_The_Velvet_Lesson_(Aphrodite).mp3", 7, "The Velvet Lesson", ["Aphrodite"])

    def test_disc_track(self):
        self.check("1-07 Title.mp3", 7, "Title", [], disc_no=1)
        self.check("2-03 - Window Seat (Chloe).ogg", 3, "Window Seat", ["Chloe"], disc_no=2)

    def test_featuring(self):
        self.check("05 - Song (feat. Sarah).mp3", 5, "Song", ["Sarah"])
        self.check("05 - Song (Valkyrie) (feat. Sarah).mp3", 5, "Song", ["Valkyrie", "Sarah"])

    def test_subtitles_and_possessives_are_not_names(self):
        self.check("08 - Did You Miss Me (I Will Email You).mp3", 8, "Did You Miss Me (I Will Email You)", [])
        self.check("10 - Song (Sarah's Theme).mp3", 10, "Song (Sarah's Theme)", [])

    def test_full_paths_and_dotted_titles(self):
        self.check("D:\\Music\\TTA\\07 - X (Valkyrie).mp3", 7, "X", ["Valkyrie"])
        self.check("Mr. Roboto", None, "Mr. Roboto", [])


class LoudnessAndDurationTests(TempDirTest):
    def test_summary_block_is_parsed_not_the_running_values(self):
        self.assertEqual(parse_ebur128(EBUR128_SAMPLE), (-14.2, -0.5))

    def test_silence_and_missing_summary(self):
        silent = EBUR128_SAMPLE.replace("Peak:       -0.5 dBFS", "Peak:       -inf dBFS")
        self.assertEqual(parse_ebur128(silent), (-14.2, None))
        self.assertEqual(parse_ebur128("ffmpeg: no such file"), (None, None))

    def test_no_ffmpeg_means_no_measurement_and_no_crash(self):
        wav = make_wav(self.tmp / "a.wav")
        self.assertEqual(measure_loudness(wav, None), (None, None))
        with self.assertLogs("aura", level="WARNING"):
            self.assertEqual(measure_loudness(wav, str(self.tmp / "no-such-ffmpeg.exe")), (None, None))

    def test_wav_duration_without_tinytag_or_ffmpeg(self):
        wav = make_wav(self.tmp / "a.wav", seconds=0.5)
        self.assertAlmostEqual(probe_duration(wav, None), 0.5, places=3)
        self.assertEqual(probe_duration(self.tmp / "missing.mp3", None), 0.0)


class ScanTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.album = self.tmp / "music" / "The Turing Accords"
        self.files = [
            make_wav(self.album / "01 - Genesis.wav"),
            make_wav(self.album / "02 - The Velvet Lesson (Aphrodite).wav"),
            make_wav(self.album / "03 - Objection (Lex, Meg & Tessa).wav"),
            make_wav(self.album / "Disc 2" / "2-01 - Did You Miss Me (Reprise) (Valkyrie).wav"),
        ]
        self.lib = Library(self.tmp / "data", ffmpeg=None)
        self.addCleanup(self.lib.close)

    def test_a_damaged_wav_never_stops_the_scan(self):
        """A truncated RIFF header makes the stdlib wave/chunk modules raise RuntimeError, which once escaped
        and ended the whole scan (found by ATK's Qt test, 2026-10-05)."""
        bad = self.album / "02b - Broken.wav"
        bad.write_bytes(b"RIFF\x10\x00\x00\x00WAVEjunkjunkjunk")
        summary = self.lib.scan(self.tmp / "music")
        self.assertEqual(summary.found, 5)
        self.assertEqual(len(self.lib.tracks()), 5)        # indexed, with no length
        self.assertEqual(self.lib.track(track_id_for(bad)).duration_s, 0.0)

    def test_scan_indexes_every_file_in_order(self):
        lines = []
        summary = self.lib.scan(self.tmp / "music", progress=lines.append)
        self.assertEqual((summary.found, summary.added, summary.updated, summary.unchanged, summary.removed),
                         (4, 4, 0, 0, 0))
        self.assertEqual(summary.failed, [])
        self.assertTrue(lines)
        tracks = self.lib.tracks()
        self.assertEqual([t.title for t in tracks],
                         ["Genesis", "The Velvet Lesson", "Objection", "Did You Miss Me (Reprise)"])
        self.assertEqual(tracks[2].performers, ["Lex", "Meg", "Tessa"])
        self.assertEqual({t.album for t in tracks}, {"The Turing Accords"})
        self.assertEqual(tracks[3].disc_no, 2)
        self.assertAlmostEqual(tracks[0].duration_s, 0.5, places=2)
        self.assertIsNone(tracks[0].lufs)
        self.assertTrue((self.tmp / "data" / "aura.db").is_file())

    def test_track_id_is_sha1_of_normalised_path(self):
        self.lib.scan(self.tmp / "music")
        path = str(self.files[1])
        expected = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(track_id_for(path), expected)
        self.assertEqual(self.lib.track(expected).title, "The Velvet Lesson")

    def test_rescan_keeps_unchanged_rows(self):
        self.lib.scan(self.tmp / "music")
        summary = self.lib.scan(self.tmp / "music")
        self.assertEqual((summary.added, summary.updated, summary.unchanged), (0, 0, 4))

    def test_manual_fields_and_ingest_results_survive_a_rescan_of_a_changed_file(self):
        self.lib.scan(self.tmp / "music")
        tid = track_id_for(self.files[1])
        t = self.lib.update(tid, notes="Bill: she is teaching, not seducing", intro_s=3.25, summary="Mine.")
        self.assertEqual((t.intro_source, t.summary_source), ("manual", "manual"))
        self.lib.update(tid, transcript_path="x.json")
        make_wav(self.files[1], seconds=1.0)  # the file changes: new size and mtime
        st = os.stat(self.files[1])
        os.utime(self.files[1], (st.st_atime, st.st_mtime + 10))
        summary = self.lib.scan(self.tmp / "music")
        self.assertEqual((summary.updated, summary.unchanged), (1, 3))
        t = self.lib.track(tid)
        self.assertAlmostEqual(t.duration_s, 1.0, places=2)
        self.assertEqual((t.notes, t.intro_s, t.intro_source, t.summary, t.summary_source, t.transcript_path),
                         ("Bill: she is teaching, not seducing", 3.25, "manual", "Mine.", "manual", "x.json"))

    def test_removed_files_leave_the_index(self):
        self.lib.scan(self.tmp / "music")
        os.remove(self.files[0])
        summary = self.lib.scan(self.tmp / "music")
        self.assertEqual(summary.removed, 1)
        self.assertIsNone(self.lib.track(track_id_for(self.files[0])))

    def test_a_cancelled_scan_removes_nothing(self):
        self.lib.scan(self.tmp / "music")
        os.remove(self.files[0])
        stop = threading.Event()
        stop.set()
        summary = self.lib.scan(self.tmp / "music", cancel=stop)
        self.assertEqual(summary.removed, 0)
        self.assertIsNotNone(self.lib.track(track_id_for(self.files[0])))
        self.assertEqual(self.lib.scan(self.tmp / "music", cancel=lambda: False).removed, 1)

    def test_update_is_whitelisted(self):
        self.lib.scan(self.tmp / "music")
        tid = track_id_for(self.files[0])
        with self.assertRaises(ValueError):
            self.lib.update(tid, path="C:/elsewhere.mp3")
        with self.assertRaises(KeyError):
            self.lib.update("nope", notes="x")
        with self.assertRaises(ValueError):
            self.lib.update(tid, intro_source="guess")

    def test_albums_roots_and_filters(self):
        self.lib.scan(self.tmp / "music")
        self.assertEqual(self.lib.albums(), [{"album": "The Turing Accords", "root": str(self.album), "count": 4}])
        self.assertEqual(self.lib.roots(), [str(self.tmp / "music")])
        self.assertEqual(len(self.lib.tracks(root=str(self.album))), 4)
        self.assertEqual(len(self.lib.tracks(root=str(self.album / "Disc 2"))), 1)
        self.assertEqual(self.lib.tracks(album="Nope"), [])

    def test_remove_missing(self):
        self.lib.scan(self.tmp / "music")
        os.remove(self.files[2])
        self.assertEqual(self.lib.remove_missing(), 1)
        self.assertEqual(len(self.lib.tracks()), 3)

    def test_index_survives_reopening(self):
        self.lib.scan(self.tmp / "music")
        self.lib.close()
        with Library(self.tmp / "data") as again:
            self.assertEqual(len(again.tracks()), 4)

    def test_data_dir_inside_the_scanned_folder_is_not_indexed(self):
        inner = Library(self.album / "data")
        self.addCleanup(inner.close)
        make_wav(self.album / "data" / "breaks" / "intro_x.wav")
        self.assertEqual(inner.scan(self.album).found, 4)

    def test_a_note_saved_while_loudness_is_measured_survives(self):
        self.lib.scan(self.tmp / "music", measure=False)
        self.lib.ffmpeg = "fake-ffmpeg"
        velvet, objection = track_id_for(self.files[1]), track_id_for(self.files[2])
        make_wav(self.files[2], seconds=1.0)      # this one changed on disk: it is re-read, then measured
        st = os.stat(self.files[2])
        os.utime(self.files[2], (st.st_atime, st.st_mtime + 10))

        def slow_measure(path, ffmpeg):
            # what the host saves for these tracks while ffmpeg is busy measuring them
            if os.path.abspath(path) == os.path.abspath(self.files[1]):
                self.lib.update(velvet, notes="typed during the scan", intro_s=1.5)
            if os.path.abspath(path) == os.path.abspath(self.files[2]):
                self.lib.update(objection, summary="ingested during the scan", summary_source="model")
            return -20.0, -3.0

        with mock.patch.object(L, "measure_loudness", slow_measure):
            summary = self.lib.scan(self.tmp / "music")
        self.assertEqual(summary.updated, 4)
        t = self.lib.track(velvet)
        self.assertEqual((t.notes, t.intro_s, t.intro_source, t.lufs), ("typed during the scan", 1.5, "manual", -20.0))
        t = self.lib.track(objection)
        self.assertEqual((t.summary, t.summary_source, t.lufs), ("ingested during the scan", "model", -20.0))
        self.assertAlmostEqual(t.duration_s, 1.0, places=2)

    def test_tags_win_when_tinytag_is_available(self):
        class FakeTag:
            title, artist, album, track, disc, duration = "Velvet (Aphrodite)", "Bill", "Tagged", 9, None, 123.0

        class FakeTinyTag:
            @staticmethod
            def get(path):
                return FakeTag()

        saved = (L._tinytag_class, L._tinytag_tried)
        L._tinytag_class, L._tinytag_tried = FakeTinyTag, True
        try:
            self.lib.scan(self.album, measure=False)
        finally:
            L._tinytag_class, L._tinytag_tried = saved
        t = self.lib.track(track_id_for(self.files[0]))
        self.assertEqual((t.title, t.performers, t.album, t.artist, t.track_no, t.duration_s),
                         ("Velvet", ["Aphrodite"], "Tagged", "Bill", 9, 123.0))


if __name__ == "__main__":
    unittest.main()
