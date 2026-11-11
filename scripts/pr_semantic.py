#!/usr/bin/env python3
"""Semantic diffing of policy JSON: assignment changes, key flattening and fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Any

VOLATILE_KEY_NAMES = {
    "id",
    "displayname",
    "assignments",
    "createddatetime",
    "lastmodifieddatetime",
    "modifieddatetime",
    "updateddatetime",
    "generateddatetime",
    "version",
    "@odata.context",
    "@odata.type",
    "@odata.etag",
    # Entra export enrichment metadata (operational, not desired config drift).
    "ownersresolved",
    "approleassignmentsresolved",
    "requiredresourceaccessresolved",
    "appownerorganizationresolved",
    "resolutionstatus",
}


ENTRA_ENRICHMENT_KEY_NAMES = {
    "ownersresolved",
    "approleassignmentsresolved",
    "requiredresourceaccessresolved",
    "appownerorganizationresolved",
    "resolutionstatus",
}


def _assignment_entries(payload: Any) -> list[dict[str, str]]:
    """Flat, string-only view of the assignments in a policy payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("assignments"), list):
        return []

    def text(source: dict[str, Any], key: str) -> str:
        return str(source.get(key, "") or "")

    entries: list[dict[str, str]] = []
    for assignment in payload["assignments"]:
        if not isinstance(assignment, dict):
            continue
        target = assignment.get("target", {})
        if not isinstance(target, dict):
            target = {}
        group_name = (
            text(target, "groupDisplayName")
            or text(target, "groupName")
            or text(target, "displayName")
            or text(assignment, "targetDisplayName")
        )
        entries.append(
            {
                "source": text(assignment, "source"),
                "intent": text(assignment, "intent"),
                "target_type": text(target, "@odata.type"),
                "group_id": text(target, "groupId"),
                "group_name": group_name,
                "collection_id": text(target, "collectionId"),
                "filter_type": text(target, "deviceAndAppManagementAssignmentFilterType"),
                "filter_id": text(target, "deviceAndAppManagementAssignmentFilterId"),
            }
        )
    return entries


def _assignment_group_label(entry: dict[str, str]) -> str:
    group_name = str(entry.get("group_name", "") or "").strip()
    group_id = str(entry.get("group_id", "") or "").strip()
    if group_name and group_id:
        return f"{group_name} ({group_id})"
    if group_name:
        return group_name
    if group_id:
        return group_id
    return "all"


def _assignment_signature(entry: dict[str, str]) -> str:
    parts = [
        f"type={entry.get('target_type', '') or 'n/a'}",
        f"group={_assignment_group_label(entry)}",
        f"collection={entry.get('collection_id', '') or 'n/a'}",
        f"intent={entry.get('intent', '') or 'n/a'}",
        f"source={entry.get('source', '') or 'n/a'}",
    ]
    filter_type = entry.get("filter_type", "") or "none"
    filter_id = entry.get("filter_id", "") or "none"
    parts.append(f"filter={filter_type}/{filter_id}")
    return "; ".join(parts)


def _is_exclusion_target_type(target_type: str) -> bool:
    lowered = str(target_type or "").strip().lower()
    return "exclusion" in lowered


def _normalized_assignment_signatures(payload: Any) -> list[str]:
    signatures = [_assignment_signature(entry) for entry in _assignment_entries(payload)]
    return sorted(signatures)


def _assignment_base_key(entry: dict[str, str]) -> tuple[str, str, str, str, str]:
    """Identity of an assignment target, ignoring its filter."""
    group_identity = str(entry.get("group_id", "") or "").strip()
    if not group_identity:
        group_identity = str(entry.get("group_name", "") or "").strip().casefold()
    return (
        entry.get("target_type", ""),
        group_identity,
        entry.get("collection_id", ""),
        entry.get("intent", ""),
        entry.get("source", ""),
    )


def _has_assignment_filter(entry: dict[str, str]) -> bool:
    return bool((entry.get("filter_type", "") or "").strip()) or bool((entry.get("filter_id", "") or "").strip())


def _filter_scope_hint(old_entry: dict[str, str], new_entry: dict[str, str]) -> str:
    old_has = _has_assignment_filter(old_entry)
    new_has = _has_assignment_filter(new_entry)
    if old_has and not new_has:
        return "scope likely broader (unfiltered base target)"
    if not old_has and new_has:
        return "scope likely narrower (filtered subset of base target)"
    old_type = (old_entry.get("filter_type", "") or "").strip().lower()
    new_type = (new_entry.get("filter_type", "") or "").strip().lower()
    if old_type != new_type:
        return "scope impact ambiguous (include/exclude semantics changed)"
    old_id = (old_entry.get("filter_id", "") or "").strip().lower()
    new_id = (new_entry.get("filter_id", "") or "").strip().lower()
    if old_id != new_id:
        return "scope impact ambiguous (different filter population)"
    return "scope impact ambiguous"


