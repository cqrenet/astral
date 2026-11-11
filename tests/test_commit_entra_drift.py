from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
