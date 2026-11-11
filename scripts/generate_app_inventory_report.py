#!/usr/bin/env python3
"""Generate a dedicated apps inventory CSV from Entra app exports."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from report_utils import safe_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Path to the Entra workload backup root (tenant-state/entra).")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where apps inventory report files will be written.",
    )
    parser.add_argument(
        "--output-name",
        default="apps-inventory.csv",
        help="Output CSV filename (default: apps-inventory.csv).",
    )
    return parser.parse_args()


def summarize_owners(owners: object) -> tuple[int, str]:
    if not isinstance(owners, list):
        return 0, ""

    labels: list[str] = []
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        label = (
            safe_text(owner.get("displayName"))
            or safe_text(owner.get("userPrincipalName"))
            or safe_text(owner.get("appId"))
            or safe_text(owner.get("id"))
            or "Unknown owner"
        )
        labels.append(label)

    return len(labels), "; ".join(labels)


def summarize_required_resource_access(entries: object) -> tuple[int, str]:
    if not isinstance(entries, list):
        return 0, ""

    summary: list[str] = []
    total_permissions = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resource_name = safe_text(entry.get("resourceDisplayName")) or "Unresolved resource"
        resource_app_id = safe_text(entry.get("resourceAppId"))
        permissions = entry.get("permissions")
        permission_labels: list[str] = []
        if isinstance(permissions, list):
            for permission in permissions:
                if not isinstance(permission, dict):
                    continue
                total_permissions += 1
                perm_type = safe_text(permission.get("type")) or "UnknownType"
                perm_label = (
                    safe_text(permission.get("value"))
                    or safe_text(permission.get("displayName"))
                    or safe_text(permission.get("id"))
                    or "UnknownPermission"
                )
                permission_labels.append(f"{perm_label} [{perm_type}]")

        resource_label = resource_name
        if resource_app_id:
            resource_label += f" ({resource_app_id})"
        if permission_labels:
            summary.append(f"{resource_label}: {', '.join(permission_labels)}")
        else:
            summary.append(resource_label)

    return total_permissions, "; ".join(summary)


def summarize_enterprise_app_role_assignments(entries: object) -> tuple[int, str]:
    if not isinstance(entries, list):
        return 0, ""

    summary: list[str] = []
    count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        count += 1
        resource_name = safe_text(entry.get("resourceDisplayName")) or "Unresolved resource"
        resource_id = safe_text(entry.get("resourceId"))
        role_name = (
            safe_text(entry.get("appRoleValue"))
            or safe_text(entry.get("appRoleDisplayName"))
            or safe_text(entry.get("appRoleId"))
            or "Default access"
        )
        label = resource_name
        if resource_id:
            label += f" ({resource_id})"
        summary.append(f"{label}: {role_name}")

    return count, "; ".join(summary)


def verified_publisher_label(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return (
        safe_text(value.get("displayName"))
        or safe_text(value.get("verifiedPublisherId"))
        or safe_text(value.get("addedDateTime"))
    )


def iter_exported_json(export_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not export_dir.exists():
        return []
    items: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(export_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append((path, payload))
    return items


FIELDNAMES = [
    "AppType",
    "DisplayName",
    "ObjectId",
    "AppId",
    "SignInAudience",
    "ServicePrincipalType",
    "AccountEnabled",
    "PublisherDomain",
    "PublisherName",
    "VerifiedPublisher",
    "CreatedDateTime",
    "OwnersCount",
    "OwnersResolved",
    "ResolvedPermissionCount",
    "ResolvedPermissions",
    "ResolvedAppRoleAssignmentCount",
    "ResolvedAppRoleAssignments",
    "SourceFile",
]


def _common_fields(app_type: str, source_path: Path, payload: dict[str, Any], root: Path) -> dict[str, str]:
    owner_count, owners = summarize_owners(payload.get("ownersResolved"))
    return {
        **dict.fromkeys(FIELDNAMES, ""),
        "AppType": app_type,
        "DisplayName": safe_text(payload.get("displayName")) or source_path.stem,
        "ObjectId": safe_text(payload.get("id")),
        "AppId": safe_text(payload.get("appId")),
        "VerifiedPublisher": verified_publisher_label(payload.get("verifiedPublisher")),
        "OwnersCount": str(owner_count),
        "OwnersResolved": owners,
        "ResolvedPermissionCount": "0",
        "ResolvedAppRoleAssignmentCount": "0",
        "SourceFile": source_path.relative_to(root).as_posix(),
    }


def app_registration_row(source_path: Path, payload: dict[str, Any], root: Path) -> dict[str, str]:
    perm_count, permissions = summarize_required_resource_access(payload.get("requiredResourceAccessResolved"))
    return {
        **_common_fields("AppRegistration", source_path, payload, root),
        "SignInAudience": safe_text(payload.get("signInAudience")),
        "PublisherDomain": safe_text(payload.get("publisherDomain")),
        "CreatedDateTime": safe_text(payload.get("createdDateTime")),
        "ResolvedPermissionCount": str(perm_count),
        "ResolvedPermissions": permissions,
    }


def enterprise_app_row(source_path: Path, payload: dict[str, Any], root: Path) -> dict[str, str]:
    assignment_count, assignments = summarize_enterprise_app_role_assignments(payload.get("appRoleAssignmentsResolved"))
    return {
        **_common_fields("EnterpriseApplication", source_path, payload, root),
        "ServicePrincipalType": safe_text(payload.get("servicePrincipalType")),
        "AccountEnabled": safe_text(payload.get("accountEnabled")),
        "PublisherName": safe_text(payload.get("publisherName")),
        "ResolvedAppRoleAssignmentCount": str(assignment_count),
        "ResolvedAppRoleAssignments": assignments,
    }


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_path = output_dir / args.output_name

    if not root.exists():
        raise SystemExit(f"Backup path does not exist: {root}")

    app_reg_items = iter_exported_json(root / "App Registrations")
    ent_app_items = iter_exported_json(root / "Enterprise Applications")
    rows = [app_registration_row(path, payload, root) for path, payload in app_reg_items]
    rows += [enterprise_app_row(path, payload, root) for path, payload in ent_app_items]
    rows.sort(key=lambda row: (row["AppType"].lower(), row["DisplayName"].lower(), row["ObjectId"].lower()))

    output_dir.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(
        "Generated apps inventory report: "
        + f"{output_path} "
        + f"(rows={len(rows)}, appRegistrations={len(app_reg_items)}, enterpriseApps={len(ent_app_items)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