def _assignment_filter_changes(old_map: dict[Any, dict[str, str]], new_map: dict[Any, dict[str, str]]) -> list[str]:
    changes: list[str] = []
    for key in sorted(set(old_map) & set(new_map)):
        old_entry, new_entry = old_map[key], new_map[key]
        old_filter = (old_entry.get("filter_type", ""), old_entry.get("filter_id", ""))
        new_filter = (new_entry.get("filter_type", ""), new_entry.get("filter_id", ""))
        if old_filter == new_filter:
            continue
        target_label = old_entry.get("target_type", "") or "assignment"
        old_text = f"{old_filter[0] or 'none'}/{old_filter[1] or 'none'}"
        new_text = f"{new_filter[0] or 'none'}/{new_filter[1] or 'none'}"
        changes.append(
            f"assignment filter ({target_label}): {old_text} -> {new_text} "
            f"[{_filter_scope_hint(old_entry, new_entry)}]"
        )
    return changes


def _assignment_scope_summary(
    old_entries: list[dict[str, str]],
    new_entries: list[dict[str, str]],
    added_entries: list[dict[str, str]],
    removed_entries: list[dict[str, str]],
) -> str | None:
    """One line on whether the effective assignment scope got broader or narrower."""
    if old_entries and not new_entries:
        return "assignment scope: likely unassigned (all assignment targets removed)"
    if not old_entries and new_entries:
        return "assignment scope: newly assigned (targets added)"
    if bool(added_entries or removed_entries) and not (added_entries and removed_entries):
        # Only additions or only removals: exclusions narrow scope, includes broaden it (reversed for removals).
        adding = bool(added_entries)
        entries = added_entries if adding else removed_entries
        all_exclusions = all(_is_exclusion_target_type(e.get("target_type", "")) for e in entries)
        no_exclusions = all(not _is_exclusion_target_type(e.get("target_type", "")) for e in entries)
        if all_exclusions:
            return (
                "assignment scope: likely narrower (more exclusion targets)"
                if adding
                else "assignment scope: likely broader (fewer exclusion targets)"
            )
        if no_exclusions:
            return (
                "assignment scope: likely broader (more include/all targets)"
                if adding
                else "assignment scope: likely narrower (fewer include/all targets)"
            )
        return "assignment scope: ambiguous (mixed target semantics)"
    return None


def _describe_assignment_changes(old_payload: Any, new_payload: Any) -> list[str]:
    old_entries = _assignment_entries(old_payload)
    new_entries = _assignment_entries(new_payload)
    if old_entries == new_entries:
        return []

    old_map = {_assignment_base_key(entry): entry for entry in old_entries}
    new_map = {_assignment_base_key(entry): entry for entry in new_entries}
    changes = _assignment_filter_changes(old_map, new_map)
    # If only filter values changed on the same assignment targets, the
    # explicit filter diff is clearer than additional added/removed noise.
    if changes and set(old_map) == set(new_map):
        return changes

    old_set = set(_normalized_assignment_signatures(old_payload))
    new_set = set(_normalized_assignment_signatures(new_payload))
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    if added:
        changes.append(f"assignment targets added: {'; '.join(added[:2])}")
    if removed:
        changes.append(f"assignment targets removed: {'; '.join(removed[:2])}")

    old_by_sig = {_assignment_signature(entry): entry for entry in old_entries}
    new_by_sig = {_assignment_signature(entry): entry for entry in new_entries}
    scope = _assignment_scope_summary(
        old_entries,
        new_entries,
        [new_by_sig[sig] for sig in added if sig in new_by_sig],
        [old_by_sig[sig] for sig in removed if sig in old_by_sig],
    )
    if scope:
        changes.append(scope)
    elif added and removed:
        changes.append("assignment scope: ambiguous (target mix changed)")
    return changes


def _strip_entra_enrichment_fields(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if str(key).strip().lower() in ENTRA_ENRICHMENT_KEY_NAMES:
                continue
            cleaned[key] = _strip_entra_enrichment_fields(child)
        return cleaned
    if isinstance(value, list):
        return [_strip_entra_enrichment_fields(item) for item in value]
    return value


def _is_entra_enrichment_only_json_change(old_excerpt: str, new_excerpt: str) -> bool:
    if not old_excerpt or not new_excerpt:
        return False
    try:
        old_payload = json.loads(old_excerpt)
        new_payload = json.loads(new_excerpt)
    except Exception:
        return False
    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):
        return False

    old_stripped = _strip_entra_enrichment_fields(old_payload)
    new_stripped = _strip_entra_enrichment_fields(new_payload)
    if old_stripped != new_stripped:
        return False
    return old_payload != new_payload


