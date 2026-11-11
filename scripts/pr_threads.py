#!/usr/bin/env python3
"""Review threads on the rolling PR: change tickets, full-detail summary threads and syncing."""

from __future__ import annotations

import base64
import re
from typing import Any

import common
from pr_ai import _format_ai_narrative_markdown
from pr_base import _debug, _request_json
from pr_changes import ChangeItem
from pr_description import DescriptionUpdate

log = common.log

AUTO_TICKET_THREAD_PREFIX = "AUTO-CHANGE-TICKET:"


AUTO_AI_REVIEW_THREAD_PREFIX = "AUTO-AI-REVIEW:"


AUTO_DETERMINISTIC_THREAD_PREFIX = "AUTO-DETERMINISTIC-SUMMARY:"


AUTO_REVIEWER_GUIDE_THREAD_PREFIX = "AUTO-REVIEWER-GUIDE:"


def _ticket_marker_for_path(path: str) -> str:
    encoded = base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")
    return f"Automation marker: {AUTO_TICKET_THREAD_PREFIX}{encoded}"


def _path_from_ticket_marker(content: str) -> str | None:
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


def _ai_review_thread_marker(workload: str) -> str:
    return f"Automation marker: {AUTO_AI_REVIEW_THREAD_PREFIX}{workload.strip().lower()}"


def _thread_has_matching_comment(comments: list[dict[str, Any]], content: str) -> bool:
    expected = content.strip()
    if not expected:
        return False
    for comment in comments:
        current = str(comment.get("content", "") or "").strip()
        if current == expected:
            return True
    return False


def _find_marked_thread(threads: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        for comment in comments:
            content = str(comment.get("content", "") or "")
            if marker in content:
                return thread
    return None


def _extract_thread_ticket(comments: list[dict[str, Any]], ticket_re: re.Pattern[str]) -> str:
    for comment in comments:
        content = str(comment.get("content", "") or "")
        match = ticket_re.search(content)
        if match:
            return match.group(0)
    return ""


def _create_ticket_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    path: str,
    ticket_pattern: str,
    change_summary: str,
    risk_summary: str,
) -> None:
    marker = _ticket_marker_for_path(path)
    _debug(f"Creating ticket thread for path: {path}")
    content = (
        "Change needed\n\n"
        f"Policy file: {path}\n\n"
        f"Detected change (auto): {change_summary}\n\n"
        f"Risk context: {risk_summary}\n\n"
        "Please reply with the related change ticket ID in this thread.\n"
        "Use /reject if this specific policy change should be excluded from the rolling PR.\n"
        "Use /accept to keep it in PR scope.\n"
        "Resolve this thread after reviewer confirmation.\n\n"
        f"Suggested ticket format: {ticket_pattern}\n\n"
        f"{marker}"
    )
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": common.THREAD_STATUS_ACTIVE,
        },
    )


def _plain_text_ai_narrative(text: str) -> str:
    formatted = _format_ai_narrative_markdown(text)
    if not formatted:
        return ""
    lines: list[str] = []
    for raw in formatted.splitlines():
        line = raw.rstrip()
        if line.startswith("#### "):
            lines.append(line[5:] + ":")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _build_full_ai_review_thread_content(workload: str, ai_summary: str) -> str:
    marker = _ai_review_thread_marker(workload)
    plain_ai = _plain_text_ai_narrative(ai_summary)
    return (
        "AI reviewer narrative (full)\n\n"
        "PR description uses a compact review summary because of Azure DevOps description size limits.\n\n"
        f"{plain_ai}\n\n"
        f"{marker}"
    ).strip()


def _create_ai_review_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    ai_summary: str,
) -> None:
    content = _build_full_ai_review_thread_content(workload, ai_summary)
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": common.THREAD_STATUS_ACTIVE,
        },
    )


def _add_thread_comment(
    repo_api: str,
    pr_id: int,
    thread_id: int,
    token: str,
    content: str,
) -> None:
    _debug(f"Adding comment to thread_id={thread_id}")
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads/{thread_id}/comments?api-version=7.1",
        token=token,
        method="POST",
        body={
            "parentCommentId": 0,
            "content": content,
            "commentType": 1,
        },
    )


