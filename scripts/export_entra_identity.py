#!/usr/bin/env python3
"""Export Entra identity objects for ASTRAL Phase 1 and v2.

Phase 1:
  1.1  Security groups referenced in CA policies — member counts, ownership, name-change detection
  1.2  Directory role assignments — permanent and PIM-eligible, with AU scope resolution
  1.3  Authentication methods policy
  1.4  Cross-tenant access settings and external collaboration policy
  1.5  Identity Protection risk policies

v2:
  2.1  Role-assignable groups — auto-watched with full membership and owners
  2.2  PIM role governance policies — per-role activation, approval, MFA settings
  2.3  Security settings — security defaults, authorization policy
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Path to Entra workload backup root (tenant-state/entra).")
    parser.add_argument("--token", required=True, help="Microsoft Graph bearer token.")
    parser.add_argument(
        "--include-groups",
        default="true",
        help="Export CA-referenced security groups with member counts (true/false).",
    )
    parser.add_argument(
        "--include-role-assignments",
        default="true",
        help="Export directory role assignments — permanent and PIM-eligible (true/false).",
    )
    parser.add_argument(
        "--include-auth-methods-policy",
        default="true",
        help="Export authentication methods policy (true/false).",
    )
    parser.add_argument(
        "--include-cross-tenant-access",
        default="true",
        help="Export cross-tenant access settings and external collaboration policy (true/false).",
    )
    parser.add_argument(
        "--include-identity-protection",
        default="true",
        help="Export Identity Protection risk policies (true/false).",
    )
    parser.add_argument(
        "--watch-groups-csv",
        default="",
        help=(
            "Comma-separated list of group IDs (GUIDs) for which full member lists should be exported. "
            "These groups are always exported even if not referenced in a CA policy. "
            "All other groups receive count-only tracking."
        ),
    )
    parser.add_argument(
        "--include-privileged-groups",
        default="true",
        help="Auto-watch all role-assignable groups (isAssignableToRole=true) with full member tracking (true/false).",
    )
    parser.add_argument(
        "--include-pim-policies",
        default="true",
        help="Export PIM role governance policies — activation duration, approval, MFA settings (true/false).",
    )
    parser.add_argument(
        "--include-security-settings",
        default="true",
        help="Export security defaults and authorization policy (true/false).",
    )
    parser.add_argument(
        "--reports-root",
        default="",
        help=(
            "Directory where markdown summary reports are written. "
            "Defaults to <root>/../reports/<root-name> (e.g. tenant-state/reports/entra). "
            "Pass explicitly to match the pipeline reports path."
        ),
    )
    parser.add_argument(
        "--previous-snapshot-ref",
        default="",
        help=(
            "Git branch or ref of the previous snapshot (e.g. origin/drift/entra). "
            "Used to detect group renames and compute member deltas for watched groups."
        ),
    )
    parser.add_argument(
        "--reports-only",
        default="false",
        help=(
            "Skip all Graph API calls. Read committed JSON snapshots from --root and write "
            "markdown reports to --reports-root. Token is not required in this mode."
        ),
    )
    parser.add_argument(
        "--fail-on-export-error",
        default="true",
        help="Fail with non-zero exit code when any requested export category fails (true/false).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    print(message, flush=True)


def to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] if len(cleaned) > 180 else cleaned


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=5, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Git helpers (for previous-snapshot lookups)
# ---------------------------------------------------------------------------

def _git(repo_root: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )


def _discover_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    proc = _git(start, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None
    top = (proc.stdout or "").strip()
    return pathlib.Path(top).resolve() if top else None


def _resolve_branch_ref(repo_root: pathlib.Path, raw: str) -> str:
    """Return the first resolvable ref from *raw* (strips origin/ prefixes, tries remote then local)."""
    branch = (raw or "").strip()
    if not branch or (branch.startswith("$(") and branch.endswith(")")):
        return ""
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        if _git(repo_root, ["show-ref", "--verify", "--quiet", ref]).returncode == 0:
            return f"origin/{branch}" if "remotes" in ref else branch
    return ""


class PreviousGroupLookup:
    """Read group snapshots from a previous git ref for rename / member-delta detection."""

    def __init__(self, repo_root: pathlib.Path, ref: str, groups_dir_rel: str) -> None:
        self._repo_root = repo_root
        self._ref = ref
        self._paths_by_id: dict[str, str] = {}
        self._cache: dict[str, dict[str, Any] | None] = {}
        if not ref or not groups_dir_rel:
            return
        proc = _git(repo_root, ["ls-tree", "-r", "--name-only", ref, "--", groups_dir_rel])
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            rel = line.strip()
            if not rel.endswith(".json"):
                continue
            stem = pathlib.PurePosixPath(rel).name[:-5]
            if "__" in stem:
                gid = stem.rsplit("__", 1)[-1].strip().lower()
                if gid:
                    self._paths_by_id[gid] = rel

    def get(self, group_id: str) -> dict[str, Any] | None:
        gid = group_id.strip().lower()
        if gid in self._cache:
            return self._cache[gid]
        rel = self._paths_by_id.get(gid)
        if not rel:
            self._cache[gid] = None
            return None
        try:
            content = _git(self._repo_root, ["show", f"{self._ref}:{rel}"]).stdout
            data = json.loads(content)
            self._cache[gid] = data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            self._cache[gid] = None
        return self._cache[gid]


# ---------------------------------------------------------------------------
# Token capability report
# ---------------------------------------------------------------------------

_CAPABILITY_MAP = [
    ({"Group.Read.All"}, "Groups snapshot (CA-referenced + watched)"),
    ({"RoleManagement.Read.Directory"}, "Permanent role assignments"),
    ({"RoleEligibilitySchedule.Read.Directory"}, "PIM-eligible role assignments"),
    ({"RoleManagementPolicy.Read.Directory"}, "PIM role governance policies"),
    ({"Policy.Read.All"}, "Auth methods, cross-tenant access, security settings"),
    ({"IdentityRiskyUser.Read.All", "IdentityRiskPolicy.Read.All"}, "Identity Protection risk policies"),
]


def _report_token_capabilities(token: str) -> None:
    """Decode the Graph JWT payload and log which export categories the token supports."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
        roles: set[str] = set(payload.get("roles") or [])
    except Exception:  # noqa: BLE001
        return

    log("Token capabilities:")
    for required, description in _CAPABILITY_MAP:
        icon = "✓" if (roles & required) else "○"
        log(f"  {icon}  {description}")
    log("")


