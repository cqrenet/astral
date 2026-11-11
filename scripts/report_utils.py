#!/usr/bin/env python3
"""Helpers shared by the generate_*_report scripts."""

from __future__ import annotations

GROUP_TARGET_TYPES = {
    "#microsoft.graph.groupAssignmentTarget",
    "#microsoft.graph.exclusionGroupAssignmentTarget",
}


def safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def resolve_assignment_target(target: dict) -> str:
    """Human-readable label for an Intune assignment target."""
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
