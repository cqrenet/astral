#!/usr/bin/env python3
"""
Update rolling PR description with automated change summary.

The summary includes:
- operation counts (added/modified/deleted/renamed)
- deterministic risk assessment
- optional Azure OpenAI reviewer narrative
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any
from urllib.parse import urlencode

import common  # same directory as this script
from pr_ai import (
    _AzureOpenAIClient,
    _build_fallback_narrative,
    _chat_messages,
    _fallback_reason,
    _minimal_reviewer_instruction,
    _reviewer_instruction,
)
from pr_base import _debug, _delay_reviewer_notifications_enabled, _env, _env_bool, _request_json, _run_git
from pr_change_source import _build_change_source_assessment, _classify_change_source
from pr_changes import (
    ChangeItem,
    _build_change_facts,
    _build_deterministic_summary,
    _change_scope,
    _changes_fingerprint,
    _classify_posture,
    _detect_hotspots,
    _fit_payload_budget,
    _parse_changes,
)
from pr_description import _compact_deterministic_summary, _publish_description, _summary_is_current
from pr_semantic import _extract_semantic_change, _is_entra_enrichment_only_json_change, _policy_fingerprint
from pr_threads import _enforce_change_ticket_threads, _sync_summary_threads


def _run_diff_name_status(repo_root: str, baseline_branch: str, drift_branch: str) -> str:
    three_dot = f"origin/{baseline_branch}...origin/{drift_branch}"
    two_dot = f"origin/{baseline_branch}..origin/{drift_branch}"
    try:
        return _run_git(repo_root, ["diff", "--name-status", "--find-renames", three_dot])
    except RuntimeError as exc:
        err = str(exc).lower()
        if "no merge base" not in err:
            raise
        print(
            "WARNING: No merge base for rolling branches "
            f"(origin/{baseline_branch}, origin/{drift_branch}); using direct diff."
        )
        return _run_git(repo_root, ["diff", "--name-status", "--find-renames", two_dot])


def _filter_operational_noise_changes(
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
    workload: str,
    changes: list[ChangeItem],
) -> tuple[list[ChangeItem], int]:
    if workload.strip().lower() != "entra":
        return (changes, 0)

    filtered: list[ChangeItem] = []
    skipped = 0
    for item in changes:
        if item.operation != "Modified":
            filtered.append(item)
            continue
        if not item.path.lower().endswith(".json"):
            filtered.append(item)
            continue
        old_excerpt = _load_policy_excerpt(repo_root, baseline_branch, item.path)
        new_excerpt = _load_policy_excerpt(repo_root, drift_branch, item.path)
        if _is_entra_enrichment_only_json_change(old_excerpt, new_excerpt):
            skipped += 1
            _debug(f"Ignoring enrichment-only Entra drift noise for path={item.path}")
            continue
        filtered.append(item)
    return (filtered, skipped)


def _load_policy_excerpt(repo_root: str, branch: str, path: str, max_chars: int = 0) -> str:
    """
    Load a small excerpt of a file from a given git branch for AI context.
    """
    try:
        normalized_branch = common.normalize_branch(branch)
        result = subprocess.run(
            ["git", "show", f"origin/{normalized_branch}:{path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        content = result.stdout.strip()
        if max_chars and len(content) > max_chars:
            return content[:max_chars] + "...(truncated)"
        return content
    except Exception:
        return ""


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


def _call_azure_openai(
    changes: list[ChangeItem],
    deterministic_summary: str,
    workload: str,
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
) -> tuple[str | None, str | None]:
    """Reviewer narrative via Azure OpenAI -> (text, error).

    Escalation: full payload; on timeout a compact payload; then (unless the failure
    was an auth error) a minimal payload; finally a deterministic fallback narrative.
    """
    if _env("ENABLE_PR_AI_SUMMARY", required=False, default="true").lower() != "true":
        return (None, None)

    endpoint = _env("AZURE_OPENAI_ENDPOINT", required=False)
    deployment = _env("AZURE_OPENAI_DEPLOYMENT", required=False)
    api_key = _env("AZURE_OPENAI_API_KEY", required=False)
    if not endpoint or not deployment or not api_key:
        return (None, "Azure OpenAI is not configured (endpoint/deployment/api-key missing)")

    max_ai_tokens = max(256, common.env_int("PR_AI_MAX_TOKENS", 1200))
    ai_timeout = max(10, common.env_int("PR_AI_REQUEST_TIMEOUT_SECONDS", 60))
    compact_timeout = max(ai_timeout, common.env_int("PR_AI_COMPACT_TIMEOUT_SECONDS", max(90, ai_timeout)))
    minimal_timeout = max(compact_timeout, common.env_int("PR_AI_MINIMAL_TIMEOUT_SECONDS", max(120, compact_timeout)))
    client = _AzureOpenAIClient(
        endpoint=endpoint,
        deployment=deployment,
        api_key=api_key,
        api_version=_env("AZURE_OPENAI_API_VERSION", required=False, default="2024-12-01-preview"),
        max_attempts=max(1, common.env_int("PR_AI_REQUEST_MAX_ATTEMPTS", 3)),
    )

    if not any(not common.is_doc_like(c.path) and not common.is_report_like(c.path) for c in changes):
        return (None, "AI summary skipped: only report/documentation changes detected")

    compact_changes = _compact_change_records(changes, repo_root, baseline_branch, drift_branch)
    change_scope = _change_scope(changes)
    security_sensitive = any(item.risk_score >= 3 for item in changes)
    top_changed_areas = _detect_hotspots(changes)
    posture_change = _classify_posture(changes)
    change_source_assessment = _build_change_source_assessment(compact_changes)

    user_payload: dict[str, Any] = {
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
    sampled_changes, payload_truncated = _fit_payload_budget(
        payload=user_payload,
        sampled_changes=compact_changes,
        max_bytes=max(16000, common.env_int("PR_AI_PAYLOAD_MAX_BYTES", 120000)),
    )
    user_payload["sampled_changes"] = sampled_changes
    user_payload["sampled_changes_total"] = len(compact_changes)
    user_payload["sampled_changes_used"] = len(sampled_changes)
    user_payload["sampled_changes_truncated"] = payload_truncated

    content, last_error = client.complete(_chat_messages("", user_payload), max_ai_tokens, ai_timeout)
    if content:
        return (content, None)

    # If the full payload times out, retry once with a compact payload.
    timed_out = "timed out" in last_error.lower() or "timeout" in last_error.lower()
    if timed_out and len(sampled_changes) > 12:
        compact_limit = max(8, common.env_int("PR_AI_COMPACT_CHANGE_LIMIT", 12))
        compact_payload = dict(user_payload)
        compact_payload["sampled_changes"] = sampled_changes[:compact_limit]
        compact_payload["sampled_changes_used"] = len(compact_payload["sampled_changes"])
        compact_payload["sampled_changes_truncated"] = True
        compact_tokens = max(256, min(max_ai_tokens, common.env_int("PR_AI_COMPACT_MAX_TOKENS", 600)))
        content, error = client.complete(_chat_messages("", compact_payload), compact_tokens, compact_timeout)
        if content:
            return (content, None)
        last_error = f"{last_error} | compact-retry: {error}".strip(" |")

    # Last resort: minimal prompt, unless the failure looks like bad credentials.
    if not any(marker in last_error.lower() for marker in ("401", "403", "unauthorized", "forbidden")):
        minimal_limit = max(3, common.env_int("PR_AI_MINIMAL_CHANGE_LIMIT", 5))
        minimal_payload = {
            "workload": workload,
            "change_scope": change_scope,
            "security_sensitive": security_sensitive,
            "posture_change": posture_change,
            "top_changed_areas": top_changed_areas,
            "change_source_assessment": change_source_assessment,
            "deterministic_summary": _compact_deterministic_summary(deterministic_summary),
            "changes": [
                {
                    key: item.get(key)
                    for key in ("operation", "path", "risk", "semantic_change", "change_source", "change_source_reasons")
                }
                for item in sampled_changes[:minimal_limit]
            ],
            "instruction": _minimal_reviewer_instruction(),
        }
        minimal_tokens = max(256, min(max_ai_tokens, common.env_int("PR_AI_MINIMAL_MAX_TOKENS", 400)))
        messages = _chat_messages(" Prioritize clarity, practical risk framing, and reviewer actionability.", minimal_payload)
        content, error = client.complete(messages, minimal_tokens, minimal_timeout)
        if content:
            return (content, None)
        if error:
            last_error = f"{last_error} | minimal-retry: {error}".strip(" |")

    print(f"WARNING: Azure OpenAI summary fallback triggered: {last_error}")
    return (
        _build_fallback_narrative(
            workload=workload,
            changes=changes,
            compact_changes=compact_changes,
            top_changed_areas=top_changed_areas,
            change_source_assessment=change_source_assessment,
            fallback_reason=_fallback_reason(last_error),
        ),
        None,
    )


def _publish_draft_pr(
    repo_api: str,
    token: str,
    pr_id: int,
    title: str,
    description: str,
    is_draft: bool,
) -> bool:
    if not is_draft or not _delay_reviewer_notifications_enabled():
        return False
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}?api-version=7.1",
        token=token,
        method="PATCH",
        body={
            "title": title,
            "description": description,
            "isDraft": False,
        },
    )
    return True


def _build_ticket_change_context(
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
    changes: list[ChangeItem],
) -> dict[str, tuple[str, str]]:
    context: dict[str, tuple[str, str]] = {}

    for item in changes:
        if common.is_doc_like(item.path) or common.is_report_like(item.path):
            continue
        baseline_path = item.old_path if item.operation == "Renamed" and item.old_path else item.path
        old_excerpt = _load_policy_excerpt(repo_root, baseline_branch, baseline_path)
        new_excerpt = _load_policy_excerpt(repo_root, drift_branch, item.path)
        semantic = _extract_semantic_change(old_excerpt, new_excerpt, item.path).strip()
        if not semantic or semantic == "No semantic key changes detected":
            semantic = "configuration content modified"
        if len(semantic) > 320:
            semantic = semantic[:317] + "..."
        change_summary = f"{item.operation}: {semantic}"
        risk_summary = f"{item.risk_label} ({item.policy_type}): {item.reason}"
        context[item.path] = (change_summary, risk_summary)

    return context


def _run_ticket_gate(
    *,
    enabled: bool,
    repo_api: str,
    pr_id: Any,
    token: str,
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
    changes: list[ChangeItem],
) -> None:
    if not enabled:
        print("Change-needed thread sync disabled (set REQUIRE_CHANGE_TICKETS=true).")
        return
    ticket_pattern = _env("CHANGE_TICKET_REGEX", required=False, default=r"[A-Z][A-Z0-9]+-\d+")
    change_context = _build_ticket_change_context(
        repo_root=repo_root,
        baseline_branch=baseline_branch,
        drift_branch=drift_branch,
        changes=changes,
    )
    created_count, closed_count = _enforce_change_ticket_threads(
        repo_api=repo_api,
        pr_id=pr_id,
        token=token,
        changes=changes,
        ticket_pattern=ticket_pattern,
        change_context=change_context,
    )
    print(
        "Change-needed thread sync complete: "
        f"created={created_count}, closed_stale={closed_count}. "
        "Merge policy should enforce unresolved thread handling."
    )


def _fetch_drift_branch(repo_root: str, baseline_branch: str, drift_branch: str) -> bool:
    """Fetch both branches; False when the drift branch does not exist on origin."""
    _run_git(repo_root, ["fetch", "--quiet", "origin", baseline_branch])
    try:
        _run_git(repo_root, ["fetch", "--quiet", "origin", drift_branch])
    except RuntimeError as exc:
        message = str(exc).lower()
        if "couldn't find remote ref" in message or "could not find remote ref" in message:
            print(f"Drift branch '{drift_branch}' not found on origin; skipping summary update.")
            return False
        raise
    return True


def _find_active_rolling_pr(repo_api: str, token: str, source_ref: str, target_ref: str) -> dict[str, Any] | None:
    query = urlencode(
        {
            "searchCriteria.status": "active",
            "searchCriteria.sourceRefName": source_ref,
            "searchCriteria.targetRefName": target_ref,
            "api-version": "7.1",
        },
        safe="/",
    )
    items = _request_json(f"{repo_api}/pullrequests?{query}", token=token).get("value", [])
    return items[0] if items else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Update rolling PR reviewer summary")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--backup-folder", required=True)
    parser.add_argument("--reports-subdir", required=True)
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--baseline-branch", required=True)
    args = parser.parse_args()
    require_ticket_gate = _env_bool("REQUIRE_CHANGE_TICKETS")
    _debug(
        "Ticket gate flags: "
        f"REQUIRE_CHANGE_TICKETS={os.environ.get('REQUIRE_CHANGE_TICKETS', '')!r}, "
        f"effective={require_ticket_gate}"
    )

    try:
        baseline_branch = common.normalize_branch(args.baseline_branch)
        drift_branch = common.normalize_branch(args.drift_branch)

        token = _env("SYSTEM_ACCESSTOKEN")
        collection_uri = _env("SYSTEM_COLLECTIONURI").rstrip("/")
        project = _env("SYSTEM_TEAMPROJECT")
        repository_id = _env("BUILD_REPOSITORY_ID")

        source_ref = f"refs/heads/{drift_branch}"
        target_ref = f"refs/heads/{baseline_branch}"
        repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{repository_id}"

        pr = _find_active_rolling_pr(repo_api, token, source_ref, target_ref)
        if not pr:
            print("No active rolling PR found; skipping summary update.")
            return 0
        pr_id = pr.get("pullRequestId")
        if not pr_id:
            print("Active PR without pullRequestId; skipping summary update.")
            return 0
        _debug(f"Active rolling PR detected: pr_id={pr_id}, source={source_ref}, target={target_ref}")

        if not _fetch_drift_branch(args.repo_root, baseline_branch, drift_branch):
            return 0
        diff_output = _run_diff_name_status(args.repo_root, baseline_branch, drift_branch)
        changes = _parse_changes(diff_output, args.backup_folder, args.reports_subdir)
        _debug(f"Parsed non-doc/report changes for summary: count={len(changes)}")
        changes, ignored_operational_count = _filter_operational_noise_changes(
            repo_root=args.repo_root,
            baseline_branch=baseline_branch,
            drift_branch=drift_branch,
            workload=args.workload,
            changes=changes,
        )
        if ignored_operational_count > 0:
            print(
                "Ignored operational-only drift changes from summary/ticket scope: "
                f"{ignored_operational_count}"
            )
        deterministic = _build_deterministic_summary(
            changes,
            drift_branch,
            baseline_branch,
            ignored_operational_count=ignored_operational_count,
        )

        full_pr = _request_json(f"{repo_api}/pullrequests/{pr_id}?api-version=7.1", token=token)
        current_description = full_pr.get("description") or ""
        pr_is_draft = bool(full_pr.get("isDraft"))
        patch_title = full_pr.get("title") or pr.get("title") or f"{args.workload} drift review (rolling)"
        ticket_gate = dict(
            enabled=require_ticket_gate,
            repo_api=repo_api,
            pr_id=pr_id,
            token=token,
            repo_root=args.repo_root,
            baseline_branch=baseline_branch,
            drift_branch=drift_branch,
            changes=changes,
        )

        if _summary_is_current(
            workload=args.workload,
            pr_id=pr_id,
            current_description=current_description,
            changes_fingerprint=_changes_fingerprint(changes).lower(),
            deterministic=deterministic,
        ):
            if _publish_draft_pr(
                repo_api=repo_api,
                token=token,
                pr_id=int(pr_id),
                title=patch_title,
                description=current_description,
                is_draft=pr_is_draft,
            ):
                print(f"Published draft PR #{pr_id} after confirming summary was already up to date.")
            print(
                f"Automated review summary fingerprint unchanged for PR #{pr_id} "
                f"({args.workload}); skipping description update."
            )
            _run_ticket_gate(**ticket_gate)
            return 0

        ai_summary, ai_error = _call_azure_openai(
            changes,
            deterministic,
            args.workload,
            args.repo_root,
            baseline_branch,
            drift_branch,
        )
        print(
            f"DEBUG summary: pr_id={pr_id} workload={args.workload} "
            f"status={full_pr.get('status')} isDraft={full_pr.get('isDraft')} "
            f"mergeStatus={full_pr.get('mergeStatus')} title_len={len(patch_title)} "
            f"current_desc_len={len(current_description)}"
        )
        update = _publish_description(
            workload=args.workload,
            deterministic=deterministic,
            ai_summary=ai_summary,
            ai_error=ai_error,
            current_description=current_description,
            patch_url=f"{repo_api}/pullrequests/{pr_id}?api-version=7.1",
            patch_title=patch_title,
            token=token,
        )
        _sync_summary_threads(
            repo_api=repo_api,
            pr_id=int(pr_id),
            token=token,
            workload=args.workload,
            deterministic=deterministic,
            ai_summary=ai_summary,
            update=update,
        )
        if _publish_draft_pr(
            repo_api=repo_api,
            token=token,
            pr_id=int(pr_id),
            title=patch_title,
            description=update.final_description,
            is_draft=pr_is_draft,
        ):
            print(f"Published draft PR #{pr_id} after automated review summary update.")
        _run_ticket_gate(**ticket_gate)
        return 0
    except Exception as exc:
        # Non-fatal on purpose: backup and PR flow should continue even if summary generation fails.
        print(f"WARNING: Failed to update automated review summary: {exc}", file=sys.stderr)
        return 1 if require_ticket_gate else 0


if __name__ == "__main__":
    sys.exit(main())
