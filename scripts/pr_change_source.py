#!/usr/bin/env python3
"""Scores whether drift looks admin-driven or platform (infrastructure) driven."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pr_changes import ChangeItem
from pr_i18n import _t

_ADMIN_POLICY_TYPES = {
    "conditional_access",
    "device_configuration",
    "compliance_policy",
    "script",
    "app_configuration",
    "app_protection",
    "identity_security",
}


_ADMIN_PATH_MARKERS = (
    "/conditional access/",
    "/named locations/",
    "/authentication strengths/",
    "/device configurations/",
    "/settings catalog/",
    "/compliance policies/",
    "/scripts/",
    "/app configuration/",
    "/app protection/",
    "/filters/",
    "/scope tags/",
    "/device management settings/",
    "/apple vpp tokens/",
    "/apple push notification/",
    "/roles/",
    "/identity protection/",
    "/privileged identity management/",
    "/admin units/",
    "/intune/applications/",
)


_ASSIGNMENT_SEMANTIC_TOKENS = (
    "assignment filter",
    "assignment scope",
    "assignment targets",
    "newly assigned",
    "unassigned",
    "scope likely broader",
    "scope likely narrower",
    "include targets",
    "exclude targets",
)


_RESOLVER_CHURN_TOKENS = (
    "resolutionstatus",
    "ownersresolved",
    "approleassignmentsresolved",
    "requiredresourceaccessresolved",
    "appownerorganizationresolved",
    "unresolved",
)


_APP_SECURITY_TOKENS = (
    "requiredresourceaccess",
    "approles",
    "oauth2permissionscopes",
    "passwordcredentials",
    "keycredentials",
    "redirecturis",
    "identifieruris",
    "preauthorizedapplications",
    "signinaudience",
)


_ENTERPRISE_APPS = "/enterprise applications/"


_APP_REGISTRATIONS = "/app registrations/"


@dataclass(frozen=True)
class _SourceSignal:
    """One piece of evidence: when fires(item, path, semantic, semantic_lower), add score to `side`."""

    side: str  # "admin" | "infrastructure"
    score: int
    reason: str
    fires: Callable[[ChangeItem, str, str, str], bool]


def _is_identity_app_path(lp: str) -> bool:
    return _ENTERPRISE_APPS in lp or _APP_REGISTRATIONS in lp


# Order matters: it decides which reasons are shown first.
_SOURCE_SIGNALS: tuple[_SourceSignal, ...] = (
    _SourceSignal(
        "admin", 3, "Policy/control workload usually reflects tenant-admin intent",
        lambda item, lp, sem, low: item.policy_type in _ADMIN_POLICY_TYPES,
    ),
    _SourceSignal(
        "admin", 2, "Changed path is typically tenant-managed configuration",
        lambda item, lp, sem, low: any(marker in lp for marker in _ADMIN_PATH_MARKERS),
    ),
    _SourceSignal(
        "admin", 2, "Enrollment settings are typically administered intentionally",
        lambda item, lp, sem, low: "/enrollment " in lp or "/enrollment/" in lp,
    ),
    _SourceSignal(
        "admin", 3, "Assignment/targeting semantics changed",
        lambda item, lp, sem, low: any(token in low for token in _ASSIGNMENT_SEMANTIC_TOKENS),
    ),
    _SourceSignal(
        "infrastructure", 3, "Enterprise application inventory often contains platform-managed object churn",
        lambda item, lp, sem, low: _ENTERPRISE_APPS in lp,
    ),
    _SourceSignal(
        "infrastructure", 2, "App registration inventory can include service/platform object churn",
        lambda item, lp, sem, low: _APP_REGISTRATIONS in lp and _ENTERPRISE_APPS not in lp,
    ),
    _SourceSignal(
        "infrastructure", 1, "New application identity objects may be introduced automatically by the platform",
        lambda item, lp, sem, low: item.operation == "Added" and _is_identity_app_path(lp),
    ),
    _SourceSignal(
        "infrastructure", 2, "No semantic setting delta detected despite file drift",
        lambda item, lp, sem, low: low == "no semantic key changes detected",
    ),
    _SourceSignal(
        "infrastructure", 2, "Semantic diff resembles resolver/metadata churn",
        lambda item, lp, sem, low: any(token in low for token in _RESOLVER_CHURN_TOKENS),
    ),
    _SourceSignal(
        "infrastructure", 1, "Object lifecycle changes in identity app inventory may be platform-driven",
        lambda item, lp, sem, low: sem in {"New configuration object added", "Configuration object removed"}
        and _is_identity_app_path(lp),
    ),
    # Security-relevant app identity changes should not be treated as pure infrastructure drift.
    _SourceSignal(
        "admin", 4,
        "Application identity shows permission, credential, or scope changes indicating tenant security impact",
        lambda item, lp, sem, low: _is_identity_app_path(lp) and any(token in low for token in _APP_SECURITY_TOKENS),
    ),
)


def _classify_change_source(item: ChangeItem, semantic_change: str = "") -> dict[str, Any]:
    """Score whether a change looks admin-driven or platform (infrastructure) driven."""
    lp = item.path.lower().replace("\\", "/")
    semantic = (semantic_change or "").strip()
    semantic_lower = semantic.lower()

    scores = {"admin": 0, "infrastructure": 0}
    reasons: dict[str, list[str]] = {"admin": [], "infrastructure": []}
    for signal in _SOURCE_SIGNALS:
        if signal.fires(item, lp, semantic, semantic_lower):
            scores[signal.side] += signal.score
            if signal.reason not in reasons[signal.side]:
                reasons[signal.side].append(signal.reason)
    admin_score, infrastructure_score = scores["admin"], scores["infrastructure"]
    admin_reasons, infrastructure_reasons = reasons["admin"], reasons["infrastructure"]

    if admin_score == 0 and infrastructure_score == 0:
        label = "mixed_or_uncertain"
        reason_list = ["Insufficient deterministic evidence to attribute source"]
    elif admin_score >= infrastructure_score + 2:
        label = "likely_admin_driven"
        reason_list = admin_reasons[:3] or ["Deterministic signals favor tenant-admin intent"]
    elif infrastructure_score >= admin_score + 2:
        label = "likely_infrastructure_driven"
        reason_list = infrastructure_reasons[:3] or ["Deterministic signals favor platform-managed drift"]
    else:
        label = "mixed_or_uncertain"
        reason_list = (admin_reasons[:2] + infrastructure_reasons[:2])[:4]
        if not reason_list:
            reason_list = ["Signals conflict or are too weak to attribute source confidently"]

    return {
        "label": label,
        "admin_score": admin_score,
        "infrastructure_score": infrastructure_score,
        "reasons": reason_list,
    }


def _dominant_source(counts: Counter[str], admin_total: int, infrastructure_total: int) -> str:
    """Overall attribution: needs a score lead of 3 and at least as many per-change votes as the other side."""
    admin_votes = counts.get("likely_admin_driven", 0)
    infrastructure_votes = counts.get("likely_infrastructure_driven", 0)
    if admin_total >= infrastructure_total + 3 and admin_votes >= infrastructure_votes:
        return "primarily_admin_driven"
    if infrastructure_total >= admin_total + 3 and infrastructure_votes >= admin_votes:
        return "primarily_infrastructure_driven"
    return "mixed_or_uncertain"


def _build_change_source_assessment(compact_changes: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    signal_counter: Counter[str] = Counter()
    admin_total = 0
    infrastructure_total = 0

    for item in compact_changes:
        counts[str(item.get("change_source") or "mixed_or_uncertain")] += 1
        scores = item.get("change_source_scores", {})
        if isinstance(scores, dict):
            admin_total += int(scores.get("admin", 0) or 0)
            infrastructure_total += int(scores.get("infrastructure", 0) or 0)
        reasons = item.get("change_source_reasons", [])
        if isinstance(reasons, list):
            for reason in reasons[:2]:
                if isinstance(reason, str) and reason.strip():
                    signal_counter[reason.strip()] += 1

    dominant_source = (
        _dominant_source(counts, admin_total, infrastructure_total) if compact_changes else "mixed_or_uncertain"
    )
    score_gap = abs(admin_total - infrastructure_total)
    if dominant_source == "mixed_or_uncertain":
        confidence = "medium" if score_gap >= 3 else "low"
    else:
        confidence = "high" if score_gap >= 6 else "medium"

    return {
        "dominant_source": dominant_source,
        "confidence": confidence,
        "counts": {
            label: counts.get(label, 0)
            for label in ("likely_admin_driven", "likely_infrastructure_driven", "mixed_or_uncertain")
        },
        "score_totals": {"admin": admin_total, "infrastructure": infrastructure_total},
        "top_signals": [reason for reason, _ in signal_counter.most_common(5)],
    }


def _format_change_source_label(label: str) -> str:
    mapping = {
        "likely_admin_driven": _t("likely admin-driven"),
        "likely_infrastructure_driven": _t("likely infrastructure/platform-driven"),
        "primarily_admin_driven": _t("primarily admin-driven"),
        "primarily_infrastructure_driven": _t("primarily infrastructure/platform-driven"),
        "mixed_or_uncertain": _t("mixed or uncertain"),
    }
    return mapping.get(label, label.replace("_", " ").strip())