# ---------------------------------------------------------------------------
# Graph client
# ---------------------------------------------------------------------------

class GraphClient:
    def __init__(self, token: str, max_retries: int = 4):
        self.token = token
        self.max_retries = max_retries

    def _get_retry_delay(self, exc: urllib.error.HTTPError, attempt: int) -> float:
        try:
            return max(0.0, float(exc.headers.get("Retry-After") or ""))
        except (ValueError, TypeError):
            return min(2 ** attempt, 10)

    def _request(
        self,
        url: str,
        method: str = "GET",
        body: dict | None = None,
        extra_headers: dict | None = None,
    ) -> Any:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(self._get_retry_delay(exc, attempt))
                    attempt += 1
                    continue
                raise
            except urllib.error.URLError:
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    attempt += 1
                    continue
                raise

    def get_collection(
        self,
        url: str,
        extra_headers: dict | None = None,
    ) -> tuple[list[dict], str | None]:
        items: list[dict] = []
        next_url: str | None = url
        while next_url:
            try:
                payload = self._request(next_url, extra_headers=extra_headers)
            except urllib.error.HTTPError as exc:
                return items, f"HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001
                return items, str(exc)
            if isinstance(payload, dict):
                value = payload.get("value")
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            items.append(item)
                next_link = payload.get("@odata.nextLink")
                next_url = next_link if isinstance(next_link, str) else None
            else:
                next_url = None
        return items, None

    def get_object(
        self,
        url: str,
        extra_headers: dict | None = None,
    ) -> tuple[dict | None, str | None]:
        try:
            payload = self._request(url, extra_headers=extra_headers)
            return (payload, None) if isinstance(payload, dict) else (None, "Unexpected non-object payload")
        except urllib.error.HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    def get_count(self, url: str) -> tuple[int | None, str | None]:
        """Fetch a plain integer (e.g. from a $count endpoint) using ConsistencyLevel: eventual."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/plain",
            "ConsistencyLevel": "eventual",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    text = resp.read().decode("utf-8").strip()
                    return int(text), None
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    attempt += 1
                    continue
                return None, f"HTTP {exc.code}"
            except (ValueError, TypeError):
                return None, "Unexpected non-integer response"
            except Exception as exc:  # noqa: BLE001
                return None, str(exc)

    def post_collection(
        self,
        url: str,
        body: dict,
    ) -> tuple[list[dict], str | None]:
        try:
            payload = self._request(url, method="POST", body=body)
        except urllib.error.HTTPError as exc:
            return [], f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)
        if isinstance(payload, dict):
            value = payload.get("value")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)], None
        return [], None


# ---------------------------------------------------------------------------
# 1.1  Group snapshots
# ---------------------------------------------------------------------------

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _parse_watch_groups_csv(value: str) -> set[str]:
    """Parse a comma-separated list of group IDs into a normalised set.

    Only accepts GUIDs — display names are not supported because they are
    unstable and require extra Graph API calls to resolve.
    """
    result: set[str] = set()
    for raw in value.split(","):
        gid = raw.strip()
        if not gid:
            continue
        if _GUID_RE.match(gid):
            result.add(gid.lower())
        else:
            log(f"Warning: watch-groups-csv entry '{gid}' is not a valid GUID and will be ignored.")
    return result

def _collect_ca_group_ids(ca_dir: pathlib.Path) -> dict[str, set[str]]:
    """Parse CA policy files and return {group_id: {policy_name, ...}} for all referenced groups."""
    refs: dict[str, set[str]] = {}
    if not ca_dir.is_dir():
        return refs
    for path in sorted(ca_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        policy_name = str(payload.get("displayName") or path.stem)
        conditions = payload.get("conditions")
        if not isinstance(conditions, dict):
            continue
        users = conditions.get("users")
        if not isinstance(users, dict):
            continue
        for key in ("includeGroups", "excludeGroups"):
            for gid in (users.get(key) or []):
                if isinstance(gid, str) and gid:
                    refs.setdefault(gid, set()).add(policy_name)
    return refs


def _fetch_group(client: GraphClient, group_id: str) -> tuple[dict | None, str | None]:
    url = (
        "https://graph.microsoft.com/v1.0/groups/"
        + urllib.parse.quote(group_id)
        + "?$select=id,displayName,description,groupTypes,securityEnabled,mailEnabled,mail,"
        "membershipRule,membershipRuleProcessingState,isAssignableToRole"
    )
    return client.get_object(url)


def _fetch_group_owners(client: GraphClient, group_id: str) -> tuple[list[dict[str, str]], str | None]:
    url = (
        "https://graph.microsoft.com/v1.0/groups/"
        + urllib.parse.quote(group_id)
        + "/owners?$select=id,displayName,userPrincipalName"
    )
    raw, error = client.get_collection(url)
    owners: list[dict[str, str]] = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        odata_type = str(o.get("@odata.type") or "").lower()
        owner_type = "user" if "user" in odata_type else ("servicePrincipal" if "serviceprincipal" in odata_type else "unknown")
        owners.append({
            "id": str(o.get("id") or ""),
            "displayName": str(o.get("displayName") or ""),
            "userPrincipalName": str(o.get("userPrincipalName") or ""),
            "type": owner_type,
        })
    owners.sort(key=lambda x: str(x.get("userPrincipalName") or x.get("displayName") or "").casefold())
    return owners, error


def _fetch_privileged_group_ids(client: GraphClient) -> set[str]:
    """Return IDs of all role-assignable groups (isAssignableToRole=true)."""
    groups, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/groups"
        "?$filter=isAssignableToRole%20eq%20true"
        "&$select=id,displayName"
    )
    if error:
        log(f"  Warning: could not fetch role-assignable groups ({error}) — privileged group auto-watch skipped.")
        return set()
    ids = {str(g.get("id") or "").strip().lower() for g in groups if isinstance(g, dict) and g.get("id")}
    if ids:
        log(f"  Found {len(ids)} role-assignable group(s) — auto-adding to watch list.")
    return ids


def _compute_member_delta(
    current: list[dict[str, str]],
    previous: list[dict[str, str]] | None,
) -> dict[str, list[dict[str, str]]] | None:
    """Return {added, removed} member sets compared to the previous snapshot, or None if no previous."""
    if previous is None:
        return None
    current_ids = {m["id"]: m for m in current if m.get("id")}
    previous_ids = {m["id"]: m for m in previous if m.get("id")}
    added = [current_ids[i] for i in current_ids if i not in previous_ids]
    removed = [previous_ids[i] for i in previous_ids if i not in current_ids]
    added.sort(key=lambda x: str(x.get("userPrincipalName") or x.get("displayName") or "").casefold())
    removed.sort(key=lambda x: str(x.get("userPrincipalName") or x.get("displayName") or "").casefold())
    return {"added": added, "removed": removed}


def _fetch_member_count(client: GraphClient, group_id: str) -> int | None:
    url = (
        "https://graph.microsoft.com/v1.0/groups/"
        + urllib.parse.quote(group_id)
        + "/members/$count"
    )
    count, error = client.get_count(url)
    if error:
        log(f"  Warning: could not fetch member count for group {group_id}: {error}")
    return count


def _fetch_group_members(client: GraphClient, group_id: str) -> tuple[list[dict[str, str]], str | None]:
    """Fetch the full member list for a watched group."""
    url = (
        "https://graph.microsoft.com/v1.0/groups/"
        + urllib.parse.quote(group_id)
        + "/members?$select=id,displayName,userPrincipalName"
    )
    raw_members, error = client.get_collection(url)
    members: list[dict[str, str]] = []
    for m in raw_members:
        if not isinstance(m, dict):
            continue
        odata_type = str(m.get("@odata.type") or "").lower()
        if "user" in odata_type:
            member_type = "user"
        elif "group" in odata_type:
            member_type = "group"
        elif "serviceprincipal" in odata_type:
            member_type = "servicePrincipal"
        else:
            member_type = "unknown"
        members.append({
            "id": str(m.get("id") or ""),
            "displayName": str(m.get("displayName") or ""),
            "userPrincipalName": str(m.get("userPrincipalName") or ""),
            "type": member_type,
        })
    members.sort(key=lambda x: (x["type"], str(x.get("userPrincipalName") or x.get("displayName") or "").casefold()))
    return members, error


def export_groups(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
    watch_groups: set[str] | None = None,
    privileged_group_ids: set[str] | None = None,
    previous_lookup: PreviousGroupLookup | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export security groups to tenant-state/entra/Groups/.

    All CA-referenced groups are exported with member counts, ownership, and
    dynamic membership rules.  Groups that are watched or role-assignable receive
    the full member list and member-delta summary.  All groups are checked for
    display-name changes against the previous snapshot.
    """
    watch_groups = watch_groups or set()
    privileged_group_ids = privileged_group_ids or set()

    # Effective watch set = explicit watch list ∪ privileged (role-assignable) groups
    effective_watch = watch_groups | privileged_group_ids

    ca_dir = root / "Conditional Access"
    refs = _collect_ca_group_ids(ca_dir)

    # Merge: CA-referenced ∪ all watched (with empty CA ref set for non-CA groups)
    for gid in effective_watch:
        if gid not in refs:
            refs[gid] = set()

    if not refs:
        log("No CA-referenced or watched groups found. Skipping groups export.")
        return 0, []

    ca_count = sum(1 for v in refs.values() if v)
    watch_only = len(effective_watch - {g for g, v in refs.items() if v})
    log(
        f"Exporting {len(refs)} group(s): {ca_count} CA-referenced, "
        f"{len(effective_watch)} watched ({watch_only} watch-only)."
    )

    out_dir = root / "Groups"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    failed: list[tuple[str, str]] = []

    for group_id, policy_names in sorted(refs.items()):
        group, error = _fetch_group(client, group_id)
        if error or not isinstance(group, dict):
            msg = error or "Empty response"
            log(f"  Warning: could not fetch group {group_id}: {msg}")
            failed.append((group_id, msg))
            continue

        is_watched = group_id.lower() in effective_watch
        is_privileged = group_id.lower() in privileged_group_ids
        display_name = str(group.get("displayName") or group_id).strip()

        # Name-change detection
        prev = previous_lookup.get(group_id) if previous_lookup else None
        previous_display_name: str | None = None
        if prev and isinstance(prev, dict):
            prev_name = str(prev.get("displayName") or "").strip()
            if prev_name and prev_name != display_name:
                previous_display_name = prev_name
                log(f"  Rename detected: '{prev_name}' → '{display_name}'")

        snapshot: dict[str, Any] = {
            "id": group_id,
            "displayName": display_name,
            "description": group.get("description"),
            "groupTypes": group.get("groupTypes") or [],
            "securityEnabled": group.get("securityEnabled"),
            "mailEnabled": group.get("mailEnabled"),
            "mail": group.get("mail"),
            "membershipRule": group.get("membershipRule"),
            "membershipRuleProcessingState": group.get("membershipRuleProcessingState"),
            "isAssignableToRole": group.get("isAssignableToRole"),
            "watched": is_watched,
            "privileged": is_privileged,
            "caReferences": sorted(policy_names),
        }
        if previous_display_name is not None:
            snapshot["previousDisplayName"] = previous_display_name

        if is_watched:
            # Full member list + delta
            members, members_error = _fetch_group_members(client, group_id)
            if members_error:
                log(f"  Warning: could not fetch members for group {display_name}: {members_error}")
                failed.append((f"{group_id} (members)", members_error))
            snapshot["memberCount"] = len(members)
            snapshot["members"] = members

            prev_members = prev.get("members") if isinstance(prev, dict) else None
            delta = _compute_member_delta(members, prev_members)
            if delta is not None:
                snapshot["memberDelta"] = delta
                added_n, removed_n = len(delta["added"]), len(delta["removed"])
                if added_n or removed_n:
                    log(f"  Group (watched): {display_name} — {len(members)} member(s) (+{added_n} −{removed_n})")
                else:
                    log(f"  Group (watched): {display_name} — {len(members)} member(s) (no change)")
            else:
                log(f"  Group (watched): {display_name} — {len(members)} member(s) (first snapshot)")

            # Owners for watched/privileged groups
            owners, owners_error = _fetch_group_owners(client, group_id)
            if owners_error:
                log(f"  Warning: could not fetch owners for group {display_name}: {owners_error}")
            snapshot["owners"] = owners
        else:
            member_count = _fetch_member_count(client, group_id)
            snapshot["memberCount"] = member_count
            log(f"  Group: {display_name} — {member_count} member(s)")

        file_name = f"{sanitize_filename(display_name)}__{group_id}.json"
        write_json(out_dir / file_name, snapshot)
        written += 1

    if reports_root is not None:
        _write_groups_report(out_dir, reports_root)

    return written, failed


