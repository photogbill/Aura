# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""RouteNote copies: only WAVs in; stereo 16-bit 44.1 kHz FLAC out, checked after writing; resampled and
dithered when the master is 48 kHz / 24-bit; mono copied to both sides at the same level; a master that will
clip is reported, never turned down; organized by album; a second run keeps what is done."""
from __future__ import annotations

import math
import shutil
import subprocess
import unittest
import wave
from pathlib import Path

from aura_test_support import TempDirTest

from aura.convert import ROUTENOTE, convert_batch, delivery_path, meets, parse_probe, probe

FFMPEG = shutil.which("ffmpeg")


def master(path: Path, *, rate: int = 48000, width: int = 3, channels: int = 2, seconds: float = 0.3,
           freq: float = 440.0, level: float = 0.5, phase: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    full = (1 << (8 * width - 1)) - 1
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(level * full * math.sin(2 * math.pi * freq * i / rate + phase))
        sample = v.to_bytes(width, "little", signed=True)
        frames += sample * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class ProbeTests(unittest.TestCase):
    def test_what_ffmpeg_prints_is_read(self):
        info = parse_probe("  Duration: 00:03:21.50, bitrate: 2304 kb/s\n"
                           "  Stream #0:0: Audio: pcm_s24le ([1][0][0][0] / 0x0001), 48000 Hz, stereo, "
                           "s32 (24 bit), 2304 kb/s")
        self.assertEqual((info.codec, info.rate, info.channels, info.bits), ("pcm_s24le", 48000, 2, 24))
        self.assertAlmostEqual(info.duration_s, 201.5)
        self.assertTrue(meets(parse_probe("Stream #0:0: Audio: flac, 44100 Hz, stereo, s16"), ROUTENOTE))
        self.assertFalse(meets(parse_probe("Stream #0:0: Audio: flac, 48000 Hz, stereo, s16"), ROUTENOTE))
        self.assertFalse(meets(parse_probe("Stream #0:0: Audio: flac, 44100 Hz, stereo, s32 (24 bit)"), ROUTENOTE))
        self.assertEqual(parse_probe("Stream #0:0: Audio: pcm_s16le, 44100 Hz, 6 channels, s16").channels, 6)
        self.assertIsNone(parse_probe("no audio here"))

    def test_copies_are_organized_by_album_and_disc(self):
        self.assertEqual(delivery_path("D:/Music/Genesis/Disc 2/2-01 - X.wav", "D:/RouteNote"),
                         Path("D:/RouteNote/Genesis/Disc 2/2-01 - X.flac"))
        self.assertEqual(delivery_path("D:/Music/Genesis/07 - Y (Singer).wav", "D:/RouteNote"),
                         Path("D:/RouteNote/Genesis/07 - Y (Singer).flac"))


@unittest.skipUnless(FFMPEG, "FFmpeg is not on PATH here")
class ConvertTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.album = self.tmp / "music" / "Genesis"
        self.out = self.tmp / "RouteNote"

    def decode(self, path: Path) -> list[tuple[int, int]]:
        raw = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le",
                              "-acodec", "pcm_s16le", "-"], capture_output=True, check=True).stdout
        samples = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
        return list(zip(samples[0::2], samples[1::2]))

    def test_a_48k_24bit_master_becomes_routenote_flac(self):
        wav = master(self.album / "01 - Awakening (Singer A).wav")
        summary = convert_batch([wav], self.out, ffmpeg=FFMPEG)
        item = summary.items[0]
        self.assertEqual(item.status, "converted", item.error)
        self.assertEqual(Path(item.dst), self.out / "Genesis" / "01 - Awakening (Singer A).flac")
        self.assertTrue(meets(probe(FFMPEG, item.dst), ROUTENOTE))
        self.assertTrue(any("48 kHz → 44.1 kHz" in c for c in item.changes), item.changes)
        self.assertTrue(any("24-bit → 16-bit, with dither" in c for c in item.changes), item.changes)
        self.assertIn(summary.resampler, ("soxr", "swr"))
        self.assertTrue(wav.is_file())                                  # the master is untouched
        self.assertEqual(list(self.out.rglob("*.tmp")) + list(self.out.rglob(".*")), [])

    def test_mono_goes_to_both_sides_at_the_same_level(self):
        wav = master(self.album / "02 - Mono Demo.wav", rate=44100, width=2, channels=1, level=0.5)
        item = convert_batch([wav], self.out, ffmpeg=FFMPEG).items[0]
        self.assertEqual(item.status, "converted", item.error)
        self.assertIn("mono → stereo (the same signal on both sides)", item.changes)
        frames = self.decode(Path(item.dst))
        self.assertTrue(all(left == right for left, right in frames))
        peak = max(abs(left) for left, _right in frames)
        self.assertGreater(peak, 0.49 * 32767)                       # not 3 dB down
        self.assertLess(peak, 0.51 * 32767)

    def test_a_master_that_will_clip_is_reported_not_turned_down(self):
        # full scale at a quarter of the rate, 45 degrees off: the samples never touch the peaks between them
        wav = master(self.album / "03 - Hot.wav", rate=48000, width=3, freq=12000, level=0.999,
                     phase=math.pi / 4, seconds=1.0)
        item = convert_batch([wav], self.out, ffmpeg=FFMPEG).items[0]
        self.assertEqual(item.status, "converted", item.error)
        self.assertIn("true peak", item.warning)

    def test_only_wavs_and_a_second_run_keeps_what_is_done(self):
        wav = master(self.album / "04 - Song.wav", seconds=0.1)
        mp3 = self.album / "04 - Song.mp3"
        mp3.write_bytes(b"ID3")
        first = convert_batch([wav, mp3], self.out, ffmpeg=FFMPEG)
        self.assertEqual([i.status for i in first.items], ["converted", "skipped"])
        second = convert_batch([wav], self.out, ffmpeg=FFMPEG)
        self.assertEqual(second.items[0].status, "kept")
        again = convert_batch([wav], self.out, ffmpeg=FFMPEG, overwrite=True)
        self.assertEqual(again.items[0].status, "converted")

    def test_two_albums_with_the_same_folder_name_do_not_overwrite_each_other(self):
        a = master(self.tmp / "one" / "Album" / "01 - Same.wav", seconds=0.1)
        b = master(self.tmp / "two" / "Album" / "01 - Same.wav", seconds=0.1, freq=880)
        items = convert_batch([a, b], self.out, ffmpeg=FFMPEG).items
        self.assertEqual([Path(i.dst).name for i in items], ["01 - Same.flac", "01 - Same (2).flac"])

    def test_stop_converts_nothing_more(self):
        wav = master(self.album / "05 - Song.wav", seconds=0.1)
        summary = convert_batch([wav], self.out, ffmpeg=FFMPEG, cancel=lambda: True)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.items, [])

    def test_no_ffmpeg_is_said_plainly(self):
        with self.assertRaises(ValueError):
            convert_batch([], self.out, ffmpeg="")
