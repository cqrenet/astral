#!/usr/bin/env python3
"""Core tools for querying ASTRAL tenant state via Azure DevOps REST API or local Git repo."""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
from typing import Any

import re

try:
    from scripts.common import request_json, run_git
except ImportError:
    from common import request_json, run_git  # type: ignore[no-redef]


def _slugify(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


_WORKLOAD_CATEGORIES: dict[str, list[str]] = {
    "intune": [
        "Compliance Policies",
        "Device Configurations",
        "Settings Catalog",
        "Applications",
        "Scripts",
        "Filters",
        "Enrollment Profiles",
        "Enrollment Configurations",
        "App Protection",
        "App Configuration",
        "Scope Tags",
        "Apple VPP Tokens",
        "Apple Push Notification",
        "Driver Updates",
        "Device Management Settings",
    ],
    "entra": [
        "Conditional Access",
        "Named Locations",
        "Authentication Strengths",
        "App Registrations",
        "Enterprise Applications",
        # Phase 1 — identity exports
        "Groups",
        "Role Assignments",
        "Authentication Methods",
        "Cross-Tenant Access",
        "Identity Protection",
        # v2
        "PIM Role Policies",
        "Security",
    ],
}


def _make_basic_auth_header(token: str) -> str:
    encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


class AstralMcpClient:
    """Client for reading ASTRAL tenant state from Azure DevOps Git or a local clone."""

    def __init__(
        self,
        organization: str | None = None,
        project: str | None = None,
        token: str | None = None,
        repo_name: str | None = None,
        branch: str = "main",
        local_root: str | None = None,
    ) -> None:
        self.organization = (organization or "").strip()
        self.project = (project or "").strip()
        self.token = (token or "").strip()
        self.repo_name = (repo_name or "").strip() or None
        self.branch = branch
        self.local_root = local_root
        self._auth_header = _make_basic_auth_header(self.token) if self.token else ""
        self._repo_id: str | None = None
        self._repo_name_resolved: str | None = None

    # ------------------------------------------------------------------
    # Local filesystem helpers
    # ------------------------------------------------------------------
    def _local_path(self, *parts: str) -> str:
        if not self.local_root:
            raise RuntimeError("local_root is not configured")
        return os.path.join(self.local_root, *parts)

    def _read_local_file(self, path: str) -> str:
        full = self._local_path(path)
        with open(full, encoding="utf-8") as f:
            return f.read()

    def _list_local_dir(self, path: str) -> list[dict[str, Any]]:
        full = self._local_path(path)
        entries: list[dict[str, Any]] = []
        if not os.path.isdir(full):
            return entries
        for name in sorted(os.listdir(full)):
            child = os.path.join(full, name)
            entries.append(
                {
                    "path": f"{path}/{name}",
                    "name": name,
                    "isFolder": os.path.isdir(child),
                }
            )
        return entries

    def _list_local_json_recursive(self, path: str) -> list[dict[str, Any]]:
        full = self._local_path(path)
        policies: list[dict[str, Any]] = []
        if not os.path.isdir(full):
            return policies
        for root, _dirs, files in os.walk(full):
            rel_root = os.path.relpath(root, self.local_root)
            for name in files:
                if name.endswith(".json"):
                    policies.append(
                        {
                            "path": f"{rel_root}/{name}".replace("\\", "/"),
                            "name": name[:-5],
                        }
                    )
        return policies

    def _find_local_json(self, path: str, name: str) -> str | None:
        full = self._local_path(path)
        if not os.path.isdir(full):
            return None
        for root, _dirs, files in os.walk(full):
            if f"{name}.json" in files:
                return os.path.join(root, f"{name}.json")
        return None

    def _local_file_commits(self, path: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.local_root:
            raise RuntimeError("local_root is not configured")
        try:
            log = run_git(
                self.local_root,
                [
                    "log",
                    f"-n{limit}",
                    f"--format=%H|%an|%ae|%ad|%s",
                    "--date=iso-strict",
                    "--",
                    path,
                ],
            )
        except RuntimeError:
            return []
        commits: list[dict[str, Any]] = []
        for line in log.splitlines():
            parts = line.split("|", 4)
            if len(parts) == 5:
                commits.append(
                    {
                        "commitId": parts[0],
                        "author": {"name": parts[1], "email": parts[2]},
                        "committer": {"date": parts[3]},
                        "comment": parts[4],
                    }
                )
        return commits

    def _local_recent_commits(self, path: str, since: str, limit: int = 50) -> list[dict[str, Any]]:
        if not self.local_root:
            raise RuntimeError("local_root is not configured")
        try:
            log = run_git(
                self.local_root,
                [
                    "log",
                    f"-n{limit}",
                    f"--since={since}",
                    f"--format=%H|%an|%ae|%ad|%s",
                    "--date=iso-strict",
                    "--",
                    path,
                ],
            )
        except RuntimeError:
            return []
        commits: list[dict[str, Any]] = []
        for line in log.splitlines():
            parts = line.split("|", 4)
            if len(parts) == 5:
                commits.append(
                    {
                        "commitId": parts[0],
                        "author": {"name": parts[1], "email": parts[2]},
                        "committer": {"date": parts[3]},
                        "comment": parts[4],
                    }
                )
        return commits

    # ------------------------------------------------------------------
    # Azure DevOps REST API helpers
    # ------------------------------------------------------------------
    def _discover_repo(self) -> str:
        if self._repo_id:
            return self._repo_id
        if not self.organization or not self.project:
            raise RuntimeError("ADO organization and project are required for remote access")
        url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories?api-version=7.1"
        result = request_json(url, headers={"Authorization": self._auth_header}, timeout=30)
        items = result.get("value", [])
        if not items:
            raise RuntimeError(f"No Git repositories found in project {self.project}")
        if self.repo_name:
            for repo in items:
                if repo.get("name") == self.repo_name:
                    self._repo_id = repo["id"]
                    self._repo_name_resolved = repo.get("name")
                    return self._repo_id
            raise RuntimeError(f"Repository '{self.repo_name}' not found in project {self.project}")
        self._repo_id = items[0]["id"]
        self._repo_name_resolved = items[0].get("name")
        return self._repo_id

    def _ado_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        self._discover_repo()
        base = (
            f"https://dev.azure.com/{self.organization}/{self.project}"
            f"/_apis/git/repositories/{self._repo_id}/{path}"
        )
        query: dict[str, str] = {"api-version": "7.1"}
        if params:
            query.update(params)
        qs = urllib.parse.urlencode(query)
        url = f"{base}?{qs}"
        return request_json(url, headers={"Authorization": self._auth_header}, timeout=30)

    def _read_remote_file(self, path: str) -> str:
        result = self._ado_get(
            "items",
            {
                "path": path,
                "versionDescriptor.versionType": "Branch",
                "versionDescriptor.version": self.branch,
                "includeContent": "true",
            },
        )
        content = result.get("content", "")
        if not content:
            return ""
        encoding = result.get("contentMetadata", {}).get("encoding", 0)
        if encoding == 1200 or encoding == 1201:  # UTF-16 LE/BE
            return base64.b64decode(content).decode("utf-16")
        # Default: UTF-8 (ADO returns base64 for binary, but often raw text for small text files)
        # In practice ADO returns base64 for most content. Try decoding.
        try:
            return base64.b64decode(content).decode("utf-8")
        except Exception:
            return content

    def _list_remote_dir(self, path: str) -> list[dict[str, Any]]:
        result = self._ado_get(
            "items",
            {
                "scopePath": path,
                "recursionLevel": "OneLevel",
                "versionDescriptor.versionType": "Branch",
                "versionDescriptor.version": self.branch,
            },
        )
        items = result.get("value", [])
        return [
            {
                "path": item.get("path", ""),
                "name": os.path.basename(item.get("path", "")),
                "isFolder": item.get("isFolder", False),
            }
            for item in items
            if item.get("path", "").rstrip("/") != path.rstrip("/")
        ]

    def _list_remote_json_recursive(self, path: str) -> list[dict[str, Any]]:
        result = self._ado_get(
            "items",
            {
                "scopePath": path,
                "recursionLevel": "Full",
                "versionDescriptor.versionType": "Branch",
                "versionDescriptor.version": self.branch,
            },
        )
        items = result.get("value", [])
        policies: list[dict[str, Any]] = []
        for item in items:
            if item.get("isFolder"):
                continue
            item_path = item.get("path", "")
            if not item_path.endswith(".json"):
                continue
            policies.append(
                {
                    "path": item_path,
                    "name": os.path.basename(item_path)[:-5],
                }
            )
        return policies

    def _find_remote_json(self, path: str, name: str) -> str | None:
        policies = self._list_remote_json_recursive(path)
        for p in policies:
            if p["name"] == name:
                return p["path"]
        return None

    def _remote_file_commits(self, path: str, limit: int = 10) -> list[dict[str, Any]]:
        result = self._ado_get(
            "commits",
            {
                "searchCriteria.itemPath": path,
                "searchCriteria.itemVersion.versionType": "Branch",
                "searchCriteria.itemVersion.version": self.branch,
                "$top": str(limit),
            },
        )
        return result.get("value", [])

    def _remote_recent_commits(self, path: str, since: str, limit: int = 50) -> list[dict[str, Any]]:
        # ADO commits API doesn't support since directly; we filter by top and then client-side
        result = self._ado_get(
            "commits",
            {
                "searchCriteria.itemPath": path,
                "searchCriteria.itemVersion.versionType": "Branch",
                "searchCriteria.itemVersion.version": self.branch,
                "$top": str(limit),
            },
        )
        return result.get("value", [])

    # ------------------------------------------------------------------
    # Unified interface
    # ------------------------------------------------------------------
    def _read_file(self, path: str) -> str:
        if self.local_root:
            return self._read_local_file(path)
        return self._read_remote_file(path)

    def _list_dir(self, path: str) -> list[dict[str, Any]]:
        if self.local_root:
            return self._list_local_dir(path)
        return self._list_remote_dir(path)

    def _file_commits(self, path: str, limit: int = 10) -> list[dict[str, Any]]:
        if self.local_root:
            return self._local_file_commits(path, limit)
        return self._remote_file_commits(path, limit)

    def _recent_commits(self, path: str, since: str, limit: int = 50) -> list[dict[str, Any]]:
        if self.local_root:
            return self._local_recent_commits(path, since, limit)
        return self._remote_recent_commits(path, since, limit)

    # ------------------------------------------------------------------
    # Public tool-facing methods
    # ------------------------------------------------------------------
    def list_workloads(self) -> list[str]:
        return list(_WORKLOAD_CATEGORIES.keys())

    def list_categories(self, workload: str) -> list[str]:
        return list(_WORKLOAD_CATEGORIES.get(workload, []))

    def list_policies(self, workload: str, category: str) -> list[dict[str, Any]]:
        path = f"tenant-state/{workload}/{category}"
        if self.local_root:
            return self._list_local_json_recursive(path)
        return self._list_remote_json_recursive(path)

    def get_policy(self, workload: str, category: str, name: str) -> dict[str, Any]:
        path = f"tenant-state/{workload}/{category}"
        if self.local_root:
            full_path = self._find_local_json(path, name)
            if not full_path:
                raise FileNotFoundError(f"Policy '{name}' not found in {path}")
            raw = self._read_local_file(os.path.relpath(full_path, self.local_root))
        else:
            remote_path = self._find_remote_json(path, name)
            if not remote_path:
                raise FileNotFoundError(f"Policy '{name}' not found in {path}")
            raw = self._read_remote_file(remote_path)
        if not raw:
            raise FileNotFoundError(f"Policy '{name}' is empty")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for policy '{name}': {exc}") from exc

    def get_policy_history(self, workload: str, category: str, name: str, limit: int = 10) -> list[dict[str, Any]]:
        path = f"tenant-state/{workload}/{category}/{name}.json"
        commits = self._file_commits(path, limit)
        return [
            {
                "commitId": c.get("commitId", ""),
                "author": c.get("author", {}),
                "committer": c.get("committer", {}),
                "comment": c.get("comment", ""),
                "url": c.get("remoteUrl", ""),
            }
            for c in commits
        ]

    def search_policies(self, workload: str, query: str) -> list[dict[str, Any]]:
        query_lower = query.lower()
        results: list[dict[str, Any]] = []
        categories = self.list_categories(workload)
        for category in categories:
            policies = self.list_policies(workload, category)
            for policy in policies:
                if query_lower in policy["name"].lower():
                    results.append({"category": category, **policy})
        return results

    def get_recent_drift(self, workload: str, hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
        path = f"tenant-state/{workload}"
        since = f"{hours} hours ago"
        commits = self._recent_commits(path, since, limit)
        return [
            {
                "commitId": c.get("commitId", ""),
                "author": c.get("author", {}),
                "committer": c.get("committer", {}),
                "comment": c.get("comment", ""),
                "url": c.get("remoteUrl", ""),
            }
            for c in commits
        ]

    def get_assignment_report(self, workload: str = "intune") -> str:
        path = f"tenant-state/reports/{workload}/policy-assignments.md"
        try:
            return self._read_file(path)
        except Exception:
            return ""

    def get_object_inventory(self, workload: str = "intune", category: str = "") -> list[dict[str, Any]]:
        if category:
            slug = _slugify(category)
            path = f"tenant-state/reports/{workload}/Object Inventory/{slug}-inventory.csv"
        else:
            path = f"tenant-state/reports/{workload}/object-inventory-all.csv"
        try:
            raw = self._read_file(path)
        except Exception:
            return []
        lines = raw.strip().splitlines()
        if not lines:
            return []
        import csv
        reader = csv.DictReader(lines)
        return list(reader)

    def list_report_definitions(self, workload: str | None = None) -> list[dict[str, Any]]:
        """Return merged report/documentation definitions (builtin + local)."""
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            return []

        def _safe_load(path: str) -> dict[str, Any]:
            try:
                return yaml.safe_load(self._read_file(path)) or {}
            except Exception:
                return {}

        base = _safe_load("reports/definitions.yml")
        local = _safe_load("reports/definitions.local.yml")

        results: list[dict[str, Any]] = []
        for section, entry_type in (("reports", "report"), ("documentation", "documentation")):
            combined: dict[str, Any] = {}
            for entry in base.get(section, []):
                if "name" in entry:
                    combined[entry["name"]] = {**entry, "source": "builtin"}
            for entry in local.get(section, []):
                if "name" in entry:
                    combined[entry["name"]] = {**entry, "source": "local"}
            for defn in combined.values():
                if workload and workload not in defn.get("workloads", []):
                    continue
                results.append({"type": entry_type, **defn})
        return results

    def list_generated_reports(self, workload: str) -> list[dict[str, Any]]:
        """List files present in tenant-state/reports/{workload}/."""
        path = f"tenant-state/reports/{workload}"
        try:
            entries = self._list_dir(path)
        except Exception:
            return []
        return [e for e in entries if not e.get("isFolder")]

    def get_report(self, workload: str, name: str) -> str:
        """Retrieve a report by its definition name (reads its primary output file)."""
        defns = self.list_report_definitions(workload)
        defn = next((d for d in defns if d.get("name") == name), None)
        if not defn:
            return ""
        outputs = defn.get("outputs", [])
        if not outputs:
            return ""
        path = f"tenant-state/reports/{workload}/{outputs[0]}"
        try:
            return self._read_file(path)
        except Exception:
            return ""

    def get_settings_report(
        self,
        workload: str = "intune",
        policy: str = "",
        category: str = "",
        os_filter: str = "",
        max_rows: int = 500,
    ) -> list[dict[str, Any]]:
        """Read policy-settings.csv and return rows, optionally filtered.

        Args:
            workload:   Workload name (default: intune).
            policy:     Case-insensitive substring filter on the Policy column.
            category:   Exact match on the Category column (e.g. "Settings Catalog").
            os_filter:  Case-insensitive substring filter on the OS column (e.g. "Win").
            max_rows:   Maximum rows to return (default 500).
        """
        import csv, io
        path = f"tenant-state/reports/{workload}/policy-settings.csv"
        try:
            raw = self._read_file(path)
        except Exception:
            return []
        reader = csv.DictReader(io.StringIO(raw))
        rows: list[dict[str, Any]] = []
        for row in reader:
            if policy and policy.lower() not in row.get("Policy", "").lower():
                continue
            if category and row.get("Category", "") != category:
                continue
            if os_filter and os_filter.lower() not in row.get("OS", "").lower():
                continue
            rows.append(dict(row))
            if len(rows) >= max_rows:
                break
        return rows

    def find_policies_for_setting(
        self,
        setting: str,
        workload: str = "intune",
    ) -> list[dict[str, Any]]:
        """Return all policies that contain a given setting (case-insensitive substring match).

        Matches against both the Setting display name and the Setting ID column.
        Returns deduplicated rows grouped by policy, showing the matched setting and value.
        """
        import csv, io
        path = f"tenant-state/reports/{workload}/policy-settings.csv"
        try:
            raw = self._read_file(path)
        except Exception:
            return []
        reader = csv.DictReader(io.StringIO(raw))
        needle = setting.lower()
        seen: dict[str, list[dict[str, Any]]] = {}
        for row in reader:
            if (
                needle in row.get("Setting", "").lower()
                or needle in row.get("Setting ID", "").lower()
            ):
                policy = row.get("Policy", "")
                if policy not in seen:
                    seen[policy] = []
                seen[policy].append({
                    "Setting": row.get("Setting", ""),
                    "Setting ID": row.get("Setting ID", ""),
                    "Value": row.get("Value", ""),
                    "Category": row.get("Category", ""),
                    "OS": row.get("OS", ""),
                })
        return [
            {"Policy": p, "Matches": matches}
            for p, matches in sorted(seen.items())
        ]


    def get_ca_groups(self) -> list[dict[str, Any]]:
        """Return all CA-referenced groups with member counts.

        Reads from tenant-state/entra/Groups/ — populated by export_entra_identity.py.
        Each entry includes displayName, memberCount, and which CA policies reference the group.
        """
        path = "tenant-state/entra/Groups"
        if self.local_root:
            policies = self._list_local_json_recursive(path)
        else:
            policies = self._list_remote_json_recursive(path)
        groups: list[dict[str, Any]] = []
        for item in policies:
            try:
                if self.local_root:
                    raw = self._read_local_file(item["path"])
                else:
                    raw = self._read_remote_file(item["path"])
                data = json.loads(raw)
                if isinstance(data, dict):
                    groups.append(data)
            except Exception:  # noqa: BLE001
                continue
        return sorted(groups, key=lambda g: str(g.get("displayName") or "").casefold())

    def get_pim_policies(self, role_name: str = "") -> list[dict[str, Any]]:
        """Return PIM governance policies optionally filtered by role name.

        Reads from tenant-state/entra/PIM Role Policies/ — populated by export_entra_identity.py.
        Each entry includes the role name, policy ID, and a rules dict keyed by rule type
        (expirationRule, approvalRule, authenticationContextRule, etc.).

        Args:
            role_name: Optional case-insensitive substring to filter by role display name.
        """
        path = "tenant-state/entra/PIM Role Policies"
        if self.local_root:
            policies = self._list_local_json_recursive(path)
        else:
            policies = self._list_remote_json_recursive(path)
        results: list[dict[str, Any]] = []
        name_filter = (role_name or "").lower()
        for item in policies:
            try:
                raw = self._read_local_file(item["path"]) if self.local_root else self._read_remote_file(item["path"])
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                if name_filter and name_filter not in str(data.get("roleDisplayName") or "").lower():
                    continue
                results.append(data)
            except Exception:  # noqa: BLE001
                continue
        return sorted(results, key=lambda r: str(r.get("roleDisplayName") or "").casefold())

    def get_security_settings(self) -> dict[str, Any]:
        """Return security defaults and authorization policy from tenant-state/entra/Security/."""
        result: dict[str, Any] = {}
        for filename, key in (
            ("Security Defaults.json", "securityDefaults"),
            ("Authorization Policy.json", "authorizationPolicy"),
        ):
            path = f"tenant-state/entra/Security/{filename}"
            try:
                raw = self._read_local_file(path) if self.local_root else self._read_remote_file(path)
                result[key] = json.loads(raw)
            except Exception:  # noqa: BLE001
                pass
        return result

    def get_privileged_role_assignments(self, role_name: str = "") -> list[dict[str, Any]]:
        """Return directory role assignments (permanent + PIM-eligible) optionally filtered by role name.

        Reads from tenant-state/entra/Role Assignments/ — populated by export_entra_identity.py.
        Each entry includes the role definition, a list of permanent assignments, and PIM-eligible assignments.

        Args:
            role_name: Optional case-insensitive substring to filter by role display name.
        """
        path = "tenant-state/entra/Role Assignments"
        if self.local_root:
            policies = self._list_local_json_recursive(path)
        else:
            policies = self._list_remote_json_recursive(path)
        roles: list[dict[str, Any]] = []
        name_filter = (role_name or "").lower()
        for item in policies:
            try:
                if self.local_root:
                    raw = self._read_local_file(item["path"])
                else:
                    raw = self._read_remote_file(item["path"])
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                if name_filter and name_filter not in str(data.get("displayName") or "").lower():
                    continue
                roles.append(data)
            except Exception:  # noqa: BLE001
                continue
        return sorted(roles, key=lambda r: str(r.get("displayName") or "").casefold())


def client_from_env() -> AstralMcpClient:
    """Create an AstralMcpClient from environment variables."""
    local_root = os.environ.get("ASTRAL_REPO_ROOT", "").strip()
    if local_root:
        return AstralMcpClient(local_root=local_root)
    return AstralMcpClient(
        organization=os.environ.get("ADO_ORGANIZATION", ""),
        project=os.environ.get("ADO_PROJECT", ""),
        token=os.environ.get("ADO_TOKEN", ""),
        repo_name=os.environ.get("ADO_REPO_NAME", ""),
        branch=os.environ.get("ADO_BRANCH", "main"),
    )
