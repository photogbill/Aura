# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Shared test helpers: a temporary folder per test (deleted afterwards), tiny WAV files, fake services."""
from __future__ import annotations

import sys
import tempfile
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:  # run from anywhere: the package under test is the one beside these tests
    sys.path.insert(0, str(REPO))


def make_wav(path: str | Path, seconds: float = 0.5, rate: int = 8000) -> Path:
    """A silent mono 16-bit WAV, made with the stdlib only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


class TempDirTest(unittest.TestCase):
    """self.tmp is a fresh folder, removed after the test (after anything registered later is closed)."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="aura-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)


def fake_synthesize(*, fell_back: bool = False, calls: list | None = None, fail_on: str = "",
                    write_file: bool = True):
    """A stand-in for the host's speech service, with the contract's return shape."""

    def synthesize(text: str, voice: str, out_path: str) -> dict:
        if calls is not None:
            calls.append((text, voice, out_path))
        if fail_on and fail_on in text:
            raise RuntimeError("speech engine crashed")
        if write_file:
            make_wav(out_path, 0.2)  # even a Piper fallback writes a file - it must still never air
        return {"path": out_path, "engine": "piper" if fell_back else "chatterbox", "voice": voice,
                "fell_back": fell_back, "note": "Chatterbox unavailable; used Piper" if fell_back else ""}

    return synthesize
