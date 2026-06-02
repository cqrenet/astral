#!/usr/bin/env python3
"""Apply per-policy reviewer reject decisions on rolling drift PRs.

Reviewer decision format inside auto Change Needed threads:
- /reject   -> remove this file-level drift from rolling PR (reset to baseline)
- /accept   -> keep this file-level drift

Latest decision command in the thread wins.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
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

_request_json = common.request_json
_run_git = common.run_git
_configure_git_identity = common.configure_git_identity

AUTO_TICKET_THREAD_PREFIX = "AUTO-CHANGE-TICKET:"
THREAD_STATUS_FIXED = 2
THREAD_STATUS_WONT_FIX = 3
THREAD_STATUS_CLOSED = 4
THREAD_STATUS_BY_DESIGN = 5
DECISION_RE = re.compile(r"(?im)^\s*(?:/|#)?(?P<decision>reject|accept)\b")


def _run_diff_name_only(repo_root: str, baseline_branch: str, drift_branch: str) -> str:
    three_dot = f"origin/{baseline_branch}...origin/{drift_branch}"
    two_dot = f"origin/{baseline_branch}..origin/{drift_branch}"
    try:
        return _run_git(repo_root, ["diff", "--name-only", three_dot])
    except RuntimeError as exc:
        stderr = str(exc).lower()
        if "no merge base" not in stderr:
            raise
        print(
            "WARNING: No merge base for rolling branches "
            f"(origin/{baseline_branch}, origin/{drift_branch}); using direct diff."
        )
        return _run_git(repo_root, ["diff", "--name-only", two_dot])


def _git_path_exists(repo_root: str, treeish: str, path: str) -> bool:
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{treeish}:{path}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode == 0


def _normalize_branch_name(branch: str) -> str:
    b = branch.strip()
    if b.startswith("refs/heads/"):
        return b[len("refs/heads/") :]
    return b


def _thread_status_code(thread: dict[str, Any]) -> int:
    status = thread.get("status")
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        mapping = {
            "fixed": THREAD_STATUS_FIXED,
            "wontfix": THREAD_STATUS_WONT_FIX,
            "closed": THREAD_STATUS_CLOSED,
            "bydesign": THREAD_STATUS_BY_DESIGN,
        }
        return mapping.get(status.strip().lower(), 1)
    return 1


def _is_thread_resolved(thread: dict[str, Any]) -> bool:
    return _thread_status_code(thread) in (
        THREAD_STATUS_FIXED,
        THREAD_STATUS_WONT_FIX,
        THREAD_STATUS_CLOSED,
        THREAD_STATUS_BY_DESIGN,
    )


def _ticket_path_from_content(content: str) -> str | None:
    marker_re = re.compile(r"<!--\s*" + re.escape(AUTO_TICKET_THREAD_PREFIX) + r"(?P<id>[A-Za-z0-9_-]+)\s*-->")
    match = marker_re.search(content or "")
    if not match:
        return None
    encoded = match.group("id")
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _is_doc_like(path: str) -> bool:
    lp = path.lower()
    return lp.endswith(".md") or lp.endswith(".markdown") or "/docs/" in lp


def _is_report_like(path: str) -> bool:
    lp = path.lower()
    return "/reports/" in lp or "assignment report" in lp


def _latest_thread_decision(comments: list[dict[str, Any]]) -> str | None:
    decision: str | None = None

    def _comment_sort_key(c: dict[str, Any]) -> tuple[int, int]:
        try:
            cid = int(c.get("id", 0))
        except Exception:
            cid = 0
        try:
            parent = int(c.get("parentCommentId", 0))
        except Exception:
            parent = 0
        return (cid, parent)

    for comment in sorted(comments, key=_comment_sort_key):
        content = str(comment.get("content", "") or "")
        match = DECISION_RE.search(content)
        if match:
            decision = match.group("decision").lower()
    return decision


def _post_thread_comment(repo_api: str, pr_id: int, thread_id: int, token: str, content: str) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply reviewer /reject decisions for rolling PR threads")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--baseline-branch", required=True)
    args = parser.parse_args()

    token = os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    if not token:
        raise SystemExit("SYSTEM_ACCESSTOKEN is empty.")

    collection_uri = os.environ["SYSTEM_COLLECTIONURI"].rstrip("/")
    project = os.environ["SYSTEM_TEAMPROJECT"]
    repository_id = os.environ["BUILD_REPOSITORY_ID"]

    drift_branch = _normalize_branch_name(args.drift_branch)
    baseline_branch = _normalize_branch_name(args.baseline_branch)

    repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{repository_id}"
    source_ref = f"refs/heads/{drift_branch}"
    target_ref = f"refs/heads/{baseline_branch}"

    query = urllib.parse.urlencode(
        {
            "searchCriteria.status": "active",
            "searchCriteria.sourceRefName": source_ref,
            "searchCriteria.targetRefName": target_ref,
            "api-version": "7.1",
        },
        quote_via=urllib.parse.quote,
        safe="/",
    )
    payload = _request_json(f"{repo_api}/pullrequests?{query}", token=token)
    prs = payload.get("value", []) if isinstance(payload, dict) else []
    if not prs:
        print("No active rolling PR found; skipping reviewer reject sync.")
        return 0

    pr = prs[0]
    pr_id = int(pr.get("pullRequestId"))

    _run_git(args.repo_root, ["fetch", "--quiet", "origin", baseline_branch])
    try:
        _run_git(args.repo_root, ["fetch", "--quiet", "origin", drift_branch])
    except RuntimeError as exc:
        if "couldn't find remote ref" in str(exc).lower() or "could not find remote ref" in str(exc).lower():
            print(f"Drift branch '{drift_branch}' not found on origin; nothing to reject.")
            return 0
        raise
    diff_paths = _run_diff_name_only(args.repo_root, baseline_branch, drift_branch)
    changed_paths = {
        p.strip()
        for p in diff_paths.splitlines()
        if p.strip() and not _is_doc_like(p.strip()) and not _is_report_like(p.strip())
    }
    if not changed_paths:
        print("No changed policy paths in rolling PR; nothing to auto-reject.")
        return 0

    threads_payload = _request_json(f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1", token=token)
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []

    rejections: list[tuple[str, int]] = []
    examined_ticket_threads = 0
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        marker_path: str | None = None
        for c in comments:
            marker_path = _ticket_path_from_content(str(c.get("content", "") or ""))
            if marker_path:
                break
        if not marker_path:
            continue
        examined_ticket_threads += 1
        if marker_path not in changed_paths:
            continue

        decision = _latest_thread_decision(comments)
        if decision == "reject":
            try:
                thread_id = int(thread.get("id"))
            except Exception:
                thread_id = -1
            rejections.append((marker_path, thread_id))

    if not rejections:
        print(
            "No /reject decisions found in auto policy threads "
            f"(examined={examined_ticket_threads}, changed_paths={len(changed_paths)})."
        )
        return 0

    print(
        "Detected /reject decisions in auto policy threads: "
        f"{len(rejections)} (examined={examined_ticket_threads})."
    )

    _run_git(args.repo_root, ["checkout", "--quiet", "--force", "-B", drift_branch, f"origin/{drift_branch}"])

    changed = 0
    baseline_tree = f"origin/{baseline_branch}"
    for path, _thread_id in sorted(set(rejections)):
        if _git_path_exists(args.repo_root, baseline_tree, path):
            _run_git(args.repo_root, ["checkout", baseline_tree, "--", path])
            _run_git(args.repo_root, ["add", "--", path])
            changed += 1
        else:
            file_abs = os.path.join(args.repo_root, path)
            if os.path.exists(file_abs):
                _run_git(args.repo_root, ["rm", "-f", "--", path])
                changed += 1

    proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=args.repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0:
        print("Reviewer /reject decisions found, but no effective diff remained after baseline reset.")
        return 0

    _configure_git_identity(args.repo_root)

    commit_msg = f"Apply reviewer /reject decisions ({args.workload})"
    _run_git(args.repo_root, ["commit", "-m", commit_msg])
    _run_git(args.repo_root, ["push", "--force-with-lease", "origin", f"HEAD:{drift_branch}"])

    for path, thread_id in rejections:
        if thread_id <= 0:
            continue
        _post_thread_comment(
            repo_api=repo_api,
            pr_id=pr_id,
            thread_id=thread_id,
            token=token,
            content=(
                "Auto-action: /reject detected. This policy drift was reset to baseline on the rolling drift branch, "
                "so it is removed from the PR diff.\n\n"
                "If tenant rollback is required immediately, run restore pipeline as remediation."
            ),
        )

    print(
        f"Applied reviewer /reject decisions for {changed} path(s) in PR #{pr_id}; "
        f"drift branch '{drift_branch}' updated."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"WARNING: Failed to apply reviewer /reject decisions: {exc}", file=sys.stderr)
        raise
