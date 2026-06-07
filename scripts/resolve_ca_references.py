#!/usr/bin/env python3
"""Resolve Conditional Access GUID references to display names in backup JSON."""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request


SPECIAL_APP_IDS = {
    "All": "All applications",
    "None": "None",
    "Office365": "Office 365",
    "MicrosoftAdminPortals": "Microsoft Admin Portals",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Path to workload backup root (for Entra: tenant-state/entra).")
    parser.add_argument("--token", required=True, help="Microsoft Graph access token.")
    parser.add_argument(
        "--groups-root",
        default="",
        help=(
            "Optional path to the exported Groups directory (tenant-state/entra/Groups). "
            "When provided, group names are resolved from the local snapshot before falling "
            "back to the Graph API, avoiding redundant API calls."
        ),
    )
    return parser.parse_args()


class GraphResolver:
    def __init__(self, token: str):
        self.token = token.strip()
        self.group_cache: dict[str, str | None] = {}
        self.role_cache: dict[str, str | None] = {}
        self.app_cache: dict[str, str | None] = {}
        self.location_cache: dict[str, str | None] = {}
        self.auth_strength_cache: dict[str, str | None] = {}
        self._warned: set[str] = set()

    def load_groups_from_snapshot(self, groups_root: pathlib.Path) -> int:
        """Pre-populate group_cache from the Groups snapshot directory.

        Returns the number of groups loaded. Groups found on disk are used as-is;
        the Graph API is only called for group IDs not present in the snapshot.
        """
        if not groups_root.is_dir():
            return 0
        loaded = 0
        for path in sorted(groups_root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            gid = str(data.get("id") or "").strip()
            display_name = str(data.get("displayName") or "").strip() or None
            if gid:
                self.group_cache[gid] = display_name
                loaded += 1
        return loaded

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        print(f"Warning: {message}")

    def _get(self, url: str) -> dict | None:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            self._warn_once(url, f"Graph lookup failed for {url} (HTTP {exc.code})")
            return None
        except Exception as exc:  # noqa: BLE001
            self._warn_once(url, f"Graph lookup failed for {url} ({exc})")
            return None

    def group_name(self, group_id: str) -> str | None:
        if group_id in self.group_cache:
            return self.group_cache[group_id]
        url = (
            "https://graph.microsoft.com/v1.0/groups/"
            + urllib.parse.quote(group_id)
            + "?$select=id,displayName"
        )
        payload = self._get(url)
        name = payload.get("displayName") if isinstance(payload, dict) else None
        self.group_cache[group_id] = name
        return name

    def role_name(self, role_template_id: str) -> str | None:
        if role_template_id in self.role_cache:
            return self.role_cache[role_template_id]
        url = (
            "https://graph.microsoft.com/v1.0/directoryRoleTemplates/"
            + urllib.parse.quote(role_template_id)
            + "?$select=id,displayName"
        )
        payload = self._get(url)
        name = payload.get("displayName") if isinstance(payload, dict) else None
        self.role_cache[role_template_id] = name
        return name

    def app_name(self, app_or_object_id: str) -> str | None:
        if app_or_object_id in SPECIAL_APP_IDS:
            return SPECIAL_APP_IDS[app_or_object_id]
        if app_or_object_id in self.app_cache:
            return self.app_cache[app_or_object_id]

        # CA app conditions usually use appId; try appId lookup first.
        url = (
            "https://graph.microsoft.com/v1.0/servicePrincipals"
            + "?$select=id,appId,displayName"
            + "&$top=1"
            + "&$filter=appId%20eq%20'"
            + urllib.parse.quote(app_or_object_id)
            + "'"
        )
        payload = self._get(url)
        name = None
        if isinstance(payload, dict):
            value = payload.get("value")
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    name = first.get("displayName")
        if not name:
            # Fallback: treat value as service principal object id.
            by_id_url = (
                "https://graph.microsoft.com/v1.0/servicePrincipals/"
                + urllib.parse.quote(app_or_object_id)
                + "?$select=id,appId,displayName"
            )
            by_id = self._get(by_id_url)
            if isinstance(by_id, dict):
                name = by_id.get("displayName")
        self.app_cache[app_or_object_id] = name
        return name

    def location_name(self, location_id: str) -> str | None:
        if location_id in self.location_cache:
            return self.location_cache[location_id]
        if location_id in {"All", "AllTrusted"}:
            name = "All locations" if location_id == "All" else "All trusted locations"
            self.location_cache[location_id] = name
            return name
        url = (
            "https://graph.microsoft.com/v1.0/identity/conditionalAccess/namedLocations/"
            + urllib.parse.quote(location_id)
            + "?$select=id,displayName"
        )
        payload = self._get(url)
        name = payload.get("displayName") if isinstance(payload, dict) else None
        self.location_cache[location_id] = name
        return name

    def auth_strength_name(self, auth_strength_id: str) -> str | None:
        if auth_strength_id in self.auth_strength_cache:
            return self.auth_strength_cache[auth_strength_id]
        url = (
            "https://graph.microsoft.com/beta/identity/conditionalAccess/authenticationStrength/policies/"
            + urllib.parse.quote(auth_strength_id)
            + "?$select=id,displayName"
        )
        payload = self._get(url)
        name = payload.get("displayName") if isinstance(payload, dict) else None
        self.auth_strength_cache[auth_strength_id] = name
        return name


def resolve_id_list(
    values: list,
    lookup_fn,
) -> list[dict[str, str]]:
    resolved: list[dict[str, str]] = []
    for raw in values:
        if not isinstance(raw, str) or not raw:
            continue
        resolved.append(
            {
                "id": raw,
                "displayName": lookup_fn(raw) or "Unresolved",
            }
        )
    return resolved


def main() -> int:
    args = parse_args()
    root = pathlib.Path(args.root).resolve()
    token = args.token.strip()

    if not token:
        print("No Graph token provided. Skipping Conditional Access reference enrichment.")
        return 0

    ca_dir = root / "Conditional Access"
    if not ca_dir.exists():
        print(f"Conditional Access folder not found at {ca_dir}. Skipping.")
        return 0

    resolver = GraphResolver(token)

    # Pre-populate group cache from the Groups snapshot (written by export_entra_identity.py)
    # to avoid duplicate Graph API calls for groups already exported.
    groups_root_raw = str(args.groups_root or "").strip()
    if groups_root_raw:
        groups_root = pathlib.Path(groups_root_raw).resolve()
    else:
        groups_root = root / "Groups"
    if groups_root.is_dir():
        loaded = resolver.load_groups_from_snapshot(groups_root)
        if loaded:
            print(f"Pre-populated group cache from snapshot: {loaded} group(s) loaded from {groups_root}")

    updated_files = 0
    processed_files = 0

    for file_path in sorted(ca_dir.glob("*.json")):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        processed_files += 1
        changed = False

        conditions = payload.get("conditions")
        if not isinstance(conditions, dict):
            conditions = {}

        users = conditions.get("users")
        if isinstance(users, dict):
            for key, lookup in (
                ("includeGroups", resolver.group_name),
                ("excludeGroups", resolver.group_name),
                ("includeRoles", resolver.role_name),
                ("excludeRoles", resolver.role_name),
            ):
                value = users.get(key)
                if isinstance(value, list):
                    resolved_key = f"{key}Resolved"
                    resolved_value = resolve_id_list(value, lookup)
                    if users.get(resolved_key) != resolved_value:
                        users[resolved_key] = resolved_value
                        changed = True

        apps = conditions.get("applications")
        if isinstance(apps, dict):
            for key in ("includeApplications", "excludeApplications"):
                value = apps.get(key)
                if isinstance(value, list):
                    resolved_key = f"{key}Resolved"
                    resolved_value = resolve_id_list(value, resolver.app_name)
                    if apps.get(resolved_key) != resolved_value:
                        apps[resolved_key] = resolved_value
                        changed = True

        locations = conditions.get("locations")
        if isinstance(locations, dict):
            for key in ("includeLocations", "excludeLocations"):
                value = locations.get(key)
                if isinstance(value, list):
                    resolved_key = f"{key}Resolved"
                    resolved_value = resolve_id_list(value, resolver.location_name)
                    if locations.get(resolved_key) != resolved_value:
                        locations[resolved_key] = resolved_value
                        changed = True

        grant_controls = payload.get("grantControls")
        if isinstance(grant_controls, dict):
            auth_strength = grant_controls.get("authenticationStrength")
            if isinstance(auth_strength, dict):
                auth_strength_id = auth_strength.get("id")
                if isinstance(auth_strength_id, str) and auth_strength_id:
                    resolved = {
                        "id": auth_strength_id,
                        "displayName": resolver.auth_strength_name(auth_strength_id) or "Unresolved",
                    }
                    if grant_controls.get("authenticationStrengthResolved") != resolved:
                        grant_controls["authenticationStrengthResolved"] = resolved
                        changed = True

        if changed:
            file_path.write_text(json.dumps(payload, indent=5, ensure_ascii=False) + "\n", encoding="utf-8")
            updated_files += 1

    print(
        "Conditional Access GUID enrichment complete. "
        + f"Processed files: {processed_files}. "
        + f"Updated files: {updated_files}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
