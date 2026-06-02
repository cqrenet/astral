#!/usr/bin/env python3
"""Queue restore automatically after merged rolling PR that contains /reject decisions."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

# common.py lives in the same directory; ensure it can be imported when the
# script is executed directly.
_sys_path_inserted = False
if __file__:
    _script_dir = str(Path(__file__).resolve().parent)
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
        _sys_path_inserted = True

import common

if _sys_path_inserted:
    sys.path.pop(0)

_env_text = common.env_text
_env_bool = common.env_bool
_request_json = common.request_json

REJECT_CMD_RE = re.compile(r"(?im)^\s*(?:/|#)?reject\b")
DECISION_RE = re.compile(r"(?im)^\s*(?:/|#)?(?P<decision>reject|accept)\b")
AUTO_TICKET_THREAD_PREFIX = "AUTO-CHANGE-TICKET:"
MERGE_MARKER_PREFIX = "AUTO-RESTORE-AFTER-MERGE:"


def _normalize_branch(branch: str) -> str:
    b = branch.strip()
    if b.startswith("refs/heads/"):
        return b[len("refs/heads/") :]
    return b


def _ref_from_branch(branch: str) -> str:
    return f"refs/heads/{_normalize_branch(branch)}"


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
            "searchCriteria.status": "completed",
            "searchCriteria.sourceRefName": source_ref,
            "searchCriteria.targetRefName": target_ref,
            "api-version": "7.1",
        },
        quote_via=urllib.parse.quote,
        safe="/",
    )
    payload = _request_json(f"{repo_api}/pullrequests?{query}", headers=headers)
    items = payload.get("value", []) if isinstance(payload, dict) else []
    return sorted(items, key=lambda x: x.get("closedDate", ""), reverse=True)


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


def _ticket_path_from_content(content: str) -> str | None:
    marker_re = re.compile(
        r"(?:^|\n)\s*(?:Automation marker:\s*)?"
        + re.escape(AUTO_TICKET_THREAD_PREFIX)
        + r"(?P<id>[A-Za-z0-9_-]+)\s*(?:$|\n)"
    )
    match = marker_re.search(content or "")
    if not match:
        return None
    encoded = match.group("id")
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _latest_thread_decision(comments: list[dict[str, Any]]) -> str | None:
    decision: str | None = None

    def _comment_sort_key(comment: dict[str, Any]) -> tuple[int, int]:
        try:
            comment_id = int(comment.get("id", 0))
        except Exception:
            comment_id = 0
        try:
            parent_id = int(comment.get("parentCommentId", 0))
        except Exception:
            parent_id = 0
        return (comment_id, parent_id)

    for comment in sorted(comments, key=_comment_sort_key):
        content = str(comment.get("content", "") or "")
        match = DECISION_RE.search(content)
        if match:
            decision = match.group("decision").lower()
    return decision


def _rejected_ticket_paths(threads: list[dict[str, Any]]) -> list[str]:
    rejected: set[str] = set()
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        marker_path: str | None = None
        for comment in comments:
            marker_path = _ticket_path_from_content(str(comment.get("content", "") or ""))
            if marker_path:
                break
        if not marker_path:
            continue

        decision = _latest_thread_decision(comments)
        if decision == "reject":
            rejected.add(marker_path)
    return sorted(rejected)


def _has_reject_signal(comments: list[str]) -> bool:
    for content in comments:
        if REJECT_CMD_RE.search(content):
            return True
        if "Auto-action: /reject detected." in content:
            return True
    return False


def _has_reject_vote(pr: dict[str, Any]) -> bool:
    for reviewer in pr.get("reviewers", []) or []:
        if not isinstance(reviewer, dict):
            continue
        try:
            vote = int(reviewer.get("vote", 0))
        except Exception:
            vote = 0
        if vote == -10:
            return True
    return False


def _has_merge_marker(comments: list[str], merge_commit: str) -> bool:
    marker = f"Automation marker: {MERGE_MARKER_PREFIX}{merge_commit}"
    return any(marker in content for content in comments)


def _is_permission_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "http 403" in msg or "forbidden" in msg


def _normalize_exclude_csv(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.lower() in {"", "none", "null", "n/a", "-", "_none_"}:
        return ""
    return normalized


def _diagnose_queue_permission(
    collection_uri: str,
    project: str,
    headers: dict[str, str],
    definition_id: int,
) -> None:
    definition_url = (
        f"{collection_uri}/{project}/_apis/build/definitions/{definition_id}"
        "?api-version=7.1"
    )
    try:
        payload = _request_json(definition_url, headers=headers)
        definition_name = str(payload.get("name", "") or "").strip()
        print(
            "Diagnostic: restore pipeline definition is readable "
            f"(id={definition_id}, name='{definition_name or 'n/a'}')."
        )
        print(
            "Diagnostic: queue call was forbidden, so missing permission is likely "
            "'Queue builds' on that restore pipeline (or pipeline is not authorized to use it)."
        )
    except Exception as diag_exc:
        print(
            "Diagnostic: unable to read restore pipeline definition "
            f"id={definition_id}. Details: {diag_exc}"
        )
        print(
            "Diagnostic: likely wrong definition ID, wrong project, or missing 'View builds' permission "
            "for the calling pipeline identity."
        )


def _queue_restore_pipeline(
    collection_uri: str,
    project: str,
    headers: dict[str, str],
    definition_id: int,
    baseline_branch: str,
    include_entra_update: bool,
    dry_run: bool,
    update_assignments: bool,
    remove_unmanaged: bool,
    max_workers: int,
    exclude_csv: str,
    restore_mode: str = "full",
    restore_paths_csv: str = "",
) -> dict[str, Any]:
    build_api = f"{collection_uri}/{project}/_apis/build/builds?api-version=7.1"
    template_parameters = {
        "dryRun": dry_run,
        "updateAssignments": update_assignments,
        "removeObjectsNotInBaseline": remove_unmanaged,
        "includeEntraUpdate": include_entra_update,
        "baselineBranch": baseline_branch,
        "maxWorkers": max_workers,
        "restoreMode": restore_mode,
    }
    if restore_mode == "selective" and restore_paths_csv.strip():
        template_parameters["restorePathsCsv"] = restore_paths_csv.strip()
    exclude_csv = _normalize_exclude_csv(exclude_csv)
    if exclude_csv:
        template_parameters["excludeCsv"] = exclude_csv
    body = {
        "definition": {"id": definition_id},
        "sourceBranch": _ref_from_branch(baseline_branch),
        "templateParameters": template_parameters,
    }
    return _request_json(build_api, headers=headers, method="POST", body=body)


def _post_pr_thread(repo_api: str, headers: dict[str, str], pr_id: int, content: str) -> None:
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        headers=headers,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": 1,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue restore after merged rolling PR with /reject decisions")
    parser.add_argument("--workload", required=True, choices=["intune", "entra"])
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--baseline-branch", required=True)
    args = parser.parse_args()

    if not _env_bool("AUTO_REMEDIATE_AFTER_MERGE", False):
        print("Post-merge auto-remediation disabled (set AUTO_REMEDIATE_AFTER_MERGE=true).")
        return 0

    token = os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    if not token:
        raise SystemExit("SYSTEM_ACCESSTOKEN is empty.")

    definition_raw = _env_text("AUTO_REMEDIATE_RESTORE_PIPELINE_ID", "")
    if not definition_raw:
        print(
            "Post-merge auto-remediation queue skipped: "
            "AUTO_REMEDIATE_RESTORE_PIPELINE_ID is empty."
        )
        return 0

    try:
        definition_id = int(definition_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid AUTO_REMEDIATE_RESTORE_PIPELINE_ID: {definition_raw}") from exc

    max_workers_raw = _env_text("AUTO_REMEDIATE_MAX_WORKERS", "10")
    try:
        max_workers = int(max_workers_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid AUTO_REMEDIATE_MAX_WORKERS: {max_workers_raw}") from exc

    lookback_hours_raw = _env_text("AUTO_REMEDIATE_AFTER_MERGE_LOOKBACK_HOURS", "168")
    try:
        lookback_hours = int(lookback_hours_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid AUTO_REMEDIATE_AFTER_MERGE_LOOKBACK_HOURS: {lookback_hours_raw}") from exc

    collection_uri = os.environ["SYSTEM_COLLECTIONURI"].rstrip("/")
    project = os.environ["SYSTEM_TEAMPROJECT"]
    repository_id = os.environ["BUILD_REPOSITORY_ID"]

    include_entra_update = _env_bool("AUTO_REMEDIATE_INCLUDE_ENTRA_UPDATE", False)
    dry_run = _env_bool("AUTO_REMEDIATE_DRY_RUN", False)
    update_assignments = _env_bool("AUTO_REMEDIATE_UPDATE_ASSIGNMENTS", True)
    remove_unmanaged = _env_bool("AUTO_REMEDIATE_REMOVE_OBJECTS", False)
    exclude_csv = _normalize_exclude_csv(_env_text("AUTO_REMEDIATE_EXCLUDE_CSV", ""))

    source_ref = _ref_from_branch(args.drift_branch)
    target_ref = _ref_from_branch(args.baseline_branch)
    repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{repository_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
    completed = _query_completed_prs(repo_api, headers, source_ref, target_ref)

    candidate: dict[str, Any] | None = None
    candidate_threads: list[dict[str, Any]] = []
    candidate_comments: list[str] = []

    for pr in completed:
        closed_at = _parse_iso_utc(str(pr.get("closedDate", "") or ""))
        if closed_at and closed_at < cutoff:
            continue

        merge_commit = (((pr.get("lastMergeCommit") or {}).get("commitId")) or "").strip()
        is_abandoned = not merge_commit
        if is_abandoned:
            merge_commit = (((pr.get("lastMergeSourceCommit") or {}).get("commitId")) or "").strip()
        if not merge_commit:
            continue

        pr_id = int(pr.get("pullRequestId"))
        threads = _threads(repo_api, headers, pr_id)
        comments = _thread_comment_contents(threads)

        has_reject = _has_reject_signal(comments)
        if is_abandoned:
            has_reject = has_reject or _has_reject_vote(pr)
        if not has_reject:
            continue

        if _has_merge_marker(comments, merge_commit):
            continue

        candidate = pr
        candidate_threads = threads
        candidate_comments = comments
        break

    if not candidate:
        print("No completed rolling PR requiring post-merge remediation was found.")
        return 0

    pr_id = int(candidate.get("pullRequestId"))
    merge_commit = (((candidate.get("lastMergeCommit") or {}).get("commitId")) or "").strip()
    is_abandoned = not merge_commit
    if is_abandoned:
        merge_commit = (((candidate.get("lastMergeSourceCommit") or {}).get("commitId")) or "").strip()
    rejected_paths = _rejected_ticket_paths(candidate_threads)

    restore_mode = "full"
    restore_paths_csv = ""
    if args.workload == "intune" and rejected_paths:
        restore_mode = "selective"
        restore_paths_csv = ",".join(rejected_paths)
        print(f"Post-merge remediation scope: selective ({len(rejected_paths)} rejected path(s)).")
        for path in rejected_paths:
            print(f" - {path}")
    else:
        print("Post-merge remediation scope: full.")

    try:
        queued = _queue_restore_pipeline(
            collection_uri=collection_uri,
            project=project,
            headers=headers,
            definition_id=definition_id,
            baseline_branch=args.baseline_branch,
            include_entra_update=include_entra_update,
            dry_run=dry_run,
            update_assignments=update_assignments,
            remove_unmanaged=remove_unmanaged,
            max_workers=max_workers,
            exclude_csv=exclude_csv,
            restore_mode=restore_mode,
            restore_paths_csv=restore_paths_csv,
        )
    except Exception as exc:
        if _is_permission_error(exc):
            print(
                "WARNING: Post-merge remediation queue skipped due permissions. "
                f"Definition={definition_id}. Details: {exc}"
            )
            _diagnose_queue_permission(collection_uri, project, headers, definition_id)
            print(
                "Grant 'Queue builds' permission for this pipeline identity on the restore pipeline "
                "and ensure the pipeline has access to run it."
            )
            return 0
        raise

    build_id = queued.get("id")
    build_url = ((queued.get("_links") or {}).get("web") or {}).get("href", "")
    if not build_url and build_id:
        build_url = f"{collection_uri}/{project}/_build/results?buildId={build_id}"

    marker = f"Automation marker: {MERGE_MARKER_PREFIX}{merge_commit}"
    pr_state = "abandoned" if is_abandoned else "merged"
    commit_label = "Source commit" if is_abandoned else "Merge commit"
    comment = (
        f"Auto-remediation queued after {pr_state} rolling PR with reviewer /reject decision(s).\n\n"
        f"Workload: {args.workload}\n"
        f"PR: #{pr_id}\n"
        f"{commit_label}: {merge_commit}\n"
        f"Restore pipeline definition: {definition_id}\n"
        f"Restore run: {build_url or '(queued)'}\n\n"
        f"{marker}"
    )

    try:
        _post_pr_thread(repo_api, headers, pr_id, comment)
    except Exception as exc:
        print(f"WARNING: Restore queued, but failed posting marker comment on PR #{pr_id}: {exc}")

    print(
        f"Queued post-merge remediation for PR #{pr_id} ({commit_label.lower()}={merge_commit}, buildId={build_id})."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"WARNING: Failed post-merge remediation check: {exc}", file=sys.stderr)
        raise