def _sync_full_ai_review_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    ai_summary: str,
) -> bool:
    marker = _ai_review_thread_marker(workload)
    desired_content = _build_full_ai_review_thread_content(workload, ai_summary)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        _create_ai_review_thread(repo_api, pr_id, token, workload, ai_summary)
        return True

    comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
    if _thread_has_matching_comment(comments, desired_content):
        return False

    thread_id = _thread_id(thread)
    if thread_id <= 0:
        _create_ai_review_thread(repo_api, pr_id, token, workload, ai_summary)
        return True

    if common.is_thread_resolved(thread):
        _set_thread_status(repo_api, pr_id, thread_id, token, common.THREAD_STATUS_ACTIVE)
    _add_thread_comment(repo_api, pr_id, thread_id, token, desired_content)
    return True


def _deterministic_thread_marker(workload: str) -> str:
    return f"Automation marker: {AUTO_DETERMINISTIC_THREAD_PREFIX}{workload.strip().lower()}"


def _build_full_deterministic_thread_content(workload: str, deterministic_summary: str) -> str:
    marker = _deterministic_thread_marker(workload)
    return (
        "Automated review summary (full)\n\n"
        "PR description uses a compact review summary because of Azure DevOps description size limits.\n\n"
        f"{deterministic_summary}\n\n"
        f"{marker}"
    ).strip()


def _create_deterministic_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    deterministic_summary: str,
) -> None:
    content = _build_full_deterministic_thread_content(workload, deterministic_summary)
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": common.THREAD_STATUS_ACTIVE,
        },
    )


def _sync_deterministic_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    deterministic_summary: str,
) -> bool:
    marker = _deterministic_thread_marker(workload)
    desired_content = _build_full_deterministic_thread_content(workload, deterministic_summary)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        _create_deterministic_thread(repo_api, pr_id, token, workload, deterministic_summary)
        return True

    comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
    if _thread_has_matching_comment(comments, desired_content):
        return False

    thread_id = _thread_id(thread)
    if thread_id <= 0:
        _create_deterministic_thread(repo_api, pr_id, token, workload, deterministic_summary)
        return True

    if common.is_thread_resolved(thread):
        _set_thread_status(repo_api, pr_id, thread_id, token, common.THREAD_STATUS_ACTIVE)
    _add_thread_comment(repo_api, pr_id, thread_id, token, desired_content)
    return True


def _close_deterministic_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
) -> bool:
    marker = _deterministic_thread_marker(workload)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        return False
    thread_id = _thread_id(thread)
    if thread_id <= 0:
        return False
    if common.is_thread_resolved(thread):
        return False
    _set_thread_status(repo_api, pr_id, thread_id, token, common.THREAD_STATUS_CLOSED)
    return True


def _reviewer_guide_thread_marker(workload: str) -> str:
    return f"Automation marker: {AUTO_REVIEWER_GUIDE_THREAD_PREFIX}{workload.strip().lower()}"


def _build_full_reviewer_guide_thread_content(workload: str) -> str:
    marker = _reviewer_guide_thread_marker(workload)
    return (
        "## Reviewer Quick Actions\n\n"
        "### 1) Accept all changes\n"
        "- Merge PR to accept drift into baseline.\n\n"
        "### 2) Reject whole PR and revert\n"
        "- Set reviewer vote to **Reject**.\n"
        "- Abandon PR.\n"
        "- Auto-remediation queues restore (if `AUTO_REMEDIATE_ON_PR_REJECTION=true`).\n\n"
        "### 3) Reject only selected policy changes\n"
        "- In each `Change Needed` policy thread, comment `/reject` for changes you do not want.\n"
        "- Optional: use `/accept` for changes you want to keep.\n"
        "- Wait for review-sync pipeline (about 5 minutes) to update PR diff.\n"
        "- Merge remaining accepted changes.\n"
        "- Post-merge auto-remediation queues restore to reconcile tenant to merged baseline "
        "(if `AUTO_REMEDIATE_AFTER_MERGE=true`).\n\n"
        f"{marker}"
    ).strip()


