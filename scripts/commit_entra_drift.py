#!/usr/bin/env python3
"""Commit Entra drift changes with best-effort change-author attribution."""

from __future__ import annotations

import argparse
import os
import datetime as dt
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request

import common
from collections import defaultdict
from dataclasses import dataclass

log = common.log


def _set_output_var(name: str, value: str, is_output: bool = True) -> None:
    # Works on Azure DevOps (##vso) and GitHub Actions (GITHUB_OUTPUT / GITHUB_ENV).
    if is_output:
        common.ci_set_output(name, value)
    else:
        common.ci_set_env(name, value)


def _warning(message: str) -> None:
    common.ci_log("warning", message)


def _parse_backup_start(value: str) -> dt.datetime:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Missing required --backup-start value. Ensure the pipeline sets BACKUP_START in the backup_entra job before invoking commit_entra_drift.py.")
    parsed = dt.datetime.strptime(candidate, "%Y.%m.%d:%H.%M.%S")
    return parsed.replace(tzinfo=dt.timezone.utc)


def _format_filter_datetime(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_entra_commit_date(repo_root: pathlib.Path, depth: int = 30) -> dt.datetime | None:
    common.git_proc(repo_root, ["fetch", f"--depth={depth}"], check=False)
    proc = common.git_proc(
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
    proc = common.git_proc(repo_root, ["diff", "--cached", "--name-only", "--", workload_root])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _remote_diff_is_empty(repo_root: pathlib.Path, drift_branch: str, workload_root: str) -> bool:
    remote_ref = f"refs/remotes/origin/{drift_branch}"
    if common.git_proc(repo_root, ["show-ref", "--verify", "--quiet", remote_ref], check=False).returncode != 0:
        return False
    return common.git_proc(repo_root, ["diff", "--quiet", f"origin/{drift_branch}", "--", workload_root], check=False).returncode == 0


def _identities_by_resource(audits: list[dict]) -> dict[str, dict[str, Identity]]:
    """Identities of successful audit events, indexed by the id of each resource they touched."""
    by_resource: dict[str, dict[str, Identity]] = defaultdict(dict)
    for audit in audits:
        result = str(audit.get("result") or "").strip().lower()
        if result and result != "success":
            continue
        identity = _extract_identity_from_audit(audit)
        targets = audit.get("targetResources")
        if identity is None or not isinstance(targets, list):
            continue
        for target in targets:
            resource_id = str(target.get("id") or "").strip() if isinstance(target, dict) else ""
            if resource_id:
                by_resource[resource_id][identity.key] = identity
    return by_resource


def _attribute_files(
    changed_files: list[str],
    by_resource: dict[str, dict[str, Identity]],
    fallback: Identity,
) -> tuple[dict[str, list[Identity]], int]:
    """Identities per changed file: its own audit trail, else its category's, else the fallback.

    Also returns how many files have a resource id that no audit event mentions.
    """
    file_identities: dict[str, list[Identity]] = {}
    resolved_by_category: dict[str, dict[str, Identity]] = defaultdict(dict)
    unresolved_count = 0
    for path in changed_files:
        resource_id = _resource_id_from_path(path)
        identities = sorted(by_resource.get(resource_id, {}).values(), key=lambda item: item.key)
        file_identities[path] = identities
        for identity in identities:
            resolved_by_category[_category_key(path)][identity.key] = identity
        if not identities and resource_id:
            unresolved_count += 1

    for path in changed_files:
        if not file_identities[path]:
            category = resolved_by_category.get(_category_key(path), {})
            file_identities[path] = sorted(category.values(), key=lambda item: item.key) or [fallback]
    return file_identities, unresolved_count


def _build_author_groups(
    changed_files: list[str],
    audits: list[dict],
    fallback: Identity,
) -> tuple[dict[str, dict[str, list[str] | list[Identity]]], int]:
    """Group changed files by the set of identities that changed them; also the unresolved-file count."""
    file_identities, unresolved_count = _attribute_files(changed_files, _identities_by_resource(audits), fallback)
    grouped: dict[str, dict[str, list[str] | list[Identity]]] = {}
    for path in changed_files:
        identities = file_identities[path]
        group_key = "&".join(identity.key for identity in identities)
        entry = grouped.setdefault(group_key, {"files": [], "identities": identities})
        entry["files"].append(path)  # type: ignore[union-attr]
    return grouped, unresolved_count


def _commit_group(
    repo_root: pathlib.Path,
    files: list[str],
    identities: list[Identity],
    backup_start: dt.datetime,
) -> None:
    for path in files:
        log.info(f"\t- Adding {repo_root / path}")
        common.git_proc(repo_root, ["add", "--all", "--", path])
    author_name = ", ".join(identity.name for identity in identities)
    author_email = ", ".join(identity.value for identity in identities)
    log.info(f"\t- Setting commit author(s): {author_name}")
    common.git_proc(repo_root, ["config", "user.name", author_name])
    common.git_proc(repo_root, ["config", "user.email", author_email])
    commit_date = backup_start.astimezone(dt.timezone.utc).strftime("%Y.%m.%d_%H.%M")
    commit_name = f"{commit_date} -- {author_name} (Entra)"
    log.info(f"\t- Creating commit '{commit_name}'")
    common.git_proc(repo_root, ["commit", "-m", commit_name])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workload-root", required=True)
    parser.add_argument("--baseline-branch", required=True)
    parser.add_argument("--drift-branch", required=True)
    # Defaults to $GRAPH_TOKEN so CI need not put the token on the command line.
    parser.add_argument("--access-token", default=os.environ.get("GRAPH_TOKEN", ""))
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--service-email", required=True)
    parser.add_argument("--build-reason", default="")
    parser.add_argument("--requested-for", default="")
    parser.add_argument("--requested-for-email", default="")
    parser.add_argument("--backup-start", required=True)
    args = parser.parse_args()

    repo_root = pathlib.Path(args.repo_root).resolve()
    workload_root = args.workload_root.strip().strip("/")
    fallback = _effective_fallback_identity(
        build_reason=args.build_reason,
        requested_for=args.requested_for,
        requested_for_email=args.requested_for_email,
        service_name=args.service_name,
        service_email=args.service_email,
    )

    common.git_proc(repo_root, ["config", "user.name", fallback.name])
    common.git_proc(repo_root, ["config", "user.email", fallback.value])
    common.git_proc(repo_root, ["add", "--all", "--", workload_root])

    changed_files = _changed_files(repo_root, workload_root)
    if not changed_files:
        log.info("No Entra change detected")
        _set_output_var("CHANGE_DETECTED", "0")
        _set_output_var("ROLLING_PR_SYNC_REQUIRED", "0")
        return 0

    if _remote_diff_is_empty(repo_root, args.drift_branch, workload_root):
        log.info("No Entra change detected (snapshot identical to existing drift branch)")
        _set_output_var("CHANGE_DETECTED", "0")
        _set_output_var("ROLLING_PR_SYNC_REQUIRED", "1")
        return 0

    backup_start = _parse_backup_start(args.backup_start)
    last_commit_date = _last_entra_commit_date(repo_root)
    if last_commit_date is None:
        _warning("Unable to obtain date of the last Entra backup config commit. All Entra audit events in the current query window will be considered.")

    audits: list[dict] = []
    try:
        log.info("Getting Entra directory audit logs")
        log.info(f"\t- from: '{last_commit_date}' (UTC) to: '{backup_start}' (UTC)")
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

    common.git_proc(repo_root, ["reset", "--quiet", "--", workload_root])
    log.info("\nCommit changes")
    for group in groups.values():
        files = group["files"]
        identities = group["identities"]
        assert isinstance(files, list)
        assert isinstance(identities, list)
        _commit_group(repo_root, files, identities, backup_start)
        unpushed = common.git_proc(repo_root, ["cherry", "-v", f"origin/{args.baseline_branch}"]).stdout.strip()
        if not unpushed:
            _warning("Nothing to commit?! This shouldn't happen.")
            _set_output_var("CHANGE_DETECTED", "0")
            _set_output_var("ROLLING_PR_SYNC_REQUIRED", "0")
            return 0

    common.git_proc(repo_root, ["push", "--force-with-lease", "origin", f"HEAD:{args.drift_branch}"])
    commit_sha = common.git_proc(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
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
        log.info(str(exc))
        raise
