#!/usr/bin/env python3
"""Create/update rolling drift PR and optionally queue remediation after rejection."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any

import common  # same directory as this script

_env_text = common.env_text
_env_bool = common.env_bool
_request_json = common.request_json
_run_git = common.run_git


def _query_prs(
    repo_api: str,
    headers: dict[str, str],
    source_ref: str,
    target_ref: str,
    status: str,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "searchCriteria.status": status,
            "searchCriteria.sourceRefName": source_ref,
            "searchCriteria.targetRefName": target_ref,
            "api-version": "7.1",
        },
        quote_via=urllib.parse.quote,
        safe="/",
    )
    url = f"{repo_api}/pullrequests?{query}"
    payload = _request_json(url, headers=headers)
    return payload.get("value", []) if isinstance(payload, dict) else []


def _pr_web_url(pr_payload: dict[str, Any]) -> str:
    pr_id = pr_payload.get("pullRequestId")
    return (
        pr_payload.get("url", "")
        .replace("_apis/git/repositories", "_git")
        .replace(f"/pullRequests/{pr_id}", f"/pullrequest/{pr_id}")
    )



def _current_tree_id(repo_root: str) -> str:
    return _run_git(repo_root, ["rev-parse", "HEAD^{tree}"])


def _tree_id_for_commitish(repo_root: str, commitish: str) -> str:
    return _run_git(repo_root, ["rev-parse", f"{commitish}^{{tree}}"])


def _ref_has_commit(repo_root: str, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode == 0


def _commit_tree_id(repo_api: str, headers: dict[str, str], commit_id: str) -> str:
    url = f"{repo_api}/commits/{commit_id}?api-version=7.1"
    payload = _request_json(url, headers=headers)
    tree_id = payload.get("treeId", "") if isinstance(payload, dict) else ""
    return tree_id.strip()


def _latest_pr_by_creation(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(prs, key=lambda x: x.get("creationDate", ""), reverse=True)


def _is_workload_config_path(path: str, workload_dir: str, backup_folder: str, reports_subdir: str) -> bool:
    lp = common.normalize_repo_path(path).lower()
    backup_norm = common.normalize_repo_path(backup_folder).lower().strip("/")
    workload_norm = common.normalize_repo_path(workload_dir).lower().strip("/")
    reports_norm = common.normalize_repo_path(reports_subdir).lower().strip("/")

    if not backup_norm or not workload_norm:
        return False

    workload_prefix = f"{backup_norm}/{workload_norm}/"
    if not lp.startswith(workload_prefix):
        return False

    if reports_norm and lp.startswith(f"{backup_norm}/{reports_norm}/"):
        return False

    if common.is_doc_like(lp) or common.is_report_like(lp):
        return False
    return True


def _config_fingerprint_from_local_tree(
    repo_root: str, commitish: str, workload_dir: str, backup_folder: str, reports_subdir: str
) -> str:
    backup_norm = common.normalize_repo_path(backup_folder).strip("/")
    workload_norm = common.normalize_repo_path(workload_dir).strip("/")
    path_prefix = f"{backup_norm}/{workload_norm}" if backup_norm and workload_norm else ""
    if not path_prefix:
        return ""

    try:
        out = _run_git(repo_root, ["ls-tree", "-r", "--full-tree", commitish, "--", path_prefix])
    except Exception:
        return ""

    pairs: list[str] = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        left, rel_path = line.split("\t", 1)
        parts = left.split()
        if len(parts) < 3 or parts[1] != "blob":
            continue
        blob_id = parts[2].strip()
        if not blob_id:
            continue
        if not _is_workload_config_path(rel_path, workload_dir, backup_folder, reports_subdir):
            continue
        pairs.append(f"{common.normalize_repo_path(rel_path)}\t{blob_id}")

    if not pairs:
        return ""
    pairs.sort(key=lambda item: item.lower())
    joined = "\n".join(pairs).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def _config_fingerprint_from_tree_api(
    repo_api: str, headers: dict[str, str], tree_id: str, workload_dir: str, backup_folder: str, reports_subdir: str
) -> str:
    if not tree_id:
        return ""
    url = f"{repo_api}/trees/{tree_id}?recursive=true&api-version=7.1"
    payload = _request_json(url, headers=headers)
    entries = payload.get("treeEntries", []) if isinstance(payload, dict) else []

    pairs: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("gitObjectType", "")).lower() != "blob":
            continue
        rel_path = str(entry.get("relativePath", ""))
        if not _is_workload_config_path(rel_path, workload_dir, backup_folder, reports_subdir):
            continue
        blob_id = str(entry.get("objectId", "")).strip()
        if not blob_id:
            continue
        pairs.append(f"{common.normalize_repo_path(rel_path)}\t{blob_id}")

    if not pairs:
        return ""
    pairs.sort(key=lambda item: item.lower())
    joined = "\n".join(pairs).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def _workload_config_diff_exists(
    repo_root: str,
    baseline_commitish: str,
    drift_commitish: str,
    workload_dir: str,
    backup_folder: str,
    reports_subdir: str,
) -> bool:
    baseline_fingerprint = _config_fingerprint_from_local_tree(
        repo_root=repo_root,
        commitish=baseline_commitish,
        workload_dir=workload_dir,
        backup_folder=backup_folder,
        reports_subdir=reports_subdir,
    )
    drift_fingerprint = _config_fingerprint_from_local_tree(
        repo_root=repo_root,
        commitish=drift_commitish,
        workload_dir=workload_dir,
        backup_folder=backup_folder,
        reports_subdir=reports_subdir,
    )

    if baseline_fingerprint and drift_fingerprint:
        return baseline_fingerprint != drift_fingerprint

    try:
        return _tree_id_for_commitish(repo_root, baseline_commitish) != _tree_id_for_commitish(repo_root, drift_commitish)
    except Exception:
        return True


def _find_matching_abandoned_pr(
    repo_api: str,
    headers: dict[str, str],
    abandoned_prs: list[dict[str, Any]],
    drift_tree: str,
    repo_root: str,
    workload_dir: str,
    backup_folder: str,
    reports_subdir: str,
    drift_commitish: str,
) -> tuple[dict[str, Any] | None, str]:
    current_config_fingerprint = _config_fingerprint_from_local_tree(
        repo_root=repo_root,
        commitish=drift_commitish,
        workload_dir=workload_dir,
        backup_folder=backup_folder,
        reports_subdir=reports_subdir,
    )
    tree_fingerprint_cache: dict[str, str] = {}

    for pr in _latest_pr_by_creation(abandoned_prs):
        commit_id = (
            ((pr.get("lastMergeSourceCommit") or {}).get("commitId"))
            or ((pr.get("lastMergeCommit") or {}).get("commitId"))
            or ""
        ).strip()
        if not commit_id:
            continue
        try:
            pr_tree = _commit_tree_id(repo_api, headers, commit_id)
        except Exception:
            continue
        if pr_tree and pr_tree == drift_tree:
            return pr, "exact-tree"

        if current_config_fingerprint and pr_tree:
            if pr_tree not in tree_fingerprint_cache:
                try:
                    tree_fingerprint_cache[pr_tree] = _config_fingerprint_from_tree_api(
                        repo_api=repo_api,
                        headers=headers,
                        tree_id=pr_tree,
                        workload_dir=workload_dir,
                        backup_folder=backup_folder,
                        reports_subdir=reports_subdir,
                    )
                except Exception:
                    tree_fingerprint_cache[pr_tree] = ""
            if tree_fingerprint_cache[pr_tree] and tree_fingerprint_cache[pr_tree] == current_config_fingerprint:
                return pr, "config-fingerprint"

    return None, ""


def _pr_has_reject_vote(pr: dict[str, Any]) -> bool:
    # Only allowlisted reviewers count: anyone can add themselves as a reviewer and vote.
    return common.has_allowed_reject_vote(pr, common.allowed_reviewers())


def _current_pr_merge_strategy(pr: dict[str, Any]) -> str:
    completion_options = pr.get("completionOptions")
    if not isinstance(completion_options, dict):
        return ""
    raw = str(completion_options.get("mergeStrategy") or "").strip()
    if not raw:
        return ""
    return common.normalize_merge_strategy(raw)


def _build_description(workload: str, drift_branch: str, baseline_branch: str, build_number: str, build_id: str) -> str:
    is_entra = workload.lower() == "entra"
    lead = "Rolling Entra drift PR — backup pipeline" if is_entra else "Rolling drift PR — backup pipeline"
    return (
        f"{lead} run `{build_number}` (build {build_id})\n\n"
        f"Source: `{drift_branch}` → Target: `{baseline_branch}`\n"
    )


def _threads_with_marker(repo_api: str, headers: dict[str, str], pr_id: int, marker: str) -> bool:
    url = f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1"
    payload = _request_json(url, headers=headers)
    threads = payload.get("value", []) if isinstance(payload, dict) else []
    for thread in threads:
        for comment in thread.get("comments", []):
            content = str(comment.get("content", ""))
            if marker in content:
                return True
    return False


@dataclass
class _Remediation:
    """Settings for queueing the restore pipeline when a rejected drift snapshot reappears."""

    enabled: bool
    definition_id: int | str
    request: common.RestoreRequest


def _load_remediation(baseline_branch: str) -> _Remediation:
    enabled = _env_bool("AUTO_REMEDIATE_ON_PR_REJECTION", False)
    definition_raw = _env_text("AUTO_REMEDIATE_RESTORE_PIPELINE_ID", "")
    if enabled and not definition_raw:
        print(
            "WARNING: AUTO_REMEDIATE_ON_PR_REJECTION=true but AUTO_REMEDIATE_RESTORE_PIPELINE_ID is empty; "
            "remediation queueing disabled for this run.",
            file=sys.stderr,
        )
        enabled = False
    max_workers_raw = _env_text("AUTO_REMEDIATE_MAX_WORKERS", "10")
    try:
        max_workers = int(max_workers_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid AUTO_REMEDIATE_MAX_WORKERS value: {max_workers_raw}") from exc
    definition_id = (
        common.parse_pipeline_id(definition_raw, "AUTO_REMEDIATE_RESTORE_PIPELINE_ID") if definition_raw else 0
    )
    return _Remediation(
        enabled=enabled,
        definition_id=definition_id,
        request=common.RestoreRequest(
            definition_id=definition_id,
            baseline_branch=baseline_branch,
            include_entra_update=_env_bool("AUTO_REMEDIATE_INCLUDE_ENTRA_UPDATE", False),
            dry_run=_env_bool("AUTO_REMEDIATE_DRY_RUN", True),  # fail safe: unset = dry run
            update_assignments=_env_bool("AUTO_REMEDIATE_UPDATE_ASSIGNMENTS", True),
            remove_unmanaged=_env_bool("AUTO_REMEDIATE_REMOVE_OBJECTS", False),
            max_workers=max_workers,
            exclude_csv=_env_text("AUTO_REMEDIATE_EXCLUDE_CSV", ""),
        ),
    )


def _report_pr(pr_id: Any, web_url: str, suppressed: bool = False) -> None:
    if pr_id is not None:
        common.ci_set_output("DRIFT_PR_ID", pr_id)
    if web_url:
        common.ci_set_output("DRIFT_PR_URL", web_url)
    common.ci_set_output("DRIFT_PR_SUPPRESSED", int(suppressed))


def _fetch_branches(repo_root: str, baseline_branch: str, drift_branch: str) -> None:
    _run_git(repo_root, ["fetch", "--quiet", "origin", baseline_branch])
    try:
        _run_git(repo_root, ["fetch", "--quiet", "origin", drift_branch])
    except RuntimeError as exc:
        message = str(exc).lower()
        if "couldn't find remote ref" not in message and "could not find remote ref" not in message:
            raise
        # Drift branch may not exist yet; the caller falls back to HEAD.


def _resolve_commitishes(repo_root: str, baseline_branch: str, drift_branch: str) -> tuple[str, str]:
    """Fetch both branches; prefer origin/<branch>, falling back to the local baseline and to HEAD."""
    _fetch_branches(repo_root, baseline_branch, drift_branch)
    baseline = f"origin/{baseline_branch}" if _ref_has_commit(repo_root, f"origin/{baseline_branch}") else baseline_branch
    drift = f"origin/{drift_branch}" if _ref_has_commit(repo_root, f"origin/{drift_branch}") else "HEAD"
    return baseline, drift


def _create_rolling_pr(
    repo_api: str,
    headers: dict[str, str],
    source_ref: str,
    target_ref: str,
    title: str,
    description: str,
    merge_strategy: str,
) -> dict[str, Any]:
    return _request_json(
        f"{repo_api}/pullrequests?api-version=7.1",
        headers=headers,
        method="POST",
        body={
            "sourceRefName": source_ref,
            "targetRefName": target_ref,
            "title": title,
            "description": description,
            "isDraft": _env_bool("ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS", False),
            "completionOptions": {"mergeStrategy": merge_strategy},
        },
    )


def _update_existing_pr(
    pr: dict[str, Any],
    repo_api: str,
    headers: dict[str, str],
    title: str,
    description: str,
    merge_strategy: str,
    workload: str,
) -> None:
    pr_id = pr.get("pullRequestId")
    current_description = str(pr.get("description") or "")
    needs_patch = (
        str(pr.get("title") or "") != title
        or not current_description.strip()
        or _current_pr_merge_strategy(pr) != merge_strategy
    )
    web_url = _pr_web_url(pr)
    if needs_patch:
        _request_json(
            f"{repo_api}/pullrequests/{pr_id}?api-version=7.1",
            headers=headers,
            method="PATCH",
            body={
                "title": title,
                "description": current_description if current_description.strip() else description,
                "completionOptions": {"mergeStrategy": merge_strategy},
            },
        )
        print(f"Updated rolling {workload} PR #{pr_id}: {web_url}")
    else:
        print(f"Rolling {workload} PR #{pr_id} already up to date: {web_url}")
    _report_pr(pr_id, web_url)


def _remediate_rejected_pr(
    rejected_pr: dict[str, Any],
    remediation: _Remediation,
    *,
    repo_api: str,
    collection_uri: str,
    project: str,
    headers: dict[str, str],
    workload: str,
    drift_tree: str,
) -> None:
    """The latest drift equals a rejected PR: queue the restore once, never recreate the PR."""
    pr_id = int(rejected_pr["pullRequestId"])
    if not _pr_has_reject_vote(rejected_pr):
        print(
            "Matched abandoned PR without reviewer Reject vote; "
            "skipping remediation and suppressing PR recreation for this unchanged drift snapshot."
        )
        return
    if not remediation.enabled:
        print(
            "Suppressed PR recreation: latest drift matches a rejected PR, "
            "but AUTO_REMEDIATE_ON_PR_REJECTION is disabled."
        )
        return
    marker = f"Automation marker: AUTO-REMEDIATE-TREE:{drift_tree}"
    if _threads_with_marker(repo_api, headers, pr_id, marker):
        print("Suppressed PR recreation: latest drift matches a previously rejected PR and remediation was already queued.")
        return

    queued = common.queue_restore_pipeline(collection_uri, project, headers, remediation.request)
    build_url = common.build_web_url(queued, collection_uri, project)
    comment = (
        "Auto-remediation queued because the latest drift matches a rejected PR.\n\n"
        f"Workload: {workload}\n"
        f"Rejected PR: #{pr_id}\n"
        f"Drift tree: {drift_tree}\n"
        f"Restore pipeline definition: {remediation.definition_id}\n"
        f"Restore run: {build_url or '(queued)'}\n\n"
        f"{marker}"
    )
    try:
        common.post_pr_thread(repo_api, headers, pr_id, comment)
    except Exception as exc:
        print(f"WARNING: Remediation queued, but failed to post PR thread on #{pr_id}: {exc}")
    print(
        f"Queued remediation pipeline run (definition={remediation.definition_id}, "
        f"buildId={queued.get('id')}) and suppressed PR recreation."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure rolling PR exists with optional remediation-on-rejection")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workload", required=True, choices=["intune", "entra"])
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--baseline-branch", required=True)
    parser.add_argument("--pr-title", required=True)
    args = parser.parse_args()

    token = os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    if not token:
        raise SystemExit("SYSTEM_ACCESSTOKEN is empty. Enable OAuth token access for this pipeline.")

    collection_uri = os.environ["SYSTEM_COLLECTIONURI"].rstrip("/")
    project = os.environ["SYSTEM_TEAMPROJECT"]
    repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{os.environ['BUILD_REPOSITORY_ID']}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    pr_merge_strategy = common.normalize_merge_strategy(_env_text("ROLLING_PR_MERGE_STRATEGY", "rebase"))
    drift_branch = common.normalize_branch(args.drift_branch)
    baseline_branch = common.normalize_branch(args.baseline_branch)
    remediation = _load_remediation(baseline_branch)
    backup_folder = _env_text("BACKUP_FOLDER", "tenant-state")
    reports_subdir = _env_text("REPORTS_SUBDIR", "reports")
    workload_dir = _env_text(
        "INTUNE_BACKUP_SUBDIR" if args.workload == "intune" else "ENTRA_BACKUP_SUBDIR",
        args.workload,
    )
    source_ref = common.ref_from_branch(drift_branch)
    target_ref = common.ref_from_branch(baseline_branch)

    description = _build_description(
        args.workload,
        drift_branch,
        baseline_branch,
        os.environ.get("BUILD_BUILDNUMBER", ""),
        os.environ.get("BUILD_BUILDID", ""),
    )
    print(f"Rolling PR completion merge strategy: {pr_merge_strategy}")

    active_prs = _query_prs(repo_api, headers, source_ref, target_ref, "active")
    if active_prs:
        _update_existing_pr(
            active_prs[0], repo_api, headers, args.pr_title, description, pr_merge_strategy, args.workload
        )
        return 0

    baseline_commitish, drift_commitish = _resolve_commitishes(args.repo_root, baseline_branch, drift_branch)
    if not _workload_config_diff_exists(
        repo_root=args.repo_root,
        baseline_commitish=baseline_commitish,
        drift_commitish=drift_commitish,
        workload_dir=workload_dir,
        backup_folder=backup_folder,
        reports_subdir=reports_subdir,
    ):
        print(
            "Suppressed PR recreation: drift branch has no effective workload configuration diff "
            f"against {baseline_branch}."
        )
        _report_pr(None, "", suppressed=True)
        return 0

    drift_tree = _tree_id_for_commitish(args.repo_root, drift_commitish)
    abandoned_prs = _query_prs(repo_api, headers, source_ref, target_ref, "abandoned")
    matching_abandoned, match_reason = _find_matching_abandoned_pr(
        repo_api=repo_api,
        headers=headers,
        abandoned_prs=abandoned_prs,
        drift_tree=drift_tree,
        repo_root=args.repo_root,
        workload_dir=workload_dir,
        backup_folder=backup_folder,
        reports_subdir=reports_subdir,
        drift_commitish=drift_commitish,
    )
    if matching_abandoned:
        if match_reason == "config-fingerprint":
            print("Matched abandoned PR using configuration fingerprint (ignoring docs/reports churn).")
        _remediate_rejected_pr(
            matching_abandoned,
            remediation,
            repo_api=repo_api,
            collection_uri=collection_uri,
            project=project,
            headers=headers,
            workload=args.workload,
            drift_tree=drift_tree,
        )
        _report_pr(None, "", suppressed=True)
        return 0

    if abandoned_prs:
        print(
            f"No abandoned PR snapshot match for current drift tree (checked {len(abandoned_prs)} abandoned PR(s)); "
            "creating/updating rolling PR."
        )
    created = _create_rolling_pr(
        repo_api,
        headers,
        source_ref,
        target_ref,
        args.pr_title,
        description,
        pr_merge_strategy,
    )
    web_url = _pr_web_url(created)
    print(f"Created rolling {args.workload} PR #{created.get('pullRequestId')}: {web_url}")
    _report_pr(created.get("pullRequestId"), web_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: Failed to ensure rolling PR: {exc}", file=sys.stderr)
        raise
