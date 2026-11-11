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
import common

log = common.log

root = pathlib.Path(os.environ["RESTORE_PATH_ENV"]).resolve()
if not root.exists():
    log.info(f"Restore source folder not found; payload normalization skipped: {root}")
    raise SystemExit(0)

graph_token = os.environ.get("GRAPH_TOKEN", "").strip()
guid_re = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
group_id_cache = {}

def is_guid(value):
    return bool(guid_re.match(str(value or "").strip()))

def resolve_group_id(group_name):
    name = str(group_name or "").strip()
    if not name or not graph_token:
        return None

    cache_key = name.lower()
    if cache_key in group_id_cache:
        return group_id_cache[cache_key]

    filter_value = name.replace("'", "''")
    query = urllib.parse.urlencode(
        {
            "$select": "id,displayName",
            "$filter": f"displayName eq '{filter_value}'",
        }
    )
    url = f"https://graph.microsoft.com/v1.0/groups?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {graph_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        group_id_cache[cache_key] = None
        return None

    values = payload.get("value", []) if isinstance(payload, dict) else []
    exact = [
        item for item in values
        if str(item.get("displayName", "")) == name and is_guid(item.get("id"))
    ]
    if len(exact) == 1:
        resolved = exact[0]["id"]
        group_id_cache[cache_key] = resolved
        return resolved

    ids = [item.get("id") for item in values if is_guid(item.get("id"))]
    resolved = ids[0] if len(ids) == 1 else None
    group_id_cache[cache_key] = resolved
    return resolved

def strip_assignment_display_labels(node):
    removed = 0
    allowed_assignment_target_keys = {
        "@odata.type",
        "groupId",
        "collectionId",
        "deviceAndAppManagementAssignmentFilterId",
        "deviceAndAppManagementAssignmentFilterType",
    }
    if isinstance(node, dict):
        odata_type = str(node.get("@odata.type", "") or "").lower()
        is_assignment_target = "assignmenttarget" in odata_type

        if is_assignment_target:
            group_name = (
                node.get("groupName")
                or node.get("groupDisplayName")
                or node.get("displayName")
            )
            group_id = str(node.get("groupId", "") or "").strip()
            is_group_target = "groupassignmenttarget" in odata_type
            if is_group_target and not is_guid(group_id):
                resolved_group_id = resolve_group_id(group_name)
                if resolved_group_id:
                    node["groupId"] = resolved_group_id
                    group_id = resolved_group_id

            for key in list(node.keys()):
                if key.startswith("@odata."):
                    continue
                if key not in allowed_assignment_target_keys:
                    if key in node:
                        node.pop(key, None)
                        removed += 1

            # Keep only valid group assignment targets for update payload.
            if is_group_target and not is_guid(node.get("groupId")):
                node["__drop_assignment_target__"] = True
        elif "groupId" in node:
            for key in ("groupDisplayName", "groupName", "displayName", "groupType"):
                if key in node:
                    node.pop(key, None)
                    removed += 1

        if "targetDisplayName" in node and isinstance(node.get("target"), dict):
            node.pop("targetDisplayName", None)
            removed += 1

        for value in node.values():
            removed += strip_assignment_display_labels(value)
    elif isinstance(node, list):
        for item in node:
            removed += strip_assignment_display_labels(item)
    return removed

def prune_invalid_assignment_targets(node):
    removed = 0
    if isinstance(node, dict):
        assignments = node.get("assignments")
        if isinstance(assignments, list):
            filtered = []
            for assignment in assignments:
                target = assignment.get("target") if isinstance(assignment, dict) else None
                if isinstance(target, dict) and target.get("__drop_assignment_target__") is True:
                    removed += 1
                    continue
                filtered.append(assignment)
            if len(filtered) != len(assignments):
                node["assignments"] = filtered
        for value in node.values():
            removed += prune_invalid_assignment_targets(value)
    elif isinstance(node, list):
        for item in node:
            removed += prune_invalid_assignment_targets(item)
    return removed

def remove_internal_markers(node):
    removed = 0
    if isinstance(node, dict):
        if "__drop_assignment_target__" in node:
            node.pop("__drop_assignment_target__", None)
            removed += 1
        for value in node.values():
            removed += remove_internal_markers(value)
    elif isinstance(node, list):
        for item in node:
            removed += remove_internal_markers(item)
    return removed

normalized_payload_json = 0
sanitized_assignment_labels = 0
invalid_assignment_targets_removed = 0
files_changed = 0

for path in sorted(root.rglob("*.json")):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue

    file_changed = False

    # IntuneCD expects payloadJson as base64 string; backup may store dict/list.
    # Some exports can be list-root JSON, so only access payloadJson on dict roots.
    if isinstance(data, dict):
        payload = data.get("payloadJson")
        if isinstance(payload, (dict, list)):
            payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            data["payloadJson"] = base64.b64encode(payload_json).decode("ascii")
            normalized_payload_json += 1
            file_changed = True

    removed = strip_assignment_display_labels(data)
    if removed > 0:
        sanitized_assignment_labels += removed
        file_changed = True

    dropped = prune_invalid_assignment_targets(data)
    if dropped > 0:
        invalid_assignment_targets_removed += dropped
        file_changed = True

    # Clean up internal markers used by prune flow.
    if remove_internal_markers(data) > 0:
        file_changed = True

    if file_changed:
        path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        files_changed += 1

log.info(
    "Restore payload normalization complete: "
    f"filesChanged={files_changed}, "
    f"appConfigPayloadJsonNormalized={normalized_payload_json}, "
    f"assignmentDisplayLabelsRemoved={sanitized_assignment_labels}, "
    f"invalidAssignmentTargetsRemoved={invalid_assignment_targets_removed}"
)
