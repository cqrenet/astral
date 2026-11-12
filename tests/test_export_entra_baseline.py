from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_entra_baseline.py"
sys.path.insert(0, str(MODULE_PATH.parent))  # scripts import their sibling common.py


def load_module():
    spec = importlib.util.spec_from_file_location("export_entra_baseline", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_entra_baseline"] = module
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

    def _resolved(self, org_name: str = "") -> dict:
        return {
            "appOwnerOrganizationResolved": {"displayName": org_name},
            "resolutionStatus": {"appRoleAssignments": {"count": 0}},
        }

    def test_enterprise_enrichment_same_result_sequential_and_parallel(self) -> None:
        def fake_enrich(item, client, by_id, lock, orgs):
            return [{"id": "o"}], [], self._resolved("Contoso")

        for workers in (1, 4):
            with self.subTest(workers=workers):
                items = [{"id": f"id{i}", "appId": f"app{i}"} for i in range(5)]
                caches = self.module._ResolutionCaches()
                with patch.object(self.module, "enrich_enterprise_application", side_effect=fake_enrich):
                    warnings = self.module._enrich_enterprise_apps(items, MagicMock(), caches, None, workers)
                self.assertEqual(warnings, 0)
                self.assertEqual(set(caches.sp_by_appid), {f"app{i}" for i in range(5)})
                self.assertEqual(set(caches.sp_by_id), {f"id{i}" for i in range(5)})
                for item in items:
                    self.assertEqual(item["ownersResolved"], [{"id": "o"}])
                    self.assertEqual(item["appOwnerOrganizationResolved"]["displayName"], "Contoso")

    def test_enterprise_enrichment_error_is_fatal_sequentially_but_recorded_in_parallel(self) -> None:
        def boom(item, client, by_id, lock, orgs):
            raise RuntimeError("HTTP 500")

        with patch.object(self.module, "enrich_enterprise_application", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.module._enrich_enterprise_apps([{"id": "1"}], MagicMock(), self.module._ResolutionCaches(), None, 1)
            items = [{"id": "1", "appOwnerOrganizationId": "t"}, {"id": "2", "appOwnerOrganizationId": "t"}]
            warnings = self.module._enrich_enterprise_apps(items, MagicMock(), self.module._ResolutionCaches(), None, 2)
        self.assertEqual(warnings, 2)
        self.assertEqual(items[0]["ownersResolved"], [])
        self.assertEqual(items[0]["resolutionStatus"]["owners"]["count"], 0)

    def test_enterprise_enrichment_backfills_org_name_from_previous_snapshot(self) -> None:
        previous = MagicMock()
        previous.get.return_value = {"appOwnerOrganizationResolved": {"displayName": "Old Org"}}
        item = {"id": "1"}
        with patch.object(
            self.module, "enrich_enterprise_application", return_value=([], [], self._resolved(""))
        ):
            self.module._enrich_enterprise_apps([item], MagicMock(), self.module._ResolutionCaches(), previous, 1)
        self.assertEqual(item["appOwnerOrganizationResolved"]["displayName"], "Old Org")

    def _graph(self, objects=None, collections=None):
        """Fake GraphClient answering by URL substring; records the object URLs requested."""
        client = MagicMock()
        client.object_urls = []

        def get_object(url):
            client.object_urls.append(url)
            for needle, answer in (objects or {}).items():
                if needle in url:
                    return answer
            return None, None

        client.get_object.side_effect = get_object
        client.get_collection.side_effect = lambda url: (collections or {}).get(url, ([], None))
        return client

    def test_required_resource_access_falls_back_to_direct_lookup_and_caches(self) -> None:
        graph_sp = {
            "id": "sp-graph",
            "displayName": "Graph",
            "oauth2PermissionScopes": [{"id": "P1", "value": "User.Read", "adminConsentDisplayName": "Sign in"}],
        }
        client = self._graph(objects={"servicePrincipals(appId=": (graph_sp, None)})
        cache: dict = {}
        app = {"requiredResourceAccess": [{"resourceAppId": "app1", "resourceAccess": [{"id": "P1", "type": "Scope"}, {"id": "P9", "type": "Scope"}]}]}

        resolved, unresolved_resources, unresolved_permissions, errors = self.module.resolve_required_resource_access(app, client, cache)

        self.assertEqual(len(client.object_urls), 2)  # filter query, then direct key lookup
        self.assertEqual(cache["app1"], graph_sp)
        self.assertEqual(resolved[0]["resourceDisplayName"], "Graph")
        self.assertEqual(resolved[0]["permissions"][0]["value"], "User.Read")
        self.assertEqual((unresolved_resources, unresolved_permissions, errors), (0, 1, []))

        self.module.resolve_required_resource_access(app, client, cache)
        self.assertEqual(len(client.object_urls), 2)  # second call served from the cache

    def test_required_resource_access_unresolvable_resource_is_reported(self) -> None:
        client = self._graph(objects={"servicePrincipals": (None, "HTTP 403")})
        app = {"requiredResourceAccess": [{"resourceAppId": "app1", "resourceAccess": [{"id": "P1", "type": "Role"}]}]}
        resolved, unresolved_resources, unresolved_permissions, errors = self.module.resolve_required_resource_access(app, client, {})
        self.assertEqual(resolved[0]["resourceDisplayName"], "Unresolved")
        self.assertEqual(resolved[0]["permissions"][0]["value"], "")
        self.assertEqual((unresolved_resources, unresolved_permissions), (1, 1))
        self.assertTrue(errors and "app1" in errors[0])

    def test_role_assignments_match_role_id_case_insensitively(self) -> None:
        import threading

        resource = {"id": "res1", "displayName": "Graph", "appRoles": [{"id": "ABC", "value": "Mail.Read", "displayName": "Read mail"}]}
        url = "https://graph.microsoft.com/v1.0/servicePrincipals/sp1/appRoleAssignments?$select=id,resourceId,appRoleId,principalType"
        assignments = [{"resourceId": "res1", "appRoleId": "abc", "principalType": "ServicePrincipal"}, {"resourceId": "res1", "appRoleId": "zzz"}, {"appRoleId": "no-resource"}]
        for lock in (None, threading.Lock()):
            with self.subTest(lock=lock is not None):
                client = self._graph(objects={"servicePrincipals/res1": (resource, None)}, collections={url: (assignments, None)})
                cache: dict = {}
                resolved, error, unresolved_resources, unresolved_roles, _ = self.module.resolve_enterprise_app_role_assignments(
                    {"id": "sp1"}, client, cache, lock
                )
                self.assertEqual([r["appRoleValue"] for r in resolved], ["Mail.Read", ""])
                self.assertEqual((error, unresolved_resources, unresolved_roles), (None, 0, 1))
                self.assertEqual(client.object_urls.count("https://graph.microsoft.com/v1.0/servicePrincipals/res1?$select=id,appId,displayName,appRoles"), 1)  # fetched once

    def test_role_assignments_without_service_principal_id(self) -> None:
        self.assertEqual(
            self.module.resolve_enterprise_app_role_assignments({}, self._graph(), {}),
            ([], "Missing service principal id", 0, 0, []),
        )

    def test_normalize_branch_name_ignores_unresolved_macro(self) -> None:
        self.assertEqual(self.module.common.normalize_branch("$(DRIFT_BRANCH_ENTRA)"), "")

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
