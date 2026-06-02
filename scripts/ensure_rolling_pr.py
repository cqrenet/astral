#!/usr/bin/env python3
"""Create/update rolling drift PR and optionally queue remediation after rejection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

_env_text = common.env_text
_env_bool = common.env_bool
_normalize_exclude_csv = common.normalize_exclude_csv
_normalize_merge_strategy = common.normalize_merge_strategy
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


def _normalize_branch(branch: str) -> str:
    b = branch.strip()
    if b.startswith("refs/heads/"):
        return b[len("refs/heads/") :]
    return b


def _ref_from_branch(branch: str) -> str:
    return f"refs/heads/{_normalize_branch(branch)}"


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


def _normalize_repo_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def _is_doc_like(path: str) -> bool:
    lp = _normalize_repo_path(path).lower()
    if lp.endswith((".md", ".html", ".htm", ".pdf", ".csv", ".txt")):
        return True
    return "/docs/" in f"/{lp}" or "/object inventory/" in f"/{lp}"


def _is_report_like(path: str) -> bool:
    lp = _normalize_repo_path(path).lower()
    return "/reports/" in f"/{lp}" or "/assignment report/" in f"/{lp}"


def _is_workload_config_path(path: str, workload_dir: str, backup_folder: str, reports_subdir: str) -> bool:
    lp = _normalize_repo_path(path).lower()
    backup_norm = _normalize_repo_path(backup_folder).lower().strip("/")
    workload_norm = _normalize_repo_path(workload_dir).lower().strip("/")
    reports_norm = _normalize_repo_path(reports_subdir).lower().strip("/")

    if not backup_norm or not workload_norm:
        return False

    workload_prefix = f"{backup_norm}/{workload_norm}/"
    if not lp.startswith(workload_prefix):
        return False

    if reports_norm and lp.startswith(f"{backup_norm}/{reports_norm}/"):
        return False

    if _is_doc_like(lp) or _is_report_like(lp):
        return False
    return True


def _config_fingerprint_from_local_tree(
    repo_root: str, commitish: str, workload_dir: str, backup_folder: str, reports_subdir: str
) -> str:
    backup_norm = _normalize_repo_path(backup_folder).strip("/")
    workload_norm = _normalize_repo_path(workload_dir).strip("/")
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
        pairs.append(f"{_normalize_repo_path(rel_path)}\t{blob_id}")

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
        pairs.append(f"{_normalize_repo_path(rel_path)}\t{blob_id}")

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
    reviewers = pr.get("reviewers", [])
    if not isinstance(reviewers, list):
        return False
    for reviewer in reviewers:
        if not isinstance(reviewer, dict):
            continue
        try:
            vote = int(reviewer.get("vote", 0))
        except Exception:
            vote = 0
        if vote == -10:
            return True
    return False


def _current_pr_merge_strategy(pr: dict[str, Any]) -> str:
    completion_options = pr.get("completionOptions")
    if not isinstance(completion_options, dict):
        return ""
    raw = str(completion_options.get("mergeStrategy") or "").strip()
    if not raw:
        return ""
    return _normalize_merge_strategy(raw)


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
) -> dict[str, Any]:
    build_api = f"{collection_uri}/{project}/_apis/build/builds?api-version=7.1"
    template_parameters = {
        "dryRun": dry_run,
        "updateAssignments": update_assignments,
        "removeObjectsNotInBaseline": remove_unmanaged,
        "includeEntraUpdate": include_entra_update,
        "baselineBranch": baseline_branch,
        "maxWorkers": max_workers,
    }
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
    url = f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1"
    body = {
        "comments": [{"parentCommentId": 0, "content": content, "commentType": 1}],
        "status": "active",
    }
    _request_json(url, headers=headers, method="POST", body=body)


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
    repository_id = os.environ["BUILD_REPOSITORY_ID"]
    build_number = os.environ.get("BUILD_BUILDNUMBER", "")
    build_id = os.environ.get("BUILD_BUILDID", "")

    auto_remediate = _env_bool("AUTO_REMEDIATE_ON_PR_REJECTION", False)
    include_entra_update = _env_bool("AUTO_REMEDIATE_INCLUDE_ENTRA_UPDATE", False)
    remediation_def_id_raw = _env_text("AUTO_REMEDIATE_RESTORE_PIPELINE_ID", "")
    remediation_dry_run = _env_bool("AUTO_REMEDIATE_DRY_RUN", False)
    remediation_update_assignments = _env_bool("AUTO_REMEDIATE_UPDATE_ASSIGNMENTS", True)
    remediation_remove_unmanaged = _env_bool("AUTO_REMEDIATE_REMOVE_OBJECTS", False)
    remediation_max_workers_raw = _env_text("AUTO_REMEDIATE_MAX_WORKERS", "10")
    remediation_exclude_csv = _normalize_exclude_csv(_env_text("AUTO_REMEDIATE_EXCLUDE_CSV", ""))
    pr_merge_strategy = _normalize_merge_strategy(_env_text("ROLLING_PR_MERGE_STRATEGY", "rebase"))
    create_as_draft = _env_bool("ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS", False)

    try:
        remediation_max_workers = int(remediation_max_workers_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid AUTO_REMEDIATE_MAX_WORKERS value: {remediation_max_workers_raw}") from exc

    if auto_remediate and not remediation_def_id_raw:
        print(
            "WARNING: AUTO_REMEDIATE_ON_PR_REJECTION=true but AUTO_REMEDIATE_RESTORE_PIPELINE_ID is empty; "
            "remediation queueing disabled for this run.",
            file=sys.stderr,
        )
        auto_remediate = False

    try:
        remediation_def_id = int(remediation_def_id_raw) if remediation_def_id_raw else 0
    except ValueError as exc:
        raise SystemExit(
            f"Invalid AUTO_REMEDIATE_RESTORE_PIPELINE_ID value: {remediation_def_id_raw}"
        ) from exc

    drift_branch = _normalize_branch(args.drift_branch)
    baseline_branch = _normalize_branch(args.baseline_branch)
    backup_folder = _env_text("BACKUP_FOLDER", "tenant-state")
    reports_subdir = _env_text("REPORTS_SUBDIR", "reports")
    workload_dir = _env_text(
        "INTUNE_BACKUP_SUBDIR" if args.workload == "intune" else "ENTRA_BACKUP_SUBDIR",
        args.workload,
    )
    source_ref = _ref_from_branch(drift_branch)
    target_ref = _ref_from_branch(baseline_branch)

    repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{repository_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    description = _build_description(args.workload, drift_branch, baseline_branch, build_number, build_id)
    completion_options = {"mergeStrategy": pr_merge_strategy}
    print(f"Rolling PR completion merge strategy: {pr_merge_strategy}")

    active_prs = _query_prs(repo_api, headers, source_ref, target_ref, "active")
    if active_prs:
        pr = active_prs[0]
        pr_id = pr.get("pullRequestId")
        current_title = str(pr.get("title") or "")
        current_description = str(pr.get("description") or "")
        current_merge_strategy = _current_pr_merge_strategy(pr)
        desired_description = current_description if current_description.strip() else description
        needs_patch = (
            current_title != args.pr_title
            or not current_description.strip()
            or current_merge_strategy != pr_merge_strategy
        )
        if needs_patch:
            update_url = f"{repo_api}/pullrequests/{pr_id}?api-version=7.1"
            _request_json(
                update_url,
                headers=headers,
                method="PATCH",
                body={
                    "title": args.pr_title,
                    "description": desired_description,
                    "completionOptions": completion_options,
                },
            )
        web_url = _pr_web_url(pr)
        if needs_patch:
            print(f"Updated rolling {args.workload} PR #{pr_id}: {web_url}")
        else:
            print(f"Rolling {args.workload} PR #{pr_id} already up to date: {web_url}")
        print(f"##vso[task.setvariable variable=DRIFT_PR_ID;isOutput=true]{pr_id}")
        if web_url:
            print(f"##vso[task.setvariable variable=DRIFT_PR_URL;isOutput=true]{web_url}")
        print("##vso[task.setvariable variable=DRIFT_PR_SUPPRESSED;isOutput=true]0")
        return 0

    _run_git(args.repo_root, ["fetch", "--quiet", "origin", baseline_branch])
    try:
        _run_git(args.repo_root, ["fetch", "--quiet", "origin", drift_branch])
    except RuntimeError as exc:
        if "couldn't find remote ref" in str(exc).lower() or "could not find remote ref" in str(exc).lower():
            pass  # Drift branch may not exist yet; fallback to HEAD below.
        else:
            raise
    baseline_commitish = f"origin/{baseline_branch}" if _ref_has_commit(args.repo_root, f"origin/{baseline_branch}") else baseline_branch
    drift_commitish = f"origin/{drift_branch}" if _ref_has_commit(args.repo_root, f"origin/{drift_branch}") else "HEAD"
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
        print("##vso[task.setvariable variable=DRIFT_PR_SUPPRESSED;isOutput=true]1")
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
            print(
                "Matched abandoned PR using configuration fingerprint "
                "(ignoring docs/reports churn)."
            )
        pr_id = int(matching_abandoned["pullRequestId"])
        if not _pr_has_reject_vote(matching_abandoned):
            print(
                "Matched abandoned PR without reviewer Reject vote; "
                "skipping remediation and suppressing PR recreation for this unchanged drift snapshot."
            )
            print("##vso[task.setvariable variable=DRIFT_PR_SUPPRESSED;isOutput=true]1")
            return 0

        if not auto_remediate:
            print(
                "Suppressed PR recreation: latest drift matches a rejected PR, "
                "but AUTO_REMEDIATE_ON_PR_REJECTION is disabled."
            )
            print("##vso[task.setvariable variable=DRIFT_PR_SUPPRESSED;isOutput=true]1")
            return 0

        marker = f"Automation marker: AUTO-REMEDIATE-TREE:{drift_tree}"
        already_queued = _threads_with_marker(repo_api, headers, pr_id, marker)

        if already_queued:
            print(
                "Suppressed PR recreation: latest drift matches a previously rejected PR and remediation was already queued."
            )
        else:
            queued = _queue_restore_pipeline(
                collection_uri=collection_uri,
                project=project,
                headers=headers,
                definition_id=remediation_def_id,
                baseline_branch=baseline_branch,
                include_entra_update=include_entra_update,
                dry_run=remediation_dry_run,
                update_assignments=remediation_update_assignments,
                remove_unmanaged=remediation_remove_unmanaged,
                max_workers=remediation_max_workers,
                exclude_csv=remediation_exclude_csv,
            )
            build_queued_id = queued.get("id")
            build_url = ((queued.get("_links") or {}).get("web") or {}).get("href", "")
            if not build_url and build_queued_id:
                build_url = f"{collection_uri}/{project}/_build/results?buildId={build_queued_id}"

            comment = (
                "Auto-remediation queued because the latest drift matches a rejected PR.\n\n"
                f"Workload: {args.workload}\n"
                f"Rejected PR: #{pr_id}\n"
                f"Drift tree: {drift_tree}\n"
                f"Restore pipeline definition: {remediation_def_id}\n"
                f"Restore run: {build_url or '(queued)'}\n\n"
                f"{marker}"
            )
            try:
                _post_pr_thread(repo_api, headers, pr_id, comment)
            except Exception as exc:
                print(f"WARNING: Remediation queued, but failed to post PR thread on #{pr_id}: {exc}")

            print(
                f"Queued remediation pipeline run (definition={remediation_def_id}, buildId={build_queued_id}) and suppressed PR recreation."
            )

        print("##vso[task.setvariable variable=DRIFT_PR_SUPPRESSED;isOutput=true]1")
        return 0

    if abandoned_prs:
        print(
            f"No abandoned PR snapshot match for current drift tree (checked {len(abandoned_prs)} abandoned PR(s)); creating/updating rolling PR."
        )

    create_url = f"{repo_api}/pullrequests?api-version=7.1"
    created = _request_json(
        create_url,
        headers=headers,
        method="POST",
        body={
            "sourceRefName": source_ref,
            "targetRefName": target_ref,
            "title": args.pr_title,
            "description": description,
            "isDraft": create_as_draft,
            "completionOptions": completion_options,
        },
    )
    pr_id = created.get("pullRequestId")
    web_url = _pr_web_url(created)
    print(f"Created rolling {args.workload} PR #{pr_id}: {web_url}")
    print(f"##vso[task.setvariable variable=DRIFT_PR_ID;isOutput=true]{pr_id}")
    if web_url:
        print(f"##vso[task.setvariable variable=DRIFT_PR_URL;isOutput=true]{web_url}")
    print("##vso[task.setvariable variable=DRIFT_PR_SUPPRESSED;isOutput=true]0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: Failed to ensure rolling PR: {exc}", file=sys.stderr)
        raise
