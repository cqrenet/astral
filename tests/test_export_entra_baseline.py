from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_entra_baseline.py"


def load_module():
    spec = importlib.util.spec_from_file_location("export_entra_baseline", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportEntraBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def _namespace(self, root: Path, fail_on_export_error: str) -> SimpleNamespace:
        return SimpleNamespace(
            root=str(root),
            token="token-value",
            include_named_locations="true",
            include_authentication_strengths="false",
            include_conditional_access="false",
            include_enterprise_applications="false",
            include_app_registrations="false",
            enterprise_app_workers=1,
            fail_on_export_error=fail_on_export_error,
            previous_snapshot_ref="",
        )

    def test_requested_export_error_is_fatal_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir(parents=True, exist_ok=True)
            args = self._namespace(root=root, fail_on_export_error="true")

            with (
                patch.object(self.module, "parse_args", return_value=args),
                patch.object(self.module, "GraphClient") as graph_client_cls,
            ):
                graph_client = MagicMock()
                graph_client.get_object.return_value = ({"value": []}, None)
                graph_client.get_collection.return_value = ([], "HTTP 500")
                graph_client_cls.return_value = graph_client

                result = self.module.main()
                self.assertEqual(result, 2)

    def test_requested_export_error_can_be_non_fatal_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            root.mkdir(parents=True, exist_ok=True)
            args = self._namespace(root=root, fail_on_export_error="false")

            with (
                patch.object(self.module, "parse_args", return_value=args),
                patch.object(self.module, "GraphClient") as graph_client_cls,
            ):
                graph_client = MagicMock()
                graph_client.get_object.return_value = ({"value": []}, None)
                graph_client.get_collection.return_value = ([], "HTTP 500")
                graph_client_cls.return_value = graph_client

                result = self.module.main()
                self.assertEqual(result, 0)

    def test_normalize_resolution_error_suppresses_transient_dns_variants(self) -> None:
        transient_samples = [
            "<urlopen error [Errno -3] Temporary failure in name resolution>",
            "Temporary failure resolving 'graph.microsoft.com'",
            "Failed to resolve host graph.microsoft.com",
            "getaddrinfo failed",
        ]
        for sample in transient_samples:
            with self.subTest(sample=sample):
                self.assertEqual(self.module.normalize_resolution_error(sample), "")

    def test_normalize_resolution_error_keeps_non_transient_http_error(self) -> None:
        self.assertEqual(self.module.normalize_resolution_error("HTTP 403"), "HTTP 403")

    def test_normalize_branch_name_ignores_unresolved_macro(self) -> None:
        self.assertEqual(self.module._normalize_branch_name("$(DRIFT_BRANCH_ENTRA)"), "")

    def test_required_resource_resolution_backfills_unresolved_from_previous(self) -> None:
        current = [
            {
                "resourceAppId": "00000003-0000-0000-c000-000000000000",
                "resourceDisplayName": "Unresolved",
                "permissions": [
                    {
                        "id": "perm-id-1",
                        "type": "Scope",
                        "value": "",
                        "displayName": "",
                        "description": "",
                    }
                ],
            }
        ]
        previous = [
            {
                "resourceAppId": "00000003-0000-0000-c000-000000000000",
                "resourceDisplayName": "Microsoft Graph",
                "permissions": [
                    {
                        "id": "perm-id-1",
                        "type": "Scope",
                        "value": "User.Read.All",
                        "displayName": "Read all users' full profiles",
                        "description": "Allows the app to read full profiles.",
                    }
                ],
            }
        ]

        merged = self.module._merge_required_resource_access_resolution(current, previous)
        self.assertEqual(merged[0]["resourceDisplayName"], "Microsoft Graph")
        self.assertEqual(merged[0]["permissions"][0]["value"], "User.Read.All")
        self.assertEqual(merged[0]["permissions"][0]["displayName"], "Read all users' full profiles")

        unresolved_resources, unresolved_permissions = self.module._count_unresolved_required_permissions(merged)
        self.assertEqual(unresolved_resources, 0)
        self.assertEqual(unresolved_permissions, 0)

    def test_app_role_resolution_backfills_unresolved_from_previous(self) -> None:
        current = [
            {
                "resourceId": "resource-1",
                "resourceDisplayName": "Unresolved",
                "appRoleId": "role-1",
                "appRoleValue": "",
                "appRoleDisplayName": "",
                "principalType": "ServicePrincipal",
            }
        ]
        previous = [
            {
                "resourceId": "resource-1",
                "resourceDisplayName": "Office 365 Exchange Online",
                "appRoleId": "role-1",
                "appRoleValue": "Exchange.ManageAsApp",
                "appRoleDisplayName": "Manage Exchange as application",
                "principalType": "ServicePrincipal",
            }
        ]

        merged = self.module._merge_app_role_assignments_resolution(current, previous)
        self.assertEqual(merged[0]["resourceDisplayName"], "Office 365 Exchange Online")
        self.assertEqual(merged[0]["appRoleValue"], "Exchange.ManageAsApp")
        self.assertEqual(merged[0]["appRoleDisplayName"], "Manage Exchange as application")

        unresolved_resources, unresolved_roles = self.module._count_unresolved_app_role_assignments(merged)
        self.assertEqual(unresolved_resources, 0)
        self.assertEqual(unresolved_roles, 0)

    def test_required_resource_access_uses_direct_appid_fallback_when_filter_returns_empty(self) -> None:
        app = {
            "requiredResourceAccess": [
                {
                    "resourceAppId": "00000003-0000-0000-c000-000000000000",
                    "resourceAccess": [
                        {
                            "id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d",
                            "type": "Scope",
                        }
                    ],
                }
            ]
        }
        client = MagicMock()
        client.get_object.side_effect = [
            ({"value": []}, None),
            (
                {
                    "id": "sp-graph",
                    "appId": "00000003-0000-0000-c000-000000000000",
                    "displayName": "Microsoft Graph",
                    "appRoles": [],
                    "oauth2PermissionScopes": [
                        {
                            "id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d",
                            "value": "User.Read",
                            "adminConsentDisplayName": "Sign in and read user profile",
                            "adminConsentDescription": "Allows sign-in and profile read.",
                        }
                    ],
                },
                None,
            ),
        ]

        resolved, unresolved_resources, unresolved_permissions, lookup_errors = self.module.resolve_required_resource_access(
            app=app,
            client=client,
            resource_sp_by_appid={},
        )

        self.assertEqual(unresolved_resources, 0)
        self.assertEqual(unresolved_permissions, 0)
        self.assertEqual(lookup_errors, [])
        self.assertEqual(resolved[0]["resourceDisplayName"], "Microsoft Graph")
        self.assertEqual(resolved[0]["permissions"][0]["value"], "User.Read")

    def test_load_resource_sp_cache_from_export_reads_enterprise_apps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            export_dir = root / "Enterprise Applications"
            export_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "id": "sp-graph",
                "appId": "00000003-0000-0000-c000-000000000000",
                "displayName": "Microsoft Graph",
                "appRoles": [{"id": "role-1", "value": "Directory.Read.All"}],
                "oauth2PermissionScopes": [{"id": "scope-1", "value": "User.Read"}],
            }
            (export_dir / "Microsoft Graph__sp-graph.json").write_text(json.dumps(payload), encoding="utf-8")

            cache = self.module._load_resource_sp_cache_from_export(root)

            self.assertIn("00000003-0000-0000-c000-000000000000", cache)
            graph = cache["00000003-0000-0000-c000-000000000000"]
            self.assertEqual(graph["displayName"], "Microsoft Graph")
            self.assertEqual(graph["appRoles"][0]["value"], "Directory.Read.All")
            self.assertEqual(graph["oauth2PermissionScopes"][0]["value"], "User.Read")

    def test_load_resource_sp_cache_from_export_ignores_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "entra"
            export_dir = root / "Enterprise Applications"
            export_dir.mkdir(parents=True, exist_ok=True)
            (export_dir / "invalid.json").write_text("{", encoding="utf-8")
            (export_dir / "missing-appid.json").write_text(json.dumps({"id": "sp-only"}), encoding="utf-8")

            cache = self.module._load_resource_sp_cache_from_export(root)

            self.assertEqual(cache, {})


if __name__ == "__main__":
    unittest.main()
