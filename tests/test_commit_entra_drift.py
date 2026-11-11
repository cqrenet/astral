from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "commit_entra_drift.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # script imports its sibling common.py

CA = "tenant-state/entra/Conditional Access"


def load_module():
    spec = importlib.util.spec_from_file_location("commit_entra_drift", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit(upn: str, resource_id: str, result: str = "success") -> dict:
    return {
        "result": result,
        "initiatedBy": {"user": {"userPrincipalName": upn, "displayName": upn.split("@")[0]}},
        "targetResources": [{"id": resource_id}],
    }


class BuildAuthorGroupsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()
        cls.fallback = cls.m.Identity(key="fallback:bot@x", value="bot@x", name="Bot")

    def _group(self, files, audits):
        groups, unresolved = self.m._build_author_groups(files, audits, self.fallback)
        return {key: group["files"] for key, group in groups.items()}, unresolved

    def test_files_are_grouped_by_the_identity_that_changed_them(self) -> None:
        a, b, c = f"{CA}/A__id1.json", f"{CA}/B__id2.json", f"{CA}/C__id3.json"
        groups, unresolved = self._group([a, b, c], [_audit("u1@x", "id1"), _audit("u2@x", "id2"), _audit("u1@x", "id3")])
        self.assertEqual(groups, {"user:u1@x": [a, c], "user:u2@x": [b]})
        self.assertEqual(unresolved, 0)

    def test_failed_audits_are_ignored_and_unmatched_files_count_as_unresolved(self) -> None:
        path = f"{CA}/A__id1.json"
        groups, unresolved = self._group([path], [_audit("u1@x", "id1", result="failure")])
        self.assertEqual(groups, {self.fallback.key: [path]})
        self.assertEqual(unresolved, 1)

    def test_unmatched_file_inherits_identities_of_its_category(self) -> None:
        known, other_cat, no_id = f"{CA}/A__id1.json", "tenant-state/entra/Named Locations/N__id9.json", f"{CA}/Plain.json"
        groups, unresolved = self._group([known, other_cat, no_id], [_audit("u1@x", "id1")])
        self.assertEqual(groups, {"user:u1@x": [known, no_id], self.fallback.key: [other_cat]})
        self.assertEqual(unresolved, 1)  # other_cat has a resource id nobody touched; Plain.json has none

    def test_multiple_identities_form_one_combined_group(self) -> None:
        path = f"{CA}/A__id1.json"
        groups, _ = self._group([path], [_audit("u2@x", "id1"), _audit("u1@x", "id1")])
        self.assertEqual(groups, {"user:u1@x&user:u2@x": [path]})  # sorted, so deterministic


class MainEndToEndTests(unittest.TestCase):
    """main() against real local repositories with canned audit events."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def _setup(self, td):
        origin, work = str(Path(td, "origin.git")), str(Path(td, "work"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        subprocess.run(["git", "clone", "-q", origin, work], check=True, capture_output=True)
        folder = Path(work, "tenant-state", "entra", "Conditional Access")
        folder.mkdir(parents=True)
        (folder / "A__id1.json").write_text("{}\n")
        (folder / "B__id2.json").write_text("{}\n")
        self._git(work, "checkout", "-q", "-b", "main")
        self._git(work, "add", ".")
        self._git(work, "commit", "-q", "-m", "baseline")
        self._git(work, "push", "-q", "origin", "main")
        self._git(work, "checkout", "-q", "-b", "drift/entra")
        (folder / "A__id1.json").write_text('{"state": "on"}\n')
        (folder / "B__id2.json").write_text('{"state": "off"}\n')
        return origin, work

    def _run(self, work, audits):
        outputs: dict[str, str] = {}
        argv = ["x", "--repo-root", work, "--workload-root", "tenant-state/entra", "--baseline-branch", "main",
                "--drift-branch", "drift/entra", "--access-token", "tok", "--service-name", "Svc",
                "--service-email", "svc@x", "--backup-start", "2026.01.02:03.04.05"]
        with patch.object(sys, "argv", argv), \
                patch.object(self.m, "_fetch_directory_audits", return_value=audits), \
                patch.object(self.m, "_set_output_var", side_effect=lambda name, value, *a: outputs.update({name: value})):
            return self.m.main(), outputs

    def test_changes_are_committed_per_author_and_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            origin, work = self._setup(td)
            code, outputs = self._run(work, [_audit("u1@x", "id1"), _audit("u2@x", "id2")])
            self.assertEqual(code, 0)
            subjects = self._git(origin, "log", "main..drift/entra", "--format=%an|%s").splitlines()
            self.assertEqual(len(subjects), 2)
            self.assertEqual({line.split("|")[0] for line in subjects}, {"u1", "u2"})
            self.assertTrue(all("(Entra)" in line and "2026.01.02_03.04" in line for line in subjects))
        self.assertEqual((outputs["CHANGE_DETECTED"], outputs["ROLLING_PR_SYNC_REQUIRED"]), ("1", "1"))
        self.assertEqual(outputs["MODIFICATION_AUTHOR"], "u1@x, u2@x")
        self.assertEqual(outputs["COMMIT_DATE"], "2026.01.02_03.04")

    def test_unreadable_audit_log_falls_back_to_pipeline_identity(self) -> None:
        import urllib.error

        with tempfile.TemporaryDirectory() as td:
            origin, work = self._setup(td)
            argv_error = urllib.error.HTTPError("https://x", 403, "forbidden", {}, None)
            with patch.object(self.m, "_fetch_directory_audits", side_effect=argv_error), \
                    patch.object(self.m, "_set_output_var"), \
                    patch.object(sys, "argv", ["x", "--repo-root", work, "--workload-root", "tenant-state/entra",
                                               "--baseline-branch", "main", "--drift-branch", "drift/entra",
                                               "--service-name", "Svc", "--service-email", "svc@x",
                                               "--backup-start", "2026.01.02:03.04.05"]):
                self.assertEqual(self.m.main(), 0)
            authors = set(self._git(origin, "log", "main..drift/entra", "--format=%an").splitlines())
        self.assertEqual(authors, {"Svc"})

    def test_no_change_reports_nothing_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, work = self._setup(td)
            self._git(work, "checkout", "-q", "--", ".")  # discard the drift
            code, outputs = self._run(work, [])
        self.assertEqual(code, 0)
        self.assertEqual(outputs, {"CHANGE_DETECTED": "0", "ROLLING_PR_SYNC_REQUIRED": "0"})


if __name__ == "__main__":
    unittest.main()
