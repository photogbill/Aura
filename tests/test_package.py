# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved
"""Bill's hard rules that can be checked mechanically: stdlib only, no Qt, no ATK, never mutagen, file hygiene."""
from __future__ import annotations

import subprocess
import sys
import unittest

from aura_test_support import REPO

FORBIDDEN_TOP_LEVEL = ("PySide6", "PySide2", "PyQt5", "PyQt6", "mutagen", "tinytag", "atk", "numpy", "requests")
MODULES = ("aura", "aura.common", "aura.library", "aura.ingest", "aura.playlist", "aura.story", "aura.script",
           "aura.breaks", "aura.show", "aura.cli")


class PackageRuleTests(unittest.TestCase):
    def test_imports_with_the_standard_library_alone(self):
        code = ("import sys\n"
                f"import {', '.join(MODULES)}\n"
                "print(aura.__version__)\n"
                f"print(sorted(m for m in sys.modules if m.split('.')[0] in {FORBIDDEN_TOP_LEVEL!r}))\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                             env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": ""}, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split("\n")[:2], ["0.1.0", "[]"])

    def test_no_forbidden_imports_in_the_source(self):
        for path in sorted((REPO / "aura").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for name in ("mutagen", "PySide", "PyQt", "import atk", "from atk"):
                self.assertNotIn(name, source, f"{path.name} mentions {name}")

    def test_every_python_file_has_the_licence_line_and_lf_endings(self):
        for path in sorted(list((REPO / "aura").glob("*.py")) + list((REPO / "tests").glob("*.py"))):
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"# SPDX-License-Identifier: LicenseRef-All-Rights-Reserved\n"), path.name)
            self.assertNotIn(b"\r", raw, path.name)
        for path in REPO.glob("*.md"):
            self.assertNotIn(b"\r", path.read_bytes(), path.name)

    def test_pyproject_declares_no_dependencies(self):
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        for line in ('name = "aura"', 'version = "0.1.0"', 'requires-python = ">=3.10"', "dependencies = []",
                     'tags = ["tinytag>=2"]'):
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
