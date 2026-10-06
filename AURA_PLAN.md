# AURA - plan and status

**Status: built 2026-10-05, not yet run on Windows.** First build: the media player core (this package).

## What AURA is

AURA (Autonomous Radio & Relief Assistant) is Bill's proposed ATK module for **overseas disaster relief** in
austere settings, from his 2026-09-30 concept: situational awareness, in-field translation, and camp
announcements and radio entertainment.

**AURA does not transmit** (Bill, 2026-09-30: ATK will not be the transmitter). It produces audio content (files,
or a stream); the deploying organisation routes that audio through its own broadcast chain and is responsible for
its own transmitter, licensing and frequency authorisation. A last-ditch, manual-arm-only distress mode was
floated as a separate concept and is undecided; it is not part of this engine.

## What this first build is

The engine behind the station's content: a music library with loudness measurement, Whisper transcripts and
model-written summaries of every song (Bill's item 6), a pre-recorded host (scripts written in a batch, read
and approved by Bill, rendered to audio ahead of time), story mode for concept albums such as
*The Turing Accords*, and a sequencer that tells ATK what to play next. ATK hosts it and does the playback.

## Rules (Bill's)

1. Standalone: standard library only; tinytag optional; never mutagen (GPL).
2. No GUI and no Qt in the engine.
3. Injected services, never hard dependencies: `transcribe`, `ask`, `synthesize`, `character_sheet`, `ffmpeg`.
4. Data stays beside the code or where the host says - never the user profile, AppData or temp.
5. The host is pre-recorded: a break that isn't there is skipped, never generated on the spot.
6. A break whose speech fell back to Piper is not aired (`needs_rerender`).
7. Bill's own recorded take for any break wins.
8. The host only says what it was given; every prompt ends with the NSTR rule.
9. A named character with no sheet or voice type stops generation (`needs_character`).
10. The spoiler line, enforced in code and proved by a test.
11. Story mode: nothing a model wrote renders until Bill has approved it (`draft -> approved -> rendered ->
    stale`, plus `needs_rerender`). Narration - Bill's own scene prose, read word for word - is written
    approved. Casual breaks need no approval step. Bill editing a rendered break makes it stale and it
    re-renders on the next render: his edit is his approval.

## Data layout

`data_dir/aura.db` (track index), `transcripts/`, `stories/`, `scripts/` (casual.txt and one per album),
`breaks/` (rendered audio), `overrides/` (Bill's own takes), `playlists/`. The command line uses
`<repo>/data`; ATK passes its own folder.

## What `Aura Build Plan.txt` said that is superseded

| Older plan | Now |
|---|---|
| Live "Phantom DJ" writing and speaking during playback | Pre-recorded host: written and rendered in a batch, approved by Bill |
| HackRF / SoapySDR TX, WBFM/NFM modulation, power profiles | **Not built.** AURA does not transmit; the deploying organisation's own chain does |
| `mutagen` for tags | `tinytag` (MIT), optional; filename parsing otherwise |
| Signal gateway (`signal-cli`, admin UUID bouncer) | Not built |
| PyDub mixing and ducking | Not in the engine: ATK plays and mixes; the engine gives gain and talk-over times |
| `album_lore.md` | Story files plus Bill's lean sheets, behind the spoiler line |

`Aura Build Plan.txt` is kept unchanged for the record. Its item 6 (Whisper large-v3 transcripts that
pre-build song summaries) is built here as `aura.ingest`.

## Not built yet

In-field translation, situational awareness and polyglot announcements. The player itself has its ATK
workspace (AURA: Player, Library, Story, Host, Setup), built the same day.
