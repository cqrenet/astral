#!/usr/bin/env python3
"""Export selected Entra baseline objects to JSON and markdown."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import common

log = common.log


TRANSIENT_RESOLUTION_ERROR_MARKERS = (
    "temporary failure in name resolution",
    "temporary failure resolving",
    "name or service not known",
    "failed to resolve",
    "nodename nor servname provided, or not known",
    "no address associated with hostname",
    "getaddrinfo failed",
    "certificate verify failed",
    "ssl: certificate_verify_failed",
    "timed out",
    "connection timed out",
    "read timed out",
    "connection reset by peer",
    "connection refused",
    "remote end closed connection without response",
    "network is unreachable",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Path to Entra workload backup root (tenant-state/entra).")
    parser.add_argument("--token", default=os.environ.get("GRAPH_TOKEN", ""), help="Microsoft Graph bearer token (default: $GRAPH_TOKEN).")
    parser.add_argument("--include-named-locations", default="true", help="Include Entra named locations export (true/false).")
    parser.add_argument(
        "--include-authentication-strengths",
        default="true",
        help="Include Entra authentication strengths export (true/false).",
    )
    parser.add_argument(
        "--include-conditional-access",
        default="true",
        help="Include Entra Conditional Access policies export (true/false).",
    )
    parser.add_argument(
        "--include-enterprise-applications",
        default="true",
        help="Include enterprise applications export (true/false).",
    )
    parser.add_argument(
        "--include-app-registrations",
        default="true",
        help="Include app registrations export (true/false).",
    )
    parser.add_argument(
        "--enterprise-app-workers",
        type=int,
        default=common.env_int("ENTRA_ENTERPRISE_APP_WORKERS", 8),
        help="Number of parallel workers used to enrich Enterprise Applications (1-32).",
    )
    parser.add_argument(
        "--fail-on-export-error",
        default="true",
        help="Fail with non-zero exit code when any requested export category fails (true/false).",
    )
    parser.add_argument(
        "--previous-snapshot-ref",
        default="",
        help="Optional git branch/ref used as fallback source for resolution backfill (for example origin/drift/entra).",
    )
    return parser.parse_args()


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] if len(cleaned) > 180 else cleaned


def _discover_repo_root(path: pathlib.Path) -> pathlib.Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(path),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    top = (proc.stdout or "").strip()
    if not top:
        return None
    return pathlib.Path(top).resolve()


def _resolve_existing_branch_ref(repo_root: pathlib.Path, branch: str) -> str:
    normalized = common.normalize_branch(branch)
    if not normalized:
        return ""
    remote_ref = f"refs/remotes/origin/{normalized}"
    if common.git_proc(repo_root, ["show-ref", "--verify", "--quiet", remote_ref], check=False).returncode == 0:
        return f"origin/{normalized}"
    local_ref = f"refs/heads/{normalized}"
    if common.git_proc(repo_root, ["show-ref", "--verify", "--quiet", local_ref], check=False).returncode == 0:
        return normalized
    return ""


def _repo_relative_posix(repo_root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return ""


def _load_resource_sp_cache_from_export(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    export_dir = root / "Enterprise Applications"
    if not export_dir.is_dir():
        return cache
    for path in sorted(export_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        app_id = str(payload.get("appId") or "").strip()
        if not app_id:
            continue
        cache[app_id] = {
            "id": str(payload.get("id") or "").strip(),
            "appId": app_id,
            "displayName": str(payload.get("displayName") or "").strip(),
            "appRoles": payload.get("appRoles") if isinstance(payload.get("appRoles"), list) else [],
            "oauth2PermissionScopes": (
                payload.get("oauth2PermissionScopes")
                if isinstance(payload.get("oauth2PermissionScopes"), list)
                else []
            ),
        }
    return cache


def _export_object_id_from_path(path: str) -> str:
    name = pathlib.PurePosixPath(path).name
    if not name.endswith(".json"):
        return ""
    stem = name[:-5]
    if "__" not in stem:
        return ""
    return stem.rsplit("__", 1)[-1].strip()


class PreviousSnapshotLookup:
    def __init__(self, repo_root: pathlib.Path, ref: str, category_repo_dir: str):
        self.repo_root = repo_root
        self.ref = ref
        self.paths_by_id: dict[str, str] = {}
        self.cache: dict[str, dict[str, Any] | None] = {}
        if not category_repo_dir:
            return
        try:
            out = common.git_proc(
                repo_root,
                ["ls-tree", "-r", "--name-only", ref, "--", category_repo_dir],
            ).stdout
        except Exception:
            return
        for raw in out.splitlines():
            rel_path = raw.strip()
            if not rel_path:
                continue
            object_id = _export_object_id_from_path(rel_path)
            if object_id:
                self.paths_by_id[object_id] = rel_path

    def get(self, object_id: str) -> dict[str, Any] | None:
        object_id = str(object_id or "").strip()
        if not object_id:
            return None
        if object_id in self.cache:
            return self.cache[object_id]
        rel_path = self.paths_by_id.get(object_id, "")
        if not rel_path:
            self.cache[object_id] = None
            return None
        try:
            content = common.git_proc(self.repo_root, ["show", f"{self.ref}:{rel_path}"]).stdout
            payload = json.loads(content)
            self.cache[object_id] = payload if isinstance(payload, dict) else None
        except Exception:
            self.cache[object_id] = None
        return self.cache[object_id]


def is_transient_resolution_error(error: str | None) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in TRANSIENT_RESOLUTION_ERROR_MARKERS)


def normalize_resolution_error(error: str | None) -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    if is_transient_resolution_error(text):
        return ""
    return text


def normalize_resolution_lookup_errors(errors: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in errors:
        text = str(raw or "").strip()
        if not text:
            continue
        if is_transient_resolution_error(text):
            continue
        normalized.append(text)
    return sorted(set(normalized))


class GraphClient:
    def __init__(self, token: str, max_retries: int = 4):
        self.token = token
        self.max_retries = max_retries

    def _request(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    retry_after = common.retry_after_seconds(exc)
                    delay = retry_after if retry_after is not None else min(2**attempt, 10)
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise
            except urllib.error.URLError:
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 10))
                    attempt += 1
                    continue
                raise

    def get_collection(self, url: str) -> tuple[list[dict], str | None]:
        items: list[dict] = []
        next_url = url
        while next_url:
            try:
                payload = self._request(next_url)
            except urllib.error.HTTPError as exc:
                return items, f"HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001
                return items, str(exc)

            value = payload.get("value")
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        items.append(item)
            next_url = payload.get("@odata.nextLink")
            if next_url and not isinstance(next_url, str):
                next_url = None
        return items, None

    def get_object(self, url: str) -> tuple[dict | None, str | None]:
        try:
            payload = self._request(url)
            if isinstance(payload, dict):
                return payload, None
            return None, "Unexpected non-object payload"
        except urllib.error.HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)


def _quote_odata_literal(value: str) -> str:
    return value.replace("'", "''")


def _normalize_owner(owner: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(owner.get("id") or ""),
        "displayName": str(owner.get("displayName") or ""),
        "userPrincipalName": str(owner.get("userPrincipalName") or ""),
        "appId": str(owner.get("appId") or ""),
        "odataType": str(owner.get("@odata.type") or ""),
    }


def resolve_owners(
    client: GraphClient,
    object_kind: str,
    object_id: str,
) -> tuple[list[dict[str, str]], str | None]:
    if not object_id:
        return [], "Missing object id"
    url = (
        f"https://graph.microsoft.com/v1.0/{object_kind}/"
        + urllib.parse.quote(object_id)
        + "/owners?$select=id,displayName,userPrincipalName,appId"
    )
    owners, error = client.get_collection(url)
    return [_normalize_owner(owner) for owner in owners], error


def _find_permission_by_id(
    resource_sp: dict[str, Any],
    permission_id: str,
    permission_type: str,
) -> dict[str, str]:
    result = {
        "id": permission_id,
        "type": permission_type,
        "value": "",
        "displayName": "",
        "description": "",
    }
    if permission_type.lower() == "role":
        for role in resource_sp.get("appRoles", []):
            if str(role.get("id") or "").lower() == permission_id.lower():
                result["value"] = str(role.get("value") or "")
                result["displayName"] = str(role.get("displayName") or "")
                result["description"] = str(role.get("description") or "")
                return result
        return result

    for scope in resource_sp.get("oauth2PermissionScopes", []):
        if str(scope.get("id") or "").lower() == permission_id.lower():
            result["value"] = str(scope.get("value") or "")
            result["displayName"] = str(scope.get("adminConsentDisplayName") or "")
            result["description"] = str(scope.get("adminConsentDescription") or "")
            return result
    return result


_SP_SELECT = "id,appId,displayName,appRoles,oauth2PermissionScopes"


def _lookup_service_principal_by_app_id(client: GraphClient, resource_app_id: str) -> tuple[dict[str, Any] | None, Any]:
    """Find a resource service principal by appId: filter query first, then the direct key lookup."""
    literal = urllib.parse.quote(_quote_odata_literal(resource_app_id))
    payload, error = client.get_object(
        "https://graph.microsoft.com/v1.0/servicePrincipals"
        f"?$top=1&$select={_SP_SELECT}&$filter=appId eq '{literal}'"
    )
    sp = None
    values = payload.get("value") if isinstance(payload, dict) else None
    if isinstance(values, list) and values and isinstance(values[0], dict):
        sp = values[0]
    if sp is None:
        direct_payload, direct_error = client.get_object(
            f"https://graph.microsoft.com/v1.0/servicePrincipals(appId='{literal}')?$select={_SP_SELECT}"
        )
        if isinstance(direct_payload, dict) and str(direct_payload.get("id") or "").strip():
            sp = direct_payload
        elif direct_error and not error:
            error = direct_error
    return sp, error


def _resolve_permissions(resource_sp: Any, accesses: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Permission details for each resourceAccess entry; also counts entries left unresolved."""
    permissions: list[dict[str, Any]] = []
    unresolved = 0
    for access in accesses:
        if not isinstance(access, dict):
            continue
        permission_id = str(access.get("id") or "")
        permission_type = str(access.get("type") or "")
        if not permission_id:
            continue
        if isinstance(resource_sp, dict):
            permission = _find_permission_by_id(resource_sp, permission_id=permission_id, permission_type=permission_type)
        else:
            permission = {"id": permission_id, "type": permission_type, "value": "", "displayName": "", "description": ""}
        permissions.append(permission)
        if not (str(permission.get("value") or "").strip() or str(permission.get("displayName") or "").strip()):
            unresolved += 1
    return permissions, unresolved


