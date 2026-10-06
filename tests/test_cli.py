# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""The command line, end to end, in a temporary data folder and without ffmpeg."""
from __future__ import annotations

import contextlib
import io

from aura_test_support import TempDirTest, make_wav

from aura.cli import default_data_dir, main


class CliTests(TempDirTest):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([*args, "--data", str(self.tmp / "data"), "--no-ffmpeg"])
        return code, out.getvalue()

    def test_scan_tracks_playlist_story_and_script_check(self):
        album = self.tmp / "music" / "The Turing Accords"
        make_wav(album / "01 - Genesis.wav")
        make_wav(album / "02 - The Velvet Lesson (Aphrodite).wav")
        code, text = self.run_cli("scan", str(self.tmp / "music"), "--no-measure")
        self.assertEqual(code, 0)
        self.assertIn("2 found, 2 added", text)
        code, text = self.run_cli("tracks")
        self.assertEqual(code, 0)
        self.assertIn("The Velvet Lesson | Aphrodite | 0:00", text)
        code, text = self.run_cli("playlist", "all.m3u8")
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "data" / "playlists" / "all.m3u8").is_file())
        code, text = self.run_cli("story-draft", str(album))
        self.assertEqual(code, 0)
        self.assertIn("Characters: Aphrodite", text)
        self.assertTrue((self.tmp / "data" / "stories" / "the-turing-accords.json").is_file())
        code, text = self.run_cli("story-draft", str(album))
        self.assertEqual(code, 1)  # never silently replaces Bill's story
        script = self.tmp / "data" / "scripts" / "casual.txt"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("=== intro:x | status: aproved\nHello.\n", encoding="utf-8")
        with self.assertLogs("aura", level="WARNING"):
            code, text = self.run_cli("script-check", "casual")
        self.assertEqual(code, 0)
        self.assertIn("draft: 1", text)
        self.assertIn("unknown status 'aproved'", text)
        self.assertEqual(self.run_cli("script-check", "nothing")[0], 2)

    def test_default_data_dir_is_beside_the_code(self):
        self.assertEqual(default_data_dir().name, "data")
        self.assertTrue((default_data_dir().parent / "aura" / "cli.py").is_file())
