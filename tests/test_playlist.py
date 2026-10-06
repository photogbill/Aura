# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Playlists: .m3u8 written and read back, with non-ASCII titles and relative paths."""
from __future__ import annotations

import os
import unittest

from aura_test_support import TempDirTest

from aura.library import Track
from aura.playlist import playlists_dir, read_m3u8, write_m3u8


class PlaylistTests(TempDirTest):
    def test_round_trip_with_non_ascii_titles_and_relative_paths(self):
        folder = self.tmp / "music"
        tracks = [
            Track(id="1", path=str(folder / "TTA" / "01 - Genèse.mp3"), root=str(folder), title="Genèse",
                  artist="Bill", duration_s=201.6),
            Track(id="2", path=str(folder / "TTA" / "02 - 東京の夜 (Hana).mp3"), root=str(folder),
                  title="東京の夜 — Ünïcödé", duration_s=0.0),
        ]
        out = write_m3u8(folder / "show.m3u8", tracks)
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#EXTM3U\n"))
        self.assertIn("#EXTINF:202,Bill - Genèse\n", text)
        self.assertIn("#EXTINF:-1,東京の夜 — Ünïcödé\n", text)
        self.assertIn(os.path.join("TTA", "01 - Genèse.mp3") + "\n", text)
        self.assertNotIn(str(folder), text)
        self.assertNotIn("\r", text)
        back = read_m3u8(out)
        self.assertEqual([b["path"] for b in back], [t.path for t in tracks])
        self.assertEqual([b["title"] for b in back], ["Bill - Genèse", "東京の夜 — Ünïcödé"])
        self.assertEqual([b["duration_s"] for b in back], [202.0, 0.0])

    def test_paths_outside_the_playlist_folder_are_absolute(self):
        elsewhere = str(self.tmp / "elsewhere" / "song.flac")
        out = write_m3u8(playlists_dir(self.tmp / "data") / "mix.m3u8",
                         [{"path": elsewhere, "title": "Song", "duration_s": 61}])
        self.assertEqual(out, self.tmp / "data" / "playlists" / "mix.m3u8")
        self.assertIn(elsewhere + "\n", out.read_text(encoding="utf-8"))
        self.assertEqual(read_m3u8(out)[0], {"path": elsewhere, "title": "Song", "duration_s": 61.0})

    def test_reads_a_bom_and_entries_without_extinf(self):
        playlist = self.tmp / "p.m3u8"
        playlist.write_bytes("\ufeff#EXTM3U\nsub/a.mp3\n".encode("utf-8"))
        self.assertEqual(read_m3u8(playlist),
                         [{"path": str(self.tmp / "sub" / "a.mp3"), "title": "a", "duration_s": 0.0}])


if __name__ == "__main__":
    unittest.main()
