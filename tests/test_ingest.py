# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Ingest: first sung word, the summary prompt, and a resumable batch with fake Whisper and a fake model."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from aura_test_support import TempDirTest, make_wav

from aura.common import NSTR_RULE
from aura.ingest import first_vocal, ingest, summary_prompt
from aura.library import Library, Track, track_id_for


class FirstVocalTests(unittest.TestCase):
    def test_skips_music_markers_and_phantoms(self):
        segments = [
            {"start": 0.0, "end": 4.0, "text": " [Music]"},
            {"start": 4.0, "end": 6.0, "text": "(music)"},
            {"start": 6.0, "end": 8.0, "text": " \u266a \u266a"},
            {"start": 8.0, "end": 9.0, "text": "..."},
            {"start": 9.0, "end": 10.0, "text": " Thank you."},
            {"start": 12.5, "end": 15.0, "text": " Velvet, let him fall"},
            {"start": 15.0, "end": 18.0, "text": " more words"},
        ]
        self.assertEqual(first_vocal(segments), 12.5)

    def test_instrumental_has_no_first_vocal(self):
        self.assertIsNone(first_vocal([{"start": 0.0, "end": 30.0, "text": "[Instrumental]"}]))
        self.assertIsNone(first_vocal([]))


class SummaryPromptTests(unittest.TestCase):
    def test_prompt_uses_only_the_given_material_and_ends_with_nstr(self):
        track = Track(id="abc", path="D:/secret/folder/02 - Velvet (Aphrodite).mp3", root="D:/secret",
                      title="The Velvet Lesson", album="The Turing Accords", track_no=2, performers=["Aphrodite"],
                      notes="Aphrodite teaches Justicia.", summary="OLD SUMMARY", duration_s=200.0)
        system, user = summary_prompt(track, "velvet, let him fall")
        self.assertTrue(system.endswith(NSTR_RULE) and user.endswith(NSTR_RULE))
        for wanted in ("The Velvet Lesson", "Aphrodite", "Aphrodite teaches Justicia.", "velvet, let him fall"):
            self.assertIn(wanted, user)
        self.assertIn("machine-heard", system)
        self.assertIn("3 to 5", system)
        self.assertNotIn("D:/secret", user)       # the path is not material
        self.assertNotIn("OLD SUMMARY", user)     # nor is an earlier summary


class IngestTests(TempDirTest):
    def setUp(self):
        super().setUp()
        album = self.tmp / "music" / "TTA"
        self.paths = [make_wav(album / "01 - Genesis.wav"), make_wav(album / "02 - The Velvet Lesson (Aphrodite).wav"),
                      make_wav(album / "03 - Objection (Lex).wav")]
        self.lib = Library(self.tmp / "data")
        self.addCleanup(self.lib.close)
        self.lib.scan(self.tmp / "music")
        self.ids = [track_id_for(p) for p in self.paths]
        self.heard: list[str] = []
        self.asked: list[str] = []
        self.broken = ""

    def transcribe(self, path):
        self.heard.append(path)
        if self.broken and self.broken in path:
            raise RuntimeError("whisper ran out of memory")
        name = Path(path).stem
        return {"text": f"lyrics of {name}", "language": "en", "duration": 0.5,
                "segments": [{"start": 0.0, "end": 12.0, "text": "[Music]"},
                             {"start": 12.0, "end": 14.0, "text": f"lyrics of {name}"}]}

    def ask(self, system, user):
        self.asked.append(user)
        return " nstr " if "Objection" in user else '"A song about learning."'

    def test_batch_writes_transcripts_intros_and_summaries(self):
        result = ingest(self.lib, self.ids, transcribe=self.transcribe, ask=self.ask)
        self.assertEqual((result.done, result.skipped, result.nstr, result.failed), (3, 0, 1, []))
        velvet = self.lib.track(self.ids[1])
        self.assertEqual((velvet.intro_s, velvet.intro_source), (12.0, "whisper"))
        self.assertEqual((velvet.summary, velvet.summary_source), ("A song about learning.", "model"))
        record = json.loads(Path(velvet.transcript_path).read_text(encoding="utf-8"))
        self.assertEqual(Path(velvet.transcript_path), self.tmp / "data" / "transcripts" / f"{velvet.id}.json")
        self.assertEqual(record["text"], "lyrics of 02 - The Velvet Lesson (Aphrodite)")
        objection = self.lib.track(self.ids[2])
        self.assertEqual((objection.summary, objection.summary_source), ("", "nstr"))

    def test_it_resumes_instead_of_redoing(self):
        ingest(self.lib, self.ids, transcribe=self.transcribe, ask=self.ask)
        heard, asked = len(self.heard), len(self.asked)
        again = ingest(self.lib, self.ids, transcribe=self.transcribe, ask=self.ask)
        self.assertEqual((again.done, again.skipped), (0, 3))
        self.assertEqual((len(self.heard), len(self.asked)), (heard, asked))

    def test_transcripts_first_then_summaries_later(self):
        first = ingest(self.lib, self.ids, transcribe=self.transcribe)
        self.assertEqual((first.done, len(self.asked)), (3, 0))
        second = ingest(self.lib, self.ids, transcribe=self.transcribe, ask=self.ask)
        self.assertEqual((second.done, second.nstr), (3, 1))
        self.assertEqual(len(self.heard), 3)  # no song was transcribed twice
        self.assertIn("lyrics of 01 - Genesis", self.asked[0])

    def test_one_failing_track_does_not_stop_the_batch(self):
        self.broken = "Velvet"
        with self.assertLogs("aura", level="WARNING"):
            result = ingest(self.lib, self.ids + ["not-a-track"], transcribe=self.transcribe, ask=self.ask)
        self.assertEqual(result.done, 2)
        self.assertEqual(len(result.failed), 2)
        self.assertIn("whisper ran out of memory", result.failed[0])
        self.assertEqual(self.lib.track(self.ids[1]).transcript_path, "")

    def test_force_redoes_but_never_overwrites_bills_own_values(self):
        ingest(self.lib, self.ids, transcribe=self.transcribe, ask=self.ask)
        self.lib.update(self.ids[0], intro_s=2.0, summary="Bill's own words.")
        ingest(self.lib, self.ids, transcribe=self.transcribe, ask=self.ask, force=True)
        t = self.lib.track(self.ids[0])
        self.assertEqual((t.intro_s, t.intro_source, t.summary, t.summary_source),
                         (2.0, "manual", "Bill's own words.", "manual"))
        self.assertEqual(len(self.heard), 6)

    def test_cancel_stops_before_the_next_track(self):
        calls = []
        result = ingest(self.lib, self.ids, transcribe=self.transcribe, cancel=lambda: len(self.heard) >= 1,
                        progress=calls.append)
        self.assertEqual(result.done, 1)
        self.assertTrue(any("cancelled" in line for line in calls))


if __name__ == "__main__":
    unittest.main()