def _create_reviewer_guide_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
) -> None:
    content = _build_full_reviewer_guide_thread_content(workload)
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": common.THREAD_STATUS_ACTIVE,
        },
    )


def _sync_reviewer_guide_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
) -> bool:
    marker = _reviewer_guide_thread_marker(workload)
    desired_content = _build_full_reviewer_guide_thread_content(workload)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        _create_reviewer_guide_thread(repo_api, pr_id, token, workload)
        return True

    comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
    if _thread_has_matching_comment(comments, desired_content):
        return False

    thread_id = _thread_id(thread)
    if thread_id <= 0:
        _create_reviewer_guide_thread(repo_api, pr_id, token, workload)
        return True

    if common.is_thread_resolved(thread):
        _set_thread_status(repo_api, pr_id, thread_id, token, common.THREAD_STATUS_ACTIVE)
    _add_thread_comment(repo_api, pr_id, thread_id, token, desired_content)
    return True


def _set_thread_status(
    repo_api: str,
    pr_id: int,
    thread_id: int,
    token: str,
    status: int,
) -> None:
    _debug(f"Updating thread status: thread_id={thread_id}, status={status}")
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads/{thread_id}?api-version=7.1",
        token=token,
        method="PATCH",
        body={"status": status},
    )


def _has_matching_detected_change_comment(
    comments: list[dict[str, Any]],
    change_summary: str,
    risk_summary: str,
) -> bool:
    expected_change = f"Detected change (auto): {change_summary}"
    expected_risk = f"Risk context: {risk_summary}"
    for comment in comments:
        content = str(comment.get("content", "") or "")
        if expected_change in content and expected_risk in content:
            return True
    return False


def _thread_id(thread: dict[str, Any]) -> int:
    try:
        tid = thread.get("id")
        if tid is None:
            return -1
        return int(tid)
    except Exception:
        return -1


def _thread_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    comments = thread.get("comments")
    return comments if isinstance(comments, list) else []


