# Aura - AURA's media player core

The content engine behind AURA's radio station: a music library, a pre-recorded host whose every word Bill can
read and approve before it airs, and a sequencer that tells the host application what to play next.

It is data, logic and files only. **No GUI, no Qt, no audio playback, nothing outside the Python standard
library.** ATK hosts it, plays the audio, and passes in the services that need a model or a voice as plain
callables. `tinytag` (MIT) is optional and used for tags when installed; mutagen is never used.

## The rules it is built on

1. **Standalone** - imports with the standard library alone (Python 3.10+).
2. **No GUI, no Qt** - the engine says what to play; the host plays it.
3. **Injected services** - `transcribe(path) -> dict`, `ask(system, user) -> str`,
   `synthesize(text, voice, out_path) -> dict`, `character_sheet(name) -> dict | None`, and `ffmpeg`
   (an exe path or None). The engine never imports ATK.
4. **Data stays where the host says** - every class takes `data_dir`; the command line uses `<repo>/data`.
   Nothing is written to the user profile, AppData or temp.
5. **The host is pre-recorded** - breaks are written and rendered in a batch ahead of time. At play time a
   break that is not ready is skipped, never generated on the spot.
6. **Speech that fell back to Piper is never aired** - it is marked `needs_rerender`.
7. **Bill's own recorded take wins** over the rendered one.
8. **The host only says what it was given** - tags, filename, Bill's notes, transcripts and lean sheets.
   Every prompt ends with the NSTR rule: *"If the material doesn't give you something true to say, answer
   exactly NSTR."* An `NSTR` reply makes no break and is counted.
9. **Character rule** - a named character without a sheet or voice type stops that break (`needs_character`)
   before any model is asked.
10. **The spoiler line** - the break after track N is written from tracks 1..N plus Bill's tease for track
    N+1 (and its title), and nothing else from N+1 on. Enforced in code and proved by a test.
11. **Story mode: nothing renders that Bill has not approved.**

## Data folder

```
data/
  aura.db                      the track index (SQLite)
  transcripts/<track_id>.json  Whisper output, one per song
  stories/<album_slug>.json    story files (an album folder's own aura_story.json wins)
  scripts/casual.txt           casual intros and back-announces
  scripts/<album_slug>.txt     an album's story breaks
  breaks/<break>.wav           rendered breaks
  overrides/<break>.wav        Bill's own takes (.wav, .mp3 or .flac), dropped in by hand
  playlists/*.m3u8
```

## Approving and replacing breaks

Open `data/scripts/<album>.txt` in Notepad. Each break starts with a line like

```
=== segue:the-turing-accords:3 | after 4 "Title A" → 5 "Title B" | status: draft
The words the host will say.
```

- **Approve** a break by changing `status: draft` to `status: approved`. Story breaks render only once approved.
- **Edit** the words freely. A break that was already rendered becomes *stale* and is re-rendered on the next
  render run; until then it is not aired.
- **Record it yourself**: save your take as `data/overrides/segue_the-turing-accords_3.wav` (the id with `:`
  replaced by `_`). It wins over anything rendered. Station IDs you record as
  `overrides/station_id_1.wav`, `station_id_2.wav`... are aired in rotation (frequency `station_ids`) even
  with no script entry.
- Keep your own notes in a `=== notes:anything` block; they are never rendered or aired.
- The ids count story tracks from 0: `segue:<album>:3` is the segue after the 4th song (the label says so).

## Moving an album folder

Track ids come from each file's full path, so moving or renaming an album folder gives its songs new ids.
A story finds its songs again by the album folder's name and the file's path inside it (or a unique file name):
`Show` does this in memory, `load_story(path, library=lib)` on loading, and
`relink_story(story, lib, store=store)` also renames the casual intros/back-announces (and your own takes for
them) to the new ids - save the story afterwards. A renamed folder keeps its old slug, so its story script and
breaks still match.

## Command line

```
python -m aura scan "D:\Music\The Turing Accords"      index (and measure loudness with ffmpeg)
python -m aura tracks                                  list what is indexed
python -m aura playlist tta.m3u8 --album "The Turing Accords"
python -m aura story-draft "D:\Music\The Turing Accords"   draft the story file (lean sheets attached)
python -m aura script-check the-turing-accords         statuses, stale breaks, problems
```

Every command takes `--data DIR`, `--ffmpeg PATH` and `--no-ffmpeg`.

## Using it from a host

```python
from aura.library import Library
from aura.ingest import ingest
from aura.story import draft_story, attach_sheets, save_story, story_path
from aura.breaks import BreakStore, write_casual, write_story, render
from aura.show import Show

lib = Library(data_dir, ffmpeg=ffmpeg_exe)
lib.scan(album_folder, progress=log_line, cancel=stop_event)
ingest(lib, [t.id for t in lib.tracks()], transcribe=whisper, ask=model)      # slow; resumable
store = BreakStore(data_dir, ffmpeg=ffmpeg_exe)
write_casual(lib, store, ids, ask=model)                                      # casual breaks: approved
write_story(story, store, ask=model, character_sheet=sheets)                  # story breaks: draft
render(store, "casual", synthesize=tts, voice="host-cut")
show = Show(lib, store, voice="host-cut", mode="story", story=story)
for item in show.next():      # [breaks..., track] - only files that exist and may be aired
    play(item.path, gain_db=item.gain_db, talkover_s=item.talkover_s)
```

## Tests

```
python -m unittest discover -s tests -v
```

Standard library `unittest` only; no network, no GPU, no ffmpeg needed (all services are faked).

## Licence

All rights reserved - see `LICENSE`.
