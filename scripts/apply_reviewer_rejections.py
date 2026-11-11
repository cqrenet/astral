#!/usr/bin/env python3
"""Apply per-policy reviewer reject decisions on rolling drift PRs.

Reviewer decision format inside auto Change Needed threads:
- /reject   -> remove this file-level drift from rolling PR (reset to baseline)
- /accept   -> keep this file-level drift

Latest decision command in the thread wins.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from typing import Any

import common  # same directory as this script

_request_json = common.request_json
_run_git = common.run_git
_configure_git_identity = common.configure_git_identity



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
    return common.git_proc(repo_root, ["cat-file", "-e", f"{treeish}:{path}"], check=False).returncode == 0


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


def _changed_policy_paths(repo_root: str, baseline_branch: str, drift_branch: str) -> set[str]:
    """Files that differ between the branches, minus docs and generated reports."""
    paths = {p.strip() for p in _run_diff_name_only(repo_root, baseline_branch, drift_branch).splitlines()}
    return {p for p in paths if p and not common.is_doc_like(p) and not common.is_report_like(p)}


def _find_rejections(
    threads: list[dict[str, Any]], changed_paths: set[str], allowed: set[str]
) -> tuple[list[tuple[str, int]], int]:
    """(path, thread id) of change-ticket threads whose latest allowed decision is /reject, and threads examined."""
    rejections: list[tuple[str, int]] = []
    examined = 0
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        marker_path = next(
            (path for c in comments if (path := common.ticket_path_from_content(str(c.get("content", "") or "")))),
            None,
        )
        if not marker_path:
            continue
        examined += 1
        if marker_path not in changed_paths or common.latest_thread_decision(comments, allowed) != "reject":
            continue
        try:
            thread_id = int(thread.get("id"))
        except Exception:
            thread_id = -1
        rejections.append((marker_path, thread_id))
    return rejections, examined


def _reset_paths_to_baseline(repo_root: str, baseline_tree: str, paths: list[str]) -> int:
    """Stage each path as it is on baseline (or deleted when baseline lacks it); returns how many changed."""
    changed = 0
    for path in paths:
        if _git_path_exists(repo_root, baseline_tree, path):
            _run_git(repo_root, ["checkout", baseline_tree, "--", path])
            _run_git(repo_root, ["add", "--", path])
            changed += 1
        elif os.path.exists(os.path.join(repo_root, path)):
            _run_git(repo_root, ["rm", "-f", "--", path])
            changed += 1
    return changed


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

    allowed = common.allowed_reviewers()
    if not allowed:
        common.ci_log("warning", "PR_DECISION_ALLOWED_REVIEWERS is empty; reviewer /reject decisions are ignored.")
        return 0

    collection_uri = os.environ["SYSTEM_COLLECTIONURI"].rstrip("/")
    project = os.environ["SYSTEM_TEAMPROJECT"]
    repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{os.environ['BUILD_REPOSITORY_ID']}"
    drift_branch = common.normalize_branch(args.drift_branch)
    baseline_branch = common.normalize_branch(args.baseline_branch)

    query = urllib.parse.urlencode(
        {
            "searchCriteria.status": "active",
            "searchCriteria.sourceRefName": common.ref_from_branch(drift_branch),
            "searchCriteria.targetRefName": common.ref_from_branch(baseline_branch),
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
    pr_id = int(prs[0].get("pullRequestId"))

    _run_git(args.repo_root, ["fetch", "--quiet", "origin", baseline_branch])
    try:
        _run_git(args.repo_root, ["fetch", "--quiet", "origin", drift_branch])
    except RuntimeError as exc:
        if "couldn't find remote ref" in str(exc).lower() or "could not find remote ref" in str(exc).lower():
            print(f"Drift branch '{drift_branch}' not found on origin; nothing to reject.")
            return 0
        raise
    changed_paths = _changed_policy_paths(args.repo_root, baseline_branch, drift_branch)
    if not changed_paths:
        print("No changed policy paths in rolling PR; nothing to auto-reject.")
        return 0

    threads_payload = _request_json(f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1", token=token)
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    rejections, examined = _find_rejections(threads, changed_paths, allowed)
    if not rejections:
        print(
            "No /reject decisions found in auto policy threads "
            f"(examined={examined}, changed_paths={len(changed_paths)})."
        )
        return 0
    print(f"Detected /reject decisions in auto policy threads: {len(rejections)} (examined={examined}).")

    _run_git(args.repo_root, ["checkout", "--quiet", "--force", "-B", drift_branch, f"origin/{drift_branch}"])
    changed = _reset_paths_to_baseline(
        args.repo_root, f"origin/{baseline_branch}", sorted({path for path, _ in rejections})
    )
    if common.git_proc(args.repo_root, ["diff", "--cached", "--quiet"], check=False).returncode == 0:
        print("Reviewer /reject decisions found, but no effective diff remained after baseline reset.")
        return 0

    _configure_git_identity(args.repo_root)
    _run_git(args.repo_root, ["commit", "-m", f"Apply reviewer /reject decisions ({args.workload})"])
    _run_git(args.repo_root, ["push", "--force-with-lease", "origin", f"HEAD:{drift_branch}"])

    for _path, thread_id in rejections:
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
