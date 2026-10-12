# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Playlist packages: export on one machine, import on another whose songs live somewhere else (and may be the
MP3s where the first had WAVs). Track ids are paths, so everything keyed by one must be re-keyed; break audio
must land where the player looks; fill keeps what is here, replace lets the package win; Bill's own takes and
story packs are never overwritten; a package from elsewhere is read as untrusted."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import unittest
import zipfile
from pathlib import Path

from aura_test_support import TempDirTest, fake_synthesize, make_wav

from aura.breaks import BreakStore, render
from aura.bundle import (EXTENSION, MANIFEST, VERSION, apply_import, export_bundle, liner_notes_html,
                         package_file_name, plan_import, read_bundle)
from aura.cli import main as cli_main
from aura.ingest import ingest
from aura.library import Library
from aura.playlist import read_m3u8
from aura.script import Break, back_id, intro_id, segue_id
from aura.story import STORY_PACK_NAME, draft_story, load_story, save_story, story_path

SONGS = ("01 - Genesis (Valkyrie).wav", "02 - The Velvet Lesson (Aphrodite).wav", "03 - Objection (Lex & Meg).wav")
VOICE = "host-cut"


def transcribe(path: str) -> dict:
    name = os.path.basename(path)
    return {"text": f"words heard in {name}", "language": "en", "duration": 1.0,
            "segments": [{"start": 2.5, "end": 3.0, "text": f"words heard in {name}"}]}


class Machine:
    """One computer: a music folder, a data folder, a library and a break store."""

    def __init__(self, root: Path, album: str = "The Turing Accords"):
        self.root = root
        self.music = root / "music"
        self.album = self.music / album
        self.data = root / "data"
        self.lib = Library(self.data)
        self.store = BreakStore(self.data)

    def scan(self, **kw):
        return self.lib.scan(self.music, **kw)

    def by_title(self, title: str):
        return next(t for t in self.lib.tracks() if t.title == title)

    def close(self):
        self.lib.close()


