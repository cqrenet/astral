from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "filter_entra_enrichment_noise.py"


def load_module():
    spec = importlib.util.spec_from_file_location("filter_entra_enrichment_noise", MODULE_PATH)
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


class FilterEntraEnrichmentNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_is_enrichment_only_change_true(self) -> None:
        old_text = json.dumps(
            {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
                "requiredResourceAccessResolved": [{"resourceDisplayName": "Microsoft Graph"}],
                "resolutionStatus": {"requiredResourceAccess": {"unresolvedPermissionCount": 0}},
            }
        )
        new_text = json.dumps(
            {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
                "requiredResourceAccessResolved": [{"resourceDisplayName": "Unresolved"}],
                "resolutionStatus": {"requiredResourceAccess": {"unresolvedPermissionCount": 6}},
            }
        )
        self.assertTrue(self.module._is_enrichment_only_change(old_text, new_text))

    def test_is_enrichment_only_change_false_when_config_changes(self) -> None:
        old_text = json.dumps(
            {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
            }
        )
        new_text = json.dumps(
            {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "11111111-0000-0000-c000-000000000000"}],
            }
        )
        self.assertFalse(self.module._is_enrichment_only_change(old_text, new_text))

    def test_filter_reverts_only_enrichment_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            workload_dir = repo / "tenant-state" / "entra" / "App Registrations"
            workload_dir.mkdir(parents=True, exist_ok=True)
            file_path = workload_dir / "Test App__id.json"
            baseline = {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
                "requiredResourceAccessResolved": [{"resourceDisplayName": "Microsoft Graph"}],
                "resolutionStatus": {"requiredResourceAccess": {"unresolvedPermissionCount": 0}},
            }
            file_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")

            enrichment_only = {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
                "requiredResourceAccessResolved": [{"resourceDisplayName": "Unresolved"}],
                "resolutionStatus": {"requiredResourceAccess": {"unresolvedPermissionCount": 6}},
            }
            file_path.write_text(json.dumps(enrichment_only, indent=2) + "\n", encoding="utf-8")

            residual_before = self.module.find_enrichment_only_modified_files(
                repo_root=repo,
                workload_root="tenant-state/entra",
            )
            self.assertEqual(residual_before, ["tenant-state/entra/App Registrations/Test App__id.json"])

            reverted = self.module.filter_enrichment_only_files(repo_root=repo, workload_root="tenant-state/entra")

            self.assertEqual(reverted, ["tenant-state/entra/App Registrations/Test App__id.json"])
            residual_after = self.module.find_enrichment_only_modified_files(
                repo_root=repo,
                workload_root="tenant-state/entra",
            )
            self.assertEqual(residual_after, [])
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(repo),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(status, "")

    def test_filter_keeps_real_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tester@example.com")
            _git(repo, "config", "user.name", "Tester")

            workload_dir = repo / "tenant-state" / "entra" / "App Registrations"
            workload_dir.mkdir(parents=True, exist_ok=True)
            file_path = workload_dir / "Test App__id.json"
            baseline = {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "00000003-0000-0000-c000-000000000000"}],
                "requiredResourceAccessResolved": [{"resourceDisplayName": "Microsoft Graph"}],
            }
            file_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")

            config_changed = {
                "displayName": "App",
                "requiredResourceAccess": [{"resourceAppId": "11111111-0000-0000-c000-000000000000"}],
                "requiredResourceAccessResolved": [{"resourceDisplayName": "Unresolved"}],
            }
            file_path.write_text(json.dumps(config_changed, indent=2) + "\n", encoding="utf-8")

            reverted = self.module.filter_enrichment_only_files(repo_root=repo, workload_root="tenant-state/entra")

            self.assertEqual(reverted, [])
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(repo),
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("Test App__id.json", status)


if __name__ == "__main__":
    unittest.main()
