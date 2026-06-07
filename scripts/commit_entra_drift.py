#!/usr/bin/env python3
"""Commit Entra drift changes with best-effort change-author attribution."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass


def _git_run(repo_root: pathlib.Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {stderr}")
    return proc


def _set_output_var(name: str, value: str, is_output: bool = True) -> None:
    suffix = ";isOutput=true" if is_output else ""
    print(f"##vso[task.setvariable variable={name}{suffix}]{value}")


def _warning(message: str) -> None:
    print(f"##vso[task.logissue type=warning]{message}")


def _parse_backup_start(value: str) -> dt.datetime:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Missing required --backup-start value. Ensure the pipeline sets BACKUP_START in the backup_entra job before invoking commit_entra_drift.py.")
    parsed = dt.datetime.strptime(candidate, "%Y.%m.%d:%H.%M.%S")
    return parsed.replace(tzinfo=dt.timezone.utc)


def _format_filter_datetime(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_entra_commit_date(repo_root: pathlib.Path, depth: int = 30) -> dt.datetime | None:
    _git_run(repo_root, ["fetch", f"--depth={depth}"], check=False)
    proc = _git_run(
        repo_root,
        [
            "--no-pager",
            "log",
            "--no-show-signature",
            f"-{depth}",
            "--format=%s%%%cI",
        ],
    )
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line or "%%%" not in line:
            continue
        subject, iso_date = line.split("%%%", 1)
        if subject.endswith(" (Entra)") and len(subject) >= 18 and subject[4] == ".":
            try:
                return dt.datetime.fromisoformat(iso_date.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
            except ValueError:
                continue
    return None


def _request_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass(frozen=True)
class Identity:
    key: str
    value: str
    name: str


def _display_or_localpart(display_name: str, principal_name: str) -> str:
    display_name = (display_name or "").strip()
    if display_name:
        return display_name
    principal_name = (principal_name or "").strip()
    if "@" in principal_name:
        return principal_name.split("@", 1)[0]
    return principal_name


def _extract_identity_from_audit(entry: dict) -> Identity | None:
    initiated_by = entry.get("initiatedBy")
    if not isinstance(initiated_by, dict):
        return None

    user = initiated_by.get("user")
    if isinstance(user, dict):
        principal_name = str(user.get("userPrincipalName") or user.get("email") or "").strip()
        display_name = str(user.get("displayName") or "").strip()
        if principal_name:
            return Identity(
                key=f"user:{principal_name}",
                value=principal_name,
                name=_display_or_localpart(display_name, principal_name),
            )
        if display_name:
            return Identity(
                key=f"display:{display_name}",
                value=display_name,
                name=display_name,
            )

    app = initiated_by.get("app")
    if isinstance(app, dict):
        display_name = str(app.get("displayName") or "").strip()
        if display_name:
            return Identity(
                key=f"sp:{display_name}",
                value=f"{display_name} (SP)",
                name=display_name,
            )

    return None


def _fetch_directory_audits(
    token: str,
    last_commit_date: dt.datetime | None,
    backup_start: dt.datetime,
) -> list[dict]:
    params = {
        "$top": "999",
        "$select": "activityDateTime,activityDisplayName,category,result,initiatedBy,targetResources",
    }
    audit_end = backup_start - dt.timedelta(minutes=10)
    filter_parts = [f"activityDateTime le {_format_filter_datetime(audit_end)}"]
    if last_commit_date is not None:
        filter_parts.append(f"activityDateTime ge {_format_filter_datetime(last_commit_date)}")
    params["$filter"] = " and ".join(filter_parts)
    url = f"https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?{urllib.parse.urlencode(params)}"

    results: list[dict] = []
    while url:
        payload = _request_json(url, token)
        value = payload.get("value")
        if isinstance(value, list):
            results.extend(item for item in value if isinstance(item, dict))
        next_link = payload.get("@odata.nextLink")
        url = str(next_link).strip() if next_link else ""
    return results


def _resource_id_from_path(path: str) -> str:
    pure = pathlib.PurePosixPath(path)
    if pure.suffix.lower() != ".json":
        return ""
    stem = pure.stem
    if "__" not in stem:
        return ""
    return stem.rsplit("__", 1)[-1].lstrip("_").strip()


def _category_key(path: str) -> str:
    pure = pathlib.PurePosixPath(path)
    parts = pure.parts
    if len(parts) < 3:
        return ""
    return "/".join(parts[:3])


def _fallback_identity(name: str, email: str) -> Identity:
    return Identity(key=f"fallback:{email}", value=email, name=name)


def _effective_fallback_identity(
    build_reason: str,
    requested_for: str,
    requested_for_email: str,
    service_name: str,
    service_email: str,
) -> Identity:
    requested_for_email = requested_for_email.strip()
    if build_reason.strip() != "Schedule" and "@" in requested_for_email:
        requested_for = requested_for.strip() or requested_for_email.split("@", 1)[0]
        return _fallback_identity(requested_for, requested_for_email)
    return _fallback_identity(service_name.strip(), service_email.strip())


def _changed_files(repo_root: pathlib.Path, workload_root: str) -> list[str]:
    proc = _git_run(repo_root, ["diff", "--cached", "--name-only", "--", workload_root])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _remote_diff_is_empty(repo_root: pathlib.Path, drift_branch: str, workload_root: str) -> bool:
    remote_ref = f"refs/remotes/origin/{drift_branch}"
    if _git_run(repo_root, ["show-ref", "--verify", "--quiet", remote_ref], check=False).returncode != 0:
        return False
    return _git_run(repo_root, ["diff", "--quiet", f"origin/{drift_branch}", "--", workload_root], check=False).returncode == 0


def _build_author_groups(
    changed_files: list[str],
    audits: list[dict],
    fallback: Identity,
) -> tuple[dict[str, dict[str, list[str] | list[Identity]]], int]:
    identities_by_resource: dict[str, dict[str, Identity]] = defaultdict(dict)
    for audit in audits:
        result = str(audit.get("result") or "").strip().lower()
        if result and result != "success":
            continue
        identity = _extract_identity_from_audit(audit)
        if identity is None:
            continue
        target_resources = audit.get("targetResources")
        if not isinstance(target_resources, list):
            continue
        for target in target_resources:
            if not isinstance(target, dict):
                continue
            resource_id = str(target.get("id") or "").strip()
            if resource_id:
                identities_by_resource[resource_id][identity.key] = identity

    resolved_by_category: dict[str, dict[str, Identity]] = defaultdict(dict)
    file_identities: dict[str, list[Identity]] = {}
    unresolved_count = 0

    for path in changed_files:
        resource_id = _resource_id_from_path(path)
        identities = list(identities_by_resource.get(resource_id, {}).values())
        if identities:
            file_identities[path] = sorted(identities, key=lambda item: item.key)
            for identity in file_identities[path]:
                resolved_by_category[_category_key(path)][identity.key] = identity
        else:
            file_identities[path] = []
            if resource_id:
                unresolved_count += 1

    for path in changed_files:
        if file_identities[path]:
            continue
        category_identities = list(resolved_by_category.get(_category_key(path), {}).values())
        if category_identities:
            file_identities[path] = sorted(category_identities, key=lambda item: item.key)
        else:
            file_identities[path] = [fallback]

    grouped: dict[str, dict[str, list[str] | list[Identity]]] = {}
    for path in changed_files:
        identities = file_identities[path] or [fallback]
        group_key = "&".join(identity.key for identity in identities)
        entry = grouped.setdefault(group_key, {"files": [], "identities": identities})
        files = entry["files"]
        assert isinstance(files, list)
        files.append(path)

    return grouped, unresolved_count


def _commit_group(
    repo_root: pathlib.Path,
    files: list[str],
    identities: list[Identity],
    backup_start: dt.datetime,
) -> None:
    for path in files:
        print(f"\t- Adding {repo_root / path}")
        _git_run(repo_root, ["add", "--all", "--", path])
    author_name = ", ".join(identity.name for identity in identities)
    author_email = ", ".join(identity.value for identity in identities)
    print(f"\t- Setting commit author(s): {author_name}")
    _git_run(repo_root, ["config", "user.name", author_name])
    _git_run(repo_root, ["config", "user.email", author_email])
    commit_date = backup_start.astimezone(dt.timezone.utc).strftime("%Y.%m.%d_%H.%M")
    commit_name = f"{commit_date} -- {author_name} (Entra)"
    print(f"\t- Creating commit '{commit_name}'")
    _git_run(repo_root, ["commit", "-m", commit_name])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workload-root", required=True)
    parser.add_argument("--reports-root", default="", help="Optional reports directory to stage alongside workload changes.")
    parser.add_argument("--baseline-branch", required=True)
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--service-email", required=True)
    parser.add_argument("--build-reason", default="")
    parser.add_argument("--requested-for", default="")
    parser.add_argument("--requested-for-email", default="")
    parser.add_argument("--backup-start", required=True)
    args = parser.parse_args()

    repo_root = pathlib.Path(args.repo_root).resolve()
    workload_root = args.workload_root.strip().strip("/")
    reports_root = args.reports_root.strip().strip("/")
    fallback = _effective_fallback_identity(
        build_reason=args.build_reason,
        requested_for=args.requested_for,
        requested_for_email=args.requested_for_email,
        service_name=args.service_name,
        service_email=args.service_email,
    )

    _git_run(repo_root, ["config", "user.name", fallback.name])
    _git_run(repo_root, ["config", "user.email", fallback.value])
    _git_run(repo_root, ["add", "--all", "--", workload_root])
    if reports_root:
        _git_run(repo_root, ["add", "--all", "--", reports_root])

    changed_files = _changed_files(repo_root, workload_root)
    if not changed_files:
        print("No Entra change detected")
        _set_output_var("CHANGE_DETECTED", "0")
        _set_output_var("ROLLING_PR_SYNC_REQUIRED", "0")
        if reports_root:
            _git_run(repo_root, ["reset", "--quiet", "--", reports_root])
        return 0

    if _remote_diff_is_empty(repo_root, args.drift_branch, workload_root):
        print("No Entra change detected (snapshot identical to existing drift branch)")
        _set_output_var("CHANGE_DETECTED", "0")
        _set_output_var("ROLLING_PR_SYNC_REQUIRED", "1")
        return 0

    backup_start = _parse_backup_start(args.backup_start)
    last_commit_date = _last_entra_commit_date(repo_root)
    if last_commit_date is None:
        _warning("Unable to obtain date of the last Entra backup config commit. All Entra audit events in the current query window will be considered.")

    audits: list[dict] = []
    try:
        print("Getting Entra directory audit logs")
        print(f"\t- from: '{last_commit_date}' (UTC) to: '{backup_start}' (UTC)")
        audits = _fetch_directory_audits(args.access_token, last_commit_date, backup_start)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            _warning("Graph token cannot read Entra directory audit logs. Falling back to pipeline identity for unresolved Entra changes.")
        else:
            raise
    except Exception as exc:  # pragma: no cover - defensive path for pipeline runtime issues
        _warning(f"Unable to query Entra directory audit logs ({exc}). Falling back to pipeline identity for unresolved Entra changes.")

    groups, unresolved_count = _build_author_groups(changed_files, audits, fallback)
    if unresolved_count > 0:
        _warning(
            f"Unable to resolve author from Entra audit logs for {unresolved_count} of {len(changed_files)} changed files. Fallback identity used where needed."
        )

    # Reset only the workload so per-author commits can re-stage individual files.
    # Reports stay staged and land in the first commit (they have no user attribution).
    _git_run(repo_root, ["reset", "--quiet", "--", workload_root])
    print("\nCommit changes")
    for group in groups.values():
        files = group["files"]
        identities = group["identities"]
        assert isinstance(files, list)
        assert isinstance(identities, list)
        _commit_group(repo_root, files, identities, backup_start)
        unpushed = _git_run(repo_root, ["cherry", "-v", f"origin/{args.baseline_branch}"]).stdout.strip()
        if not unpushed:
            _warning("Nothing to commit?! This shouldn't happen.")
            _set_output_var("CHANGE_DETECTED", "0")
            _set_output_var("ROLLING_PR_SYNC_REQUIRED", "0")
            return 0

    _git_run(repo_root, ["push", "--force-with-lease", "origin", f"HEAD:{args.drift_branch}"])
    commit_sha = _git_run(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
    modification_authors = sorted({identity.value for group in groups.values() for identity in group["identities"]})  # type: ignore[index]
    _set_output_var("CHANGE_DETECTED", "1")
    _set_output_var("ROLLING_PR_SYNC_REQUIRED", "1")
    _set_output_var("COMMIT_SHA", commit_sha)
    _set_output_var("COMMIT_DATE", backup_start.strftime("%Y.%m.%d_%H.%M"))
    _set_output_var("MODIFICATION_AUTHOR", ", ".join(modification_authors))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