# ---------------------------------------------------------------------------
# 1.2  Role assignments
# ---------------------------------------------------------------------------

_PRINCIPAL_TYPE_ORDER = {"user": 0, "group": 1, "serviceprincipal": 2}


def _batch_resolve_principals(
    client: GraphClient,
    principal_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Resolve principal display names/UPNs via directoryObjects/getByIds (batched, max 1000 per call)."""
    result: dict[str, dict[str, str]] = {}
    chunk_size = 1000
    for i in range(0, len(principal_ids), chunk_size):
        chunk = principal_ids[i : i + chunk_size]
        items, error = client.post_collection(
            "https://graph.microsoft.com/v1.0/directoryObjects/getByIds",
            body={"ids": chunk, "types": ["user", "group", "servicePrincipal"]},
        )
        if error:
            log(f"  Warning: batch principal resolution failed: {error}")
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            if not pid:
                continue
            odata_type = str(item.get("@odata.type") or "").lower()
            principal_type = "unknown"
            if "user" in odata_type:
                principal_type = "user"
            elif "group" in odata_type:
                principal_type = "group"
            elif "serviceprincipal" in odata_type:
                principal_type = "servicePrincipal"
            result[pid] = {
                "displayName": str(item.get("displayName") or ""),
                "userPrincipalName": str(item.get("userPrincipalName") or ""),
                "principalType": principal_type,
            }
    return result


def _batch_resolve_au_scopes(
    client: GraphClient,
    scope_ids: set[str],
) -> dict[str, str]:
    """Resolve administrative unit IDs to display names. Returns {au_id: displayName}."""
    au_ids = {s for s in scope_ids if s and s != "/" and "/administrativeUnits/" in s}
    resolved: dict[str, str] = {}
    for scope in au_ids:
        au_id = scope.rstrip("/").rsplit("/", 1)[-1]
        if not au_id:
            continue
        payload, error = client.get_object(
            "https://graph.microsoft.com/v1.0/directory/administrativeUnits/"
            + urllib.parse.quote(au_id)
            + "?$select=id,displayName"
        )
        if not error and isinstance(payload, dict):
            resolved[scope] = str(payload.get("displayName") or au_id)
        else:
            resolved[scope] = au_id
    return resolved


def _normalize_assignment(
    assignment: dict,
    principal_by_id: dict[str, dict[str, str]],
    assignment_type: str,
    scope_display: dict[str, str] | None = None,
) -> dict[str, Any]:
    principal_id = str(assignment.get("principalId") or "").strip()
    principal_info = principal_by_id.get(principal_id, {})
    scope_id = str(assignment.get("directoryScopeId") or "/")
    entry: dict[str, Any] = {
        "id": str(assignment.get("id") or ""),
        "principalId": principal_id,
        "principalDisplayName": principal_info.get("displayName", ""),
        "principalType": principal_info.get("principalType", ""),
        "userPrincipalName": principal_info.get("userPrincipalName", ""),
        "directoryScopeId": scope_id,
        "directoryScopeDisplayName": (scope_display or {}).get(scope_id, "/" if scope_id == "/" else scope_id),
        "assignmentType": assignment_type,
    }
    if assignment_type == "eligible":
        entry["startDateTime"] = assignment.get("startDateTime")
        entry["endDateTime"] = assignment.get("endDateTime")
    return entry


def export_role_assignments(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export directory role definitions with permanent and PIM-eligible assignments."""
    failed: list[tuple[str, str]] = []

    # Fetch all role definitions and filter enabled ones client-side.
    # Graph does not support server-side $filter on isEnabled for this endpoint.
    role_defs, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/roleManagement/directory/roleDefinitions"
        "?$select=id,displayName,description,isBuiltIn,isEnabled"
    )
    if error:
        log(f"  Warning: could not fetch role definitions: {error}")
        failed.append(("Role Definitions", error))
        if fail_on_error:
            return 0, failed
        role_defs = []

    role_defs = [r for r in role_defs if isinstance(r, dict) and r.get("isEnabled") is not False]
    log(f"Fetched {len(role_defs)} enabled role definition(s).")
    role_def_by_id = {
        str(r.get("id") or ""): r
        for r in role_defs
        if r.get("id")
    }

    # Fetch permanent (direct) assignments
    perm_assignments, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/roleManagement/directory/roleAssignments"
        "?$select=id,principalId,roleDefinitionId,directoryScopeId"
    )
    if error:
        log(f"  Warning: could not fetch permanent role assignments: {error}")
        failed.append(("Permanent Role Assignments", error))
        perm_assignments = []

    log(f"Fetched {len(perm_assignments)} permanent role assignment(s).")

    # Fetch PIM-eligible assignments.
    # HTTP 403 means the service principal lacks RoleEligibilitySchedule.Read.Directory —
    # this permission is not always granted; the export remains useful with permanent-only data.
    eligible_assignments, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/roleManagement/directory/roleEligibilityScheduleInstances"
        "?$select=id,principalId,roleDefinitionId,directoryScopeId,startDateTime,endDateTime"
    )
    if error:
        # PIM eligible assignments are always optional — tenants without PIM, without the
        # RoleEligibilitySchedule.Read.Directory permission, or without P2 licensing all
        # produce different error codes (403, 404, 400). Never treat as fatal.
        log(f"  Warning: PIM-eligible assignments skipped ({error}). Permanent-only data will be exported.")
        if error == "HTTP 403":
            log("    Tip: grant RoleEligibilitySchedule.Read.Directory to the service principal to include PIM data.")
        eligible_assignments = []

    log(f"Fetched {len(eligible_assignments)} PIM-eligible assignment(s).")

    # Collect all unique principal IDs for batch resolution
    all_principal_ids: list[str] = list({
        str(a.get("principalId") or "").strip()
        for a in (perm_assignments + eligible_assignments)
        if isinstance(a, dict) and a.get("principalId")
    })
    principal_by_id: dict[str, dict[str, str]] = {}
    if all_principal_ids:
        log(f"Resolving {len(all_principal_ids)} unique principal(s)...")
        principal_by_id = _batch_resolve_principals(client, all_principal_ids)

    # Resolve administrative unit scope names for non-tenant-wide assignments
    all_scope_ids = {
        str(a.get("directoryScopeId") or "/")
        for a in (perm_assignments + eligible_assignments)
        if isinstance(a, dict)
    }
    scope_display: dict[str, str] = {}
    au_scopes = {s for s in all_scope_ids if s != "/" and "/administrativeUnits/" in s}
    if au_scopes:
        log(f"Resolving {len(au_scopes)} administrative unit scope(s)...")
        scope_display = _batch_resolve_au_scopes(client, au_scopes)

    # Group assignments by role definition ID
    perm_by_role: dict[str, list[dict]] = {}
    for assignment in perm_assignments:
        if not isinstance(assignment, dict):
            continue
        rid = str(assignment.get("roleDefinitionId") or "").strip()
        if rid:
            perm_by_role.setdefault(rid, []).append(assignment)

    eligible_by_role: dict[str, list[dict]] = {}
    for assignment in eligible_assignments:
        if not isinstance(assignment, dict):
            continue
        rid = str(assignment.get("roleDefinitionId") or "").strip()
        if rid:
            eligible_by_role.setdefault(rid, []).append(assignment)

    # Write one file per role that has at least one assignment
    out_dir = root / "Role Assignments"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for role_id, role_def in sorted(role_def_by_id.items(), key=lambda kv: str(kv[1].get("displayName") or "").casefold()):
        perm = perm_by_role.get(role_id, [])
        eligible = eligible_by_role.get(role_id, [])
        if not perm and not eligible:
            continue

        display_name = str(role_def.get("displayName") or role_id).strip()

        def _sort_key(entry: dict) -> tuple:
            ptype = str(entry.get("principalType") or "").lower()
            order = _PRINCIPAL_TYPE_ORDER.get(ptype, 9)
            name = str(entry.get("principalDisplayName") or entry.get("userPrincipalName") or "").casefold()
            return (order, name)

        snapshot: dict[str, Any] = {
            "id": role_id,
            "displayName": display_name,
            "description": role_def.get("description"),
            "isBuiltIn": role_def.get("isBuiltIn"),
            "permanentAssignments": sorted(
                [_normalize_assignment(a, principal_by_id, "permanent", scope_display) for a in perm],
                key=_sort_key,
            ),
            "eligibleAssignments": sorted(
                [_normalize_assignment(a, principal_by_id, "eligible", scope_display) for a in eligible],
                key=_sort_key,
            ),
        }

        file_name = f"{sanitize_filename(display_name)}__{role_id}.json"
        write_json(out_dir / file_name, snapshot)
        written += 1

    # Also write any roles referenced in assignments but missing from role definitions (handles custom roles not returned)
    all_role_ids_in_assignments = set(perm_by_role) | set(eligible_by_role)
    orphaned = all_role_ids_in_assignments - set(role_def_by_id)
    for role_id in sorted(orphaned):
        perm = perm_by_role.get(role_id, [])
        eligible = eligible_by_role.get(role_id, [])
        snapshot = {
            "id": role_id,
            "displayName": role_id,
            "description": None,
            "isBuiltIn": None,
            "permanentAssignments": [_normalize_assignment(a, principal_by_id, "permanent", scope_display) for a in perm],
            "eligibleAssignments": [_normalize_assignment(a, principal_by_id, "eligible", scope_display) for a in eligible],
        }
        write_json(out_dir / f"Unknown Role__{role_id}.json", snapshot)
        written += 1

    if reports_root is not None:
        _write_role_assignments_report(out_dir, reports_root)

    log(f"Exported role assignments for {written} role(s).")
    return written, failed


