#!/usr/bin/env python3
"""Queue restore automatically after merged rolling PR that contains /reject decisions."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import urllib.parse
from typing import Any

import common  # same directory as this script

log = common.log

_env_text = common.env_text
_env_bool = common.env_bool
_request_json = common.request_json

MERGE_MARKER_PREFIX = "AUTO-RESTORE-AFTER-MERGE:"


def _parse_iso_utc(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _query_completed_prs(
    repo_api: str,
    headers: dict[str, str],
    source_ref: str,
    target_ref: str,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "searchCriteria.status": "all",
            "searchCriteria.sourceRefName": source_ref,
            "searchCriteria.targetRefName": target_ref,
            "api-version": "7.1",
        },
        quote_via=urllib.parse.quote,
        safe="/",
    )
    payload = _request_json(f"{repo_api}/pullrequests?{query}", headers=headers)
    items = payload.get("value", []) if isinstance(payload, dict) else []
    # Include completed (merged) and abandoned PRs; skip active/notSet ones.
    closed = [pr for pr in items if pr.get("status") in ("completed", "abandoned")]
    return sorted(closed, key=lambda x: x.get("closedDate", ""), reverse=True)


def _threads(repo_api: str, headers: dict[str, str], pr_id: int) -> list[dict[str, Any]]:
    payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        headers=headers,
    )
    return payload.get("value", []) if isinstance(payload, dict) else []


def _thread_comment_contents(threads: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        for comment in comments:
            out.append(str(comment.get("content", "") or ""))
    return out


def _rejected_ticket_paths(threads: list[dict[str, Any]], allowed: set[str]) -> list[str]:
    rejected: set[str] = set()
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        marker_path: str | None = None
        for comment in comments:
            marker_path = common.ticket_path_from_content(str(comment.get("content", "") or ""))
            if marker_path:
                break
        if not marker_path:
            continue

        if common.latest_thread_decision(comments, allowed) == "reject":
            rejected.add(marker_path)
    return sorted(rejected)


def _has_reject_signal(threads: list[dict[str, Any]], allowed: set[str]) -> bool:
    """True when an allowed reviewer issued /reject in any thread.

    Bot-authored text (e.g. "Auto-action: /reject detected.") is deliberately not
    trusted: any PR commenter could post the same text.
    """
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        for comment in comments:
            if not common.is_trusted_decision_comment(comment, allowed):
                continue
            match = common.DECISION_RE.search(str(comment.get("content", "") or ""))
            if match and match.group("decision").lower() == "reject":
                return True
    return False


def _has_merge_marker(comments: list[str], merge_commit: str) -> bool:
    marker = f"Automation marker: {MERGE_MARKER_PREFIX}{merge_commit}"
    return any(marker in content for content in comments)


def _is_permission_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "http 403" in msg or "forbidden" in msg


def _diagnose_queue_permission(
    collection_uri: str,
    project: str,
    headers: dict[str, str],
    definition_id: int | str,
) -> None:
    definition_url = (
        f"{collection_uri}/{project}/_apis/build/definitions/{definition_id}"
        "?api-version=7.1"
    )
    try:
        payload = _request_json(definition_url, headers=headers)
        definition_name = str(payload.get("name", "") or "").strip()
        log.info(
            "Diagnostic: restore pipeline definition is readable "
            f"(id={definition_id}, name='{definition_name or 'n/a'}')."
        )
        log.info(
            "Diagnostic: queue call was forbidden, so missing permission is likely "
            "'Queue builds' on that restore pipeline (or pipeline is not authorized to use it)."
        )
    except Exception as diag_exc:
        log.info(
            "Diagnostic: unable to read restore pipeline definition "
            f"id={definition_id}. Details: {diag_exc}"
        )
        log.info(
            "Diagnostic: likely wrong definition ID, wrong project, or missing 'View builds' permission "
            "for the calling pipeline identity."
        )


def _int_env(name: str, default: str) -> int:
    raw = _env_text(name, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid {name}: {raw}") from exc


def _commit_of(pr: dict[str, Any]) -> tuple[str, bool]:
    """(commit id, abandoned): the merge commit, or for abandoned PRs the last source commit."""
    merge_commit = (((pr.get("lastMergeCommit") or {}).get("commitId")) or "").strip()
    if merge_commit:
        return merge_commit, False
    return (((pr.get("lastMergeSourceCommit") or {}).get("commitId")) or "").strip(), True


def _find_candidate(
    completed: list[dict[str, Any]],
    repo_api: str,
    headers: dict[str, str],
    allowed: set[str],
    cutoff: dt.datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """First completed PR in the lookback window with a reviewer reject and no restore queued yet."""
    for pr in completed:
        closed_at = _parse_iso_utc(str(pr.get("closedDate", "") or ""))
        if closed_at and closed_at < cutoff:
            continue
        commit, is_abandoned = _commit_of(pr)
        if not commit:
            continue
        threads = _threads(repo_api, headers, int(pr.get("pullRequestId")))
        has_reject = _has_reject_signal(threads, allowed)
        if is_abandoned:
            has_reject = has_reject or common.has_allowed_reject_vote(pr, allowed)
        if has_reject and not _has_merge_marker(_thread_comment_contents(threads), commit):
            return pr, threads
    return None


def _restore_request(
    definition_id: int | str, baseline_branch: str, max_workers: int, restore_mode: str, restore_paths_csv: str
) -> common.RestoreRequest:
    """The restore run's parameters, from the AUTO_REMEDIATE_* settings (dry run unless explicitly disabled)."""
    return common.RestoreRequest(
        definition_id=definition_id,
        baseline_branch=baseline_branch,
        include_entra_update=_env_bool("AUTO_REMEDIATE_INCLUDE_ENTRA_UPDATE", False),
        dry_run=_env_bool("AUTO_REMEDIATE_DRY_RUN", True),  # fail safe: unset = dry run
        update_assignments=_env_bool("AUTO_REMEDIATE_UPDATE_ASSIGNMENTS", True),
        remove_unmanaged=_env_bool("AUTO_REMEDIATE_REMOVE_OBJECTS", False),
        max_workers=max_workers,
        exclude_csv=_env_text("AUTO_REMEDIATE_EXCLUDE_CSV", ""),
        restore_mode=restore_mode,
        restore_paths_csv=restore_paths_csv,
    )


