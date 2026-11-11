#!/usr/bin/env python3
"""Builds the Azure OpenAI request for the PR review summary and walks the retry ladder."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import common
from pr_ai import (
    _AzureOpenAIClient,
    _build_fallback_narrative,
    _chat_messages,
    _fallback_reason,
    _minimal_reviewer_instruction,
    _reviewer_instruction,
)
from pr_base import _env, _load_policy_excerpt
from pr_change_source import _build_change_source_assessment, _classify_change_source
from pr_changes import (
    ChangeItem,
    _build_change_facts,
    _change_scope,
    _classify_posture,
    _detect_hotspots,
    _fit_payload_budget,
)
from pr_description import _compact_deterministic_summary
from pr_semantic import _extract_semantic_change, _policy_fingerprint

_MINIMAL_CHANGE_KEYS = ("operation", "path", "risk", "semantic_change", "change_source", "change_source_reasons")
_AUTH_ERROR_MARKERS = ("401", "403", "unauthorized", "forbidden")


def _compact_change_records(
    changes: list[ChangeItem], repo_root: str, baseline_branch: str, drift_branch: str
) -> list[dict[str, Any]]:
    """Highest-risk changes (max 80) as the per-change records sent to the model."""
    candidates = [
        item for item in changes if not common.is_doc_like(item.path) and not common.is_report_like(item.path)
    ]
    shortlist = sorted(
        candidates,
        key=lambda item: (
            -item.risk_score,
            0 if item.operation == "Deleted" else 1,
            0 if "conditional access" in item.path.lower() else 1,
            item.path.lower(),
        ),
    )[:80]
    records: list[dict[str, Any]] = []
    for item in shortlist:
        baseline_path = item.old_path if item.operation == "Renamed" and item.old_path else item.path
        old_excerpt = _load_policy_excerpt(repo_root, baseline_branch, baseline_path)
        new_excerpt = _load_policy_excerpt(repo_root, drift_branch, item.path)
        semantic_change = _extract_semantic_change(old_excerpt, new_excerpt, item.path)
        old_fingerprint = _policy_fingerprint(old_excerpt)
        new_fingerprint = _policy_fingerprint(new_excerpt)
        source = _classify_change_source(item, semantic_change)
        records.append(
            {
                "operation": item.operation,
                "path": item.path,
                "old_path": item.old_path or "",
                "policy_type": item.policy_type,
                "severity": item.severity,
                "risk": item.risk_label,
                "reason": item.reason,
                "semantic_change": semantic_change,
                "old_fingerprint": old_fingerprint,
                "new_fingerprint": new_fingerprint,
                "fingerprint_changed": old_fingerprint != new_fingerprint,
                "change_source": source["label"],
                "change_source_reasons": source["reasons"],
                "change_source_scores": {
                    "admin": source["admin_score"],
                    "infrastructure": source["infrastructure_score"],
                },
            }
        )
    return records


@dataclass
class _AiSettings:
    client: _AzureOpenAIClient
    max_tokens: int
    timeout: int
    compact_timeout: int
    minimal_timeout: int


def _ai_settings(endpoint: str, deployment: str, api_key: str) -> _AiSettings:
    ai_timeout = max(10, common.env_int("PR_AI_REQUEST_TIMEOUT_SECONDS", 60))
    compact_timeout = max(ai_timeout, common.env_int("PR_AI_COMPACT_TIMEOUT_SECONDS", max(90, ai_timeout)))
    return _AiSettings(
        client=_AzureOpenAIClient(
            endpoint=endpoint,
            deployment=deployment,
            api_key=api_key,
            api_version=_env("AZURE_OPENAI_API_VERSION", required=False, default="2024-12-01-preview"),
            max_attempts=max(1, common.env_int("PR_AI_REQUEST_MAX_ATTEMPTS", 3)),
        ),
        max_tokens=max(256, common.env_int("PR_AI_MAX_TOKENS", 1200)),
        timeout=ai_timeout,
        compact_timeout=compact_timeout,
        minimal_timeout=max(compact_timeout, common.env_int("PR_AI_MINIMAL_TIMEOUT_SECONDS", max(120, compact_timeout))),
    )


@dataclass
class _ReviewContext:
    """Everything the three prompt sizes are derived from."""

    workload: str
    deterministic_summary: str
    change_scope: Any
    security_sensitive: bool
    posture_change: Any
    top_changed_areas: Any
    change_source_assessment: dict[str, Any]
    compact_changes: list[dict[str, Any]]
    sampled_changes: list[dict[str, Any]]
    full_payload: dict[str, Any]


def _build_review_context(
    changes: list[ChangeItem],
    deterministic_summary: str,
    workload: str,
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
) -> _ReviewContext:
    compact_changes = _compact_change_records(changes, repo_root, baseline_branch, drift_branch)
    change_scope = _change_scope(changes)
    security_sensitive = any(item.risk_score >= 3 for item in changes)
    top_changed_areas = _detect_hotspots(changes)
    posture_change = _classify_posture(changes)
    change_source_assessment = _build_change_source_assessment(compact_changes)

    payload: dict[str, Any] = {
        "workload": workload,
        "change_scope": change_scope,
        "security_sensitive": security_sensitive,
        "baseline_alignment": "proposed_change_to_authoritative_config",
        "baseline_profile": os.environ.get("SECURITY_BASELINE_PROFILE", "authoritative-main"),
        "posture_change": posture_change,
        "top_changed_areas": top_changed_areas,
        "change_facts": _build_change_facts(changes),
        "change_source_assessment": change_source_assessment,
        "deterministic_summary": deterministic_summary,
        "instruction": _reviewer_instruction(),
    }
    sampled_changes, truncated = _fit_payload_budget(
        payload=payload,
        sampled_changes=compact_changes,
        max_bytes=max(16000, common.env_int("PR_AI_PAYLOAD_MAX_BYTES", 120000)),
    )
    payload["sampled_changes"] = sampled_changes
    payload["sampled_changes_total"] = len(compact_changes)
    payload["sampled_changes_used"] = len(sampled_changes)
    payload["sampled_changes_truncated"] = truncated
    return _ReviewContext(
        workload=workload,
        deterministic_summary=deterministic_summary,
        change_scope=change_scope,
        security_sensitive=security_sensitive,
        posture_change=posture_change,
        top_changed_areas=top_changed_areas,
        change_source_assessment=change_source_assessment,
        compact_changes=compact_changes,
        sampled_changes=sampled_changes,
        full_payload=payload,
    )


def _compact_payload(ctx: _ReviewContext) -> dict[str, Any]:
    """Same prompt with only the first few sampled changes."""
    limit = max(8, common.env_int("PR_AI_COMPACT_CHANGE_LIMIT", 12))
    payload = dict(ctx.full_payload)
    payload["sampled_changes"] = ctx.sampled_changes[:limit]
    payload["sampled_changes_used"] = len(payload["sampled_changes"])
    payload["sampled_changes_truncated"] = True
    return payload


def _minimal_payload(ctx: _ReviewContext) -> dict[str, Any]:
    limit = max(3, common.env_int("PR_AI_MINIMAL_CHANGE_LIMIT", 5))
    return {
        "workload": ctx.workload,
        "change_scope": ctx.change_scope,
        "security_sensitive": ctx.security_sensitive,
        "posture_change": ctx.posture_change,
        "top_changed_areas": ctx.top_changed_areas,
        "change_source_assessment": ctx.change_source_assessment,
        "deterministic_summary": _compact_deterministic_summary(ctx.deterministic_summary),
        "changes": [{key: item.get(key) for key in _MINIMAL_CHANGE_KEYS} for item in ctx.sampled_changes[:limit]],
        "instruction": _minimal_reviewer_instruction(),
    }


def _request_narrative(ctx: _ReviewContext, settings: _AiSettings) -> tuple[str, str]:
    """(content, accumulated errors). Full prompt; on timeout a compact one; then a minimal one."""
    client = settings.client
    content, last_error = client.complete(_chat_messages("", ctx.full_payload), settings.max_tokens, settings.timeout)
    if content:
        return content, ""

    timed_out = "timed out" in last_error.lower() or "timeout" in last_error.lower()
    if timed_out and len(ctx.sampled_changes) > 12:
        tokens = max(256, min(settings.max_tokens, common.env_int("PR_AI_COMPACT_MAX_TOKENS", 600)))
        content, error = client.complete(
            _chat_messages("", _compact_payload(ctx)), tokens, settings.compact_timeout
        )
        if content:
            return content, ""
        last_error = f"{last_error} | compact-retry: {error}".strip(" |")

    # Last resort, unless the failure looks like bad credentials.
    if not any(marker in last_error.lower() for marker in _AUTH_ERROR_MARKERS):
        tokens = max(256, min(settings.max_tokens, common.env_int("PR_AI_MINIMAL_MAX_TOKENS", 400)))
        messages = _chat_messages(
            " Prioritize clarity, practical risk framing, and reviewer actionability.", _minimal_payload(ctx)
        )
        content, error = client.complete(messages, tokens, settings.minimal_timeout)
        if content:
            return content, ""
        if error:
            last_error = f"{last_error} | minimal-retry: {error}".strip(" |")
    return "", last_error


def _call_azure_openai(
    changes: list[ChangeItem],
    deterministic_summary: str,
    workload: str,
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
) -> tuple[str | None, str | None]:
    """Reviewer narrative via Azure OpenAI -> (text, error); falls back to a deterministic narrative."""
    if _env("ENABLE_PR_AI_SUMMARY", required=False, default="true").lower() != "true":
        return (None, None)

    endpoint = _env("AZURE_OPENAI_ENDPOINT", required=False)
    deployment = _env("AZURE_OPENAI_DEPLOYMENT", required=False)
    api_key = _env("AZURE_OPENAI_API_KEY", required=False)
    if not endpoint or not deployment or not api_key:
        return (None, "Azure OpenAI is not configured (endpoint/deployment/api-key missing)")
    if not any(not common.is_doc_like(c.path) and not common.is_report_like(c.path) for c in changes):
        return (None, "AI summary skipped: only report/documentation changes detected")

    settings = _ai_settings(endpoint, deployment, api_key)
    ctx = _build_review_context(changes, deterministic_summary, workload, repo_root, baseline_branch, drift_branch)
    content, last_error = _request_narrative(ctx, settings)
    if content:
        return (content, None)

    print(f"WARNING: Azure OpenAI summary fallback triggered: {last_error}")
    return (
        _build_fallback_narrative(
            workload=workload,
            changes=changes,
            compact_changes=ctx.compact_changes,
            top_changed_areas=ctx.top_changed_areas,
            change_source_assessment=ctx.change_source_assessment,
            fallback_reason=_fallback_reason(last_error),
        ),
        None,
    )