def resolve_required_resource_access(
    app: dict[str, Any],
    client: GraphClient,
    resource_sp_by_appid: dict[str, dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    required = app.get("requiredResourceAccess")
    if not isinstance(required, list):
        return [], 0, 0, []

    resolved: list[dict[str, Any]] = []
    unresolved_resource_count = 0
    unresolved_permission_count = 0
    lookup_errors: list[str] = []
    for item in required:
        if not isinstance(item, dict):
            continue
        resource_app_id = str(item.get("resourceAppId") or "")
        if not resource_app_id:
            continue
        if resource_app_id not in resource_sp_by_appid:
            sp, error = _lookup_service_principal_by_app_id(client, resource_app_id)
            if error:
                lookup_errors.append(f"resourceAppId {resource_app_id}: {error}")
            resource_sp_by_appid[resource_app_id] = sp

        resource_sp = resource_sp_by_appid.get(resource_app_id)
        resource_name = str(resource_sp.get("displayName") or "") if isinstance(resource_sp, dict) else ""
        if not resource_name:
            unresolved_resource_count += 1
        permissions, unresolved = _resolve_permissions(resource_sp, item.get("resourceAccess", []))
        unresolved_permission_count += unresolved
        resolved.append(
            {
                "resourceAppId": resource_app_id,
                "resourceDisplayName": resource_name or "Unresolved",
                "permissions": permissions,
            }
        )

    return (
        resolved,
        unresolved_resource_count,
        unresolved_permission_count,
        normalize_resolution_lookup_errors(lookup_errors),
    )


def _find_app_role(resource_sp: Any, app_role_id: str) -> tuple[str, str]:
    """(value, display name) of the app role with this id on the resource, or ('', '')."""
    if isinstance(resource_sp, dict):
        for role in resource_sp.get("appRoles", []):
            if str(role.get("id") or "").lower() == app_role_id.lower():
                return str(role.get("value") or ""), str(role.get("displayName") or "")
    return "", ""


def _load_resource_sp(
    client: GraphClient,
    resource_id: str,
    cache: dict[str, dict[str, Any] | None],
    lock: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resource service principal by object id: from the cache, else fetched once and cached.

    Returns (service principal, lookup error); the error is only set when a fetch was made.
    """
    with lock:
        if resource_id in cache:
            return cache[resource_id], None
    payload, error = client.get_object(
        "https://graph.microsoft.com/v1.0/servicePrincipals/"
        + urllib.parse.quote(resource_id)
        + "?$select=id,appId,displayName,appRoles"
    )
    with lock:
        if resource_id not in cache:  # another worker may have stored it meanwhile
            cache[resource_id] = payload if isinstance(payload, dict) else None
        return cache[resource_id], error


def resolve_enterprise_app_role_assignments(
    service_principal: dict[str, Any],
    client: GraphClient,
    resource_sp_by_id: dict[str, dict[str, Any] | None],
    resource_sp_lock: threading.Lock | None = None,
) -> tuple[list[dict[str, Any]], str | None, int, int, list[str]]:
    sp_id = str(service_principal.get("id") or "")
    if not sp_id:
        return [], "Missing service principal id", 0, 0, []

    assignments, assignment_error = client.get_collection(
        "https://graph.microsoft.com/v1.0/servicePrincipals/"
        + urllib.parse.quote(sp_id)
        + "/appRoleAssignments?$select=id,resourceId,appRoleId,principalType"
    )
    lock = resource_sp_lock if resource_sp_lock is not None else contextlib.nullcontext()
    resolved: list[dict[str, Any]] = []
    unresolved_resource_count = 0
    unresolved_role_count = 0
    lookup_errors: list[str] = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        resource_id = str(assignment.get("resourceId") or "")
        app_role_id = str(assignment.get("appRoleId") or "")
        if not resource_id:
            continue
        resource_sp, error = _load_resource_sp(client, resource_id, resource_sp_by_id, lock)
        if error:
            lookup_errors.append(f"resourceId {resource_id}: {error}")
        resource_name = str(resource_sp.get("displayName") or "") if isinstance(resource_sp, dict) else ""
        if not resource_name:
            unresolved_resource_count += 1
        role_value, role_display_name = _find_app_role(resource_sp, app_role_id)
        if not role_value and not role_display_name:
            unresolved_role_count += 1

        resolved.append(
            {
                "resourceId": resource_id,
                "resourceDisplayName": resource_name or "Unresolved",
                "appRoleId": app_role_id,
                "appRoleValue": role_value,
                "appRoleDisplayName": role_display_name,
                "principalType": str(assignment.get("principalType") or ""),
            }
        )
    return (
        resolved,
        assignment_error,
        unresolved_resource_count,
        unresolved_role_count,
        normalize_resolution_lookup_errors(lookup_errors),
    )


def resolve_org_owner(
    org_id: str,
    local_org_by_id: dict[str, str],
) -> dict[str, str]:
    org_id_text = str(org_id or "").strip()
    if not org_id_text:
        return {"tenantId": "", "displayName": "", "resolution": "missing"}
    display_name = local_org_by_id.get(org_id_text, "")
    if display_name:
        return {
            "tenantId": org_id_text,
            "displayName": display_name,
            "resolution": "localTenant",
        }
    return {
        "tenantId": org_id_text,
        "displayName": "",
        "resolution": "externalOrUnresolved",
    }


def _is_unresolved_marker(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    return text.lower() == "unresolved"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _owner_key(owner: dict[str, Any]) -> str:
    return _text(owner.get("id")) or _text(owner.get("appId")) or _text(owner.get("userPrincipalName")).casefold()


def _index_dicts(items: list[Any], key_fn: Any) -> dict[Any, dict[str, Any]]:
    """Dict items by key (the last one wins); items without a usable key are skipped."""
    indexed: dict[Any, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict):
            key = key_fn(item)
            if key:
                indexed[key] = item
    return indexed


def _fill_missing(target: dict[str, Any], previous: dict[str, Any], fields: tuple[str, ...]) -> None:
    """Copy non-empty values from *previous* into fields that are empty on *target*."""
    for field in fields:
        if not _text(target.get(field)):
            value = _text(previous.get(field))
            if value:
                target[field] = value


def _fill_resource_name(target: dict[str, Any], previous: dict[str, Any]) -> None:
    if _is_unresolved_marker(target.get("resourceDisplayName")):
        name = _text(previous.get("resourceDisplayName"))
        if name and not _is_unresolved_marker(name):
            target["resourceDisplayName"] = name


def _merge_owner_resolution(
    current: list[dict[str, str]],
    previous: list[dict[str, Any]],
) -> list[dict[str, str]]:
    previous_by_key = _index_dicts(previous, _owner_key)
    merged: list[dict[str, str]] = []
    for item in current:
        enriched = dict(item)
        _fill_missing(enriched, previous_by_key.get(_owner_key(enriched), {}), ("displayName",))
        merged.append(enriched)
    return merged


def _permission_key(perm: dict[str, Any]) -> tuple[str, str]:
    return (_text(perm.get("id")), _text(perm.get("type")).lower())


def _merge_permissions(current: list[Any], previous: list[Any]) -> list[Any]:
    previous_by_key = _index_dicts(previous, _permission_key)  # tuple keys are never empty
    merged: list[Any] = []
    for perm in current:
        if not isinstance(perm, dict):
            merged.append(perm)
            continue
        merged_perm = dict(perm)
        prev = previous_by_key.get(_permission_key(merged_perm), {})
        _fill_missing(merged_perm, prev, ("value", "displayName", "description"))
        merged.append(merged_perm)
    return merged


def _merge_required_resource_access_resolution(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_by_resource = _index_dicts(previous, lambda item: _text(item.get("resourceAppId")))
    merged: list[dict[str, Any]] = []
    for item in current:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        enriched = dict(item)
        prev = previous_by_resource.get(_text(enriched.get("resourceAppId")), {})
        _fill_resource_name(enriched, prev)
        current_perms, previous_perms = enriched.get("permissions"), prev.get("permissions")
        if isinstance(current_perms, list) and isinstance(previous_perms, list):
            enriched["permissions"] = _merge_permissions(current_perms, previous_perms)
        merged.append(enriched)
    return merged


def _role_assignment_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (_text(item.get("resourceId")), _text(item.get("appRoleId")), _text(item.get("principalType")))


def _merge_app_role_assignments_resolution(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_by_key = _index_dicts(previous, _role_assignment_key)
    merged: list[dict[str, Any]] = []
    for item in current:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        enriched = dict(item)
        prev = previous_by_key.get(_role_assignment_key(enriched), {})
        _fill_resource_name(enriched, prev)
        _fill_missing(enriched, prev, ("appRoleValue", "appRoleDisplayName"))
        merged.append(enriched)
    return merged


def _count_unresolved_required_permissions(required: list[dict[str, Any]]) -> tuple[int, int]:
    unresolved_resource_count = 0
    unresolved_permission_count = 0
    for item in required:
        if not isinstance(item, dict):
            continue
        if _is_unresolved_marker(item.get("resourceDisplayName")):
            unresolved_resource_count += 1
        permissions = item.get("permissions")
        if not isinstance(permissions, list):
            continue
        for permission in permissions:
            if not isinstance(permission, dict):
                continue
            if not str(permission.get("value") or "").strip() and not str(permission.get("displayName") or "").strip():
                unresolved_permission_count += 1
    return unresolved_resource_count, unresolved_permission_count


def _count_unresolved_app_role_assignments(assignments: list[dict[str, Any]]) -> tuple[int, int]:
    unresolved_resource_count = 0
    unresolved_role_count = 0
    for item in assignments:
        if not isinstance(item, dict):
            continue
        if _is_unresolved_marker(item.get("resourceDisplayName")):
            unresolved_resource_count += 1
        if not str(item.get("appRoleValue") or "").strip() and not str(item.get("appRoleDisplayName") or "").strip():
            unresolved_role_count += 1
    return unresolved_resource_count, unresolved_role_count


def _owners_need_backfill(owners: list[dict[str, str]]) -> bool:
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        if _owner_key(owner) and not str(owner.get("displayName") or "").strip():
            return True
    return False


def _required_resource_access_needs_backfill(required: list[dict[str, Any]]) -> bool:
    unresolved_resources, unresolved_permissions = _count_unresolved_required_permissions(required)
    return unresolved_resources > 0 or unresolved_permissions > 0


def _app_role_assignments_need_backfill(assignments: list[dict[str, Any]]) -> bool:
    unresolved_resources, unresolved_roles = _count_unresolved_app_role_assignments(assignments)
    return unresolved_resources > 0 or unresolved_roles > 0


def enrich_enterprise_application(
    item: dict[str, Any],
    client: GraphClient,
    resource_sp_by_id: dict[str, dict[str, Any] | None],
    resource_sp_lock: threading.Lock,
    local_org_by_id: dict[str, str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    object_id = str(item.get("id") or "").strip()
    owners, owners_error = resolve_owners(
        client=client,
        object_kind="servicePrincipals",
        object_id=object_id,
    )
    (
        role_assignments,
        role_assignment_error,
        unresolved_resources,
        unresolved_roles,
        role_lookup_errors,
    ) = resolve_enterprise_app_role_assignments(
        service_principal=item,
        client=client,
        resource_sp_by_id=resource_sp_by_id,
        resource_sp_lock=resource_sp_lock,
    )
    resolution_status = {
        "owners": {
            "count": len(owners),
            "error": normalize_resolution_error(owners_error),
        },
        "appRoleAssignments": {
            "count": len(role_assignments),
            "collectionError": normalize_resolution_error(role_assignment_error),
            "unresolvedResourceCount": unresolved_resources,
            "unresolvedRoleCount": unresolved_roles,
            "lookupErrors": normalize_resolution_lookup_errors(role_lookup_errors),
        },
    }
    owner_org = resolve_org_owner(
        org_id=str(item.get("appOwnerOrganizationId") or ""),
        local_org_by_id=local_org_by_id,
    )
    return owners, role_assignments, {"resolutionStatus": resolution_status, "appOwnerOrganizationResolved": owner_org}


def write_collection(
    root: pathlib.Path,
    rel_dir: str,
    title: str,
    items: list[dict],
    source_url: str,
) -> int:
    out_dir = root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for idx, item in enumerate(items, start=1):
        object_id = str(item.get("id") or item.get("templateId") or f"item-{idx}")
        display_name = (
            str(item.get("displayName") or item.get("name") or object_id)
            .replace("\n", " ")
            .strip()
        )
        file_name = f"{sanitize_filename(display_name)}__{object_id}.json"
        (out_dir / file_name).write_text(
            json.dumps(item, indent=5, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written += 1

    md_path = out_dir / f"{title}.md"
    lines = [
        f"# {title}",
        "",
        f"Source: `{source_url}`",
        f"Object count: **{written}**",
        "",
        "| Name | Id |",
        "|---|---|",
    ]
    for item in sorted(
        items,
        key=lambda x: (
            str(x.get("displayName") or x.get("name") or "").strip().casefold(),
            str(x.get("id") or x.get("templateId") or "").strip().casefold(),
        ),
    ):
        name = str(item.get("displayName") or item.get("name") or "Unknown").replace("|", "\\|")
        oid = str(item.get("id") or item.get("templateId") or "")
        lines.append(f"| {name} | {oid} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def _owner_org_unresolved(resolved: dict[str, Any]) -> bool:
    return not str(resolved["appOwnerOrganizationResolved"].get("displayName") or "").strip()


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _store_enterprise_app_enrichment(
    item: dict[str, Any],
    owners: list[dict[str, str]],
    role_assignments: list[dict[str, Any]],
    resolved: dict[str, Any],
    previous_lookup: PreviousSnapshotLookup | None,
) -> None:
    """Backfill unresolved gaps from the previous snapshot, then write the enrichment onto *item*."""
    object_id = str(item.get("id") or "").strip()
    needs_backfill = (
        _owners_need_backfill(owners)
        or _app_role_assignments_need_backfill(role_assignments)
        or _owner_org_unresolved(resolved)
    )
    previous_item = previous_lookup.get(object_id) if previous_lookup and object_id and needs_backfill else None
    if isinstance(previous_item, dict):
        owners = _merge_owner_resolution(owners, _as_list(previous_item.get("ownersResolved")))
        role_assignments = _merge_app_role_assignments_resolution(
            role_assignments, _as_list(previous_item.get("appRoleAssignmentsResolved"))
        )
        previous_owner_org = previous_item.get("appOwnerOrganizationResolved")
        if isinstance(previous_owner_org, dict) and _owner_org_unresolved(resolved):
            previous_name = str(previous_owner_org.get("displayName") or "").strip()
            if previous_name:
                resolved["appOwnerOrganizationResolved"]["displayName"] = previous_name
        unresolved_resources, unresolved_roles = _count_unresolved_app_role_assignments(role_assignments)
        app_role_status = resolved["resolutionStatus"].get("appRoleAssignments", {})
        if isinstance(app_role_status, dict):
            app_role_status["count"] = len(role_assignments)
            app_role_status["unresolvedResourceCount"] = unresolved_resources
            app_role_status["unresolvedRoleCount"] = unresolved_roles
            resolved["resolutionStatus"]["appRoleAssignments"] = app_role_status
    item["ownersResolved"] = owners
    item["appRoleAssignmentsResolved"] = role_assignments
    item["appOwnerOrganizationResolved"] = resolved["appOwnerOrganizationResolved"]
    item["resolutionStatus"] = resolved["resolutionStatus"]


def _enterprise_app_failure(
    item: dict[str, Any], local_org_by_id: dict[str, str], exc: Exception
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    normalized_error = normalize_resolution_error(str(exc))
    resolved = {
        "appOwnerOrganizationResolved": resolve_org_owner(
            org_id=str(item.get("appOwnerOrganizationId") or ""),
            local_org_by_id=local_org_by_id,
        ),
        "resolutionStatus": {
            "owners": {"count": 0, "error": normalized_error},
            "appRoleAssignments": {
                "count": 0,
                "collectionError": normalized_error,
                "unresolvedResourceCount": 0,
                "unresolvedRoleCount": 0,
                "lookupErrors": [],
            },
        },
    }
    return [], [], resolved


def _log_progress(label: str, done: int, total: int) -> None:
    if done == 1 or done % 25 == 0 or done == total:
        log.info(f"{label} progress: {done}/{total}")


def _enrich_enterprise_apps(
    items: list[Any],
    client: GraphClient,
    resource_sp_by_appid: dict[str, dict[str, Any] | None],
    resource_sp_by_id: dict[str, dict[str, Any] | None],
    local_org_by_id: dict[str, str],
    previous_lookup: PreviousSnapshotLookup | None,
    workers: int,
) -> int:
    """Enrich Enterprise Application items in place; returns the number of failed lookups (warnings).

    With one worker an enrichment error is fatal (propagates); with several, the failing app
    gets an error placeholder and the export continues.
    """
    apps = [item for item in items if isinstance(item, dict)]
    total = len(apps)
    log.info(f"Resolving Enterprise Applications details for {total} objects using {workers} worker(s)...")
    for item in apps:
        app_id = str(item.get("appId") or "").strip()
        object_id = str(item.get("id") or "").strip()
        if app_id and app_id not in resource_sp_by_appid:
            resource_sp_by_appid[app_id] = item
        if object_id and object_id not in resource_sp_by_id:
            resource_sp_by_id[object_id] = item

    lock = threading.Lock()

    def _enrich(item: dict[str, Any]):
        return enrich_enterprise_application(item, client, resource_sp_by_id, lock, local_org_by_id)

    warnings = 0

    def _handle(item: dict[str, Any], result: tuple | None, error: Exception | None) -> None:
        nonlocal warnings
        if error is not None:
            warnings += 1
            result = _enterprise_app_failure(item, local_org_by_id, error)
        owners, role_assignments, resolved = result
        _store_enterprise_app_enrichment(item, owners, role_assignments, resolved, previous_lookup)

    if workers == 1:
        for done, item in enumerate(apps, start=1):
            _log_progress("Enterprise Applications", done, total)
            _handle(item, _enrich(item), None)
        return warnings

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_item = {pool.submit(_enrich, item): item for item in apps}
        for done, future in enumerate(concurrent.futures.as_completed(future_to_item), start=1):
            item = future_to_item[future]
            try:
                result, error = future.result(), None
            except Exception as exc:  # noqa: BLE001
                result, error = None, exc
            _handle(item, result, error)
            _log_progress("Enterprise Applications", done, total)
    return warnings


def _enrich_app_registrations(
    items: list[Any],
    client: GraphClient,
    resource_sp_by_appid: dict[str, dict[str, Any] | None],
    previous_lookup: PreviousSnapshotLookup | None,
) -> None:
    total = len(items)
    log.info(f"Resolving App Registrations details for {total} objects...")
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        _log_progress("App Registrations", idx, total)
        object_id = str(item.get("id") or "").strip()
        owners, owners_error = resolve_owners(client=client, object_kind="applications", object_id=object_id)
        (
            required_resolved,
            unresolved_resources,
            unresolved_permissions,
            required_lookup_errors,
        ) = resolve_required_resource_access(
            app=item,
            client=client,
            resource_sp_by_appid=resource_sp_by_appid,
        )
        needs_backfill = _owners_need_backfill(owners) or _required_resource_access_needs_backfill(required_resolved)
        previous_item = previous_lookup.get(object_id) if previous_lookup and object_id and needs_backfill else None
        if isinstance(previous_item, dict):
            owners = _merge_owner_resolution(owners, _as_list(previous_item.get("ownersResolved")))
            required_resolved = _merge_required_resource_access_resolution(
                required_resolved, _as_list(previous_item.get("requiredResourceAccessResolved"))
            )
            unresolved_resources, unresolved_permissions = _count_unresolved_required_permissions(required_resolved)
        item["ownersResolved"] = owners
        item["requiredResourceAccessResolved"] = required_resolved
        item["resolutionStatus"] = {
            "owners": {
                "count": len(owners),
                "error": normalize_resolution_error(owners_error),
            },
            "requiredResourceAccess": {
                "resourceCount": len(required_resolved),
                "unresolvedResourceCount": unresolved_resources,
                "unresolvedPermissionCount": unresolved_permissions,
                "lookupErrors": normalize_resolution_lookup_errors(required_lookup_errors),
            },
        }


GRAPH = "https://graph.microsoft.com"
ENTERPRISE_APP_SELECT = (
    "id,appId,displayName,servicePrincipalType,appOwnerOrganizationId,"
    "accountEnabled,publisherName,preferredSingleSignOnMode,tags,"
    "appRoleAssignmentRequired,appRoles,oauth2PermissionScopes,"
    "homepage,replyUrls,logoutUrl,servicePrincipalNames,verifiedPublisher,"
    "passwordCredentials,keyCredentials"
)
APP_REGISTRATION_SELECT = (
    "id,appId,displayName,description,signInAudience,publisherDomain,"
    "identifierUris,createdDateTime,tags,requiredResourceAccess,api,web,spa,"
    "publicClient,isFallbackPublicClient,verifiedPublisher,"
    "passwordCredentials,keyCredentials"
)
# (CLI flag, title == output folder, Graph URL)
EXPORT_CATALOG = (
    ("include_named_locations", "Named Locations", f"{GRAPH}/v1.0/identity/conditionalAccess/namedLocations"),
    (
        "include_authentication_strengths",
        "Authentication Strengths",
        f"{GRAPH}/beta/identity/conditionalAccess/authenticationStrength/policies",
    ),
    ("include_conditional_access", "Conditional Access", f"{GRAPH}/v1.0/identity/conditionalAccess/policies"),
    (
        "include_enterprise_applications",
        "Enterprise Applications",
        f"{GRAPH}/v1.0/servicePrincipals?$filter=servicePrincipalType%20eq%20'Application'&$select={ENTERPRISE_APP_SELECT}",
    ),
    ("include_app_registrations", "App Registrations", f"{GRAPH}/v1.0/applications?$select={APP_REGISTRATION_SELECT}"),
)


def _previous_snapshot_ref(repo_root: pathlib.Path, requested_ref: str) -> str:
    candidates = [
        requested_ref,
        os.getenv("DRIFT_BRANCH_ENTRA", ""),
        os.getenv("DRIFT_BRANCH", ""),
        "origin/drift/entra",
        os.getenv("BASELINE_BRANCH", ""),
    ]
    for candidate_raw in candidates:
        candidate = _resolve_existing_branch_ref(repo_root, candidate_raw)
        if candidate:
            return candidate
    return ""


def _local_org_names(client: GraphClient) -> tuple[dict[str, str], int]:
    """Map organization id -> display name for the local tenant; returns (names, warnings)."""
    log.info("Resolving local organization details...")
    payload, error = client.get_object("https://graph.microsoft.com/v1.0/organization?$select=id,displayName")
    if error:
        log.warning(f"unable to resolve local organization details ({error})")
        return {}, 1
    names: dict[str, str] = {}
    values = payload.get("value") if isinstance(payload, dict) else None
    for org in _as_list(values):
        if not isinstance(org, dict):
            continue
        org_id = str(org.get("id") or "").strip()
        if org_id:
            names[org_id] = str(org.get("displayName") or "").strip()
    return names, 0


def _prime_resource_sp_caches(
    root: pathlib.Path,
    by_appid: dict[str, dict[str, Any] | None],
    by_id: dict[str, dict[str, Any] | None],
) -> None:
    """Seed the resource service-principal caches from a previous local Enterprise Applications export."""
    cached = _load_resource_sp_cache_from_export(root)
    if not cached:
        return
    by_appid.update(cached)
    for sp in cached.values():
        object_id = str(sp.get("id") or "").strip()
        if object_id:
            by_id[object_id] = sp
    log.info(f"Primed resource service-principal cache from local Enterprise Applications export: {len(cached)} objects")


def _previous_snapshot_lookups(root: pathlib.Path, requested_ref: str) -> dict[str, PreviousSnapshotLookup]:
    """Per-category lookups into the previous snapshot, used to backfill unresolved enrichment."""
    repo_root = _discover_repo_root(root)
    snapshot_ref = _previous_snapshot_ref(repo_root, requested_ref) if repo_root else ""
    root_repo_rel = _repo_relative_posix(repo_root, root) if repo_root is not None else ""
    if repo_root is None or not snapshot_ref or not root_repo_rel:
        log.info("No previous snapshot reference found for resolution backfill; unresolved placeholders may cause drift noise.")
        return {}
    log.info(f"Using previous snapshot reference for resolution backfill: {snapshot_ref}")
    return {
        title: PreviousSnapshotLookup(
            repo_root=repo_root,
            ref=snapshot_ref,
            category_repo_dir=f"{root_repo_rel}/{title}".strip("/"),
        )
        for title in ("Enterprise Applications", "App Registrations")
    }


def main() -> int:
    args = parse_args()
    root = pathlib.Path(args.root).resolve()
    token = args.token.strip()
    if not token:
        raise SystemExit("Microsoft Graph token required (--token or GRAPH_TOKEN).")
    workers = max(1, min(int(args.enterprise_app_workers), 32))

    exports = [
        {"title": title, "rel_dir": title, "url": url}
        for flag, title, url in EXPORT_CATALOG
        if common.to_bool(getattr(args, flag))
    ]
    if not exports:
        log.info("All Entra export categories are disabled. Skipping Entra baseline export.")
        return 0

    client = GraphClient(token)
    resource_sp_by_appid: dict[str, dict[str, Any] | None] = {}
    resource_sp_by_id: dict[str, dict[str, Any] | None] = {}
    if common.to_bool(args.include_app_registrations):
        _prime_resource_sp_caches(root, resource_sp_by_appid, resource_sp_by_id)
    previous_lookup_by_title = _previous_snapshot_lookups(root, args.previous_snapshot_ref)
    local_org_by_id, warnings = _local_org_names(client)

    total_written = 0
    failed_exports: list[tuple[str, str]] = []
    for export in exports:
        title = export["title"]
        log.info(f"Starting export: {title}")
        items, error = client.get_collection(export["url"])
        if error:
            log.warning(f"unable to export {title} from {export['url']} ({error})")
            warnings += 1
            failed_exports.append((title, str(error)))
            continue

        if title == "Enterprise Applications":
            warnings += _enrich_enterprise_apps(
                items,
                client,
                resource_sp_by_appid,
                resource_sp_by_id,
                local_org_by_id,
                previous_lookup_by_title.get(title),
                workers,
            )
        elif title == "App Registrations":
            _enrich_app_registrations(items, client, resource_sp_by_appid, previous_lookup_by_title.get(title))

        written = write_collection(
            root=root,
            rel_dir=export["rel_dir"],
            title=title,
            items=items,
            source_url=export["url"],
        )
        total_written += written
        log.info(f"Exported {written} objects: {title}")

    if failed_exports and common.to_bool(args.fail_on_export_error):
        log.error("Entra baseline export failed because one or more requested categories could not be exported:")
        for title, error in failed_exports:
            log.error(f" - {title}: {error}")
        log.error("Requested category failures are treated as fatal to avoid committing a partial or stale backup snapshot.")
        return 2

    log.info(f"Entra baseline export complete. Total objects written: {total_written}. Warnings: {warnings}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
