#!/usr/bin/env python3
"""Core tools for querying ASTRAL tenant state.

Sources: a local clone (ASTRAL_REPO_ROOT), a clone kept in sync from any Git forge
(ASTRAL_REPO_URL: Gitea, GitHub, Azure DevOps...), or the Azure DevOps REST API (ADO_*).
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from typing import Any

try:
    from scripts.common import request_json, run_git
except ImportError:
    from common import request_json, run_git  # type: ignore[no-redef]

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
    ],
}


def _validate(workload: str, category: str | None = None, name: str | None = None) -> None:
    """Allowlist tool inputs before they become repo paths (blocks ../ traversal)."""
    if workload not in _WORKLOAD_CATEGORIES:
        raise ValueError(f"Unknown workload '{workload}'. Use one of: {', '.join(_WORKLOAD_CATEGORIES)}")
    if category is not None and category not in _WORKLOAD_CATEGORIES[workload]:
        raise ValueError(f"Unknown category '{category}' for workload '{workload}'.")
    if name is not None and (name in {"", ".", ".."} or "/" in name or "\\" in name):
        raise ValueError(f"Invalid policy name '{name}'.")


def _make_basic_auth_header(token: str) -> str:
    encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def _parse_git_log(log: str) -> list[dict[str, Any]]:
    """Parse `git log --format=%H|%an|%ae|%ad|%s` into the Azure DevOps commit shape."""
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


class _LocalSource:
    """Reads tenant state from a local Git clone."""

    def __init__(self, root: str) -> None:
        self.root = root

    def _path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def read_file(self, path: str) -> str:
        with open(self._path(path), encoding="utf-8") as f:
            return f.read()

    def list_json_recursive(self, path: str) -> list[dict[str, Any]]:
        full = self._path(path)
        policies: list[dict[str, Any]] = []
        if not os.path.isdir(full):
            return policies
        for root, _dirs, files in os.walk(full):
            rel_root = os.path.relpath(root, self.root)
            for name in files:
                if name.endswith(".json"):
                    policies.append({"path": f"{rel_root}/{name}".replace("\\", "/"), "name": name[:-5]})
        return policies

    def find_json(self, path: str, name: str) -> str | None:
        """Repo-relative path of `<name>.json` somewhere under *path*, or None."""
        for policy in self.list_json_recursive(path):
            if policy["name"] == name:
                return policy["path"]
        return None

    def _log(self, path: str, limit: int, extra: list[str]) -> list[dict[str, Any]]:
        try:
            log = run_git(
                self.root,
                ["log", f"-n{limit}", *extra, "--format=%H|%an|%ae|%ad|%s", "--date=iso-strict", "--", path],
            )
        except RuntimeError:
            return []
        return _parse_git_log(log)

    def file_commits(self, path: str, limit: int) -> list[dict[str, Any]]:
        return self._log(path, limit, [])

    def recent_commits(self, path: str, since: dt.datetime, limit: int) -> list[dict[str, Any]]:
        return self._log(path, limit, [f"--since={since.isoformat()}"])


class _AdoSource:
    """Reads tenant state through the Azure DevOps Git REST API."""

    def __init__(self, organization: str, project: str, token: str, repo_name: str | None, branch: str) -> None:
        self.organization = organization
        self.project = project
        self.repo_name = repo_name
        self.branch = branch
        self._auth_header = _make_basic_auth_header(token) if token else ""
        self._repo_id: str | None = None

    def _discover_repo(self) -> str:
        if self._repo_id:
            return self._repo_id
        if not self.organization or not self.project:
            raise RuntimeError("ADO organization and project are required for remote access")
        url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories?api-version=7.1"
        items = request_json(url, headers={"Authorization": self._auth_header}, timeout=30).get("value", [])
        if not items:
            raise RuntimeError(f"No Git repositories found in project {self.project}")
        if self.repo_name:
            repo = next((r for r in items if r.get("name") == self.repo_name), None)
            if repo is None:
                raise RuntimeError(f"Repository '{self.repo_name}' not found in project {self.project}")
        else:
            repo = items[0]
        self._repo_id = repo["id"]
        return self._repo_id

    def _get(self, path: str, params: dict[str, str]) -> Any:
        repo_id = self._discover_repo()
        query = urllib.parse.urlencode({"api-version": "7.1", **params})
        url = (
            f"https://dev.azure.com/{self.organization}/{self.project}"
            f"/_apis/git/repositories/{repo_id}/{path}?{query}"
        )
        return request_json(url, headers={"Authorization": self._auth_header}, timeout=30)

    def _items(self, path: str, recursion: str) -> list[dict[str, Any]]:
        result = self._get(
            "items",
            {
                "path": path,
                "recursionLevel": recursion,
                "versionDescriptor.versionType": "Branch",
                "versionDescriptor.version": self.branch,
            },
        )
        return result.get("value", [])

    def read_file(self, path: str) -> str:
        result = self._get(
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
        if encoding in (1200, 1201):  # UTF-16 LE/BE
            return base64.b64decode(content).decode("utf-16")
        # ADO returns base64 for most content, raw text for some small text files.
        try:
            return base64.b64decode(content).decode("utf-8")
        except Exception:
            return content

    def list_json_recursive(self, path: str) -> list[dict[str, Any]]:
        return [
            {"path": item["path"], "name": os.path.basename(item["path"])[:-5]}
            for item in self._items(path, "Full")
            if not item.get("isFolder") and item.get("path", "").endswith(".json")
        ]

    def find_json(self, path: str, name: str) -> str | None:
        return next((p["path"] for p in self.list_json_recursive(path) if p["name"] == name), None)

    def _commits(self, path: str, limit: int, extra: dict[str, str]) -> list[dict[str, Any]]:
        result = self._get(
            "commits",
            {
                "searchCriteria.itemPath": path,
                "searchCriteria.itemVersion.versionType": "Branch",
                "searchCriteria.itemVersion.version": self.branch,
                "$top": str(limit),
                **extra,
            },
        )
        return result.get("value", [])

    def file_commits(self, path: str, limit: int) -> list[dict[str, Any]]:
        return self._commits(path, limit, {})

    def recent_commits(self, path: str, since: dt.datetime, limit: int) -> list[dict[str, Any]]:
        return self._commits(path, limit, {"searchCriteria.fromDate": since.isoformat()})


def _commit_summary(commit: dict[str, Any]) -> dict[str, Any]:
    return {
        "commitId": commit.get("commitId", ""),
        "author": commit.get("author", {}),
        "committer": commit.get("committer", {}),
        "comment": commit.get("comment", ""),
        "url": commit.get("remoteUrl", ""),
    }


class AstralMcpClient:
    """Reads ASTRAL tenant state from a local clone (local_root) or Azure DevOps Git (REST)."""

    def __init__(
        self,
        organization: str | None = None,
        project: str | None = None,
        token: str | None = None,
        repo_name: str | None = None,
        branch: str = "main",
        local_root: str | None = None,
    ) -> None:
        self.local_root = local_root
        self.branch = branch
        self._source: _LocalSource | _AdoSource = (
            _LocalSource(local_root)
            if local_root
            else _AdoSource(
                (organization or "").strip(),
                (project or "").strip(),
                (token or "").strip(),
                (repo_name or "").strip() or None,
                branch,
            )
        )

    def list_workloads(self) -> list[str]:
        return list(_WORKLOAD_CATEGORIES.keys())

    def list_categories(self, workload: str) -> list[str]:
        return list(_WORKLOAD_CATEGORIES.get(workload, []))

    def list_policies(self, workload: str, category: str) -> list[dict[str, Any]]:
        _validate(workload, category)
        return self._source.list_json_recursive(f"tenant-state/{workload}/{category}")

    def get_policy(self, workload: str, category: str, name: str) -> dict[str, Any]:
        _validate(workload, category, name)
        path = f"tenant-state/{workload}/{category}"
        policy_path = self._source.find_json(path, name)
        if not policy_path:
            raise FileNotFoundError(f"Policy '{name}' not found in {path}")
        raw = self._source.read_file(policy_path)
        if not raw:
            raise FileNotFoundError(f"Policy '{name}' is empty")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for policy '{name}': {exc}") from exc

    def get_policy_history(self, workload: str, category: str, name: str, limit: int = 10) -> list[dict[str, Any]]:
        _validate(workload, category, name)
        limit = max(1, min(int(limit), 100))
        path = f"tenant-state/{workload}/{category}/{name}.json"
        return [_commit_summary(c) for c in self._source.file_commits(path, limit)]

    def search_policies(self, workload: str, query: str) -> list[dict[str, Any]]:
        _validate(workload)
        query_lower = query.lower()
        return [
            {"category": category, **policy}
            for category in self.list_categories(workload)
            for policy in self.list_policies(workload, category)
            if query_lower in policy["name"].lower()
        ]

    def get_recent_drift(self, workload: str, hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
        _validate(workload)
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=int(hours))
        commits = self._source.recent_commits(f"tenant-state/{workload}", since, limit)
        return [_commit_summary(c) for c in commits]

    def get_assignment_report(self, workload: str = "intune") -> str:
        _validate(workload)
        try:
            return self._source.read_file(f"tenant-state/reports/{workload}/policy-assignments.md")
        except Exception:
            return ""

    def get_object_inventory(self, workload: str = "intune", category: str = "") -> list[dict[str, Any]]:
        if not category:
            return []
        _validate(workload, category)
        slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")  # as generate_object_inventory_reports
        try:
            raw = self._source.read_file(f"tenant-state/reports/{workload}/Object Inventory/{slug}-inventory.csv")
        except Exception:
            return []
        lines = raw.strip().splitlines()
        return list(csv.DictReader(lines)) if lines else []


_clone_lock = threading.Lock()
_clone_synced_at = 0.0


def sync_remote_clone(force: bool = False) -> str | None:
    """Clone/refresh ASTRAL_REPO_URL into ASTRAL_REPO_CLONE_DIR; None when not configured.

    Env: ASTRAL_REPO_URL (https), ASTRAL_REPO_TOKEN (read-only token), ASTRAL_REPO_BRANCH (main),
    ASTRAL_REPO_CLONE_DIR (/tmp/astral-repo), ASTRAL_REPO_REFRESH_SECONDS (300).
    The token is passed per git command (http.extraheader), never stored in .git/config.
    """
    global _clone_synced_at
    url = os.environ.get("ASTRAL_REPO_URL", "").strip()
    if not url:
        return None
    if not url.startswith("https://") and not url.startswith("file://"):
        raise RuntimeError("ASTRAL_REPO_URL must be an https:// URL.")
    dest = os.environ.get("ASTRAL_REPO_CLONE_DIR", "/tmp/astral-repo")
    branch = os.environ.get("ASTRAL_REPO_BRANCH", "main").strip() or "main"
    interval = int(os.environ.get("ASTRAL_REPO_REFRESH_SECONDS", "300"))
    token = os.environ.get("ASTRAL_REPO_TOKEN", "").strip()
    auth: list[str] = []
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    with _clone_lock:
        have_clone = os.path.isdir(os.path.join(dest, ".git"))
        if have_clone and not force and time.monotonic() - _clone_synced_at < interval:
            return dest
        if not have_clone:
            subprocess.run(["git", *auth, "clone", "--quiet", "--single-branch", "--branch", branch, "--", url, dest],
                           check=True, capture_output=True)
        else:
            subprocess.run(["git", *auth, "-C", dest, "fetch", "--quiet", "origin", branch], check=True, capture_output=True)
            subprocess.run(["git", "-C", dest, "reset", "--quiet", "--hard", "FETCH_HEAD"], check=True, capture_output=True)
        _clone_synced_at = time.monotonic()
    return dest


def refresh_remote_clone() -> None:
    """Best-effort refresh before a tool call; on failure keep serving the last synced snapshot."""
    try:
        sync_remote_clone()
    except Exception as exc:  # network/forge outage must not take the MCP server down
        logging.getLogger(__name__).warning("Repository refresh failed; serving last snapshot (%s)", exc)


def client_from_env() -> AstralMcpClient:
    """Create an AstralMcpClient from environment variables."""
    local_root = os.environ.get("ASTRAL_REPO_ROOT", "").strip()
    if local_root:
        return AstralMcpClient(local_root=local_root)
    clone_root = sync_remote_clone(force=True)
    if clone_root:
        return AstralMcpClient(local_root=clone_root)
    return AstralMcpClient(
        organization=os.environ.get("ADO_ORGANIZATION", ""),
        project=os.environ.get("ADO_PROJECT", ""),
        token=os.environ.get("ADO_TOKEN", ""),
        repo_name=os.environ.get("ADO_REPO_NAME", ""),
        branch=os.environ.get("ADO_BRANCH", "main"),
    )
