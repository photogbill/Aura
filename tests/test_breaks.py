# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Breaks: the store, writing casual and story breaks with a fake model, rendering with fake speech,
and what may be aired (own takes win; fallback speech never airs)."""
from __future__ import annotations

import threading
from pathlib import Path

from aura_test_support import TempDirTest, fake_synthesize, make_wav

from aura.breaks import BreakStore, casual_prompt, render, safe_id, write_casual, write_story
from aura.common import NSTR_RULE, temp_path_for
from aura.library import Library
from aura.script import Break, text_hash
from aura.story import Story, StoryTrack


def sheet_for_everyone(name):
    return {"voice_type": "mezzo", "history": f"{name} has a past."}


class StoreTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = BreakStore(self.tmp / "data")

    def test_layout_save_load_get_and_status(self):
        for folder in ("scripts", "breaks", "overrides"):
            self.assertTrue((self.tmp / "data" / folder).is_dir())
        b = Break(id="intro:t1", kind="intro", text="Hello.")
        path = self.store.save("casual", [b])
        self.assertEqual(path, self.tmp / "data" / "scripts" / "casual.txt")
        self.assertEqual(self.store.scripts(), [path])
        self.assertEqual(self.store.load("casual"), [b])
        self.assertEqual(self.store.get("intro:t1"), b)
        self.assertIsNone(self.store.get("intro:nope"))
        self.assertEqual(self.store.set_status("intro:t1", "approved").status, "approved")
        self.assertEqual(self.store.load("casual")[0].status, "approved")
        with self.assertRaises(KeyError):
            self.store.set_status("intro:nope", "approved")
        with self.assertRaises(ValueError):
            self.store.set_status("intro:t1", "aired")
        with self.assertRaises(ValueError):
            self.store.load("../escape")

    def test_notepad_edits_are_seen_at_once(self):
        self.store.save("casual", [Break(id="intro:t1", kind="intro", text="Hello.")])
        path = self.store.script_path("casual")
        path.write_text(path.read_text(encoding="utf-8").replace("status: draft", "status: approved") + "\n",
                        encoding="utf-8")
        self.assertEqual(self.store.get("intro:t1").status, "approved")

    def test_put_keeps_story_order(self):
        order = ["segue:a:0", "recap:a:0", "segue:a:1", "recap:a:1"]
        for bid in ("recap:a:1", "segue:a:0", "segue:a:1"):
            self.store.put("a", Break(id=bid, kind=bid.split(":")[0], text="t"), order)
        self.assertEqual([b.id for b in self.store.load("a")], ["segue:a:0", "segue:a:1", "recap:a:1"])

    def test_safe_id(self):
        self.assertEqual(safe_id("segue:the-turing-accords:3"), "segue_the-turing-accords_3")


class RenderTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = BreakStore(self.tmp / "data")
        self.store.save("casual", [
            Break(id="intro:t1", kind="intro", text="Here is the first song.", status="approved"),
            Break(id="back:t1", kind="back", text="That was the first song.", status="draft"),
            Break(id="notes:mine", kind="notes", text="Bill's reminder: never aired."),
        ])

    def test_render_makes_approved_breaks_playable(self):
        calls = []
        result = render(self.store, "casual", synthesize=fake_synthesize(calls=calls), voice="host-cut")
        self.assertEqual((result["rendered"], result["fell_back"], result["skipped"], result["failed"]), (1, 0, 2, []))
        self.assertEqual([c[0] for c in calls], ["Here is the first song."])
        b = self.store.get("intro:t1")
        self.assertEqual((b.status, b.voice, b.engine, b.audio, b.text_hash, b.lufs),
                         ("rendered", "host-cut", "chatterbox", "breaks/intro_t1.wav", text_hash(b.text), None))
        self.assertEqual(self.store.playable("intro:t1", "host-cut"), str(self.tmp / "data" / "breaks" / "intro_t1.wav"))
        self.assertIsNone(self.store.playable("back:t1", "host-cut"))       # a draft is never aired
        self.assertIsNone(self.store.playable("notes:mine", "host-cut"))
        again = render(self.store, "casual", synthesize=fake_synthesize(calls=calls), voice="host-cut")
        self.assertEqual((again["rendered"], len(calls)), (0, 1))          # nothing to redo

    def test_drafts_render_only_when_asked_and_casual(self):
        result = render(self.store, "casual", synthesize=fake_synthesize(), voice="v", approved_only=False)
        self.assertEqual(result["rendered"], 2)

    def test_a_fallback_render_is_marked_needs_rerender_and_never_aired(self):
        result = render(self.store, "casual", synthesize=fake_synthesize(fell_back=True), voice="host-cut")
        self.assertEqual((result["rendered"], result["fell_back"]), (0, 1))
        b = self.store.get("intro:t1")
        self.assertEqual((b.status, b.fell_back, b.engine), ("needs_rerender", True, "piper"))
        self.assertIn("Piper", b.note)
        self.assertTrue((self.tmp / "data" / b.audio).is_file())           # the file exists...
        self.assertIsNone(self.store.playable("intro:t1", "host-cut"))     # ...and still never airs
        fixed = render(self.store, "casual", synthesize=fake_synthesize(), voice="host-cut")
        self.assertEqual(fixed["rendered"], 1)                             # tried again next time
        self.assertIsNotNone(self.store.playable("intro:t1", "host-cut"))

    def assert_fell_back(self, spoken: dict, expected_in_note: str) -> None:
        def synth(text, voice, out_path):
            make_wav(out_path, 0.2)
            return dict(spoken, path=out_path)

        result = render(self.store, "casual", synthesize=synth, voice="host-cut")
        self.assertEqual((result["rendered"], result["fell_back"]), (0, 1))
        b = self.store.get("intro:t1")
        self.assertEqual((b.status, b.fell_back), ("needs_rerender", True))
        self.assertIn(expected_in_note, b.note)
        self.assertIsNone(self.store.playable("intro:t1", "host-cut"))

    def test_piper_speaking_is_a_fall_back_even_when_not_reported(self):
        self.assert_fell_back({"engine": "piper", "voice": "host-cut", "fell_back": False, "note": ""},
                              "Piper spoke")

    def test_another_voice_speaking_is_a_fall_back_even_when_not_reported(self):
        self.assert_fell_back({"engine": "chatterbox", "voice": "default-voice", "fell_back": False, "note": ""},
                              "spoken by engine chatterbox, voice default-voice")

    def test_a_reported_fall_back_records_what_spoke(self):
        self.assert_fell_back({"engine": "piper", "voice": "en_US-amy", "fell_back": True, "note": "no GPU"},
                              "the speech service reported a fall-back")

    def test_playable_never_airs_fallback_speech_even_if_marked_rendered(self):
        audio = make_wav(self.tmp / "data" / "breaks" / "hand.wav")
        text = "Hand-edited."
        self.store.put("casual", Break(id="intro:t9", kind="intro", text=text, status="rendered", voice="v",
                                       audio="breaks/hand.wav", text_hash=text_hash(text), fell_back=True))
        self.assertTrue(audio.is_file())
        self.assertIsNone(self.store.playable("intro:t9", "v"))

    def test_stale_breaks_are_not_aired_and_are_rerendered(self):
        render(self.store, "casual", synthesize=fake_synthesize(), voice="host-cut")
        self.assertIsNone(self.store.playable("intro:t1", "host-warm"))   # another voice now
        breaks = self.store.load("casual")
        breaks[0].text = "Here is the first song, edited."
        self.store.save("casual", breaks)
        self.assertIsNone(self.store.playable("intro:t1", "host-cut"))    # text edited since the render
        result = render(self.store, "casual", synthesize=fake_synthesize(), voice="host-cut")
        self.assertEqual(result["rendered"], 1)
        self.assertIsNotNone(self.store.playable("intro:t1", "host-cut"))

    def test_bills_own_take_wins(self):
        own = make_wav(self.tmp / "data" / "overrides" / "back_t1.mp3")
        self.assertEqual(self.store.playable("back:t1", "host-cut"), str(own))       # even over a draft
        render(self.store, "casual", synthesize=fake_synthesize(), voice="host-cut")
        own_wav = make_wav(self.tmp / "data" / "overrides" / "intro_t1.wav")
        self.assertEqual(self.store.playable("intro:t1", "host-cut"), str(own_wav))   # over a rendered one
        station = make_wav(self.tmp / "data" / "overrides" / "station_id_7.flac")
        self.assertEqual(self.store.playable("station_id:7", "x"), str(station))      # with no script entry
        self.assertEqual(self.store.own_station_ids(), ["station_id:7"])

    def test_one_failure_does_not_stop_the_batch(self):
        self.store.put("casual", Break(id="intro:t2", kind="intro", text="Crash here.", status="approved"))
        self.store.put("casual", Break(id="intro:t3", kind="intro", text="No file.", status="approved"))

        def synth(text, voice, out_path):
            if "No file" in text:
                return {"path": out_path, "engine": "x", "voice": voice, "fell_back": False, "note": ""}
            return fake_synthesize(fail_on="Crash")(text, voice, out_path)

        with self.assertLogs("aura", level="WARNING"):
            result = render(self.store, "casual", synthesize=synth, voice="v")
        self.assertEqual(result["rendered"], 1)
        self.assertEqual(len(result["failed"]), 2)
        self.assertEqual(self.store.get("intro:t2").status, "approved")


class ThreadSafetyTests(TempDirTest):
    def test_two_threads_updating_one_script_lose_nothing(self):
        """Thread A pauses between reading the script and saving it; thread B saves in that gap if it can."""
        a_has_read, b_done = threading.Event(), threading.Event()

        class PausingStore(BreakStore):
            def load(self, script):
                breaks = super().load(script)
                if threading.current_thread().name == "A" and not a_has_read.is_set():
                    a_has_read.set()
                    b_done.wait(0.5)   # B's put lands here unless the store's lock makes it wait
                return breaks

        store = PausingStore(self.tmp / "data")
        store.save("casual", [Break(id="intro:t0", kind="intro", text="Already here.")])
        a = threading.Thread(name="A", target=lambda: store.put("casual", Break(id="intro:ta", kind="intro", text="A")))

        def b_work():
            store.set_status("intro:t0", "approved")
            store.put("casual", Break(id="intro:tb", kind="intro", text="B"))
            b_done.set()

        a.start()
        self.assertTrue(a_has_read.wait(5))
        b = threading.Thread(name="B", target=b_work)
        b.start()
        a.join(5)
        b.join(5)
        breaks = {x.id: x for x in store.load("casual")}
        self.assertEqual(set(breaks), {"intro:t0", "intro:ta", "intro:tb"})
        self.assertEqual(breaks["intro:t0"].status, "approved")

    def test_temporary_files_differ_between_threads(self):
        target = self.tmp / "scripts" / "casual.txt"
        names = []
        worker = threading.Thread(target=lambda: names.append(temp_path_for(target)))
        worker.start()
        worker.join()
        names.append(temp_path_for(target))
        self.assertNotEqual(names[0], names[1])
        self.assertEqual(names[0].parent, target.parent)   # beside the file, never in the temp folder


class CasualWritingTests(TempDirTest):
    def setUp(self):
        super().setUp()
        album = self.tmp / "music" / "TTA"
        make_wav(album / "01 - Genesis.wav")
        make_wav(album / "02 - The Velvet Lesson (Aphrodite).wav")
        self.lib = Library(self.tmp / "data")
        self.addCleanup(self.lib.close)
        self.lib.scan(self.tmp / "music")
        self.ids = [t.id for t in self.lib.tracks()]
        self.lib.update(self.ids[1], notes="Bill: a lesson, not a seduction.", summary="About a lesson.")
        self.store = BreakStore(self.tmp / "data")
        self.asked = []

    def ask(self, system, user):
        self.asked.append(user)
        if "Genesis" in user and "what they just heard" in user:
            return "NSTR"
        return "Coming up: something true."

    def test_prompt_material(self):
        system, user = casual_prompt(self.lib.track(self.ids[1]), "intro")
        self.assertTrue(system.endswith(NSTR_RULE) and user.endswith(NSTR_RULE))
        for wanted in ("The Velvet Lesson", "Aphrodite", "Bill: a lesson, not a seduction.", "About a lesson."):
            self.assertIn(wanted, user)

    def test_writes_approved_breaks_counts_nstr_and_keeps_existing(self):
        result = write_casual(self.lib, self.store, self.ids, ask=self.ask)
        self.assertEqual((result["written"], result["nstr"], result["skipped"], result["failed"]), (3, 1, 0, []))
        breaks = self.store.load("casual")
        self.assertEqual({b.status for b in breaks}, {"approved"})
        self.assertEqual({b.id for b in breaks},
                         {f"intro:{self.ids[0]}", f"intro:{self.ids[1]}", f"back:{self.ids[1]}"})
        asked = len(self.asked)
        again = write_casual(self.lib, self.store, self.ids, ask=self.ask)
        self.assertEqual((again["written"], again["skipped"], again["nstr"]), (0, 3, 1))
        self.assertEqual(len(self.asked), asked + 1)   # only the NSTR one was asked again
        forced = write_casual(self.lib, self.store, self.ids, ask=self.ask, force=True)
        self.assertEqual(forced["written"], 3)

    def test_a_failing_model_call_is_recorded(self):
        def broken(system, user):
            raise TimeoutError("model server not answering")

        with self.assertLogs("aura", level="WARNING"):
            result = write_casual(self.lib, self.store, self.ids, ask=broken, kinds=("intro",))
        self.assertEqual((result["written"], len(result["failed"])), (0, 2))


class StoryWritingTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = BreakStore(self.tmp / "data")
        self.story = Story(album="The Turing Accords", root=str(self.tmp / "The Turing Accords"), tracks=[
            StoryTrack(track_id="t0", title="Genesis", characters=["Minerva"]),
            StoryTrack(track_id="t1", title="The Velvet Lesson", characters=["Aphrodite"],
                       scene="Aphrodite's studio, after hours.", tease="A lesson begins."),
            StoryTrack(track_id="t2", title="Objection", characters=["Lex"]),
        ])
        self.asked = []

    def ask(self, system, user):
        self.asked.append(user)
        return "NSTR" if "recap the story so far, through song 1" in user else "The story so far, briefly."

    def test_model_breaks_are_drafts_and_narration_is_approved(self):
        result = write_story(self.story, self.store, ask=self.ask, character_sheet=sheet_for_everyone)
        self.assertEqual((result["written"], result["nstr"], result["skipped"], result["needs_character"],
                          result["failed"]), (5, 1, 0, [], []))
        self.assertEqual(len(self.asked), 5)  # 2 segues + 3 recaps; the narration needed no model
        breaks = self.store.load("the-turing-accords")
        self.assertEqual([b.id for b in breaks], [
            "segue:the-turing-accords:0", "narration:the-turing-accords:1", "segue:the-turing-accords:1",
            "recap:the-turing-accords:1", "recap:the-turing-accords:2"])
        # What a model wrote waits for Bill; his own scene prose read verbatim does not.
        self.assertEqual({b.status for b in breaks if b.kind != "narration"}, {"draft"})
        narration = self.store.get("narration:the-turing-accords:1")
        self.assertEqual(narration.status, "approved")
        self.assertEqual(narration.text, "Aphrodite's studio, after hours.")
        self.assertEqual(self.store.get("segue:the-turing-accords:0").title,
                         'after 1 "Genesis" → 2 "The Velvet Lesson"')
        again = write_story(self.story, self.store, ask=self.ask, character_sheet=sheet_for_everyone)
        self.assertEqual((again["written"], again["skipped"]), (0, 5))

    def test_include_next_title_reaches_the_segue_prompt(self):
        write_story(self.story, self.store, ask=self.ask, character_sheet=sheet_for_everyone,
                    include_next_title=False)
        from aura.story import segue_prompt
        _s, off = segue_prompt(self.story, 0, character_sheet=sheet_for_everyone, include_next_title=False)
        _s, on = segue_prompt(self.story, 0, character_sheet=sheet_for_everyone, include_next_title=True)
        self.assertNotEqual(off, on)          # the setting changes the prompt at all...
        self.assertIn(off, self.asked)        # ...and write_story passed it through
        self.assertNotIn(on, self.asked)

    def test_story_drafts_never_render_until_approved(self):
        write_story(self.story, self.store, ask=self.ask, character_sheet=sheet_for_everyone)
        result = render(self.store, "the-turing-accords", synthesize=fake_synthesize(), voice="v",
                        approved_only=False)
        self.assertEqual(result["rendered"], 1)            # only the narration: Bill's own words
        self.assertIsNone(self.store.playable("segue:the-turing-accords:0", "v"))
        self.store.set_status("segue:the-turing-accords:0", "approved")
        result = render(self.store, "the-turing-accords", synthesize=fake_synthesize(), voice="v")
        self.assertEqual(result["rendered"], 1)
        self.assertIsNotNone(self.store.playable("segue:the-turing-accords:0", "v"))

    def test_a_missing_character_sheet_spends_no_generation(self):
        def no_minerva(name):
            return None if name == "Minerva" else sheet_for_everyone(name)

        result = write_story(self.story, self.store, ask=self.ask, character_sheet=no_minerva)
        self.assertEqual(self.asked, [])                    # Minerva is in track 0, so every break needs her
        self.assertEqual(result["needs_character"], ["Minerva"])
        self.assertEqual(result["written"], 1)              # the narration: Bill's own words, no model
        self.assertEqual([b.kind for b in self.store.load("the-turing-accords")], ["narration"])

    def test_a_late_character_blocks_only_later_breaks(self):
        def no_lex(name):
            return None if name == "Lex" else sheet_for_everyone(name)

        result = write_story(self.story, self.store, ask=self.ask, character_sheet=no_lex)
        self.assertEqual(result["needs_character"], ["Lex"])
        self.assertEqual(len(self.asked), 4)                # segues 0,1 and recaps 0,1; not recap 2
        self.assertIsNone(self.store.get("recap:the-turing-accords:2"))
