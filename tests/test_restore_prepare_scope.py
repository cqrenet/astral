#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "restore_prepare_scope.py"
PYTHON = sys.executable


@unittest.skipUnless(PYTHON, "python3 not available")
class RestorePrepareScopeTests(unittest.TestCase):
    def _run(self, root: Path, temp: Path, paths: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "scripts"),
            "GITHUB_WORKSPACE": str(root),
            "RUNNER_TEMP": str(temp),
            "BACKUP_FOLDER": "tenant-state",
            "INTUNE_BACKUP_SUBDIR": "intune",
            "RESTORE_MODE": "selective",
            "RESTORE_PATHS_CSV": paths,
            "ASTRAL_SCM": "github",
        }
        return subprocess.run([PYTHON, str(SCRIPT)], capture_output=True, text=True, env=env)

    def test_leading_parent_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            temp = base / "runner-temp"
            (root / "tenant-state" / "intune").mkdir(parents=True)
            temp.mkdir()
            (root / "tenant-state" / "intune" / "policy.json").write_text("{}\n")

            result = self._run(root, temp, "../../etc/passwd")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Path traversal is not allowed", result.stdout)

    def test_dot_prefix_is_allowed_for_a_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            temp = base / "runner-temp"
            source = root / "tenant-state" / "intune" / "policy.json"
            source.parent.mkdir(parents=True)
            temp.mkdir()
            source.write_text("{}\n")

            result = self._run(root, temp, "./policy.json")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((temp / "restore-scope-intune" / "policy.json").is_file())


if __name__ == "__main__":
    unittest.main()
