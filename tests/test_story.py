# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Story mode: drafting, lean sheets, story files, and THE SPOILER LINE.

The spoiler tests plant a sentinel string in every field of every track. Break n's prompts may contain
material from tracks 0..n, plus track n+1's tease and title (segues only) - and nothing else from n+1 on.
"""
from __future__ import annotations

import json
import unittest

from aura_test_support import TempDirTest, make_wav

from aura.common import NSTR_RULE
from aura.ingest import ingest
from aura.library import Library
from aura.breaks import BreakStore
from aura.script import Break
from aura.story import (DEPTHS, STORY_PACK_NAME, NeedsCharacter, Story, StoryTrack, attach_sheets,
                        characters_needed, draft_story, known_through, load_story, parse_lean_sheet,
                        recap_prompt, relink_story, save_story, segue_prompt, story_path)

FIELDS = ("TITLE", "ACT", "CHAR", "SCENE", "LYRICS", "SUMMARY", "TEASE", "NOTES", "ID", "FILE")


def mark(field: str, k: int) -> str:
    return f"{field}_{k}_Q"  # the _Q stops TITLE_1_Q from matching inside TITLE_11_Q


def sentinel_story(count: int = 6) -> Story:
    return Story(album="The Turing Accords", root="D:/Music/The Turing Accords", tracks=[
        StoryTrack(track_id=mark("ID", k), title=mark("TITLE", k), act=mark("ACT", k), characters=[mark("CHAR", k)],
                   scene=mark("SCENE", k), lyrics=mark("LYRICS", k), lyrics_source="sheet",
                   summary=mark("SUMMARY", k), tease=mark("TEASE", k), notes=mark("NOTES", k),
                   file=mark("FILE", k))
        for k in range(count)])


def every_sheet(name: str) -> dict:
    # the sheet repeats the name, so a sheet leaked for a later character would show up in the prompt
    return {"voice_type": f"voice of {name}", "history": f"history of {name}"}


class SpoilerLineTests(unittest.TestCase):
    def assert_spoiler_free(self, text: str, n: int, *, allowed_next: tuple[str, ...]) -> None:
        for k in range(n + 1, 6):
            for field in FIELDS:
                if k == n + 1 and field in allowed_next:
                    continue
                self.assertNotIn(mark(field, k), text, f"{field} of track {k} leaked into break {n}")

    def test_known_through_returns_nothing_from_later_tracks(self):
        story = sentinel_story()
        for n in range(6):
            known = known_through(story, n)
            self.assertEqual(set(known), {"heard", "tease", "next_title"})
            self.assertEqual(len(known["heard"]), n + 1)
            self.assert_spoiler_free(json.dumps(known), n, allowed_next=("TEASE", "TITLE"))
        self.assertEqual(known_through(story, 5)["tease"], "")
        with self.assertRaises(IndexError):
            known_through(story, 6)

    def test_segue_prompts_hold_nothing_from_later_tracks(self):
        story = sentinel_story()
        for n in range(6):
            for depth in DEPTHS:
                for with_title in (True, False):
                    system, user = segue_prompt(story, n, character_sheet=every_sheet, depth=depth,
                                                include_next_title=with_title)
                    allowed = ("TEASE", "TITLE") if with_title else ("TEASE",)
                    self.assert_spoiler_free(system + user, n, allowed_next=allowed)

    def test_recap_prompts_hold_nothing_from_the_next_track_at_all(self):
        story = sentinel_story()
        for n in range(6):
            system, user = recap_prompt(story, n, character_sheet=every_sheet)
            self.assert_spoiler_free(system + user, n, allowed_next=())

    def test_the_tease_and_title_of_the_next_track_do_reach_the_segue(self):
        story = sentinel_story()
        for n in range(5):
            _system, user = segue_prompt(story, n, character_sheet=every_sheet)
            self.assertIn(mark("TEASE", n + 1), user)
            self.assertIn(mark("TITLE", n + 1), user)
            self.assertIn(mark("LYRICS", n), user)            # the song that just ended, in full
            self.assertIn(f"history of {mark('CHAR', n)}", user)
            self.assertIn(mark("TITLE", 0), user)
            _system, user = segue_prompt(story, n, character_sheet=every_sheet, include_next_title=False)
            self.assertNotIn(mark("TITLE", n + 1), user)

    def test_every_prompt_ends_with_the_nstr_rule(self):
        story = sentinel_story()
        for system, user in (segue_prompt(story, 2, character_sheet=every_sheet),
                             recap_prompt(story, 2, character_sheet=every_sheet)):
            self.assertTrue(system.endswith(NSTR_RULE))
            self.assertTrue(user.endswith(NSTR_RULE))
            self.assertIn("ONLY the material", system)
        with self.assertRaises(ValueError):
            segue_prompt(story, 2, character_sheet=every_sheet, depth="epic")


class NeedsCharacterTests(unittest.TestCase):
    def test_a_character_without_a_sheet_or_voice_type_blocks_the_prompt(self):
        story = sentinel_story(3)

        def sheets(name):
            if name == mark("CHAR", 1):
                return None
            if name == mark("CHAR", 2):
                return {"voice_type": "", "history": "unvoiced"}
            return every_sheet(name)

        self.assertEqual(characters_needed(story, 0, sheets), [])           # CHAR_1 is not heard yet
        self.assertEqual(characters_needed(story, 2, sheets), [mark("CHAR", 1), mark("CHAR", 2)])
        segue_prompt(story, 0, character_sheet=sheets)
        with self.assertRaises(NeedsCharacter) as caught:
            segue_prompt(story, 1, character_sheet=sheets)
        self.assertEqual(caught.exception.names, [mark("CHAR", 1)])
        with self.assertRaises(NeedsCharacter):
            recap_prompt(story, 2, character_sheet=sheets)

    def test_a_failing_lookup_counts_as_missing(self):
        def broken(name):
            raise RuntimeError("ATK character registry offline")

        with self.assertLogs("aura", level="WARNING"):
            self.assertEqual(characters_needed(sentinel_story(1), 0, broken), [mark("CHAR", 0)])


SHEET = """# 02 - The Velvet Lesson (Aphrodite)

