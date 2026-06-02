from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "filter_intune_omission_noise.py"


def load_module():
    spec = importlib.util.spec_from_file_location("filter_intune_omission_noise", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class FilterIntuneOmissionNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_is_flaky_path(self) -> None:
        self.assertTrue(self.module._is_flaky_path("tenant-state/intune/Scripts/Shell/MacOS.json"))
        self.assertFalse(self.module._is_flaky_path("tenant-state/intune/Configuration Policies/policy.json"))

    def test_restore_omitted_macos_script(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            shell_dir = repo / "tenant-state" / "intune" / "Scripts" / "Shell"
            script_data_dir = shell_dir / "Script Data"
            script_data_dir.mkdir(parents=True, exist_ok=True)

            json_file = shell_dir / "MacOS__abc.json"
            sh_file = script_data_dir / "script__abc.sh"

            baseline_json = {"displayName": "MacOS Script", "scriptContent": "echo hello"}
            json_file.write_text(json.dumps(baseline_json, indent=2) + "\n", encoding="utf-8")
            sh_file.write_text("#!/bin/bash\necho hello\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")

            # Simulate IntuneCD omitting the macOS script files
            json_file.unlink()
            sh_file.unlink()

            restored, skipped = self.module.restore_omitted_files(
                repo_root=repo,
                backup_root=repo / "tenant-state" / "intune",
                baseline_ref="HEAD",
            )

            self.assertEqual(len(restored), 2)
            self.assertIn("tenant-state/intune/Scripts/Shell/MacOS__abc.json", restored)
            self.assertIn("tenant-state/intune/Scripts/Shell/Script Data/script__abc.sh", restored)
            self.assertEqual(skipped, [])

            self.assertTrue(json_file.exists())
            self.assertTrue(sh_file.exists())
            self.assertEqual(json.loads(json_file.read_text(encoding="utf-8")), baseline_json)

    def test_does_not_restore_non_flaky_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            policy_dir = repo / "tenant-state" / "intune" / "Configuration Policies"
            policy_dir.mkdir(parents=True, exist_ok=True)
            policy_file = policy_dir / "Policy__abc.json"
            policy_file.write_text(json.dumps({"name": "Policy"}, indent=2) + "\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")

            policy_file.unlink()

            restored, skipped = self.module.restore_omitted_files(
                repo_root=repo,
                backup_root=repo / "tenant-state" / "intune",
                baseline_ref="HEAD",
            )

            self.assertEqual(restored, [])
            self.assertEqual(skipped, [])
            self.assertFalse(policy_file.exists())

    def test_no_op_when_files_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            shell_dir = repo / "tenant-state" / "intune" / "Scripts" / "Shell"
            shell_dir.mkdir(parents=True, exist_ok=True)
            json_file = shell_dir / "MacOS__abc.json"
            json_file.write_text(json.dumps({"name": "MacOS"}, indent=2) + "\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")

            restored, skipped = self.module.restore_omitted_files(
                repo_root=repo,
                backup_root=repo / "tenant-state" / "intune",
                baseline_ref="HEAD",
            )

            self.assertEqual(restored, [])
            self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