def _summarize_app_security_fields(excerpt: str) -> str:
    """Summarize security-relevant fields in an App Registration or Enterprise Application."""
    signals: list[str] = []
    try:
        payload = json.loads(excerpt)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("requiredResourceAccess"):
        signals.append("requiredResourceAccess present")
    if payload.get("appRoles"):
        signals.append("appRoles present")
    if payload.get("passwordCredentials"):
        signals.append("passwordCredentials present")
    if payload.get("keyCredentials"):
        signals.append("keyCredentials present")
    web = payload.get("web", {}) if isinstance(payload.get("web"), dict) else {}
    if web.get("redirectUris"):
        signals.append("redirectUris present")
    if payload.get("identifierUris"):
        signals.append("identifierUris present")
    api = payload.get("api", {}) if isinstance(payload.get("api"), dict) else {}
    if api.get("preAuthorizedApplications"):
        signals.append("preAuthorizedApplications present")
    sign_in_audience = payload.get("signInAudience")
    if sign_in_audience and str(sign_in_audience) != "AzureADMyOrg":
        signals.append(f"signInAudience={sign_in_audience}")
    return "; ".join(signals)


def _is_volatile_key_name(key: str) -> bool:
    lowered = key.lower()
    # Metadata suffix/prefix variants are volatile too.
    return lowered in VOLATILE_KEY_NAMES or lowered.endswith("datetime") or lowered.startswith("@odata.")


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    text = str(value).strip()
    if not text:
        return '""'
    return text[:77] + "..." if len(text) > 80 else text


def _flatten_json(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested JSON into dotted keys, dropping volatile keys."""
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not _is_volatile_key_name(key):
                flat.update(_flatten_json(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(obj, list):
        # Include a lightweight content hash so list item edits (for example
        # assignment filters) are detected even when list length is unchanged.
        preview = json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        flat[prefix] = f"list[{len(obj)}]#{hashlib.sha256(preview.encode('utf-8')).hexdigest()[:8]}"
    else:
        flat[prefix] = obj
    return flat


def _extract_semantic_change(old_excerpt: str, new_excerpt: str, path: str = "") -> str:
    """
    Derive a semantic description of configuration changes by flattening JSON
    structures and comparing dotted-key paths. This gives more precise signals
    for nested policy settings such as Conditional Access conditions or
    Defender settings.
    """
    if not old_excerpt and new_excerpt:
        is_app_path = any(m in path.lower() for m in ("/app registrations/", "/enterprise applications/"))
        app_signals = _summarize_app_security_fields(new_excerpt) if is_app_path else ""
        return f"New configuration object added ({app_signals})" if app_signals else "New configuration object added"
    if old_excerpt and not new_excerpt:
        return "Configuration object removed"

    try:
        old_json = json.loads(old_excerpt) if old_excerpt else {}
        new_json = json.loads(new_excerpt) if new_excerpt else {}
        old_flat = _flatten_json(old_json)
        new_flat = _flatten_json(new_json)

        state_changes: list[str] = []
        value_changes: list[str] = []
        structure_changes: list[str] = []
        for key in sorted(set(old_flat) | set(new_flat)):
            old_val, new_val = old_flat.get(key), new_flat.get(key)
            if old_val == new_val:
                continue
            if key not in old_flat:
                structure_changes.append(f"{key} added")
            elif key not in new_flat:
                structure_changes.append(f"{key} removed")
            else:
                change_text = f"{key}: {_format_scalar(old_val)} -> {_format_scalar(new_val)}"
                is_state = key.lower().endswith(".state") or key.lower() == "state"
                (state_changes if is_state else value_changes).append(change_text)

        ordered = _describe_assignment_changes(old_json, new_json) + state_changes + value_changes + structure_changes
        return "; ".join(ordered[:8]) if ordered else "No semantic key changes detected"
    except Exception:
        return "Configuration content modified" if old_excerpt != new_excerpt else ""


def _policy_fingerprint(json_excerpt: str) -> str:
    """
    Generate a stable fingerprint for a policy configuration.

    Intune exports contain metadata (IDs, assignments, timestamps)
    that differ across tenants and exports. This function removes
    those fields and fingerprints only the configuration content.
    """

    if not json_excerpt:
        return ""

    def _sanitize(obj: Any):
        if isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                lowered = k.lower()
                if lowered == "assignments":
                    cleaned[k] = _normalized_assignment_signatures(obj)
                    continue
                if lowered in VOLATILE_KEY_NAMES or lowered.endswith("datetime") or lowered.startswith("@odata."):
                    continue
                cleaned[k] = _sanitize(v)
            return cleaned

        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]

        return obj

    try:
        parsed = json.loads(json_excerpt)

        # Prefer settings-like sections if present (common in Intune exports)
        for key in ("settings", "settingsDelta", "settingDefinitions", "configuration"):
            if isinstance(parsed, dict) and key in parsed:
                parsed = parsed[key]
                break

        sanitized = _sanitize(parsed)

        normalized = json.dumps(
            sanitized,
            sort_keys=True,
            separators=(",", ":"),
        )

    except Exception:
        normalized = json_excerpt.strip()

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
