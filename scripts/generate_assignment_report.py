#!/usr/bin/env python3
"""Generate a policy assignment inventory report from Intune backup JSON files."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


GROUP_TARGET_TYPES = {
    "#microsoft.graph.groupAssignmentTarget",
    "#microsoft.graph.exclusionGroupAssignmentTarget",
}

DEFAULT_POLICY_TYPES = {
    "app configuration",
    "app protection",
    "applications",
    "compliance policies",
    "conditional access",
    "device configurations",
    "enrollment configurations",
    "enrollment profiles",
    "filters",
    "scripts",
    "settings catalog",
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
        "--policy-type",
        action="append",
        default=[],
        help=(
            "Optional filter for policy type (top-level backup folder name). "
            "Repeat the flag or pass a comma-separated list."
        ),
    )
    parser.add_argument(
        "--graph-type",
        action="append",
        default=[],
        help=(
            "Optional filter for Graph @odata.type values. "
            "Repeat the flag or pass a comma-separated list."
        ),
    )
    return parser.parse_args()


@dataclass
class AssignmentRow:
    category: str
    policy_type: str
    object_name: str
    object_type: str
    assignment_state: str
    assignment_count: int
    intent: str
    assignment_target: str
    target_type: str
    assignment_filter: str
    filter_type: str
    source_file: str


def safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_intent(intent: str) -> str:
    normalized = safe_text(intent).lower()
    if normalized in {"apply", "include"}:
        return "Include"
    if normalized in {"exclude"}:
        return "Exclude"
    if not normalized:
        return "Include"
    return normalized.capitalize()


def infer_intent(assignment: dict, target_type: str) -> str:
    target_type_lower = safe_text(target_type).lower()
    if "exclusion" in target_type_lower:
        return "Exclude"
    explicit = safe_text(assignment.get("intent"))
    if explicit:
        return normalize_intent(explicit)
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


def escape_md_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def parse_filter_values(raw_values: list[str]) -> set[str]:
    values = set()
    for raw in raw_values:
        for item in safe_text(raw).split(","):
            normalized = safe_text(item)
            if normalized:
                values.add(normalized.lower())
    return values


def iter_assignment_rows(
    root: Path,
    policy_type_filter: set[str],
    graph_type_filter: set[str],
) -> Iterable[AssignmentRow]:
    excluded_categories = {
        "App Registrations",
        "Enterprise Applications",
    }
    for path in sorted(root.rglob("*.json")):
        try:
            rel_path = path.relative_to(root)
        except ValueError:
            continue

        if rel_path.parts and rel_path.parts[0] in {"reports"}:
            continue
        if "__archive__" in rel_path.parts:
            continue

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        object_name = safe_text(payload.get("displayName")) or safe_text(payload.get("name"))
        if not object_name:
            object_name = path.stem.split("__")[0]
        object_type = safe_text(payload.get("@odata.type"))
        category = "/".join(rel_path.parent.parts)
        policy_type = rel_path.parts[0] if rel_path.parts else ""

        if any(
            category == excluded or category.startswith(f"{excluded}/")
            for excluded in excluded_categories
        ):
            continue

        if policy_type_filter and policy_type.lower() not in policy_type_filter:
            continue
        if graph_type_filter and object_type.lower() not in graph_type_filter:
            continue

        assignments = payload.get("assignments")
        if not isinstance(assignments, list):
            yield AssignmentRow(
                category=category,
                policy_type=policy_type,
                object_name=object_name,
                object_type=object_type,
                assignment_state="NotExported",
                assignment_count=0,
                intent="None",
                assignment_target="Not exported in backup",
                target_type="",
                assignment_filter="",
                filter_type="",
                source_file=rel_path.as_posix(),
            )
            continue

        if not assignments:
            yield AssignmentRow(
                category=category,
                policy_type=policy_type,
                object_name=object_name,
                object_type=object_type,
                assignment_state="Unassigned",
                assignment_count=0,
                intent="None",
                assignment_target="No assignments",
                target_type="",
                assignment_filter="",
                filter_type="",
                source_file=rel_path.as_posix(),
            )
            continue

        assignment_count = len([item for item in assignments if isinstance(item, dict)])
        if assignment_count == 0:
            yield AssignmentRow(
                category=category,
                policy_type=policy_type,
                object_name=object_name,
                object_type=object_type,
                assignment_state="Unassigned",
                assignment_count=0,
                intent="None",
                assignment_target="No assignments",
                target_type="",
                assignment_filter="",
                filter_type="",
                source_file=rel_path.as_posix(),
            )
            continue

        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            target = assignment.get("target") if isinstance(assignment.get("target"), dict) else {}
            target_type = safe_text(target.get("@odata.type"))
            intent = infer_intent(assignment, target_type)
            assignment_target = resolve_assignment_target(target)
            assignment_filter = safe_text(target.get("deviceAndAppManagementAssignmentFilterId"))
            filter_type = safe_text(target.get("deviceAndAppManagementAssignmentFilterType"))
            yield AssignmentRow(
                category=category,
                policy_type=policy_type,
                object_name=object_name,
                object_type=object_type,
                assignment_state="Assigned",
                assignment_count=assignment_count,
                intent=intent,
                assignment_target=assignment_target,
                target_type=target_type,
                assignment_filter=assignment_filter,
                filter_type=filter_type,
                source_file=rel_path.as_posix(),
            )


def write_csv(rows: list[AssignmentRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
                [
                    "Category",
                    "PolicyType",
                    "ObjectName",
                    "ObjectType",
                    "AssignmentState",
                    "AssignmentCount",
                    "Intent",
                    "AssignmentTarget",
                    "TargetType",
                "AssignmentFilter",
                "FilterType",
                "SourceFile",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.category,
                    row.policy_type,
                    row.object_name,
                    row.object_type,
                    row.assignment_state,
                    row.assignment_count,
                    row.intent,
                    row.assignment_target,
                    row.target_type,
                    row.assignment_filter,
                    row.filter_type,
                    row.source_file,
                ]
            )


def write_markdown(rows: list[AssignmentRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    objects = {(row.category, row.object_name, row.source_file) for row in rows}
    assigned_objects = {
        (row.category, row.object_name, row.source_file)
        for row in rows
        if row.assignment_state == "Assigned"
    }
    unassigned_objects = {
        (row.category, row.object_name, row.source_file)
        for row in rows
        if row.assignment_state == "Unassigned"
    }
    not_exported_objects = {
        (row.category, row.object_name, row.source_file)
        for row in rows
        if row.assignment_state == "NotExported"
    }
    policy_type_counts = {}
    for row in rows:
        key = row.policy_type or "Unknown"
        policy_type_counts[key] = policy_type_counts.get(key, 0) + 1

    with output_path.open("w", encoding="utf-8") as handle:
        handle.write("# Policy Assignment Inventory Report\n\n")
        handle.write(f"Generated: `{generated}`\n\n")
        handle.write(f"- Total objects in report: **{len(objects)}**\n")
        handle.write(f"- Objects with assignments: **{len(assigned_objects)}**\n")
        handle.write(f"- Objects without assignments: **{len(unassigned_objects)}**\n")
        handle.write(f"- Objects with assignment field not exported: **{len(not_exported_objects)}**\n")
        handle.write(f"- Total rows: **{len(rows)}**\n\n")
        handle.write("## Rows by policy type\n\n")
        handle.write("| Policy Type | Rows |\n")
        handle.write("|---|---|\n")
        for policy_type, count in sorted(policy_type_counts.items(), key=lambda item: item[0].lower()):
            handle.write(f"| {escape_md_cell(policy_type)} | {count} |\n")
        handle.write("\n")
        handle.write(
            "| Policy Type | Category | Object | Object Type | Assignment State | Assignment Count | Intent | Assignment Target | Target Type | Filter | Filter Type | Source |\n"
        )
        handle.write("|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for row in rows:
            handle.write(
                "| "
                + " | ".join(
                    [
                        escape_md_cell(row.policy_type),
                        escape_md_cell(row.category),
                        escape_md_cell(row.object_name),
                        escape_md_cell(row.object_type),
                        escape_md_cell(row.assignment_state),
                        escape_md_cell(str(row.assignment_count)),
                        escape_md_cell(row.intent),
                        escape_md_cell(row.assignment_target),
                        escape_md_cell(row.target_type),
                        escape_md_cell(row.assignment_filter),
                        escape_md_cell(row.filter_type),
                        escape_md_cell(row.source_file),
                    ]
                )
                + " |\n"
            )


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir).resolve()
    policy_type_filter = parse_filter_values(args.policy_type)
    graph_type_filter = parse_filter_values(args.graph_type)
    using_default_policy_scope = False

    if not policy_type_filter:
        policy_type_filter = set(DEFAULT_POLICY_TYPES)
        using_default_policy_scope = True

    if not root.exists():
        raise SystemExit(f"Backup path does not exist: {root}")

    rows = sorted(
        iter_assignment_rows(root, policy_type_filter, graph_type_filter),
        key=lambda x: (
            x.policy_type.lower(),
            x.category.lower(),
            x.object_name.lower(),
            x.assignment_state,
            x.intent.lower(),
            x.assignment_target.lower(),
        ),
    )

    markdown_path = output_dir / "policy-assignments.md"
    csv_path = output_dir / "policy-assignments.csv"
    write_markdown(rows, markdown_path)
    write_csv(rows, csv_path)

    print(
        f"Generated assignment report with {len(rows)} rows: "
        f"{markdown_path} and {csv_path}"
    )
    if using_default_policy_scope:
        print(
            "Applied default policy scope: "
            + ", ".join(sorted(DEFAULT_POLICY_TYPES))
        )
    elif policy_type_filter:
        print(f"Applied policy type filter: {', '.join(sorted(policy_type_filter))}")
    if graph_type_filter:
        print(f"Applied graph type filter: {', '.join(sorted(graph_type_filter))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