def _queue_restore(
    collection_uri: str, project: str, headers: dict[str, str], request: common.RestoreRequest
) -> dict[str, Any] | None:
    """Queue the restore; None (after explaining how to fix it) when the identity lacks permission."""
    try:
        return common.queue_restore_pipeline(collection_uri, project, headers, request)
    except Exception as exc:
        if not _is_permission_error(exc):
            raise
        log.warning(
            "Post-merge remediation queue skipped due permissions. "
            f"Definition={request.definition_id}. Details: {exc}"
        )
        _diagnose_queue_permission(collection_uri, project, headers, request.definition_id)
        log.info(
            "Grant 'Queue builds' permission for this pipeline identity on the restore pipeline "
            "and ensure the pipeline has access to run it."
        )
        return None


def _restore_scope(workload: str, rejected_paths: list[str]) -> tuple[str, str]:
    """(restore mode, restore paths CSV): selective for Intune with rejected paths, otherwise full."""
    if workload == "intune" and rejected_paths:
        log.info(f"Post-merge remediation scope: selective ({len(rejected_paths)} rejected path(s)).")
        for path in rejected_paths:
            log.info(f" - {path}")
        return "selective", ",".join(rejected_paths)
    log.info("Post-merge remediation scope: full.")
    return "full", ""


