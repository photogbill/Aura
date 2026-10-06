# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""The script file Bill edits: exact round trip, forgiving parsing, and stale detection."""
from __future__ import annotations

import unittest

from aura.script import (Break, clean_text, lint_script, parse_script, refresh_status, render_script, segue_id,
                         text_hash)


def sample_breaks() -> list[Break]:
    return [
        Break(id="segue:the-turing-accords:3", kind="segue", status="rendered",
              title='after 4 "Title A" → 5 "Title B"',
              text="The text of the break,\nas many lines as it needs.\n\nEven a second paragraph.",
              voice="host-cut", audio="breaks/segue_the-turing-accords_3.wav", text_hash="1a2b3c4d5e6f7a8b",
              engine="chatterbox", lufs=-18.2),
        Break(id="intro:9f1c2d3e4f5a6b7c", kind="intro", text="Here is Aphrodite — café au lait.",
              status="draft", title='before "Title"'),
        Break(id="recap:the-turing-accords:0", kind="recap", text="Line one", status="needs_rerender",
              voice="host-cut", audio="breaks/recap.wav", text_hash="ffff", fell_back=True, engine="piper",
              note="Chatterbox unavailable · used Piper"),
        Break(id="station_id:1", kind="station_id", text="=== this line is text\n\\ and so is this\nvoice: x"),
        Break(id="back:abc", kind="back", text="voice: x · audio: y · looks like a render line"),
        Break(id="notes:album", kind="notes", text="", status="approved"),
    ]


class RoundTripTests(unittest.TestCase):
    def test_parse_render_round_trip_keeps_every_field(self):
        breaks = sample_breaks()
        self.assertEqual(parse_script(render_script(breaks)), breaks)

    def test_the_file_reads_the_way_the_brief_shows(self):
        text = render_script(sample_breaks()[:1])
        self.assertIn('=== segue:the-turing-accords:3 | after 4 "Title A" → 5 "Title B" | status: rendered\n'
                      "voice: host-cut · audio: breaks/segue_the-turing-accords_3.wav · hash: 1a2b3c4d5e6f7a8b"
                      " · engine: chatterbox · lufs: -18.2\nThe text of the break,\n", text)
        unrendered = render_script(sample_breaks()[1:2]).split("\n=== ", 1)[1]
        self.assertNotIn("\nvoice:", unrendered)  # no render line before a render

    def test_kind_must_match_the_id(self):
        with self.assertRaises(ValueError):
            render_script([Break(id="intro:x", kind="back", text="t")])

    def test_ids(self):
        self.assertEqual(segue_id("the-turing-accords", 3), "segue:the-turing-accords:3")


class ForgivingParserTests(unittest.TestCase):
    def test_notepad_edits(self):
        text = ("﻿My own heading line, ignored\r\n"
                "=== segue:tta:0 | after 1 \"A\" → 2 \"B\" | Status: Approved\r\n"
                "\r\n"
                "voice: v · audio: - · hash: - · engine: - · lufs: -\r\n"
                "Words Bill typed.   \r\n"
                "A line that is not a field: kept as text\r\n"
                "\r\n"
                "===intro:x|status:aproved\r\n"
                "Typo in the status.\r\n")
        with self.assertLogs("aura", level="WARNING"):
            first, second = parse_script(text)
        self.assertEqual((first.id, first.kind, first.status, first.voice), ("segue:tta:0", "segue", "approved", "v"))
        self.assertEqual(first.text, "Words Bill typed.\nA line that is not a field: kept as text")
        self.assertEqual((second.id, second.status, second.text), ("intro:x", "draft", "Typo in the status."))
        with self.assertLogs("aura", level="WARNING"):
            self.assertTrue(any("aproved" in p for p in lint_script(text)))

    def test_lint_finds_duplicates(self):
        text = render_script([Break(id="intro:x", kind="intro", text="a"), Break(id="intro:x", kind="intro", text="b")])
        self.assertTrue(any("more than once" in p for p in lint_script(text)))


class StaleTests(unittest.TestCase):
    def rendered(self, text="Hello there.", voice="host-cut") -> Break:
        return Break(id="intro:x", kind="intro", text=text, status="rendered", voice=voice, audio="breaks/x.wav",
                     text_hash=text_hash(text))

    def test_untouched_break_stays_rendered(self):
        self.assertEqual(refresh_status(self.rendered(), "host-cut").status, "rendered")
        self.assertEqual(refresh_status(self.rendered(), "").status, "rendered")

    def test_edited_text_is_stale(self):
        b = self.rendered()
        b.text = "Hello there, listeners."
        self.assertEqual(refresh_status(b, "host-cut").status, "stale")
        self.assertEqual(b.status, "rendered")  # a copy is returned; the input is not changed

    def test_line_endings_and_trailing_spaces_are_not_edits(self):
        b = self.rendered(text="One\nTwo")
        b.text = "One  \r\nTwo\r\n"
        self.assertEqual(refresh_status(b, "host-cut").status, "rendered")
        self.assertEqual(clean_text("\n\n  a  \r\n\r\n"), "  a")

    def test_changed_voice_is_stale(self):
        self.assertEqual(refresh_status(self.rendered(), "host-warm").status, "stale")

    def test_only_rendered_breaks_go_stale(self):
        b = Break(id="intro:x", kind="intro", text="edited", status="approved", text_hash="old")
        self.assertEqual(refresh_status(b, "v").status, "approved")


if __name__ == "__main__":
    unittest.main()
