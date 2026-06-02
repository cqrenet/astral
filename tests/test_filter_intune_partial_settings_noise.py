from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "filter_intune_partial_settings_noise.py"


def load_module():
    spec = importlib.util.spec_from_file_location("filter_intune_partial_settings_noise", MODULE_PATH)
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


class FilterIntunePartialSettingsNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_partial_payload_detection(self) -> None:
        self.assertTrue(self.module._is_partial_settings_payload({"settingCount": 1}))
        self.assertTrue(self.module._is_partial_settings_payload({"settingCount": 2, "settings": []}))
        self.assertFalse(self.module._is_partial_settings_payload({"settingCount": 0, "settings": []}))
        self.assertFalse(self.module._is_partial_settings_payload({"settingCount": 2, "settings": [{"id": "0"}]}))

    def test_restore_partial_settings_from_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            workload_dir = repo / "tenant-state" / "intune" / "Settings Catalog"
            workload_dir.mkdir(parents=True, exist_ok=True)
            file_path = workload_dir / "Policy__abc.json"

            baseline = {
                "name": "Policy",
                "settingCount": 2,
                "settings": [{"id": "0"}, {"id": "1"}],
            }
            file_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")

            partial = {
                "name": "Policy",
                "settingCount": 2,
            }
            file_path.write_text(json.dumps(partial, indent=2) + "\n", encoding="utf-8")

            restored, unresolved = self.module.restore_partial_settings_from_baseline(
                repo_root=repo,
                backup_root=repo / "tenant-state" / "intune",
                baseline_ref="HEAD",
            )

            self.assertEqual(restored, ["tenant-state/intune/Settings Catalog/Policy__abc.json"])
            self.assertEqual(unresolved, [])
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["settings"], [{"id": "0"}, {"id": "1"}])

    def test_partial_settings_unresolved_without_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            (repo / "README.md").write_text("test\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "init")

            workload_dir = repo / "tenant-state" / "intune" / "Settings Catalog"
            workload_dir.mkdir(parents=True, exist_ok=True)
            file_path = workload_dir / "Policy__missing.json"
            file_path.write_text(json.dumps({"settingCount": 4}, indent=2) + "\n", encoding="utf-8")

            restored, unresolved = self.module.restore_partial_settings_from_baseline(
                repo_root=repo,
                backup_root=repo / "tenant-state" / "intune",
                baseline_ref="HEAD",
            )

            self.assertEqual(restored, [])
            self.assertEqual(unresolved, ["tenant-state/intune/Settings Catalog/Policy__missing.json"])


if __name__ == "__main__":
    unittest.main()