class BundleTestBase(TempDirTest):
    def setUp(self):
        super().setUp()
        self.home = Machine(self.tmp / "home")
        self.addCleanup(self.home.close)
        for i, name in enumerate(SONGS):
            make_wav(self.home.album / name, seconds=0.5 + i * 0.1)
        self.home.scan()
        self.ids = [t.id for t in self.home.lib.tracks()]
        ingest(self.home.lib, self.ids, transcribe=transcribe,
               ask=lambda s, u: "A song about beginnings.")
        genesis = self.ids[0]
        self.home.lib.update(genesis, notes="Bill's note: open loud.", intro_s=4.0)   # intro made manual
        self.home.lib.update(self.ids[1], summary="My own words.", summary_source="manual")
        self.home.store.save("casual", [
            Break(id=intro_id(genesis), kind="intro", text="Here is Genesis.", status="approved"),
            Break(id=back_id(genesis), kind="back", text="That was Genesis.", status="approved"),
            Break(id=intro_id(self.ids[1]), kind="intro", text="Velvet, next.", status="draft"),
            Break(id=intro_id(self.ids[2]), kind="intro", text="Objection!", status="approved"),
        ])
        render(self.home.store, "casual", synthesize=fake_synthesize(), voice=VOICE)
        # Objection's speech fell back to Piper: it must never air, here or anywhere it is taken
        self.home.store.update_fields("casual", intro_id(self.ids[2]), status="needs_rerender", fell_back=True)
        make_wav(self.home.store.overrides_dir / f"back_{self.ids[1]}.wav")       # Bill's own take
        self.package = self.tmp / "out" / package_file_name("Field Set 1")

    def export(self, ffmpeg=None, ids=None, **kw):
        return export_bundle(self.home.lib, self.home.store, ids or self.ids, self.package, name="Field Set 1",
                             ffmpeg=ffmpeg, **kw)

    def field_machine(self, ext: str = ".wav", parent: str = "elsewhere", copy_bytes: bool = True) -> Machine:
        m = Machine(self.tmp / parent)
        self.addCleanup(m.close)
        for name in SONGS:
            target = m.album / (Path(name).stem + ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            if copy_bytes and ext == ".wav":
                shutil.copyfile(self.home.album / name, target)
            else:
                target.write_bytes(b"not the same bytes " + name.encode())
        m.scan()
        return m


class ExportTests(BundleTestBase):
    def test_the_package_holds_the_work_and_not_the_songs(self):
        summary = self.export()
        self.assertFalse(summary.cancelled)
        self.assertTrue(self.package.name.endswith(EXTENSION))
        with zipfile.ZipFile(self.package) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read(MANIFEST))
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual([s["key"] for s in manifest["songs"]], self.ids)
        self.assertFalse(any(n.endswith(tuple(SONGS)) for n in names), names)
        self.assertEqual(summary.transcripts, 3)
        self.assertTrue({f"transcripts/{i}.json" for i in self.ids} <= names)
        self.assertIn("playlist.m3u8", names)
        self.assertIn("liner_notes.html", names)
        self.assertEqual(len(manifest["songs"][0]["sha256"]), 64)
        # no temporary file left beside the package
        self.assertEqual(sorted(p.name for p in self.package.parent.iterdir()), [self.package.name])

    def test_only_audio_that_would_air_travels(self):
        summary = self.export()
        with zipfile.ZipFile(self.package) as zf:
            manifest = json.loads(zf.read(MANIFEST))
            names = set(zf.namelist())
        breaks = {b["id"]: b for b in manifest["breaks"]}
        genesis = self.ids[0]
        self.assertEqual(breaks[intro_id(genesis)]["status"], "rendered")
        self.assertIn(breaks[intro_id(genesis)]["audio"], names)
        self.assertEqual(breaks[intro_id(genesis)]["voice"], VOICE)
        self.assertEqual(breaks[intro_id(self.ids[1])]["status"], "draft")          # a draft stays a draft
        self.assertEqual(breaks[intro_id(self.ids[1])]["audio"], "")
        fell = breaks[intro_id(self.ids[2])]
        self.assertEqual((fell["status"], fell["audio"], fell["voice"]), ("approved", "", ""))
        own = breaks[back_id(self.ids[1])]
        self.assertFalse(own["in_script"])
        self.assertIn(own["own_take"], names)
        self.assertEqual((summary.audio, summary.own_takes, summary.voices), (2, 1, [VOICE]))

    def test_without_ffmpeg_break_audio_travels_as_it_was(self):
        summary = self.export(ffmpeg=None)
        self.assertEqual((summary.converted, summary.kept_as_is), (0, 2))
        self.assertTrue(summary.warnings)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not on PATH here")
    def test_break_audio_becomes_mp3_and_own_takes_stay_untouched(self):
        summary = self.export(ffmpeg=shutil.which("ffmpeg"))
        self.assertEqual((summary.converted, summary.kept_as_is), (2, 0))
        with zipfile.ZipFile(self.package) as zf:
            names = zf.namelist()
            self.assertTrue(all(n.endswith(".mp3") for n in names if n.startswith("breaks/")), names)
            own = [n for n in names if n.startswith("own_takes/")]
            self.assertEqual(own, [f"own_takes/back_{self.ids[1]}.wav"])
            self.assertEqual(zf.read(own[0]), (self.home.store.overrides_dir / f"back_{self.ids[1]}.wav").read_bytes())

    def test_stop_leaves_no_package_and_no_temporary_file(self):
        summary = self.export(cancel=lambda: True)
        self.assertTrue(summary.cancelled)
        self.assertFalse(self.package.exists())
        self.assertEqual(list(self.package.parent.iterdir()), [])

    def test_songs_not_in_the_library_are_left_out_and_none_at_all_is_an_error(self):
        summary = self.export(ids=["0000000000000000", *self.ids[:1]])
        self.assertEqual((summary.songs, summary.left_out), (1, ["0000000000000000"]))
        with self.assertRaises(ValueError):
            self.export(ids=["0000000000000000"])

    def test_liner_notes_escape_what_they_show(self):
        page = liner_notes_html({"name": "<script>x</script>", "songs": [
            {"key": "k", "title": "A & B <i>", "summary": "s", "summary_source": "model", "performers": ["Val"]}]},
            {"k": "</pre><b>"}, {})
        self.assertNotIn("<script>x", page)
        self.assertIn("A &amp; B &lt;i&gt;", page)
        self.assertIn("&lt;/pre&gt;&lt;b&gt;", page)


