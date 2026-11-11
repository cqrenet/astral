from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "resolve_ca_references.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # script imports its sibling common.py


def load_module():
    spec = importlib.util.spec_from_file_location("resolve_ca_references", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResolver:
    def group_name(self, value):
        return {"g1": "Admins"}.get(value)

    role_name = app_name = location_name = group_name

    def auth_strength_name(self, value):
        return "MFA" if value == "s1" else None


class EnrichPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _policy(self) -> dict:
        return {
            "conditions": {"users": {"includeGroups": ["g1", "g9"]}, "applications": {"includeApplications": []}},
            "grantControls": {"authenticationStrength": {"id": "s1"}},
        }

    def test_resolves_ids_and_marks_unresolved(self) -> None:
        policy = self._policy()
        self.assertTrue(self.m.enrich_policy(policy, FakeResolver()))
        self.assertEqual(
            policy["conditions"]["users"]["includeGroupsResolved"],
            [{"id": "g1", "displayName": "Admins"}, {"id": "g9", "displayName": "Unresolved"}],
        )
        self.assertEqual(policy["grantControls"]["authenticationStrengthResolved"], {"id": "s1", "displayName": "MFA"})

    def test_second_pass_changes_nothing(self) -> None:
        policy = self._policy()
        self.m.enrich_policy(policy, FakeResolver())
        self.assertFalse(self.m.enrich_policy(policy, FakeResolver()))

    def test_tolerates_missing_or_malformed_sections(self) -> None:
        self.assertFalse(self.m.enrich_policy({}, FakeResolver()))
        self.assertFalse(self.m.enrich_policy({"conditions": {"users": "bad"}, "grantControls": []}, FakeResolver()))


if __name__ == "__main__":
    unittest.main()