def _marker_comment(
    workload: str, pr_id: int, commit: str, is_abandoned: bool, definition_id: int | str, build_url: str
) -> str:
    """PR thread text that records the queued restore; its marker line prevents queueing twice."""
    commit_label = "Source commit" if is_abandoned else "Merge commit"
    return (
        f"Auto-remediation queued after {'abandoned' if is_abandoned else 'merged'} rolling PR "
        "with reviewer /reject decision(s).\n\n"
        f"Workload: {workload}\n"
        f"PR: #{pr_id}\n"
        f"{commit_label}: {commit}\n"
        f"Restore pipeline definition: {definition_id}\n"
        f"Restore run: {build_url or '(queued)'}\n\n"
        f"Automation marker: {MERGE_MARKER_PREFIX}{commit}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue restore after merged rolling PR with /reject decisions")
    parser.add_argument("--workload", required=True, choices=["intune", "entra"])
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--baseline-branch", required=True)
    args = parser.parse_args()

    if not _env_bool("AUTO_REMEDIATE_AFTER_MERGE", False):
        log.info("Post-merge auto-remediation disabled (set AUTO_REMEDIATE_AFTER_MERGE=true).")
        return 0

    token = os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    if not token:
        raise SystemExit("SYSTEM_ACCESSTOKEN is empty.")

    allowed = common.allowed_reviewers()
    if not allowed:
        common.ci_log(
            "warning",
            "PR_DECISION_ALLOWED_REVIEWERS is empty; "
            "post-merge remediation from reviewer decisions is disabled.",
        )
        return 0

    definition_raw = _env_text("AUTO_REMEDIATE_RESTORE_PIPELINE_ID", "")
    if not definition_raw:
        log.info("Post-merge auto-remediation queue skipped: AUTO_REMEDIATE_RESTORE_PIPELINE_ID is empty.")
        return 0
    definition_id = common.parse_pipeline_id(definition_raw, "AUTO_REMEDIATE_RESTORE_PIPELINE_ID")
    max_workers = _int_env("AUTO_REMEDIATE_MAX_WORKERS", "10")  # validate up front, before any API call
    lookback_hours = _int_env("AUTO_REMEDIATE_AFTER_MERGE_LOOKBACK_HOURS", "168")

    collection_uri = os.environ["SYSTEM_COLLECTIONURI"].rstrip("/")
    project = os.environ["SYSTEM_TEAMPROJECT"]
    repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{os.environ['BUILD_REPOSITORY_ID']}"
    headers = common.ado_headers(token)

    completed = _query_completed_prs(
        repo_api,
        headers,
        common.ref_from_branch(args.drift_branch),
        common.ref_from_branch(args.baseline_branch),
    )
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
    found = _find_candidate(completed, repo_api, headers, allowed, cutoff)
    if not found:
        log.info("No completed rolling PR requiring post-merge remediation was found.")
        return 0
    candidate, candidate_threads = found
    pr_id = int(candidate.get("pullRequestId"))
    merge_commit, is_abandoned = _commit_of(candidate)

    restore_mode, restore_paths_csv = _restore_scope(args.workload, _rejected_ticket_paths(candidate_threads, allowed))

    request = _restore_request(definition_id, args.baseline_branch, max_workers, restore_mode, restore_paths_csv)
    queued = _queue_restore(collection_uri, project, headers, request)
    if queued is None:
        return 0

    build_id = queued.get("id")
    build_url = common.build_web_url(queued, collection_uri, project)
    comment = _marker_comment(args.workload, pr_id, merge_commit, is_abandoned, definition_id, build_url)
    try:
        common.post_pr_thread(repo_api, headers, pr_id, comment)
    except Exception as exc:
        log.warning(f"Restore queued, but failed posting marker comment on PR #{pr_id}: {exc}")

    log.info(
        f"Queued post-merge remediation for PR #{pr_id} "
        f"({'source commit' if is_abandoned else 'merge commit'}={merge_commit}, buildId={build_id})."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.warning(f"Failed post-merge remediation check: {exc}")
        raise