# ---------------------------------------------------------------------------
# 1.3  Authentication methods policy
# ---------------------------------------------------------------------------

def export_auth_methods_policy(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export the authentication methods policy including per-method configurations."""
    failed: list[tuple[str, str]] = []

    policy, error = client.get_object(
        "https://graph.microsoft.com/v1.0/policies/authenticationMethodsPolicy"
    )
    if error or not isinstance(policy, dict):
        msg = error or "Empty response"
        log(f"  Warning: could not fetch authentication methods policy: {msg}")
        failed.append(("Authentication Methods Policy", msg))
        return 0, failed

    out_dir = root / "Authentication Methods"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "Authentication Methods Policy.json", policy)

    method_configs = policy.get("authenticationMethodConfigurations") or []
    if reports_root is not None:
        _write_auth_methods_report(out_dir, reports_root)

    log(f"Exported authentication methods policy ({len(method_configs)} method configuration(s)).")
    return 1, failed


# ---------------------------------------------------------------------------
# 1.4  Cross-tenant access + external collaboration
# ---------------------------------------------------------------------------

def export_cross_tenant_access(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export cross-tenant access policy (default + partners) and external identities policy."""
    failed: list[tuple[str, str]] = []
    out_dir = root / "Cross-Tenant Access"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    # Default cross-tenant access settings
    default_settings, error = client.get_object(
        "https://graph.microsoft.com/v1.0/policies/crossTenantAccessPolicy/default"
    )
    if error or not isinstance(default_settings, dict):
        msg = error or "Empty response"
        log(f"  Warning: could not fetch cross-tenant access default settings: {msg}")
        failed.append(("Cross-Tenant Access Default Settings", msg))
    else:
        write_json(out_dir / "Default Settings.json", default_settings)
        written += 1
        log("  Exported cross-tenant access default settings.")

    # Per-partner cross-tenant access settings
    partners, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/policies/crossTenantAccessPolicy/partners"
    )
    if error:
        log(f"  Warning: could not fetch cross-tenant access partners: {error}")
        failed.append(("Cross-Tenant Access Partners", error))
    else:
        partners_dir = out_dir / "Partners"
        partners_dir.mkdir(parents=True, exist_ok=True)
        for partner in partners:
            if not isinstance(partner, dict):
                continue
            tenant_id = str(partner.get("tenantId") or "").strip()
            display_name = str(partner.get("displayName") or partner.get("tenantId") or "Unknown").strip()
            file_name = f"{sanitize_filename(display_name)}__{tenant_id}.json"
            write_json(partners_dir / file_name, partner)
            written += 1
        log(f"  Exported {len(partners)} cross-tenant access partner setting(s).")

        pass  # partner rows are included in the consolidated cross-tenant report written below

    # External identities / collaboration policy.
    # HTTP 400 means the policy is not configured for this tenant type — treat as informational.
    ext_policy, error = client.get_object(
        "https://graph.microsoft.com/v1.0/policies/externalIdentitiesPolicy"
    )
    if error:
        if error in ("HTTP 400", "HTTP 404"):
            log(f"  Info: external identities policy not available for this tenant ({error}). Skipping.")
        else:
            log(f"  Warning: could not fetch external identities policy: {error}")
            failed.append(("External Identities Policy", error))
    elif not isinstance(ext_policy, dict):
        pass
    else:
        write_json(out_dir / "External Collaboration Settings.json", ext_policy)
        written += 1
        log("  Exported external collaboration settings.")

    if reports_root is not None:
        _write_cross_tenant_report(out_dir, reports_root)

    return written, failed


