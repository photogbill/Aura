# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""AURA's media player core: the music library, the pre-recorded radio host and the show sequencer.

The engine is data, logic and files only. It imports nothing outside the Python standard library (tinytag is
used when installed, never required), never imports ATK or Qt, and takes every model or speech service as a
plain callable from the host, so it can be tested with fakes and run without a GPU.
"""

__version__ = "0.1.0"
