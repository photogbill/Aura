# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Lets `python -m aura ...` run the command-line tool."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