def _index_auto_threads(threads: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map policy path -> its auto change-ticket thread (identified by the marker comment)."""
    by_path: dict[str, dict[str, Any]] = {}
    for thread in threads:
        marker_path = next(
            (
                path
                for comment in _thread_comments(thread)
                if (path := _path_from_ticket_marker(str(comment.get("content", "") or "")))
            ),
            None,
        )
        if marker_path:
            by_path[marker_path] = thread
    return by_path


def _refresh_ticket_thread(
    thread: dict[str, Any],
    path: str,
    change_summary: str,
    risk_summary: str,
    *,
    repo_api: str,
    pr_id: int,
    token: str,
) -> None:
    """Re-open a resolved thread whose path drifted again and post the latest change if it is new."""
    thread_id = _thread_id(thread)
    if common.is_thread_resolved(thread) and thread_id > 0:
        _debug(f"Re-opening resolved auto thread for changed path: thread_id={thread_id}, path={path}")
        _set_thread_status(repo_api, pr_id, thread_id, token, common.THREAD_STATUS_ACTIVE)
        _add_thread_comment(
            repo_api=repo_api,
            pr_id=pr_id,
            thread_id=thread_id,
            token=token,
            content=(
                "Policy drift changed again for this path; re-opening thread for fresh review. "
                "Use `/accept` or `/reject` and resolve after decision."
            ),
        )
    if thread_id > 0 and not _has_matching_detected_change_comment(
        comments=_thread_comments(thread),
        change_summary=change_summary,
        risk_summary=risk_summary,
    ):
        _add_thread_comment(
            repo_api=repo_api,
            pr_id=pr_id,
            thread_id=thread_id,
            token=token,
            content=f"Detected change (auto): {change_summary}\n\nRisk context: {risk_summary}",
        )
    _debug(
        f"Auto thread already exists for path={path} "
        f"(thread_id={thread_id}, resolved={common.is_thread_resolved(thread)})"
    )


def _enforce_change_ticket_threads(
    repo_api: str,
    pr_id: int,
    token: str,
    changes: list[ChangeItem],
    ticket_pattern: str,
    change_context: dict[str, tuple[str, str]],
) -> tuple[int, int]:
    """Make sure every changed policy path has an open review thread; returns (created, closed_stale)."""
    tracked_paths = sorted(
        {item.path for item in changes if not common.is_doc_like(item.path) and not common.is_report_like(item.path)}
    )
    _debug(f"Tracked changed paths: count={len(tracked_paths)}")
    if tracked_paths:
        _debug(f"Tracked paths preview: {'; '.join(tracked_paths[:10])}")

    threads = _request_json(f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1", token=token).get("value", [])
    _debug(f"Fetched PR threads: total={len(threads)}")
    auto_threads_by_path = _index_auto_threads(threads)
    _debug(f"Detected auto ticket threads: total={len(auto_threads_by_path)}")

    # Close stale auto-generated threads when file is no longer part of drift.
    closed_count = 0
    tracked_set = set(tracked_paths)
    for path, thread in auto_threads_by_path.items():
        thread_id = _thread_id(thread)
        if path not in tracked_set and not common.is_thread_resolved(thread) and thread_id > 0:
            _debug(f"Closing stale auto thread: thread_id={thread_id}, path={path}")
            _set_thread_status(repo_api, pr_id, thread_id, token, common.THREAD_STATUS_CLOSED)
            closed_count += 1

    created_count = 0
    for path in tracked_paths:
        change_summary, risk_summary = change_context.get(
            path, ("`Modified` configuration content modified", "n/a")
        )
        thread = auto_threads_by_path.get(path)
        if thread:
            _refresh_ticket_thread(
                thread, path, change_summary, risk_summary, repo_api=repo_api, pr_id=pr_id, token=token
            )
            continue
        _create_ticket_thread(
            repo_api=repo_api,
            pr_id=pr_id,
            token=token,
            path=path,
            ticket_pattern=ticket_pattern,
            change_summary=change_summary,
            risk_summary=risk_summary,
        )
        created_count += 1

    return (created_count, closed_count)


def _log_thread_sync(synced: bool, pr_id: Any, workload: str, what: str, up_to_date_msg: str = "") -> None:
    if synced:
        log.info(f"Updated {what} for PR #{pr_id} ({workload}).")
    elif up_to_date_msg:
        log.info(f"{up_to_date_msg} for PR #{pr_id} ({workload}).")


def _sync_summary_threads(
    *,
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    deterministic: str,
    ai_summary: str | None,
    update: DescriptionUpdate,
) -> None:
    """Keep the full-detail PR threads consistent with what the description holds. Never raises."""
    common_args = {"repo_api": repo_api, "pr_id": pr_id, "token": token, "workload": workload}
    if update.compacted:
        try:
            synced = _sync_deterministic_thread(**common_args, deterministic_summary=deterministic)
            _log_thread_sync(
                synced, pr_id, workload, "full deterministic summary thread",
                "Full deterministic summary thread already up to date",
            )
        except Exception as exc:
            log.warning(f"Failed to sync full deterministic summary thread for PR #{pr_id}: {exc}")
    else:
        try:
            if _close_deterministic_thread(**common_args):
                log.info(
                    f"Closed full deterministic summary thread for PR #{pr_id} ({workload}) "
                    "because description now fits."
                )
        except Exception as exc:
            log.warning(f"Failed to close deterministic summary thread for PR #{pr_id}: {exc}")

    log.info(
        f"Updated automated review summary for PR #{pr_id} ({workload})."
        if update.summary_updated
        else f"Automated review summary already up to date for PR #{pr_id} ({workload})."
    )
    if ai_summary and update.ai_moved_to_thread:
        try:
            synced = _sync_full_ai_review_thread(**common_args, ai_summary=ai_summary)
            _log_thread_sync(
                synced, pr_id, workload, "full AI reviewer narrative thread",
                "Full AI reviewer narrative thread already up to date",
            )
        except Exception as exc:
            log.warning(f"Failed to sync full AI reviewer narrative thread for PR #{pr_id}: {exc}")
    try:
        synced = _sync_reviewer_guide_thread(**common_args)
        _log_thread_sync(synced, pr_id, workload, "reviewer guide thread", "Reviewer guide thread already up to date")
    except Exception as exc:
        log.warning(f"Failed to sync reviewer guide thread for PR #{pr_id}: {exc}")