# ---------------------------------------------------------------------------
# 1.5  Identity Protection risk policies
# ---------------------------------------------------------------------------

_SECURITY_ENDPOINTS = [
    {
        "url": "https://graph.microsoft.com/v1.0/policies/identitySecurityDefaultsEnforcementPolicy",
        "filename": "Security Defaults.json",
        "display": "security defaults",
    },
    {
        "url": "https://graph.microsoft.com/v1.0/policies/authorizationPolicy",
        "filename": "Authorization Policy.json",
        "display": "authorization policy",
    },
]

_IDENTITY_PROTECTION_POLICIES = [
    {
        "key": "signInRiskPolicy",
        "url": "https://graph.microsoft.com/beta/identityProtection/signInRiskPolicy",
        "filename": "Sign-in Risk Policy.json",
        "display": "sign-in risk policy",
    },
    {
        "key": "userRiskPolicy",
        "url": "https://graph.microsoft.com/beta/identityProtection/userRiskPolicy",
        "filename": "User Risk Policy.json",
        "display": "user risk policy",
    },
]


def export_identity_protection(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export Identity Protection risk policies (sign-in risk, user risk)."""
    failed: list[tuple[str, str]] = []
    out_dir = root / "Identity Protection"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for policy_spec in _IDENTITY_PROTECTION_POLICIES:
        policy, error = client.get_object(policy_spec["url"])
        if error:
            # 404 and 400 both indicate the feature is not licensed or configured for this tenant.
            if error in ("HTTP 404", "HTTP 400"):
                log(f"  Info: {policy_spec['display']} not available ({error}) — tenant may not have Identity Protection P2 license. Skipping.")
            else:
                log(f"  Warning: could not fetch {policy_spec['display']}: {error}")
                failed.append((policy_spec["key"], error))
            continue
        if not isinstance(policy, dict):
            continue
        write_json(out_dir / policy_spec["filename"], policy)
        written += 1
        state = str(policy.get("isEnabled", policy.get("state", "unknown")))
        log(f"  Exported {policy_spec['display']} (state: {state}).")

    if written > 0 and reports_root is not None:
        _write_identity_protection_report(out_dir, reports_root)

    return written, failed


# ---------------------------------------------------------------------------
# 2.2  PIM role governance policies
# ---------------------------------------------------------------------------

def export_pim_policies(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export per-role PIM governance policies — activation duration, approval, MFA settings.

    Requires RoleManagementPolicy.Read.Directory permission.
    Stores one JSON file per role in tenant-state/entra/PIM Role Policies/.
    """
    failed: list[tuple[str, str]] = []

    # Step 1: get all policy assignments (links role definitions to governance policies)
    assignments, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/policies/roleManagementPolicyAssignments"
        "?$filter=scopeId%20eq%20'/'%20and%20scopeType%20eq%20'DirectoryRole'"
        "&$select=id,policyId,roleDefinitionId"
    )
    if error:
        log(f"  Warning: could not fetch PIM policy assignments: {error}")
        if error in ("HTTP 403", "HTTP 400", "HTTP 404"):
            log("    PIM governance policies require RoleManagementPolicy.Read.Directory permission. Skipping.")
        else:
            failed.append(("PIM Policy Assignments", error))
        return 0, failed

    log(f"Fetched {len(assignments)} PIM policy assignment(s).")

    # Step 2: fetch role definitions for display names
    role_defs, error = client.get_collection(
        "https://graph.microsoft.com/v1.0/roleManagement/directory/roleDefinitions"
        "?$select=id,displayName,isBuiltIn"
    )
    role_name_by_id = {
        str(r.get("id") or ""): str(r.get("displayName") or "")
        for r in role_defs
        if isinstance(r, dict) and r.get("id")
    }

    out_dir = root / "PIM Role Policies"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        policy_id = str(assignment.get("policyId") or "").strip()
        role_def_id = str(assignment.get("roleDefinitionId") or "").strip()
        if not policy_id or not role_def_id:
            continue

        role_name = role_name_by_id.get(role_def_id, role_def_id)

        # Step 3: fetch rules for this policy (activation duration, approval, MFA, etc.)
        rules, error = client.get_collection(
            "https://graph.microsoft.com/v1.0/policies/roleManagementPolicies/"
            + urllib.parse.quote(policy_id)
            + "/rules"
        )
        if error:
            log(f"  Warning: could not fetch rules for policy {policy_id} (role: {role_name}): {error}")
            failed.append((f"PIM rules for {role_name}", error))
            continue

        # Organise rules by @odata.type for readability
        rules_by_type: dict[str, Any] = {}
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_type = str(rule.get("@odata.type") or rule.get("id") or "unknown")
            # Strip the #microsoft.graph. prefix for cleaner keys
            key = rule_type.replace("#microsoft.graph.", "")
            rules_by_type[key] = rule

        snapshot: dict[str, Any] = {
            "roleDefinitionId": role_def_id,
            "roleDisplayName": role_name,
            "policyId": policy_id,
            "rules": rules_by_type,
        }

        file_name = f"{sanitize_filename(role_name)}__{role_def_id}.json"
        write_json(out_dir / file_name, snapshot)
        written += 1

    if reports_root is not None:
        _write_pim_policies_report(out_dir, reports_root)

    log(f"Exported PIM governance policies for {written} role(s).")
    return written, failed


