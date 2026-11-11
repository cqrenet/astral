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
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import common  # same directory as this script
from pr_base import (
    _debug,
    _delay_reviewer_notifications_enabled,
    _env,
    _env_bool,
    _load_policy_excerpt,
    _request_json,
    _run_git,
)
from pr_changes import ChangeItem, _build_deterministic_summary, _changes_fingerprint, _parse_changes
from pr_description import _publish_description, _summary_is_current
from pr_review import _call_azure_openai
from pr_semantic import _extract_semantic_change, _is_entra_enrichment_only_json_change
from pr_threads import _enforce_change_ticket_threads, _sync_summary_threads

log = common.log


def _run_diff_name_status(repo_root: str, baseline_branch: str, drift_branch: str) -> str:
    three_dot = f"origin/{baseline_branch}...origin/{drift_branch}"
    two_dot = f"origin/{baseline_branch}..origin/{drift_branch}"
    try:
        return _run_git(repo_root, ["diff", "--name-status", "--find-renames", three_dot])
    except RuntimeError as exc:
        err = str(exc).lower()
        if "no merge base" not in err:
            raise
        log.warning(
            "No merge base for rolling branches "
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
        log.info("Change-needed thread sync disabled (set REQUIRE_CHANGE_TICKETS=true).")
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
    log.info(
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
            log.info(f"Drift branch '{drift_branch}' not found on origin; skipping summary update.")
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


def _collect_changes(args: argparse.Namespace, baseline_branch: str, drift_branch: str) -> tuple[list[ChangeItem], int]:
    """Policy changes between the branches, minus operational-only drift; returns (changes, ignored count)."""
    diff_output = _run_diff_name_status(args.repo_root, baseline_branch, drift_branch)
    changes = _parse_changes(diff_output, args.backup_folder, args.reports_subdir)
    _debug(f"Parsed non-doc/report changes for summary: count={len(changes)}")
    changes, ignored = _filter_operational_noise_changes(
        repo_root=args.repo_root,
        baseline_branch=baseline_branch,
        drift_branch=drift_branch,
        workload=args.workload,
        changes=changes,
    )
    if ignored > 0:
        log.info(f"Ignored operational-only drift changes from summary/ticket scope: {ignored}")
    return changes, ignored


@dataclass
class _Run:
    """One summary refresh of the active rolling PR."""

    workload: str
    repo_root: str
    repo_api: str
    token: str
    pr_id: int
    baseline_branch: str
    drift_branch: str
    changes: list[ChangeItem]
    title: str
    is_draft: bool
    require_ticket_gate: bool

    def publish_draft(self, description: str, reason: str) -> None:
        published = _publish_draft_pr(
            repo_api=self.repo_api,
            token=self.token,
            pr_id=self.pr_id,
            title=self.title,
            description=description,
            is_draft=self.is_draft,
        )
        if published:
            log.info(f"Published draft PR #{self.pr_id} {reason}.")

    def run_ticket_gate(self) -> None:
        _run_ticket_gate(
            enabled=self.require_ticket_gate,
            repo_api=self.repo_api,
            pr_id=self.pr_id,
            token=self.token,
            repo_root=self.repo_root,
            baseline_branch=self.baseline_branch,
            drift_branch=self.drift_branch,
            changes=self.changes,
        )


def _refresh_summary(args: argparse.Namespace, require_ticket_gate: bool) -> int:
    baseline_branch = common.normalize_branch(args.baseline_branch)
    drift_branch = common.normalize_branch(args.drift_branch)
    token = _env("SYSTEM_ACCESSTOKEN")
    repo_api = (
        f"{_env('SYSTEM_COLLECTIONURI').rstrip('/')}/{_env('SYSTEM_TEAMPROJECT')}"
        f"/_apis/git/repositories/{_env('BUILD_REPOSITORY_ID')}"
    )
    source_ref, target_ref = f"refs/heads/{drift_branch}", f"refs/heads/{baseline_branch}"

    pr = _find_active_rolling_pr(repo_api, token, source_ref, target_ref)
    if not pr:
        log.info("No active rolling PR found; skipping summary update.")
        return 0
    pr_id = pr.get("pullRequestId")
    if not pr_id:
        log.info("Active PR without pullRequestId; skipping summary update.")
        return 0
    _debug(f"Active rolling PR detected: pr_id={pr_id}, source={source_ref}, target={target_ref}")

    if not _fetch_drift_branch(args.repo_root, baseline_branch, drift_branch):
        return 0
    changes, ignored_operational_count = _collect_changes(args, baseline_branch, drift_branch)
    deterministic = _build_deterministic_summary(
        changes, drift_branch, baseline_branch, ignored_operational_count=ignored_operational_count
    )

    full_pr = _request_json(f"{repo_api}/pullrequests/{pr_id}?api-version=7.1", token=token)
    current_description = full_pr.get("description") or ""
    run = _Run(
        workload=args.workload,
        repo_root=args.repo_root,
        repo_api=repo_api,
        token=token,
        pr_id=int(pr_id),
        baseline_branch=baseline_branch,
        drift_branch=drift_branch,
        changes=changes,
        title=full_pr.get("title") or pr.get("title") or f"{args.workload} drift review (rolling)",
        is_draft=bool(full_pr.get("isDraft")),
        require_ticket_gate=require_ticket_gate,
    )

    if _summary_is_current(
        workload=args.workload,
        pr_id=pr_id,
        current_description=current_description,
        changes_fingerprint=_changes_fingerprint(changes).lower(),
        deterministic=deterministic,
    ):
        run.publish_draft(current_description, "after confirming summary was already up to date")
        log.info(
            f"Automated review summary fingerprint unchanged for PR #{pr_id} "
            f"({args.workload}); skipping description update."
        )
        run.run_ticket_gate()
        return 0

    ai_summary, ai_error = _call_azure_openai(
        changes, deterministic, args.workload, args.repo_root, baseline_branch, drift_branch
    )
    _debug(
        f"summary: pr_id={pr_id} workload={args.workload} "
        f"status={full_pr.get('status')} isDraft={full_pr.get('isDraft')} "
        f"mergeStatus={full_pr.get('mergeStatus')} title_len={len(run.title)} "
        f"current_desc_len={len(current_description)}"
    )
    update = _publish_description(
        workload=args.workload,
        deterministic=deterministic,
        ai_summary=ai_summary,
        ai_error=ai_error,
        current_description=current_description,
        patch_url=f"{repo_api}/pullrequests/{pr_id}?api-version=7.1",
        patch_title=run.title,
        token=token,
    )
    _sync_summary_threads(
        repo_api=repo_api,
        pr_id=run.pr_id,
        token=token,
        workload=args.workload,
        deterministic=deterministic,
        ai_summary=ai_summary,
        update=update,
    )
    run.publish_draft(update.final_description, "after automated review summary update")
    run.run_ticket_gate()
    return 0


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
        return _refresh_summary(args, require_ticket_gate)
    except Exception as exc:
        # Non-fatal on purpose: backup and PR flow should continue even if summary generation fails.
        log.warning(f"Failed to update automated review summary: {exc}")
        return 1 if require_ticket_gate else 0


if __name__ == "__main__":
    sys.exit(main())
