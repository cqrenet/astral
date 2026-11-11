#!/usr/bin/env python3
"""Normalize Intune restore payloads for IntuneCD update.

Encodes payloadJson as base64, strips display-only assignment labels, resolves group IDs by name,
drops group targets without a valid ID. Env: RESTORE_PATH_ENV (folder), GRAPH_TOKEN (optional).
Shared by azure-pipelines-restore.yml and .github/workflows/astral-restore.yml.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import common

log = common.log

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
# Keys that survive on an assignment target; everything else is a display-only label.
ALLOWED_TARGET_KEYS = {
    "@odata.type",
    "groupId",
    "collectionId",
    "deviceAndAppManagementAssignmentFilterId",
    "deviceAndAppManagementAssignmentFilterType",
}
DROP_MARKER = "__drop_assignment_target__"

GroupResolver = Callable[[Any], "str | None"]


def is_guid(value: Any) -> bool:
    return bool(GUID_RE.match(str(value or "").strip()))


def make_group_resolver(graph_token: str) -> GroupResolver:
    """Resolve a group display name to its object id via Graph (cached; None when ambiguous or unknown)."""
    cache: dict[str, str | None] = {}

    def resolve(group_name: Any) -> str | None:
        name = str(group_name or "").strip()
        if not name or not graph_token:
            return None
        key = name.lower()
        if key in cache:
            return cache[key]
        odata_name = name.replace("'", "''")
        query = urllib.parse.urlencode({"$select": "id,displayName", "$filter": f"displayName eq '{odata_name}'"})
        request = urllib.request.Request(
            f"https://graph.microsoft.com/v1.0/groups?{query}",
            headers={"Authorization": f"Bearer {graph_token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            cache[key] = None
            return None

        values = payload.get("value", []) if isinstance(payload, dict) else []
        exact = [item for item in values if str(item.get("displayName", "")) == name and is_guid(item.get("id"))]
        if len(exact) == 1:
            cache[key] = exact[0]["id"]
        else:
            ids = [item.get("id") for item in values if is_guid(item.get("id"))]
            cache[key] = ids[0] if len(ids) == 1 else None
        return cache[key]

    return resolve


def _walk(node: Any, visit: Callable[[dict[str, Any]], int]) -> int:
    """Apply *visit* to every dict in the tree (parents before children); returns the summed counts."""
    total = 0
    if isinstance(node, dict):
        total += visit(node)
        for value in node.values():
            total += _walk(value, visit)
    elif isinstance(node, list):
        for item in node:
            total += _walk(item, visit)
    return total


def _clean_assignment_target(node: dict[str, Any], odata_type: str, resolve_group: GroupResolver) -> int:
    """Strip display labels from an assignment target, resolving a missing group id by name."""
    removed = 0
    is_group_target = "groupassignmenttarget" in odata_type
    if is_group_target and not is_guid(node.get("groupId")):
        group_name = node.get("groupName") or node.get("groupDisplayName") or node.get("displayName")
        resolved = resolve_group(group_name)
        if resolved:
            node["groupId"] = resolved
    for key in list(node.keys()):
        if not key.startswith("@odata.") and key not in ALLOWED_TARGET_KEYS:
            node.pop(key)
            removed += 1
    if is_group_target and not is_guid(node.get("groupId")):
        node[DROP_MARKER] = True  # pruned from the assignments list later
    return removed


def strip_assignment_display_labels(node: Any, resolve_group: GroupResolver = lambda name: None) -> int:
    """Remove display-only assignment labels everywhere; returns how many keys were removed."""

    def visit(item: dict[str, Any]) -> int:
        removed = 0
        odata_type = str(item.get("@odata.type", "") or "").lower()
        if "assignmenttarget" in odata_type:
            removed += _clean_assignment_target(item, odata_type, resolve_group)
        elif "groupId" in item:
            for key in ("groupDisplayName", "groupName", "displayName", "groupType"):
                if key in item:
                    item.pop(key)
                    removed += 1
        if "targetDisplayName" in item and isinstance(item.get("target"), dict):
            item.pop("targetDisplayName")
            removed += 1
        return removed

    return _walk(node, visit)


def prune_invalid_assignment_targets(node: Any) -> int:
    """Drop assignments whose target was marked for dropping; returns how many were removed."""

    def visit(item: dict[str, Any]) -> int:
        assignments = item.get("assignments")
        if not isinstance(assignments, list):
            return 0
        kept = [
            a
            for a in assignments
            if not (isinstance(a, dict) and isinstance(a.get("target"), dict) and a["target"].get(DROP_MARKER) is True)
        ]
        if len(kept) != len(assignments):
            item["assignments"] = kept
        return len(assignments) - len(kept)

    return _walk(node, visit)


def _pop_marker(item: dict[str, Any]) -> int:
    if DROP_MARKER in item:
        item.pop(DROP_MARKER)
        return 1
    return 0


def remove_internal_markers(node: Any) -> int:
    return _walk(node, _pop_marker)


def encode_payload_json(data: Any) -> bool:
    """IntuneCD expects payloadJson as a base64 string; backups may store a dict or list."""
    if not isinstance(data, dict) or not isinstance(data.get("payloadJson"), (dict, list)):
        return False
    raw = json.dumps(data["payloadJson"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    data["payloadJson"] = base64.b64encode(raw).decode("ascii")
    return True


def main() -> int:
    root = pathlib.Path(os.environ["RESTORE_PATH_ENV"]).resolve()
    if not root.exists():
        log.info(f"Restore source folder not found; payload normalization skipped: {root}")
        return 0

    resolve_group = make_group_resolver(os.environ.get("GRAPH_TOKEN", "").strip())
    normalized_payload_json = labels_removed = targets_removed = files_changed = 0
    for path in sorted(root.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        encoded = encode_payload_json(data)
        labels = strip_assignment_display_labels(data, resolve_group)
        dropped = prune_invalid_assignment_targets(data)
        markers = remove_internal_markers(data)

        normalized_payload_json += encoded
        labels_removed += labels
        targets_removed += dropped
        if encoded or labels or dropped or markers:
            path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
            files_changed += 1

    log.info(
        "Restore payload normalization complete: "
        f"filesChanged={files_changed}, "
        f"appConfigPayloadJsonNormalized={normalized_payload_json}, "
        f"assignmentDisplayLabelsRemoved={labels_removed}, "
        f"invalidAssignmentTargetsRemoved={targets_removed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