> Smoky contralto jazz, 120 BPM, brushed drums
> velvet hook

## Scene
Aphrodite's studio, after hours. Justicia arrives early.

She is nervous.

## Lyrics
[Verse 1]
Velvet, let him fall
Slow is how it starts
"""


class DraftAndSheetTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.album = self.tmp / "music" / "The Turing Accords"
        make_wav(self.album / "01 - Genesis.wav")
        make_wav(self.album / "02 - The Velvet Lesson (Aphrodite).wav")
        make_wav(self.album / "03 - Objection (Lex & Meg).wav")
        self.lib = Library(self.tmp / "data")
        self.addCleanup(self.lib.close)
        self.lib.scan(self.tmp / "music")

    def test_draft_story_from_the_library(self):
        ingest(self.lib, [t.id for t in self.lib.tracks()],
               transcribe=lambda p: {"text": "heard words", "language": "en", "duration": 1.0, "segments": []})
        story = draft_story(self.lib, str(self.album))
        self.assertEqual(story.album, "The Turing Accords")
        self.assertEqual(story.slug, "the-turing-accords")
        self.assertEqual([t.title for t in story.tracks], ["Genesis", "The Velvet Lesson", "Objection"])
        self.assertEqual(story.tracks[2].characters, ["Lex", "Meg"])
        self.assertEqual((story.tracks[0].lyrics, story.tracks[0].lyrics_source), ("heard words", "transcript"))
        with self.assertRaises(ValueError):
            draft_story(self.lib, str(self.tmp / "nothing-here"))

    def test_lean_sheets_attach_by_stem_or_title(self):
        (self.album / "02 - The Velvet Lesson (Aphrodite).md").write_text(SHEET, encoding="utf-8")
        (self.album / "sheets").mkdir()
        (self.album / "sheets" / "Objection.txt").write_text(
            "Scene: The courthouse steps.\n\n[Chorus]\nObjection!\n", encoding="utf-8")
        story = draft_story(self.lib, str(self.album))
        self.assertEqual(attach_sheets(story, self.album), 2)
        velvet = story.tracks[1]
        self.assertEqual(velvet.scene, "Aphrodite's studio, after hours. Justicia arrives early.\n\nShe is nervous.")
        self.assertEqual(velvet.lyrics, "[Verse 1]\nVelvet, let him fall\nSlow is how it starts")
        self.assertEqual(velvet.lyrics_source, "sheet")
        self.assertNotIn("120 BPM", velvet.lyrics + velvet.scene)   # the style prompt is dropped
        self.assertEqual((story.tracks[2].scene, story.tracks[2].lyrics), ("The courthouse steps.", "[Chorus]\nObjection!"))
        self.assertEqual(story.tracks[0].scene, "")

    def test_parse_lean_sheet_without_a_scene(self):
        self.assertEqual(parse_lean_sheet("> style\n[Verse]\nla la\n"), ("", "[Verse]\nla la"))

    def test_story_files_round_trip_and_the_story_pack_wins(self):
        story = draft_story(self.lib, str(self.album))
        story.tracks[1].tease = "Next: a lesson in velvet."
        path = story_path(self.tmp / "data", str(self.album))
        self.assertEqual(path, self.tmp / "data" / "stories" / "the-turing-accords.json")
        save_story(story, path)
        self.assertEqual(load_story(path), story)
        self.assertIn('\n  "album": "The Turing Accords"', path.read_text(encoding="utf-8"))
        pack = self.album / STORY_PACK_NAME
        pack.write_text(json.dumps({"album": "Pack", "tracks": [{"track_id": "x", "title": "Only"}]}),
                        encoding="utf-8")
        self.assertEqual(story_path(self.tmp / "data", str(self.album)), pack)
        loaded = load_story(pack)
        self.assertEqual((loaded.album, loaded.root, loaded.tracks[0].title), ("Pack", str(self.album), "Only"))


class RelinkTests(TempDirTest):
    """Track ids come from full paths: a moved or renamed album folder must not orphan its story."""

    def setUp(self):
        super().setUp()
        self.album = self.tmp / "music" / "The Turing Accords"
        for name in ("01 - Genesis.wav", "02 - The Velvet Lesson (Aphrodite).wav", "Disc 2/2-01 - Encore.wav"):
            make_wav(self.album / name)
        self.lib = Library(self.tmp / "data")
        self.addCleanup(self.lib.close)
        self.lib.scan(self.tmp / "music")
        self.story = draft_story(self.lib, str(self.album))
        self.old_ids = [t.track_id for t in self.story.tracks]

    def move(self, new_parent: str, new_name: str = "The Turing Accords"):
        (self.tmp / new_parent).mkdir()
        target = self.tmp / new_parent / new_name
        self.album.rename(target)
        self.lib.scan(self.tmp)
        self.lib.remove_missing()
        return target

    def test_draft_records_each_file_inside_the_album(self):
        self.assertEqual([t.file for t in self.story.tracks],
                         ["01 - Genesis.wav", "02 - The Velvet Lesson (Aphrodite).wav", "Disc 2/2-01 - Encore.wav"])

    def test_a_moved_album_is_relinked_with_its_casual_breaks_and_own_takes(self):
        store = BreakStore(self.tmp / "data")
        store.save("casual", [Break(id=f"intro:{self.old_ids[1]}", kind="intro", text="Velvet next.")])
        own = make_wav(self.tmp / "data" / "overrides" / f"intro_{self.old_ids[1]}.wav")
        slug = self.story.slug
        target = self.move("elsewhere")
        self.assertEqual(relink_story(self.story, self.lib, store=store), 3)
        new_ids = [t.id for t in self.lib.tracks(root=str(target))]
        self.assertEqual([t.track_id for t in self.story.tracks], new_ids)
        self.assertEqual((self.story.root, self.story.slug), (str(target), slug))
        self.assertEqual([b.id for b in store.load("casual")], [f"intro:{new_ids[1]}"])
        self.assertFalse(own.exists())
        self.assertEqual(store.playable(f"intro:{new_ids[1]}", "v"),
                         str(self.tmp / "data" / "overrides" / f"intro_{new_ids[1]}.wav"))
        self.assertEqual(relink_story(self.story, self.lib), 0)   # nothing left to do

    def test_a_renamed_album_keeps_its_slug(self):
        path = self.tmp / "data" / "stories" / "tta.json"
        save_story(self.story, path)
        slug = self.story.slug
        self.move("renamed", "TTA Final Mix")
        with self.assertLogs("aura", level="INFO"):
            loaded = load_story(path, library=self.lib)
        self.assertEqual([t.track_id for t in loaded.tracks], [t.id for t in self.lib.tracks()])
        self.assertEqual(loaded.slug, slug)                    # its scripts and break ids still match

    def test_an_older_story_without_file_names_relinks_by_title(self):
        for t in self.story.tracks:
            t.file = ""
        self.move("elsewhere")
        self.assertEqual(relink_story(self.story, self.lib), 3)


if __name__ == "__main__":
    unittest.main()
