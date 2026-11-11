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

    def _ado(self):
        return self.module._Ado("https://dev.azure.com/org", "proj", "repo", {"Authorization": "Bearer t"})

    def test_ado_gateway_builds_the_expected_requests(self) -> None:
        ado = self._ado()
        self.assertEqual(ado.repo_api, "https://dev.azure.com/org/proj/_apis/git/repositories/repo")
        seen = []

        def fake(url, headers, method="GET", body=None):
            seen.append((method, url, body))
            if "threads" in url:
                return {"value": [{"comments": [{"content": "x"}, {"content": "has MARK inside"}]}]}
            return {"value": [{"pullRequestId": 1}], "treeId": " tree "}

        with patch.object(self.module, "_request_json", side_effect=fake):
            self.assertEqual(ado.query_prs("refs/heads/drift/entra", "refs/heads/main", "abandoned"), [{"pullRequestId": 1}])
            self.assertEqual(ado.commit_tree_id("abc"), "tree")
            self.assertTrue(ado.threads_with_marker(5, "MARK"))
            self.assertFalse(ado.threads_with_marker(5, "OTHER"))
            ado.update_pr(5, "T", "D", "squash")
            ado.create_pr("refs/heads/d", "refs/heads/main", "T", "D", "rebase")
        query_url = seen[0][1]
        self.assertIn("searchCriteria.status=abandoned", query_url)
        self.assertIn("searchCriteria.sourceRefName=refs/heads/drift/entra", query_url)  # slashes stay readable
        self.assertEqual(seen[1][1].split("?")[0].rsplit("/", 2)[-2:], ["commits", "abc"])
        self.assertEqual((seen[4][0], seen[4][2]["completionOptions"]), ("PATCH", {"mergeStrategy": "squash"}))
        self.assertEqual((seen[5][0], seen[5][2]["sourceRefName"], seen[5][2]["isDraft"]), ("POST", "refs/heads/d", False))

    def test_workload_scope_contains_only_workload_configuration_files(self) -> None:
        scope = self.module._WorkloadScope("entra", "tenant-state", "reports")
        self.assertTrue(scope.contains("tenant-state/entra/Conditional Access/policy.json"))
        self.assertFalse(scope.contains("tenant-state/entra/Conditional Access/policy.md"))
        self.assertFalse(scope.contains("tenant-state/reports/entra/assignment_report.md"))
        self.assertFalse(scope.contains("tenant-state/intune/Scripts/a.json"))  # other workload
        self.assertEqual(scope.prefix, "tenant-state/entra")
        self.assertEqual(self.module._WorkloadScope("", "tenant-state", "reports").prefix, "")
        self.assertFalse(self.module._WorkloadScope("entra", "", "reports").contains("entra/a.json"))

    def test_tree_api_fingerprint_matches_local_fingerprint_rules(self) -> None:
        scope = self.module._WorkloadScope("entra", "tenant-state", "reports")
        entries = [
            {"gitObjectType": "blob", "relativePath": "tenant-state/entra/CA/p.json", "objectId": "b1"},
            {"gitObjectType": "blob", "relativePath": "tenant-state/entra/README.md", "objectId": "b2"},  # doc
            {"gitObjectType": "tree", "relativePath": "tenant-state/entra/CA", "objectId": "t1"},
            {"gitObjectType": "blob", "relativePath": "tenant-state/entra/CA/q.json", "objectId": ""},  # no blob id
        ]
        ado = self._ado()
        with patch.object(self.module, "_request_json", return_value={"treeEntries": entries}):
            with_docs = ado.config_fingerprint("tree1", scope)
        with patch.object(self.module, "_request_json", return_value={"treeEntries": entries[:1]}):
            only_config = ado.config_fingerprint("tree1", scope)
        self.assertTrue(with_docs)
        self.assertEqual(with_docs, only_config)
        self.assertEqual(ado.config_fingerprint("", scope), "")

    def test_find_matching_abandoned_pr_by_tree_then_by_config_fingerprint(self) -> None:
        scope = self.module._WorkloadScope("entra", "tenant-state", "reports")
        newest = {"pullRequestId": 3, "creationDate": "2026-03", "lastMergeSourceCommit": {"commitId": "c3"}}
        older = {"pullRequestId": 2, "creationDate": "2026-02", "lastMergeSourceCommit": {"commitId": "c2"}}
        no_commit = {"pullRequestId": 1, "creationDate": "2026-01"}
        trees = {"c3": "treeX", "c2": "treeY"}

        ado = self._ado()
        with patch.object(self.module, "_config_fingerprint_from_local_tree", return_value="fp"), \
                patch.object(self.module._Ado, "commit_tree_id", side_effect=lambda c: trees[c]):
            # exact tree match wins and prefers the most recently created PR
            pr, reason = self.module._find_matching_abandoned_pr(ado, [older, newest, no_commit], "treeY", "/repo", scope, "HEAD")
            self.assertEqual((pr["pullRequestId"], reason), (2, "exact-tree"))

            with patch.object(self.module._Ado, "config_fingerprint", side_effect=lambda tree, _scope: "fp" if tree == "treeY" else "other") as api:
                pr, reason = self.module._find_matching_abandoned_pr(ado, [older, newest], "no-match", "/repo", scope, "HEAD")
                self.assertEqual((pr["pullRequestId"], reason), (2, "config-fingerprint"))
                self.assertEqual(api.call_count, 2)  # newest checked first, then the older match

            with patch.object(self.module._Ado, "config_fingerprint", side_effect=RuntimeError("api down")):
                self.assertEqual(self.module._find_matching_abandoned_pr(ado, [newest], "no-match", "/repo", scope, "HEAD"), (None, ""))

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
                scope=self.module._WorkloadScope("entra", "tenant-state", "reports"),
            )

            report_file.write_text("report v2\n", encoding="utf-8")
            doc_file.write_text("doc v2\n", encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "doc/report only"], repo)
            fp2 = self.module._config_fingerprint_from_local_tree(
                repo_root=str(repo),
                commitish="HEAD",
                scope=self.module._WorkloadScope("entra", "tenant-state", "reports"),
            )

            config_file.write_text('{"state":"disabled"}\n', encoding="utf-8")
            _run(["git", "add", "."], repo)
            _run(["git", "commit", "-m", "config change"], repo)
            fp3 = self.module._config_fingerprint_from_local_tree(
                repo_root=str(repo),
                commitish="HEAD",
                scope=self.module._WorkloadScope("entra", "tenant-state", "reports"),
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
                    scope=self.module._WorkloadScope("intune", "tenant-state", "reports"),
                )
            )
            self.assertTrue(
                self.module._workload_config_diff_exists(
                    repo_root=str(repo),
                    baseline_commitish=baseline_commit,
                    drift_commitish=config_change_commit,
                    scope=self.module._WorkloadScope("intune", "tenant-state", "reports"),
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
                with patch.object(self.module._Ado, "query_prs", return_value=[]):
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
                with patch.object(self.module._Ado, "query_prs", side_effect=[[], []]):
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
                    self.module._Ado,
                    "query_prs",
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