# ---------------------------------------------------------------------------
# 2.3  Security settings
# ---------------------------------------------------------------------------

def export_security_settings(
    client: GraphClient,
    root: pathlib.Path,
    fail_on_error: bool,
    reports_root: pathlib.Path | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Export security defaults and authorization policy to tenant-state/entra/Security/."""
    failed: list[tuple[str, str]] = []
    out_dir = root / "Security"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for spec in _SECURITY_ENDPOINTS:
        policy, error = client.get_object(spec["url"])
        if error:
            if error in ("HTTP 400", "HTTP 404"):
                log(f"  Info: {spec['display']} not available ({error}). Skipping.")
            else:
                log(f"  Warning: could not fetch {spec['display']}: {error}")
                failed.append((spec["display"], error))
            continue
        if not isinstance(policy, dict):
            continue
        write_json(out_dir / spec["filename"], policy)
        written += 1
        log(f"  Exported {spec['display']}.")

    if written > 0 and reports_root is not None:
        _write_security_report(out_dir, reports_root)

    return written, failed


# ---------------------------------------------------------------------------
# Standalone report writers — read from committed JSON, no API calls
# ---------------------------------------------------------------------------

def _write_groups_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    if not data_dir.is_dir():
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    md_lines = [
        "# Groups",
        "",
        "Security groups referenced in Conditional Access policies, explicitly watched, or role-assignable.",
        f"Object count: **{sum(1 for _ in data_dir.glob('*.json'))}**",
        "",
        "| Name | Id | Members | Privileged | Watched | CA Policies |",
        "|---|---|---|---|---|---|",
    ]
    for gf in sorted(data_dir.glob("*.json")):
        try:
            data = json.loads(gf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        name = str(data.get("displayName") or "").replace("|", "\\|")
        rename = f" *(was: {data['previousDisplayName']})*" if data.get("previousDisplayName") else ""
        gid = str(data.get("id") or "")
        count = data.get("memberCount")
        count_str = str(count) if count is not None else "?"
        md_lines.append(
            f"| {name}{rename} | {gid} | {count_str} "
            f"| {'Yes' if data.get('privileged') else ''} "
            f"| {'Yes' if data.get('watched') else ''} "
            f"| {', '.join(str(p) for p in (data.get('caReferences') or []))} |"
        )
    (reports_root / "groups.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_role_assignments_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    if not data_dir.is_dir():
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, int, int]] = []
    for rf in sorted(data_dir.glob("*.json")):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows.append((
            str(data.get("displayName") or "").replace("|", "\\|"),
            len(data.get("permanentAssignments") or []),
            len(data.get("eligibleAssignments") or []),
        ))
    md_lines = [
        "# Role Assignments",
        "",
        "Directory roles with at least one permanent or PIM-eligible assignment.",
        f"Object count: **{len(rows)}**",
        "",
        "| Role | Permanent | Eligible |",
        "|---|---|---|",
    ] + [f"| {name} | {p} | {e} |" for name, p, e in rows]
    (reports_root / "role-assignments.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_auth_methods_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    policy_path = data_dir / "Authentication Methods Policy.json"
    if not policy_path.exists():
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    method_configs = policy.get("authenticationMethodConfigurations") or []
    md_lines = [
        "# Authentication Methods Policy",
        "",
        "| Method | State |",
        "|---|---|",
    ] + [
        f"| {str(m.get('id') or '').replace('|', chr(92) + '|')} | {m.get('state', '')} |"
        for m in sorted(method_configs, key=lambda m: str(m.get("id") or "").casefold())
    ]
    (reports_root / "auth-methods-policy.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_cross_tenant_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    if not data_dir.is_dir():
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    partners_dir = data_dir / "Partners"
    partner_rows: list[tuple[str, str]] = []
    if partners_dir.is_dir():
        for pf in sorted(partners_dir.glob("*.json")):
            try:
                p = json.loads(pf.read_text(encoding="utf-8"))
                name = str(p.get("displayName") or p.get("tenantId") or "").replace("|", "\\|")
                tid = str(p.get("tenantId") or "")
                partner_rows.append((name, tid))
            except Exception:  # noqa: BLE001
                continue
    md_lines = ["# Cross-Tenant Access", "", f"Object count: **{2 + len(partner_rows)}**", ""]
    if (data_dir / "Default Settings.json").exists():
        md_lines += ["## Default Settings", "", "See `Cross-Tenant Access/Default Settings.json`.", ""]
    if partner_rows:
        md_lines += [f"## Partners ({len(partner_rows)})", "", "| Tenant | Id |", "|---|---|"]
        md_lines += [f"| {name} | {tid} |" for name, tid in partner_rows]
        md_lines.append("")
    if (data_dir / "External Collaboration Settings.json").exists():
        md_lines += ["## External Collaboration Settings", "", "See `Cross-Tenant Access/External Collaboration Settings.json`.", ""]
    (reports_root / "cross-tenant-access.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_identity_protection_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    if not data_dir.is_dir():
        return
    rows: list[tuple[str, str]] = []
    for spec in _IDENTITY_PROTECTION_POLICIES:
        path = data_dir / spec["filename"]
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            state = str(data.get("isEnabled", data.get("state", "unknown")))
        except Exception:  # noqa: BLE001
            state = "unknown"
        rows.append((spec["display"].title(), state))
    if not rows:
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    md_lines = [
        "# Identity Protection",
        "",
        f"Object count: **{len(rows)}**",
        "",
        "| Policy | State |",
        "|---|---|",
    ] + [f"| {name} | {state} |" for name, state in rows]
    (reports_root / "identity-protection.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_pim_policies_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    if not data_dir.is_dir():
        return
    rows: list[tuple[str, str]] = []
    for rf in sorted(data_dir.glob("*.json")):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            rows.append((str(data.get("roleDisplayName") or "").replace("|", "\\|"), str(data.get("policyId") or "")))
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    md_lines = [
        "# PIM Role Policies",
        "",
        "Per-role PIM governance settings: activation duration, approval requirements, MFA enforcement.",
        f"Object count: **{len(rows)}**",
        "",
        "| Role | Policy ID |",
        "|---|---|",
    ] + [f"| {name} | {pid} |" for name, pid in rows]
    (reports_root / "pim-role-policies.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_security_report(data_dir: pathlib.Path, reports_root: pathlib.Path) -> None:
    if not data_dir.is_dir():
        return
    present = [spec for spec in _SECURITY_ENDPOINTS if (data_dir / spec["filename"]).exists()]
    if not present:
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    md_lines = [
        "# Security Settings",
        "",
        "Tenant-wide security configuration.",
        f"Object count: **{len(present)}**",
        "",
        "| Item | File |",
        "|---|---|",
    ] + [f"| {spec['display'].title()} | Security/{spec['filename']} |" for spec in present]
    (reports_root / "security.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def generate_identity_reports(root: pathlib.Path, reports_root: pathlib.Path) -> None:
    """Generate all identity markdown reports by reading committed JSON. No API calls needed."""
    log(f"Generating identity reports from {root} → {reports_root}")
    _write_groups_report(root / "Groups", reports_root)
    _write_role_assignments_report(root / "Role Assignments", reports_root)
    _write_auth_methods_report(root / "Authentication Methods", reports_root)
    _write_cross_tenant_report(root / "Cross-Tenant Access", reports_root)
    _write_identity_protection_report(root / "Identity Protection", reports_root)
    _write_pim_policies_report(root / "PIM Role Policies", reports_root)
    _write_security_report(root / "Security", reports_root)
    log("Identity reports written.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    root = pathlib.Path(args.root).resolve()
    token = args.token.strip()
    fail_on_export_error = to_bool(args.fail_on_export_error)

    # Reports-only mode: read committed JSON, generate markdown, no API calls.
    if to_bool(args.reports_only):
        reports_root_raw = args.reports_root.strip() if args.reports_root else ""
        if reports_root_raw:
            rr = pathlib.Path(reports_root_raw).resolve()
        else:
            rr = root.parent / "reports" / root.name
        generate_identity_reports(root, rr)
        return 0

    if not token:
        log("No Graph token provided. Skipping Entra identity export.")
        return 0

    include_groups = to_bool(args.include_groups)
    include_privileged_groups = to_bool(args.include_privileged_groups)
    include_role_assignments = to_bool(args.include_role_assignments)
    include_auth_methods = to_bool(args.include_auth_methods_policy)
    include_cross_tenant = to_bool(args.include_cross_tenant_access)
    include_identity_protection = to_bool(args.include_identity_protection)
    include_pim_policies = to_bool(args.include_pim_policies)
    include_security_settings = to_bool(args.include_security_settings)
    watch_groups = _parse_watch_groups_csv(args.watch_groups_csv)

    if not any([include_groups, include_role_assignments, include_auth_methods,
                include_cross_tenant, include_identity_protection,
                include_pim_policies, include_security_settings]):
        log("All Entra identity export categories are disabled. Skipping.")
        return 0

    # Derive reports root: explicit arg > <root>/../reports/<root.name>
    if args.reports_root.strip():
        reports_root: pathlib.Path | None = pathlib.Path(args.reports_root).resolve()
    else:
        derived = root.parent / "reports" / root.name
        reports_root = derived
    if reports_root is not None and reports_root.mkdir(parents=True, exist_ok=True) is None:
        reports_root.mkdir(parents=True, exist_ok=True)

    client = GraphClient(token)
    _report_token_capabilities(token)

    if watch_groups:
        log(f"Watched groups (explicit): {len(watch_groups)} group(s)")

    # Resolve previous snapshot ref for rename/delta detection
    previous_lookup: PreviousGroupLookup | None = None
    if include_groups:
        repo_root = _discover_repo_root(root)
        if repo_root is not None:
            candidates = [
                args.previous_snapshot_ref,
                os.getenv("DRIFT_BRANCH_ENTRA", ""),
                "drift/entra",
            ]
            ref = ""
            for raw in candidates:
                ref = _resolve_branch_ref(repo_root, raw)
                if ref:
                    break
            if ref:
                groups_dir_rel = str(root.relative_to(repo_root) / "Groups").replace("\\", "/")
                previous_lookup = PreviousGroupLookup(repo_root, ref, groups_dir_rel)
                log(f"Using previous snapshot ref '{ref}' for group rename/delta detection.")

    total_written = 0
    all_failed: list[tuple[str, str]] = []

    if include_groups:
        log("=== 1.1 Exporting groups ===")
        privileged_group_ids: set[str] = set()
        if include_privileged_groups:
            privileged_group_ids = _fetch_privileged_group_ids(client)
        written, failed = export_groups(
            client, root, fail_on_export_error,
            reports_root=reports_root,
            watch_groups=watch_groups,
            privileged_group_ids=privileged_group_ids,
            previous_lookup=previous_lookup,
        )
        total_written += written
        all_failed.extend(failed)

    if include_role_assignments:
        log("=== 1.2 Exporting directory role assignments ===")
        written, failed = export_role_assignments(client, root, fail_on_export_error, reports_root=reports_root)
        total_written += written
        all_failed.extend(failed)

    if include_auth_methods:
        log("=== 1.3 Exporting authentication methods policy ===")
        written, failed = export_auth_methods_policy(client, root, fail_on_export_error, reports_root=reports_root)
        total_written += written
        all_failed.extend(failed)

    if include_cross_tenant:
        log("=== 1.4 Exporting cross-tenant access settings ===")
        written, failed = export_cross_tenant_access(client, root, fail_on_export_error, reports_root=reports_root)
        total_written += written
        all_failed.extend(failed)

    if include_identity_protection:
        log("=== 1.5 Exporting identity protection policies ===")
        written, failed = export_identity_protection(client, root, fail_on_export_error, reports_root=reports_root)
        total_written += written
        all_failed.extend(failed)

    if include_pim_policies:
        log("=== 2.2 Exporting PIM role governance policies ===")
        written, failed = export_pim_policies(client, root, fail_on_export_error, reports_root=reports_root)
        total_written += written
        all_failed.extend(failed)

    if include_security_settings:
        log("=== 2.3 Exporting security settings ===")
        written, failed = export_security_settings(client, root, fail_on_export_error, reports_root=reports_root)
        total_written += written
        all_failed.extend(failed)

    # Fatal failure handling — only raise if the category was requested AND fail_on_export_error
    fatal_failures = [f for f in all_failed if fail_on_export_error]
    if fatal_failures:
        log("Entra identity export failed because one or more categories could not be exported:")
        for category, error in fatal_failures:
            log(f"  - {category}: {error}")
        log(
            "Category failures are treated as fatal to avoid committing a partial or stale snapshot."
        )
        return 2

    log(
        f"Entra identity export complete. Total objects written: {total_written}."
        + (f" Warnings: {len(all_failed)}." if all_failed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