class ImportTests(BundleTestBase):
    def test_a_moved_library_gets_everything_under_its_own_ids(self):
        self.export()
        field = self.field_machine()
        plan = plan_import(self.package, field.lib)
        self.assertEqual([s.how for s in plan.songs], ["exact"] * 3)
        self.assertEqual(plan.voices, [VOICE])
        before = sorted(p.relative_to(field.data) for p in field.data.rglob("*") if p.is_file())
        plan_import(self.package, field.lib)                       # a preview writes nothing
        self.assertEqual(sorted(p.relative_to(field.data) for p in field.data.rglob("*") if p.is_file()), before)

        summary = apply_import(self.package, plan, field.lib, field.store)
        new_ids = [t.id for t in field.lib.tracks()]
        self.assertNotEqual(new_ids, self.ids)
        genesis = field.by_title("Genesis")
        self.assertEqual((genesis.notes, genesis.intro_s, genesis.intro_source),
                         ("Bill's note: open loud.", 4.0, "manual"))
        self.assertEqual(field.by_title("The Velvet Lesson").summary_source, "manual")
        record = json.loads(Path(genesis.transcript_path).read_text(encoding="utf-8"))
        self.assertEqual((record["track_id"], record["path"]), (genesis.id, genesis.path))
        self.assertEqual(Path(genesis.transcript_path).parent, field.data / "transcripts")
        # the break audio is in data/breaks under the new id and airs in the voice it was rendered in
        airs = field.store.playable(intro_id(genesis.id), VOICE)
        self.assertIsNotNone(airs)
        self.assertEqual(Path(airs).parent, field.data / "breaks")
        self.assertIsNone(field.store.playable(intro_id(genesis.id), "another-voice"))   # stale in another voice
        self.assertIsNone(field.store.playable(intro_id(field.by_title("Objection").id), VOICE))
        velvet = field.by_title("The Velvet Lesson")
        self.assertEqual(field.store.get(intro_id(velvet.id)).status, "draft")
        self.assertEqual(field.store.playable(back_id(velvet.id), VOICE),
                         str(field.store.overrides_dir / f"back_{velvet.id}.wav"))
        self.assertEqual((summary.audio_files, summary.own_takes_added, summary.transcripts_added), (2, 1, 3))
        entries = read_m3u8(summary.playlist)
        self.assertEqual([e["path"] for e in entries], [t.path for t in field.lib.tracks()])
        self.assertEqual(Path(summary.playlist).name, "Field Set 1.m3u8")

    def test_the_mp3_machine_finds_the_songs_the_wavs_were_ingested_as(self):
        self.export()
        field = self.field_machine(ext=".mp3")
        plan = plan_import(self.package, field.lib)
        self.assertEqual([s.how for s in plan.songs], ["same_song"] * 3)
        apply_import(self.package, plan, field.lib, field.store)
        genesis = field.by_title("Genesis")
        self.assertTrue(genesis.path.endswith(".mp3"))
        self.assertEqual(genesis.intro_s, 4.0)
        self.assertIsNone(genesis.lufs)                    # loudness was measured on other bytes: not carried
        self.assertIsNotNone(field.store.playable(intro_id(genesis.id), VOICE))

    def test_a_renamed_folder_is_found_by_file_name_then_by_title(self):
        self.export()
        field = Machine(self.tmp / "renamed", album="Album One")
        self.addCleanup(field.close)
        for name in SONGS[:2]:
            make_wav(field.album / name.replace(".wav", ".flac"), seconds=0.1)
        make_wav(field.album / "3 Objection.flac", seconds=0.1)   # renamed file: only the title is left
        field.lib.scan(field.music)
        for t in field.lib.tracks():                          # tags would say the album; the folder does not
            field.lib.update(t.id, album="The Turing Accords")
        plan = plan_import(self.package, field.lib)
        self.assertEqual([s.how for s in plan.songs], ["file_name", "file_name", "title"])

    def test_an_ambiguous_name_is_never_guessed(self):
        self.export()
        field = Machine(self.tmp / "twice", album="A")
        self.addCleanup(field.close)
        make_wav(field.music / "A" / "01 - Genesis (Valkyrie).mp3", seconds=0.1)
        make_wav(field.music / "B" / "01 - Genesis (Valkyrie).mp3", seconds=0.1)
        field.scan()
        for t in field.lib.tracks():
            field.lib.update(t.id, album="Something else")
        plan = plan_import(self.package, field.lib)
        self.assertEqual(plan.songs[0].how, "")
        self.assertEqual(plan.found(), 0)

    def test_fill_keeps_what_is_here_and_replace_lets_the_package_win(self):
        self.export()
        field = self.field_machine()
        genesis = field.by_title("Genesis")
        field.lib.update(genesis.id, notes="Field note.", summary="Field summary.", summary_source="model")
        field.store.save("casual", [Break(id=intro_id(genesis.id), kind="intro", text="Field intro.",
                                          status="approved")])
        own = make_wav(field.store.overrides_dir / f"back_{field.by_title('The Velvet Lesson').id}.wav", 0.3)
        own_bytes = own.read_bytes()
        plan = plan_import(self.package, field.lib)

        fill = apply_import(self.package, plan, field.lib, field.store, mode="fill")
        g = field.by_title("Genesis")
        self.assertEqual(g.notes, "Field note.")                       # kept
        self.assertEqual(g.summary, "Field summary.")                  # model here, model there: kept
        self.assertEqual(field.store.get(intro_id(g.id)).text, "Field intro.")
        self.assertEqual(fill.breaks_kept, 1)
        self.assertEqual(fill.own_takes_kept, 1)

        replace = apply_import(self.package, plan, field.lib, field.store, mode="replace")
        g = field.by_title("Genesis")
        self.assertEqual(g.notes, "Bill's note: open loud.")
        self.assertEqual(g.summary, "A song about beginnings.")
        self.assertEqual(field.store.get(intro_id(g.id)).text, "Here is Genesis.")
        self.assertIsNotNone(field.store.playable(intro_id(g.id), VOICE))
        self.assertEqual(replace.breaks_replaced, 4)        # fill had added the other three
        self.assertEqual(own.read_bytes(), own_bytes)                   # Bill's own take is never replaced
        self.assertEqual(replace.own_takes_kept, 1)

    def test_importing_twice_is_harmless(self):
        self.export()
        field = self.field_machine()
        plan = plan_import(self.package, field.lib)
        apply_import(self.package, plan, field.lib, field.store)
        again = apply_import(self.package, plan, field.lib, field.store)
        self.assertEqual((again.breaks_added, again.transcripts_added, again.songs_updated), (0, 0, 0))
        self.assertEqual(Path(again.playlist).name, "Field Set 1.m3u8")      # same content: no "(imported)"
        self.assertEqual(len(field.store.load("casual")), 4)            # each break once, not twice

    def test_a_different_playlist_of_the_same_name_is_kept(self):
        self.export()
        field = self.field_machine()
        mine = field.data / "playlists" / "Field Set 1.m3u8"
        mine.parent.mkdir(parents=True)
        mine.write_text("#EXTM3U\n", encoding="utf-8")
        summary = apply_import(self.package, plan_import(self.package, field.lib), field.lib, field.store)
        self.assertEqual(Path(summary.playlist).name, "Field Set 1 (imported).m3u8")
        self.assertEqual(mine.read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_songs_not_here_are_reported_and_their_breaks_wait(self):
        self.export()
        field = self.field_machine()
        os.remove(field.by_title("Genesis").path)
        field.lib.remove_missing()
        plan = plan_import(self.package, field.lib)
        summary = apply_import(self.package, plan, field.lib, field.store)
        self.assertEqual(summary.songs_found, 2)
        self.assertEqual(len(summary.songs_missing), 1)
        self.assertEqual(summary.breaks_unmatched, 2)


class StoryTests(BundleTestBase):
    def setUp(self):
        super().setUp()
        story = draft_story(self.home.lib, str(self.home.album))
        story.tracks[0].act = "Act I"
        story.tracks[0].tease = "Someone is coming."
        save_story(story, story_path(self.home.data, str(self.home.album)))
        self.slug = story.slug
        self.home.store.save(self.slug, [
            Break(id=segue_id(self.slug, 0), kind="segue", text="And so it began.", status="approved")])
        render(self.home.store, self.slug, synthesize=fake_synthesize(), voice=VOICE)

    def test_the_story_travels_and_points_at_the_new_machine(self):
        self.export(ids=self.ids[:1])                         # one song is enough to carry its album's story
        field = self.field_machine()
        summary = apply_import(self.package, plan_import(self.package, field.lib), field.lib, field.store)
        self.assertEqual(summary.stories_saved, 1)
        story = load_story(story_path(field.data, str(field.album)))
        self.assertEqual(story.root, str(field.album))
        self.assertEqual([t.track_id for t in story.tracks], [t.id for t in field.lib.tracks()])
        self.assertEqual(story.tracks[0].tease, "Someone is coming.")
        self.assertIsNotNone(field.store.playable(segue_id(self.slug, 0), VOICE))

    def test_a_story_pack_in_the_album_folder_is_never_written(self):
        self.export()
        field = self.field_machine()
        pack = field.album / STORY_PACK_NAME
        pack.write_text('{"format": "aura-story", "version": 1, "album": "mine", "tracks": []}\n', encoding="utf-8")
        before = pack.read_bytes()
        summary = apply_import(self.package, plan_import(self.package, field.lib), field.lib, field.store,
                               mode="replace")
        self.assertEqual(pack.read_bytes(), before)
        self.assertEqual(summary.stories_saved, 0)
        self.assertTrue(summary.stories_kept)


class UntrustedPackageTests(BundleTestBase):
    def write(self, manifest: dict, extra: dict | None = None, name: str = "made-elsewhere") -> Path:
        path = self.tmp / (name + ".aura.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(MANIFEST, json.dumps(manifest))
            for name, data in (extra or {}).items():
                zf.writestr(name, data)
        return path

    def test_what_is_not_a_package_is_refused_with_a_reason(self):
        junk = self.tmp / "junk.aura.zip"
        junk.write_bytes(b"not a zip")
        other = self.write({"format": "something"}, name="other")
        newer = self.write({"format": "aura-playlist-package", "version": VERSION + 1}, name="newer")
        for path, words in ((junk, "not a zip"), (other, "not an AURA"), (newer, "newer AURA")):
            with self.assertRaises(ValueError) as caught:
                read_bundle(path)
            self.assertIn(words, str(caught.exception))

    def test_a_file_outside_the_package_folders_is_never_read_or_written(self):
        self.export()
        field = self.field_machine()
        with zipfile.ZipFile(self.package) as zf:
            manifest = json.loads(zf.read(MANIFEST))
        manifest["breaks"][0]["audio"] = "../../evil.mp3"
        manifest["songs"][0]["transcript"] = "transcripts/../../evil.json"
        bad = self.write(manifest, {"../../evil.mp3": b"x"})
        with self.assertRaises(ValueError):
            apply_import(bad, plan_import(bad, field.lib), field.lib, field.store)
        self.assertFalse(any(p.name.startswith("evil") for p in self.tmp.rglob("*")))


class CommandLineTests(BundleTestBase):
    def run_cli(self, *argv: str) -> int:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = cli_main(list(argv))
        self.printed = printed.getvalue()
        return code

    def test_export_then_dry_run_then_import(self):
        out = self.tmp / "cli" / "set"
        self.assertEqual(self.run_cli("export", str(out), "--data", str(self.home.data), "--no-ffmpeg",
                                      "--name", "CLI set"), 0)
        package = out.with_name("set" + EXTENSION)
        self.assertTrue(package.is_file())
        field = self.field_machine()
        field.close()
        self.assertEqual(self.run_cli("import", str(package), "--data", str(field.data), "--dry-run"), 0)
        self.assertIn("3 of 3 song(s)", self.printed)
        self.assertEqual(self.run_cli("import", str(package), "--data", str(field.data)), 0)
        self.assertIn("Playlist:", self.printed)
        with Library(field.data) as lib:
            self.assertTrue(all(t.transcript_path for t in lib.tracks()))


if __name__ == "__main__":
    unittest.main()
