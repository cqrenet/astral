#!/usr/bin/env python3
"""Change model, risk classification and deterministic summary for the PR review summary."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

import common

@dataclass
class ChangeItem:
    operation: str
    path: str
    risk_score: int
    risk_label: str
    reason: str
    policy_type: str
    severity: str
    old_path: str | None = None


def _risk_label(score: int) -> str:
    if score >= 3:
        return "HIGH"
    if score == 2:
        return "MEDIUM"
    return "LOW"


def _classify_policy_type(path: str) -> str:
    lp = path.lower()
    if "conditional access" in lp:
        return "conditional_access"
    if "device configurations" in lp or "settings catalog" in lp:
        return "device_configuration"
    if "compliance policies" in lp:
        return "compliance_policy"
    if "scripts" in lp:
        return "script"
    if "app configuration" in lp:
        return "app_configuration"
    if "app protection" in lp:
        return "app_protection"
    if "roles" in lp or "identity" in lp or "authentication" in lp:
        return "identity_security"
    return "other"


def _severity_from_change(operation: str, risk_score: int, policy_type: str) -> str:
    if operation == "Deleted" and risk_score >= 3:
        return "CRITICAL"
    if policy_type in ("conditional_access", "identity_security") and risk_score >= 3:
        return "HIGH"
    if risk_score == 3:
        return "HIGH"
    if risk_score == 2:
        return "MEDIUM"
    return "LOW"


def _classify_risk(path: str, operation: str, backup_folder: str, reports_subdir: str) -> tuple[int, str]:
    p = path.replace("\\", "/")
    lp = p.lower()

    if common.is_doc_like(p):
        return (1, "Documentation/report artifact")

    if lp.startswith(f"{backup_folder.lower()}/{reports_subdir.lower()}/") or "/assignment report/" in lp:
        return (1, "Generated report/documentation output")

    high_markers = [
        "/conditional access/",
        "/compliance policies/",
        "/device configurations/",
        "/settings catalog/",
        "/scripts/",
        "/entra/conditional-access/",
        "/entra/authentication-strengths/",
        "/entra/named-locations/",
        "/entra/app-registrations/",
        "/entra/enterprise-applications/",
        "/authentication/",
        "/identity protection/",
        "/roles/",
        "/privileged identity management/",
        "/admin units/",
    ]
    medium_markers = [
        "/applications/",
        "/app protection/",
        "/app configuration/",
        "/enrollment ",
        "/enrollment/",
        "/filters/",
        "/scope tags/",
        "/device management settings/",
        "/apple vpp tokens/",
        "/apple push notification/",
    ]

    score = 1
    reason = "Metadata or lower-impact configuration area"
    if any(marker in lp for marker in high_markers):
        score = 3
        reason = "Security or broad policy area"
    elif any(marker in lp for marker in medium_markers):
        score = 2
        reason = "Workload configuration area"

    if operation == "Deleted":
        score = min(3, score + 1)
        reason = f"{reason}; deletion increases impact"
    elif operation == "Renamed":
        score = min(3, score + 1)
        reason = f"{reason}; rename may hide semantic changes"

    script_suffixes = (".ps1", ".sh", ".mobileconfig", ".xml")
    if p.lower().endswith(script_suffixes):
        score = 3
        reason = "Script/payload change can have immediate device impact"

    return (score, reason)


def _parse_changes(diff_output: str, backup_folder: str, reports_subdir: str) -> list[ChangeItem]:
    op_map = {
        "A": "Added",
        "M": "Modified",
        "D": "Deleted",
        "R": "Renamed",
        "C": "Copied",
        "T": "TypeChanged",
        "U": "Unmerged",
        "X": "Unknown",
        "B": "Broken",
    }

    backup_root = backup_folder.strip().strip("/").lower()
    backup_prefix = f"{backup_root}/" if backup_root else ""

    def _is_policy_scope_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("/").lower()
        if not backup_prefix:
            return True
        return normalized.startswith(backup_prefix)

    changes: list[ChangeItem] = []
    for raw_line in diff_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        status_token = parts[0]
        status_code = status_token[0]
        operation = op_map.get(status_code, "Modified")
        old_path: str | None = None
        if operation == "Renamed" and len(parts) >= 3:
            old_path = parts[1]
            path = parts[2]
        else:
            path = parts[-1]
        in_scope = _is_policy_scope_path(path) or (old_path is not None and _is_policy_scope_path(old_path))
        if not in_scope:
            continue
        if common.is_doc_like(path) or common.is_report_like(path):
            continue
        risk_score, reason = _classify_risk(path, operation, backup_folder, reports_subdir)
        if old_path:
            old_risk_score, old_reason = _classify_risk(old_path, operation, backup_folder, reports_subdir)
            if old_risk_score > risk_score:
                risk_score = old_risk_score
                reason = old_reason
        policy_type = _classify_policy_type(path)
        severity = _severity_from_change(operation, risk_score, policy_type)
        changes.append(
            ChangeItem(
                operation=operation,
                path=path,
                risk_score=risk_score,
                risk_label=_risk_label(risk_score),
                reason=reason,
                policy_type=policy_type,
                severity=severity,
                old_path=old_path,
            )
        )
    return changes


def _changes_fingerprint(changes: list[ChangeItem]) -> str:
    canonical = sorted(
        f"{item.operation}\t{item.old_path or ''}\t{item.path}\t{item.risk_score}\t{item.policy_type}"
        for item in changes
    )
    payload = "\n".join(canonical)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def _ellipsize(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def _ellipsize_path(path: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(path) <= max_len:
        return path
    if max_len <= 6:
        return path[-max_len:]
    tail = max_len - 4
    return ".../" + path[-tail:]


def _build_deterministic_summary(
    changes: list[ChangeItem],
    drift_branch: str,
    baseline_branch: str,
    ignored_operational_count: int = 0,
) -> str:
    op_counter = Counter(item.operation for item in changes)
    risk_counter = Counter(item.risk_label for item in changes)
    overall_risk = _risk_label(max((item.risk_score for item in changes), default=1))
    changes_fingerprint = _changes_fingerprint(changes)

    lines: list[str] = []
    lines.append("### Change Metrics")
    lines.append(f"- **Scope:** `{drift_branch}` -> `{baseline_branch}`")
    lines.append(f"- **Changed Files:** **{len(changes)}**")
    if ignored_operational_count > 0:
        lines.append(f"- **Operational-Only Changes Ignored:** **{ignored_operational_count}**")
    lines.append(f"- **Change Fingerprint:** `{changes_fingerprint}`")
    lines.append("")
    lines.append("| **Operation** | **Count** |")
    lines.append("|---|---:|")
    wrote_operation_row = False
    for op in ("Added", "Modified", "Deleted", "Renamed", "Copied", "TypeChanged"):
        count = op_counter.get(op, 0)
        if count:
            lines.append(f"| {op} | {count} |")
            wrote_operation_row = True
    if not wrote_operation_row:
        lines.append("| (none) | 0 |")
    lines.append("")
    lines.append("| **Risk Level** | **Count** |")
    lines.append("|---|---:|")
    lines.append(f"| HIGH | {risk_counter.get('HIGH', 0)} |")
    lines.append(f"| MEDIUM | {risk_counter.get('MEDIUM', 0)} |")
    lines.append(f"| LOW | {risk_counter.get('LOW', 0)} |")
    lines.append("")
    lines.append(f"> **Overall Risk:** **{overall_risk}**")

    highlights = sorted(
        [item for item in changes if item.risk_score >= 2 and not common.is_doc_like(item.path)],
        key=lambda item: (
            -item.risk_score,
            0 if item.operation == "Deleted" else 1,
            item.path.lower(),
            item.operation,
        ),
    )[:8]

    if highlights:
        lines.append("")
        lines.append("### Top Risk Items")
        lines.append("| **Severity** | **Operation** | **Area** | **File** | **Why It Matters** |")
        lines.append("|---|---|---|---|---|")
        for item in highlights:
            severity = _md_cell(f"{item.severity} / {item.risk_label}")
            operation = _md_cell(item.operation)
            area = _md_cell(item.policy_type)
            file_path = _md_cell(_ellipsize_path(item.path, 120))
            reason = _md_cell(_ellipsize(item.reason, 80))
            lines.append(
                f"| {severity} | `{operation}` | `{area}` | `{file_path}` | {reason} |"
            )

    return "\n".join(lines)


def _fit_payload_budget(
    payload: dict[str, Any],
    sampled_changes: list[dict[str, Any]],
    max_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    trimmed = list(sampled_changes)
    truncated = False
    while True:
        probe = dict(payload)
        probe["sampled_changes"] = trimmed
        if len(json.dumps(probe, ensure_ascii=True)) <= max_bytes:
            return (trimmed, truncated)
        if not trimmed:
            return (trimmed, True)
        truncated = True
        # Drop in chunks to converge quickly on very large payloads.
        drop = max(1, len(trimmed) // 8)
        trimmed = trimmed[:-drop]


def _change_scope(changes: list[ChangeItem]) -> str:
    count = len(changes)
    if count <= 3:
        return "SMALL"
    if count <= 15:
        return "MODERATE"
    return "LARGE"


def _build_change_facts(changes: list[ChangeItem]) -> dict[str, Any]:
    op_counter = Counter(item.operation for item in changes)
    risk_counter = Counter(item.risk_label for item in changes)
    highest_risk = _risk_label(max((item.risk_score for item in changes), default=1))
    return {
        "total_changes": len(changes),
        "operations": dict(op_counter),
        "risk_counts": dict(risk_counter),
        "highest_risk": highest_risk,
    }


def _detect_hotspots(changes: list[ChangeItem]) -> list[str]:
    area_counter: Counter[str] = Counter()
    for item in changes:
        parts = [part for part in item.path.replace("\\", "/").split("/") if part]
        if not parts:
            continue

        area = parts[0]
        if len(parts) >= 3 and parts[1].lower() in {"intune", "entra"}:
            # tenant-state/<workload>/<area>/...
            area = parts[2]
        elif len(parts) >= 2:
            area = parts[1]

        area_counter[area.lower()] += 1
    return [area for area, _ in area_counter.most_common(5)]


def _classify_posture(changes: list[ChangeItem]) -> str:
    """
    Classify overall security posture effect of the change set.
    """
    high_risk_changes = [c for c in changes if c.risk_score >= 3]

    if not changes:
        return "cosmetic_change"

    if any(c.operation == "Deleted" and c.risk_score >= 3 for c in changes):
        return "potential_security_weakening"

    if high_risk_changes:
        return "security_relevant_change"

    if all(c.risk_score == 1 for c in changes):
        return "cosmetic_change"

    return "functional_configuration_change"
