#!/usr/bin/env python3
"""Generate broad object inventory CSV reports from backup JSON files."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


GROUP_TARGET_TYPES = {
    "#microsoft.graph.groupAssignmentTarget",
    "#microsoft.graph.exclusionGroupAssignmentTarget",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Path to the workload backup root (for example tenant-state/intune).")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where report files will be written.",
    )
    parser.add_argument(
        "--per-type-dir",
        default="Object Inventory",
        help="Directory name under output-dir for per-policy-type CSVs.",
    )
    return parser.parse_args()


def safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def slugify(value: str) -> str:
    text = safe_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def infer_intent(assignment: dict, target_type: str) -> str:
    if "exclusion" in target_type.lower():
        return "Exclude"
    explicit = safe_text(assignment.get("intent")).lower()
    if explicit in {"exclude"}:
        return "Exclude"
    return "Include"


def resolve_assignment_target(target: dict) -> str:
    target_type = safe_text(target.get("@odata.type"))
    if target_type == "#microsoft.graph.allDevicesAssignmentTarget":
        return "All devices"
    if target_type == "#microsoft.graph.allLicensedUsersAssignmentTarget":
        return "All users"
    if target_type in GROUP_TARGET_TYPES:
        return (
            safe_text(target.get("groupDisplayName"))
            or safe_text(target.get("groupName"))
            or safe_text(target.get("groupId"))
            or "Unresolved group"
        )
    return (
        safe_text(target.get("groupDisplayName"))
        or safe_text(target.get("groupName"))
        or safe_text(target.get("displayName"))
        or safe_text(target.get("id"))
        or "Unknown target"
    )


def summarize_assignments(payload: dict) -> dict[str, object]:
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        return {
            "state": "NotExported",
            "total": 0,
            "include_targets": "",
            "exclude_targets": "",
            "all_users_assigned": "false",
            "all_devices_assigned": "false",
        }

    include_targets: list[str] = []
    exclude_targets: list[str] = []
    all_users = False
    all_devices = False

    valid = [item for item in assignments if isinstance(item, dict)]
    for assignment in valid:
        target = assignment.get("target") if isinstance(assignment.get("target"), dict) else {}
        target_type = safe_text(target.get("@odata.type"))
        target_name = resolve_assignment_target(target)
        intent = infer_intent(assignment, target_type)
        if target_type == "#microsoft.graph.allLicensedUsersAssignmentTarget":
            all_users = True
        if target_type == "#microsoft.graph.allDevicesAssignmentTarget":
            all_devices = True
        if intent == "Exclude":
            exclude_targets.append(target_name)
        else:
            include_targets.append(target_name)

    state = "Assigned" if valid else "Unassigned"
    if assignments == []:
        state = "Unassigned"

    return {
        "state": state,
        "total": len(valid),
        "include_targets": "; ".join(sorted(set(include_targets))),
        "exclude_targets": "; ".join(sorted(set(exclude_targets))),
        "all_users_assigned": str(all_users).lower(),
        "all_devices_assigned": str(all_devices).lower(),
    }


def iter_rows(root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in {"reports"}:
            continue
        if "__archive__" in rel.parts:
            continue

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        summary = summarize_assignments(payload)
        policy_type = rel.parts[0] if rel.parts else ""
        category = "/".join(rel.parent.parts)
        object_name = safe_text(payload.get("displayName")) or safe_text(payload.get("name"))
        if not object_name:
            object_name = path.stem.split("__")[0]

        rows.append(
            {
                "PolicyType": policy_type,
                "Category": category,
                "ObjectName": object_name,
                "ObjectType": safe_text(payload.get("@odata.type")),
                "ObjectId": safe_text(payload.get("id")),
                "AppId": safe_text(payload.get("appId")),
                "Description": safe_text(payload.get("description")),
                "AssignmentState": safe_text(summary["state"]),
                "AssignmentCount": str(summary["total"]),
                "IncludeTargets": safe_text(summary["include_targets"]),
                "ExcludeTargets": safe_text(summary["exclude_targets"]),
                "AllUsersAssigned": safe_text(summary["all_users_assigned"]),
                "AllDevicesAssigned": safe_text(summary["all_devices_assigned"]),
                "SourceFile": rel.as_posix(),
            }
        )

    rows.sort(
        key=lambda row: (
            row["PolicyType"].lower(),
            row["Category"].lower(),
            row["ObjectName"].lower(),
            row["SourceFile"].lower(),
        )
    )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "PolicyType",
        "Category",
        "ObjectName",
        "ObjectType",
        "ObjectId",
        "AppId",
        "Description",
        "AssignmentState",
        "AssignmentCount",
        "IncludeTargets",
        "ExcludeTargets",
        "AllUsersAssigned",
        "AllDevicesAssigned",
        "SourceFile",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir).resolve()
    per_type_root = output_dir / args.per_type_dir

    if not root.exists():
        raise SystemExit(f"Backup path does not exist: {root}")

    rows = iter_rows(root)
    all_report = output_dir / "object-inventory-all.csv"
    write_csv(all_report, rows)

    per_type_counts: dict[str, int] = {}
    for policy_type in sorted({row["PolicyType"] for row in rows}):
        type_rows = [row for row in rows if row["PolicyType"] == policy_type]
        per_type_report = per_type_root / f"{slugify(policy_type)}-inventory.csv"
        write_csv(per_type_report, type_rows)
        per_type_counts[policy_type] = len(type_rows)

    print(
        f"Generated object inventory reports: all={all_report}, "
        f"perTypeCount={len(per_type_counts)}, rows={len(rows)}"
    )
    for policy_type, count in sorted(per_type_counts.items(), key=lambda item: item[0].lower()):
        print(f" - {policy_type}: {count} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
