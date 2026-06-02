from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ensure_rolling_pr.py"


def load_module():
    # Preload common helper so the script can import it.
    common_path = MODULE_PATH.parent / "common.py"
    common_spec = importlib.util.spec_from_file_location("common", common_path)
    if common_spec is not None and common_spec.loader is not None:
        common_mod = importlib.util.module_from_spec(common_spec)
        sys.modules["common"] = common_mod
        common_spec.loader.exec_module(common_mod)

    module_name = "ensure_rolling_pr"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class EnsureRollingPrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_is_workload_config_path_filters_docs_and_reports(self) -> None:
        is_path = self.module._is_workload_config_path

        self.assertTrue(
            is_path(
                "tenant-state/entra/Conditional Access/policy.json",
                workload_dir="entra",
                backup_folder="tenant-state",
                reports_subdir="reports",
            )
        )
        self.assertFalse(
            is_path(
                "tenant-state/entra/Conditional Access/policy.md",
                workload_dir="entra",
                backup_folder="tenant-state",
                reports_subdir="reports",
            )
        )
        self.assertFalse(
            is_path(
                "tenant-state/reports/entra/assignment_report.md",
                workload_dir="entra",
                backup_folder="tenant-state",
                reports_subdir="reports",
            )
        )

    def test_config_fingerprint_ignores_docs_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _run(["git", "init"], repo)
            _run(["git", "config", "user.name", "Test"], repo)
            _run(["git", "config", "user.email", "test@example.com"], repo)

            config_file = repo / "tenant-state" / "entra" / "Conditional Access" / "policy.json"
            report_file = repo / "tenant-state" / "reports" / "entra" / "summary.md"
            doc_file = repo / "tenant-state" / "entra" / "README.md"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.parent.mkdir(parents=True, exist_ok=True)
            doc_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text('{"state":"enabled"}\n', encoding="utf-8")
            report_file.write_text("report v1\n", encoding="utf-8")
            doc_file.write_text("doc v1\n", encoding="utf-8")

            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "initial"], repo)

            fp1 = self.module._config_fingerprint_from_local_tree(
                repo_root=str(repo),
                commitish="HEAD",
                workload_dir="entra",
                backup_folder="tenant-state",
                reports_subdir="reports",
            )

            report_file.write_text("report v2\n", encoding="utf-8")
            doc_file.write_text("doc v2\n", encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "doc/report only"], repo)
            fp2 = self.module._config_fingerprint_from_local_tree(
                repo_root=str(repo),
                commitish="HEAD",
                workload_dir="entra",
                backup_folder="tenant-state",
                reports_subdir="reports",
            )

            config_file.write_text('{"state":"disabled"}\n', encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "config change"], repo)
            fp3 = self.module._config_fingerprint_from_local_tree(
                repo_root=str(repo),
                commitish="HEAD",
                workload_dir="entra",
                backup_folder="tenant-state",
                reports_subdir="reports",
            )

            self.assertTrue(fp1)
            self.assertEqual(fp1, fp2)
            self.assertNotEqual(fp2, fp3)

    def test_ref_has_commit_for_local_and_missing_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _run(["git", "init"], repo)
            _run(["git", "config", "user.name", "Test"], repo)
            _run(["git", "config", "user.email", "test@example.com"], repo)
            (repo / "README.md").write_text("x\n", encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "init"], repo)

            self.assertTrue(self.module._ref_has_commit(str(repo), "HEAD"))
            self.assertFalse(self.module._ref_has_commit(str(repo), "origin/does-not-exist"))

    def test_workload_config_diff_exists_ignores_docs_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _run(["git", "init"], repo)
            _run(["git", "config", "user.name", "Test"], repo)
            _run(["git", "config", "user.email", "test@example.com"], repo)

            config_file = repo / "tenant-state" / "intune" / "Device Configurations" / "policy.json"
            report_file = repo / "tenant-state" / "reports" / "intune" / "summary.md"
            doc_file = repo / "tenant-state" / "intune" / "README.md"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.parent.mkdir(parents=True, exist_ok=True)
            doc_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text('{"setting":"enabled"}\n', encoding="utf-8")
            report_file.write_text("report v1\n", encoding="utf-8")
            doc_file.write_text("doc v1\n", encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "baseline"], repo)
            baseline_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            report_file.write_text("report v2\n", encoding="utf-8")
            doc_file.write_text("doc v2\n", encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "doc only"], repo)
            doc_only_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            config_file.write_text('{"setting":"disabled"}\n', encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "config change"], repo)
            config_change_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            self.assertFalse(
                self.module._workload_config_diff_exists(
                    repo_root=str(repo),
                    baseline_commitish=baseline_commit,
                    drift_commitish=doc_only_commit,
                    workload_dir="intune",
                    backup_folder="tenant-state",
                    reports_subdir="reports",
                )
            )
            self.assertTrue(
                self.module._workload_config_diff_exists(
                    repo_root=str(repo),
                    baseline_commitish=baseline_commit,
                    drift_commitish=config_change_commit,
                    workload_dir="intune",
                    backup_folder="tenant-state",
                    reports_subdir="reports",
                )
            )

    def test_main_suppresses_pr_creation_when_drift_matches_baseline_config(self) -> None:
        env = {
            "SYSTEM_ACCESSTOKEN": "token",
            "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example",
            "SYSTEM_TEAMPROJECT": "Project",
            "BUILD_REPOSITORY_ID": "repo-id",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch.object(
                sys,
                "argv",
                [
                    "ensure_rolling_pr.py",
                    "--repo-root",
                    "/tmp/repo",
                    "--workload",
                    "intune",
                    "--drift-branch",
                    "drift/intune",
                    "--baseline-branch",
                    "main",
                    "--pr-title",
                    "Intune drift review (rolling)",
                ],
            ):
                with patch.object(self.module, "_query_prs", return_value=[]):
                    with patch.object(self.module, "_run_git"):
                        with patch.object(self.module, "_ref_has_commit", return_value=True):
                            with patch.object(self.module, "_workload_config_diff_exists", return_value=False):
                                with patch.object(self.module, "_request_json") as request_json:
                                    result = self.module.main()

        self.assertEqual(result, 0)
        request_json.assert_not_called()

    def test_main_creates_pr_as_draft_when_notification_delay_enabled(self) -> None:
        env = {
            "SYSTEM_ACCESSTOKEN": "token",
            "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example",
            "SYSTEM_TEAMPROJECT": "Project",
            "BUILD_REPOSITORY_ID": "repo-id",
            "BUILD_BUILDNUMBER": "42",
            "BUILD_BUILDID": "1001",
            "ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS": "true",
        }
        created_bodies: list[dict[str, object]] = []

        def request_json(url: str, headers: dict[str, str], method: str = "GET", body: dict[str, object] | None = None):
            if method == "POST" and url.endswith("/pullrequests?api-version=7.1"):
                created_bodies.append(body or {})
                return {"pullRequestId": 123}
            raise AssertionError(f"Unexpected request: {method} {url}")

        with patch.dict(os.environ, env, clear=False):
            with patch.object(
                sys,
                "argv",
                [
                    "ensure_rolling_pr.py",
                    "--repo-root",
                    "/tmp/repo",
                    "--workload",
                    "intune",
                    "--drift-branch",
                    "drift/intune",
                    "--baseline-branch",
                    "main",
                    "--pr-title",
                    "Intune drift review (rolling)",
                ],
            ):
                with patch.object(self.module, "_query_prs", side_effect=[[], []]):
                    with patch.object(self.module, "_run_git"):
                        with patch.object(self.module, "_ref_has_commit", return_value=True):
                            with patch.object(self.module, "_workload_config_diff_exists", return_value=True):
                                with patch.object(self.module, "_tree_id_for_commitish", return_value="tree123"):
                                    with patch.object(self.module, "_find_matching_abandoned_pr", return_value=(None, "")):
                                        with patch.object(self.module, "_request_json", side_effect=request_json):
                                            result = self.module.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(created_bodies), 1)
        self.assertTrue(created_bodies[0]["isDraft"])

    def test_main_skips_active_pr_patch_when_already_up_to_date(self) -> None:
        env = {
            "SYSTEM_ACCESSTOKEN": "token",
            "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example",
            "SYSTEM_TEAMPROJECT": "Project",
            "BUILD_REPOSITORY_ID": "repo-id",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch.object(
                sys,
                "argv",
                [
                    "ensure_rolling_pr.py",
                    "--repo-root",
                    "/tmp/repo",
                    "--workload",
                    "intune",
                    "--drift-branch",
                    "drift/intune",
                    "--baseline-branch",
                    "main",
                    "--pr-title",
                    "Intune drift review (rolling)",
                ],
            ):
                with patch.object(
                    self.module,
                    "_query_prs",
                    return_value=[
                        {
                            "pullRequestId": 123,
                            "title": "Intune drift review (rolling)",
                            "description": "Existing description with summary",
                            "completionOptions": {"mergeStrategy": "rebase"},
                            "url": "https://dev.azure.com/example/_apis/git/repositories/repo/pullRequests/123",
                        }
                    ],
                ):
                    with patch.object(self.module, "_request_json") as request_json:
                        result = self.module.main()

        self.assertEqual(result, 0)
        request_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
