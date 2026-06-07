from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_entra_identity.py"


def load_module():
    spec = importlib.util.spec_from_file_location("export_entra_identity", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        token="test-token",
        include_groups="false",
        include_role_assignments="false",
        include_auth_methods_policy="false",
        include_cross_tenant_access="false",
        include_identity_protection="false",
        watch_groups_csv="",
        fail_on_export_error="true",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestGroupExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def _make_ca_dir(self, tmp: Path, policies: list[dict]) -> Path:
        ca_dir = tmp / "entra" / "Conditional Access"
        ca_dir.mkdir(parents=True, exist_ok=True)
        for idx, policy in enumerate(policies):
            path = ca_dir / f"Policy{idx}__{policy.get('id', str(idx))}.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
        return tmp / "entra"

    def test_collect_ca_group_ids_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ca_dir = Path(td) / "Conditional Access"
            ca_dir.mkdir()
            refs = self.m._collect_ca_group_ids(ca_dir)
            self.assertEqual(refs, {})

    def test_collect_ca_group_ids_extracts_include_and_exclude(self) -> None:
        policy = {
            "displayName": "Require MFA",
            "id": "pol1",
            "conditions": {
                "users": {
                    "includeGroups": ["group-aaa"],
                    "excludeGroups": ["group-bbb", "group-ccc"],
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            ca_dir = Path(td) / "Conditional Access"
            ca_dir.mkdir()
            (ca_dir / "Policy__pol1.json").write_text(json.dumps(policy), encoding="utf-8")
            refs = self.m._collect_ca_group_ids(ca_dir)
        self.assertIn("group-aaa", refs)
        self.assertIn("group-bbb", refs)
        self.assertIn("group-ccc", refs)
        self.assertIn("Require MFA", refs["group-aaa"])
        self.assertIn("Require MFA", refs["group-bbb"])

    def test_export_groups_no_ca_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            written, failed = self.m.export_groups(client, root, fail_on_error=True)
        self.assertEqual(written, 0)
        self.assertEqual(failed, [])
        client.get_object.assert_not_called()

    def test_export_groups_writes_json_with_member_count(self) -> None:
        policy = {
            "displayName": "Block Legacy Auth",
            "id": "pol-1",
            "conditions": {"users": {"excludeGroups": ["grp-123"]}},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            ca_dir = root / "Conditional Access"
            ca_dir.mkdir(parents=True)
            (ca_dir / "Block__pol-1.json").write_text(json.dumps(policy), encoding="utf-8")

            client = MagicMock()
            client.get_object.return_value = (
                {"id": "grp-123", "displayName": "Break-Glass", "groupTypes": [], "securityEnabled": True, "mailEnabled": False, "mail": None},
                None,
            )
            client.get_count.return_value = (2, None)

            written, failed = self.m.export_groups(client, root, fail_on_error=True)

            self.assertEqual(written, 1)
            self.assertEqual(failed, [])

            group_files = list((root / "Groups").glob("*.json"))
            self.assertEqual(len(group_files), 1)
            data = json.loads(group_files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["id"], "grp-123")
            self.assertEqual(data["displayName"], "Break-Glass")
            self.assertEqual(data["memberCount"], 2)
            self.assertIn("Block Legacy Auth", data["caReferences"])

    def test_watched_group_includes_full_member_list(self) -> None:
        policy = {
            "displayName": "Require MFA",
            "id": "pol-2",
            "conditions": {"users": {"excludeGroups": ["aaaaaaaa-0000-0000-0000-000000000001"]}},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            ca_dir = root / "Conditional Access"
            ca_dir.mkdir(parents=True)
            (ca_dir / "MFA__pol-2.json").write_text(json.dumps(policy), encoding="utf-8")

            client = MagicMock()
            client.get_object.return_value = (
                {"id": "aaaaaaaa-0000-0000-0000-000000000001", "displayName": "Break-Glass", "groupTypes": [], "securityEnabled": True, "mailEnabled": False, "mail": None},
                None,
            )
            # Members for watched group
            client.get_collection.return_value = (
                [
                    {"id": "u1", "@odata.type": "#microsoft.graph.user", "displayName": "Alice", "userPrincipalName": "alice@c.com"},
                    {"id": "u2", "@odata.type": "#microsoft.graph.user", "displayName": "Bob", "userPrincipalName": "bob@c.com"},
                ],
                None,
            )

            watch = {"aaaaaaaa-0000-0000-0000-000000000001"}
            written, failed = self.m.export_groups(client, root, fail_on_error=True, watch_groups=watch)

            self.assertEqual(written, 1)
            self.assertEqual(failed, [])
            group_files = list((root / "Groups").glob("*.json"))
            self.assertEqual(len(group_files), 1)
            data = json.loads(group_files[0].read_text(encoding="utf-8"))
            self.assertTrue(data["watched"])
            self.assertEqual(data["memberCount"], 2)
            self.assertEqual(len(data["members"]), 2)
            self.assertEqual(data["members"][0]["userPrincipalName"], "alice@c.com")

    def test_watched_group_exported_even_if_not_ca_referenced(self) -> None:
        # CA dir exists but has no policies referencing our watched group
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            ca_dir = root / "Conditional Access"
            ca_dir.mkdir(parents=True)

            client = MagicMock()
            client.get_object.return_value = (
                {"id": "bbbbbbbb-0000-0000-0000-000000000001", "displayName": "IT Admins", "groupTypes": [], "securityEnabled": True, "mailEnabled": False, "mail": None},
                None,
            )
            client.get_collection.return_value = ([], None)

            watch = {"bbbbbbbb-0000-0000-0000-000000000001"}
            written, failed = self.m.export_groups(client, root, fail_on_error=True, watch_groups=watch)

            self.assertEqual(written, 1)
            group_files = list((root / "Groups").glob("*.json"))
            data = json.loads(group_files[0].read_text(encoding="utf-8"))
            self.assertTrue(data["watched"])
            self.assertEqual(data["caReferences"], [])

    def test_parse_watch_groups_csv_accepts_valid_guids(self) -> None:
        result = self.m._parse_watch_groups_csv(
            "aaaaaaaa-0000-0000-0000-000000000001, BBBBBBBB-0000-0000-0000-000000000002"
        )
        self.assertIn("aaaaaaaa-0000-0000-0000-000000000001", result)
        self.assertIn("bbbbbbbb-0000-0000-0000-000000000002", result)

    def test_parse_watch_groups_csv_ignores_non_guids(self) -> None:
        result = self.m._parse_watch_groups_csv("Break-Glass, , not-a-guid")
        self.assertEqual(result, set())

    def test_parse_watch_groups_csv_empty_string(self) -> None:
        self.assertEqual(self.m._parse_watch_groups_csv(""), set())

    def test_export_groups_graph_error_fails_gracefully(self) -> None:
        policy = {
            "displayName": "Test Policy",
            "id": "p1",
            "conditions": {"users": {"includeGroups": ["grp-xyz"]}},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            ca_dir = root / "Conditional Access"
            ca_dir.mkdir(parents=True)
            (ca_dir / "Test__p1.json").write_text(json.dumps(policy), encoding="utf-8")

            client = MagicMock()
            client.get_object.return_value = (None, "HTTP 403")

            written, failed = self.m.export_groups(client, root, fail_on_error=False)

        self.assertEqual(written, 0)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0], "grp-xyz")


class TestRoleAssignmentExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_export_role_assignments_writes_files(self) -> None:
        role_defs = [{"id": "role-1", "displayName": "Global Administrator", "description": "Full control.", "isBuiltIn": True, "isEnabled": True}]
        perm_assignments = [{"id": "assign-1", "principalId": "user-1", "roleDefinitionId": "role-1", "directoryScopeId": "/"}]
        eligible_assignments: list = []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()

            client = MagicMock()
            # get_collection calls: role definitions, permanent assignments, eligible assignments
            client.get_collection.side_effect = [
                (role_defs, None),
                (perm_assignments, None),
                (eligible_assignments, None),
            ]
            client.post_collection.return_value = (
                [{"id": "user-1", "@odata.type": "#microsoft.graph.user", "displayName": "Alice Admin", "userPrincipalName": "alice@contoso.com"}],
                None,
            )

            written, failed = self.m.export_role_assignments(client, root, fail_on_error=True)

            self.assertEqual(written, 1)
            self.assertEqual(failed, [])

            role_files = list((root / "Role Assignments").glob("*.json"))
            self.assertEqual(len(role_files), 1)
            data = json.loads(role_files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["displayName"], "Global Administrator")
            self.assertEqual(len(data["permanentAssignments"]), 1)
            self.assertEqual(data["permanentAssignments"][0]["principalDisplayName"], "Alice Admin")
            self.assertEqual(data["permanentAssignments"][0]["userPrincipalName"], "alice@contoso.com")
            self.assertEqual(data["eligibleAssignments"], [])

    def test_export_role_assignments_skips_roles_with_no_assignments(self) -> None:
        role_defs = [
            {"id": "role-1", "displayName": "Global Administrator", "isBuiltIn": True, "isEnabled": True},
            {"id": "role-2", "displayName": "Reports Reader", "isBuiltIn": True, "isEnabled": True},
        ]
        perm_assignments = [{"id": "a1", "principalId": "u1", "roleDefinitionId": "role-1", "directoryScopeId": "/"}]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()

            client = MagicMock()
            client.get_collection.side_effect = [(role_defs, None), (perm_assignments, None), ([], None)]
            client.post_collection.return_value = ([{"id": "u1", "@odata.type": "#microsoft.graph.user", "displayName": "Bob"}], None)

            written, _ = self.m.export_role_assignments(client, root, fail_on_error=True)

        # Only Global Admin (has an assignment) should be written; Reports Reader (no assignments) skipped
        self.assertEqual(written, 1)

    def test_batch_resolve_principals_groups_by_type(self) -> None:
        client = MagicMock()
        client.post_collection.return_value = (
            [
                {"id": "u1", "@odata.type": "#microsoft.graph.user", "displayName": "Alice", "userPrincipalName": "alice@c.com"},
                {"id": "sp1", "@odata.type": "#microsoft.graph.servicePrincipal", "displayName": "My App", "userPrincipalName": ""},
            ],
            None,
        )
        m = self.m
        result = m._batch_resolve_principals(client, ["u1", "sp1"])
        self.assertEqual(result["u1"]["principalType"], "user")
        self.assertEqual(result["sp1"]["principalType"], "servicePrincipal")


class TestAuthMethodsPolicyExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_exports_policy_file(self) -> None:
        policy = {
            "id": "authMethodsPolicy-id",
            "description": "Authentication methods policy",
            "authenticationMethodConfigurations": [
                {"id": "fido2", "state": "enabled"},
                {"id": "microsoftAuthenticator", "state": "enabled"},
                {"id": "voice", "state": "disabled"},
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.return_value = (policy, None)
            written, failed = self.m.export_auth_methods_policy(client, root, fail_on_error=True)

            self.assertEqual(written, 1)
            self.assertEqual(failed, [])
            out = root / "Authentication Methods" / "Authentication Methods Policy.json"
            self.assertTrue(out.exists())
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(data["authenticationMethodConfigurations"]), 3)

    def test_returns_failure_on_graph_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.return_value = (None, "HTTP 403")
            written, failed = self.m.export_auth_methods_policy(client, root, fail_on_error=False)

        self.assertEqual(written, 0)
        self.assertEqual(len(failed), 1)


class TestCrossTenantAccessExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_exports_default_and_external(self) -> None:
        default_settings = {"isServiceDefault": True, "inboundTrust": {}}
        ext_policy = {"allowInvitesFrom": "adminsAndGuestInviters"}
        partners: list = []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.side_effect = [(default_settings, None), (ext_policy, None)]
            client.get_collection.return_value = (partners, None)

            written, failed = self.m.export_cross_tenant_access(client, root, fail_on_error=True)

            self.assertEqual(failed, [])
            self.assertTrue((root / "Cross-Tenant Access" / "Default Settings.json").exists())
            self.assertTrue((root / "Cross-Tenant Access" / "External Collaboration Settings.json").exists())

    def test_exports_partner_per_file(self) -> None:
        default_settings: dict = {}
        ext_policy: dict = {}
        partners = [
            {"tenantId": "tenant-aaa", "displayName": "Partner A"},
            {"tenantId": "tenant-bbb", "displayName": "Partner B"},
        ]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.side_effect = [(default_settings, None), (ext_policy, None)]
            client.get_collection.return_value = (partners, None)

            written, failed = self.m.export_cross_tenant_access(client, root, fail_on_error=True)

            partner_files = list((root / "Cross-Tenant Access" / "Partners").glob("*.json"))
            self.assertEqual(len(partner_files), 2)


class TestIdentityProtectionExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_exports_both_risk_policies(self) -> None:
        signin_policy = {"id": "signInRiskPolicy", "isEnabled": True, "riskLevelIfRisky": "high"}
        user_policy = {"id": "userRiskPolicy", "isEnabled": True, "riskLevelIfRisky": "high"}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.side_effect = [(signin_policy, None), (user_policy, None)]

            written, failed = self.m.export_identity_protection(client, root, fail_on_error=True)

            self.assertEqual(written, 2)
            self.assertEqual(failed, [])
            self.assertTrue((root / "Identity Protection" / "Sign-in Risk Policy.json").exists())
            self.assertTrue((root / "Identity Protection" / "User Risk Policy.json").exists())

    def test_skips_404_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.side_effect = [(None, "HTTP 404"), (None, "HTTP 404")]

            written, failed = self.m.export_identity_protection(client, root, fail_on_error=True)

        self.assertEqual(written, 0)
        # 404 is treated as informational (P2 license not present), not a failure
        self.assertEqual(failed, [])

    def test_non_404_error_adds_to_failed(self) -> None:
        signin_policy = {"id": "signInRiskPolicy"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            client = MagicMock()
            client.get_object.side_effect = [(signin_policy, None), (None, "HTTP 500")]

            written, failed = self.m.export_identity_protection(client, root, fail_on_error=False)

        self.assertEqual(written, 1)
        self.assertEqual(len(failed), 1)


class TestMainEntryPoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_module()

    def test_empty_token_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            args = _make_args(root=str(root), token="", include_groups="true")
            with patch.object(self.m, "parse_args", return_value=args):
                result = self.m.main()
        self.assertEqual(result, 0)

    def test_all_disabled_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            args = _make_args(root=str(root), token="tok")
            with patch.object(self.m, "parse_args", return_value=args):
                result = self.m.main()
        self.assertEqual(result, 0)

    def test_export_error_with_fail_on_error_true_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            # Enable role assignments but make Graph fail
            args = _make_args(root=str(root), token="tok", include_role_assignments="true")
            with (
                patch.object(self.m, "parse_args", return_value=args),
                patch.object(self.m, "export_role_assignments", return_value=(0, [("Role Definitions", "HTTP 403")])),
            ):
                result = self.m.main()
        self.assertEqual(result, 2)

    def test_export_error_with_fail_on_error_false_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir()
            args = _make_args(root=str(root), token="tok", include_role_assignments="true", fail_on_export_error="false")
            with (
                patch.object(self.m, "parse_args", return_value=args),
                patch.object(self.m, "export_role_assignments", return_value=(0, [("Role Definitions", "HTTP 403")])),
            ):
                result = self.m.main()
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
