#!/usr/bin/env python3
"""Shared utilities for Intune / Entra drift backup scripts."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class _LogFormatter(logging.Formatter):
    """Plain message for INFO (pipeline logs stay as before); 'LEVEL: message' for warnings and above."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        return message if record.levelno <= logging.INFO else f"{record.levelname}: {message}"


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("astral")
    if logger.handlers:
        return logger
    formatter = _LogFormatter()
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    for handler in (out, err):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO")
    logger.propagate = False
    return logger


# Shared diagnostic logger: INFO to stdout, WARNING and above to stderr. Machine-readable output
# (CI commands, JSON results) is still printed directly, never logged.
log = _build_logger()


def env_text(name: str, default: str = "") -> str:
    """Read and sanitize an environment variable, treating unresolved Azure DevOps
    macros $(...) as empty.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if re.fullmatch(r"\$\([^)]+\)", value):
        return default
    if not value:
        return default
    return value


def scm_platform() -> str:
    """'gitea', 'github' or 'ado' (ASTRAL_SCM overrides detection).

    Gitea runners also set GITHUB_ACTIONS=true, so GITEA_ACTIONS is checked first.
    """
    explicit = env_text("ASTRAL_SCM", "").lower()
    if explicit in {"github", "gitea", "ado"}:
        return explicit
    if os.environ.get("GITEA_ACTIONS") == "true":
        return "gitea"
    return "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "ado"


def _actions_runner() -> bool:
    """GitHub Actions or Gitea Actions (same GITHUB_OUTPUT/GITHUB_ENV/::cmd:: protocol)."""
    return scm_platform() in {"github", "gitea"}


def _one_line(value: Any) -> str:
    # Values end up in line-oriented CI files/commands; a newline would inject extra entries.
    return str(value).replace("\r", " ").replace("\n", " ")


def ci_set_output(name: str, value: Any) -> None:
    """Job output: GITHUB_OUTPUT on GitHub, task variable (+ output variable) on Azure DevOps."""
    value = _one_line(value)
    if _actions_runner() and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
        return
    print(f"##vso[task.setvariable variable={name}]{value}")
    print(f"##vso[task.setvariable variable={name};isOutput=true]{value}")


def ci_set_env(name: str, value: Any) -> None:
    """Variable for later steps of the same job."""
    value = _one_line(value)
    if _actions_runner() and os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
        return
    print(f"##vso[task.setvariable variable={name}]{value}")


def ci_log(level: str, message: str) -> None:
    """level: 'warning' or 'error'."""
    message = _one_line(message)
    if _actions_runner():
        print(f"::{level}::{message}")
    else:
        print(f"##vso[task.logissue type={level}]{message}")


def to_bool(value: Any) -> bool:
    """Interpret a string flag ("1", "true", "yes", "y", "on") as a boolean."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_bool(name: str, default: bool = False) -> bool:
    """Interpret an environment variable as a boolean."""
    raw = env_text(name, "")
    if not raw:
        return default
    return to_bool(raw)


def env_int(name: str, default: int) -> int:
    """Read an integer environment variable; unset, empty or invalid values give *default*."""
    raw = env_text(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Branch / path helpers
# ---------------------------------------------------------------------------
_BRANCH_PREFIXES = ("origin/", "refs/heads/", "refs/remotes/origin/")


def normalize_branch(branch: str) -> str:
    """Short branch name: strips refs/heads/, origin/ and refs/remotes/origin/ prefixes.

    Unresolved Azure DevOps macros ``$(...)`` become an empty string.
    """
    normalized = str(branch or "").strip()
    if normalized.startswith("$(") and normalized.endswith(")"):
        return ""
    changed = True
    while changed:
        changed = False
        for prefix in _BRANCH_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                changed = True
    return normalized


def ref_from_branch(branch: str) -> str:
    return f"refs/heads/{normalize_branch(branch)}"


def normalize_repo_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


_DOC_SUFFIXES = (".md", ".markdown", ".html", ".htm", ".pdf", ".csv", ".txt")


def is_doc_like(path: str) -> bool:
    """Documentation / generated-report file (not tenant configuration)."""
    lp = normalize_repo_path(path).lower()
    return lp.endswith(_DOC_SUFFIXES) or "/docs/" in f"/{lp}" or "/object inventory/" in f"/{lp}"


def is_report_like(path: str) -> bool:
    lp = normalize_repo_path(path).lower()
    return "/reports/" in f"/{lp}" or "/assignment report/" in f"/{lp}"


# ---------------------------------------------------------------------------
# Review thread status (Azure DevOps codes)
# ---------------------------------------------------------------------------
THREAD_STATUS_ACTIVE = 1
THREAD_STATUS_FIXED = 2
THREAD_STATUS_WONT_FIX = 3
THREAD_STATUS_CLOSED = 4
THREAD_STATUS_BY_DESIGN = 5
THREAD_STATUS_PENDING = 6

_RESOLVED_THREAD_STATUSES = (
    THREAD_STATUS_FIXED,
    THREAD_STATUS_WONT_FIX,
    THREAD_STATUS_CLOSED,
    THREAD_STATUS_BY_DESIGN,
)
_THREAD_STATUS_BY_NAME = {
    "active": THREAD_STATUS_ACTIVE,
    "pending": THREAD_STATUS_PENDING,
    "fixed": THREAD_STATUS_FIXED,
    "wontfix": THREAD_STATUS_WONT_FIX,
    "closed": THREAD_STATUS_CLOSED,
    "bydesign": THREAD_STATUS_BY_DESIGN,
}


def thread_status_code(thread: dict[str, Any]) -> int:
    status = thread.get("status")
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        return _THREAD_STATUS_BY_NAME.get(status.strip().lower(), THREAD_STATUS_ACTIVE)
    return THREAD_STATUS_ACTIVE


def is_thread_resolved(thread: dict[str, Any]) -> bool:
    return thread_status_code(thread) in _RESOLVED_THREAD_STATUSES


# Reviewer decisions must be explicit slash commands; bare prose such as
# "Reject reasons below..." must not count as a decision.
DECISION_RE = re.compile(r"(?im)^\s*/(?P<decision>reject|accept)\b")


TICKET_MARKER_RE = re.compile(
    # "Automation marker: AUTO-CHANGE-TICKET:<id>" (written by update_pr_review_summary) or the
    # legacy "<!-- AUTO-CHANGE-TICKET:<id> -->". Quoted lines ("> ...") are replies, not markers.
    r"(?m)^(?!\s*>)\s*(?:<!--\s*)?(?:Automation marker:\s*)?AUTO-CHANGE-TICKET:(?P<id>[A-Za-z0-9_-]+)\s*(?:-->)?\s*$"
)


def ticket_path_from_content(content: str) -> str | None:
    """Policy path encoded in a change-ticket thread marker, or None."""
    match = TICKET_MARKER_RE.search(content or "")
    if not match:
        return None
    encoded = match.group("id")
    try:
        return base64.urlsafe_b64decode((encoded + "=" * (-len(encoded) % 4)).encode("ascii")).decode("utf-8")
    except Exception:
        return None


def allowed_reviewers() -> set[str]:
    """Identities (UPN/email or ADO identity id) allowed to issue /reject, /accept and Reject votes.

    Read from PR_DECISION_ALLOWED_REVIEWERS (comma-separated). Empty means nobody:
    reviewer decisions are ignored (fail closed).
    """
    # ponytail: static allowlist; switch to ADO group membership lookup if the list gets unwieldy.
    raw = env_text("PR_DECISION_ALLOWED_REVIEWERS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_allowed_identity(identity: Any, allowed: set[str]) -> bool:
    """True when an ADO IdentityRef (comment author or PR reviewer) is on the allowlist."""
    if not isinstance(identity, dict) or not allowed:
        return False
    return any(str(identity.get(key) or "").strip().lower() in allowed for key in ("uniqueName", "id"))


def is_trusted_decision_comment(comment: dict[str, Any], allowed: set[str]) -> bool:
    """Allowed author, not deleted, never edited (GitHub lets write users edit others' comments)."""
    if comment.get("isDeleted") or comment.get("isEdited"):
        return False
    return is_allowed_identity(comment.get("author"), allowed)


def latest_thread_decision(comments: list[dict[str, Any]], allowed: set[str]) -> str | None:
    """Latest /reject or /accept from an allowed, non-deleted comment author in one thread."""

    def _sort_key(comment: dict[str, Any]) -> tuple[int, int]:
        try:
            comment_id = int(comment.get("id", 0))
        except Exception:
            comment_id = 0
        try:
            parent_id = int(comment.get("parentCommentId", 0))
        except Exception:
            parent_id = 0
        return (comment_id, parent_id)

    decision: str | None = None
    for comment in sorted(comments, key=_sort_key):
        if not is_trusted_decision_comment(comment, allowed):
            continue
        match = DECISION_RE.search(str(comment.get("content", "") or ""))
        if match:
            decision = match.group("decision").lower()
    return decision


def has_allowed_reject_vote(pr: dict[str, Any], allowed: set[str]) -> bool:
    """True when an allowed reviewer voted Reject (-10) on the PR."""
    reviewers = pr.get("reviewers", [])
    if not isinstance(reviewers, list):
        return False
    for reviewer in reviewers:
        try:
            vote = int(reviewer.get("vote", 0)) if isinstance(reviewer, dict) else 0
        except Exception:
            vote = 0
        if vote == -10 and is_allowed_identity(reviewer, allowed):
            return True
    return False


def normalize_exclude_csv(value: str) -> str:
    """Normalize an exclude CSV value, treating sentinel values as empty."""
    normalized = str(value or "").strip()
    if normalized.lower() in {"", "none", "null", "n/a", "-", "_none_"}:
        return ""
    return normalized


def normalize_merge_strategy(value: str) -> str:
    """Normalize a merge strategy string to an Azure DevOps API value."""
    raw = (value or "").strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "nofastforward": "noFastForward",
        "mergecommit": "noFastForward",
        "merge": "noFastForward",
        "squash": "squash",
        "rebase": "rebase",
        "rebasefastforward": "rebase",
        "rebaseff": "rebase",
        "rebasemerge": "rebaseMerge",
    }
    return aliases.get(raw, "rebase")


def retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """Seconds from a Retry-After header (numeric form only), or None."""
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


_RETRY_HTTP_CODES = {429, 500, 502, 503, 504}


def _raise_http_error(method: str, url: str, exc: urllib.error.HTTPError) -> None:
    """Raise a RuntimeError carrying the response body (first 2 KiB) when there is one, else re-raise."""
    try:
        detail = exc.read().decode("utf-8", errors="replace")[:2048]
    except Exception:
        detail = ""
    if detail:
        raise RuntimeError(f"{method} {url} failed: HTTP Error {exc.code}: {exc.reason} — {detail}") from exc
    raise exc


def request_json(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    token: str | None = None,
    timeout: float = 60,
    max_retries: int = 0,
) -> Any:
    """Make a JSON HTTP request and return the parsed response.

    If *token* is provided, an Authorization header is added automatically.
    If *max_retries* is greater than zero, transient HTTP errors (429, 500,
    502, 503, 504) and network errors are retried with exponential back-off
    (or the server's Retry-After).

    On GitHub and Gitea, Azure DevOps REST calls (URLs containing /_apis/) are
    translated by forge_compat, so the PR/review scripts run unchanged on all platforms.
    """
    if "/_apis/" in url and _actions_runner():
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.append(here)
        import forge_compat

        return forge_compat.request_json(url, method=method, body=body, headers=headers, token=token)

    req_headers: dict[str, str] = {"Accept": "application/json"}
    if token is not None:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers is not None:
        req_headers.update(headers)
    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    for attempt in range(max_retries + 1):
        is_last_attempt = attempt == max_retries
        request = urllib.request.Request(url, data=payload, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_HTTP_CODES or is_last_attempt:
                _raise_http_error(method, url, exc)
            delay = retry_after_seconds(exc)
            time.sleep(delay if delay is not None else 2**attempt)
        except urllib.error.URLError:
            if is_last_attempt:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("request_json exhausted all retries")  # only reachable with max_retries < 0


def git_proc(
    repo_root: str | os.PathLike[str],
    args: list[str],
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    """Run a git command and return the CompletedProcess (str output, or bytes when text=False)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=text,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr if text else proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {(stderr or '').strip()}")
    return proc


def run_git(repo_root: str | os.PathLike[str], args: list[str], check: bool = True) -> str:
    """Run a git command and return stdout as a stripped string."""
    return git_proc(repo_root, args, check=check).stdout.strip()


def fetch_branches(repo_root: str | os.PathLike[str], baseline_branch: str, drift_branch: str) -> bool:
    """Fetch baseline and drift branches from origin; False when the drift branch does not exist there."""
    run_git(repo_root, ["fetch", "--quiet", "origin", baseline_branch])
    try:
        run_git(repo_root, ["fetch", "--quiet", "origin", drift_branch])
    except RuntimeError as exc:
        message = str(exc).lower()
        if "couldn't find remote ref" in message or "could not find remote ref" in message:
            return False
        raise
    return True


def git_show(repo_root: str | os.PathLike[str], ref: str, rel_path: str) -> bytes | None:
    """File content at *ref* as bytes, or None when the path does not exist there."""
    proc = git_proc(repo_root, ["show", f"{ref}:{rel_path}"], check=False, text=False)
    return proc.stdout if proc.returncode == 0 else None


def git_show_text(repo_root: str | os.PathLike[str], ref: str, rel_path: str) -> str | None:
    content = git_show(repo_root, ref, rel_path)
    return None if content is None else content.decode("utf-8", errors="replace")


def configure_git_identity(
    repo_root: str | os.PathLike[str],
    fallback_name: str | None = None,
    fallback_email: str | None = None,
) -> None:
    """Configure git user.name and user.email from pipeline env vars."""
    requested_for = (os.environ.get("BUILD_REQUESTEDFOR") or "").strip()
    requested_for_email = (os.environ.get("BUILD_REQUESTEDFOREMAIL") or "").strip()
    fallback_name = (fallback_name or os.environ.get("USER_NAME") or "ASTRAL Backup Service").strip()
    fallback_email = (fallback_email or os.environ.get("USER_EMAIL") or "intune-backup@local.invalid").strip()

    author_name = requested_for or fallback_name
    author_email = requested_for_email if "@" in requested_for_email else fallback_email

    run_git(repo_root, ["config", "user.name", author_name])
    run_git(repo_root, ["config", "user.email", author_email])


# ---------------------------------------------------------------------------
# Restore-pipeline queueing (shared by ensure_rolling_pr and queue_post_merge_restore)
# ---------------------------------------------------------------------------
def parse_pipeline_id(raw: str, name: str) -> int | str:
    """Restore pipeline reference: numeric definition id on Azure DevOps, workflow file name on GitHub/Gitea."""
    try:
        return int(raw)
    except ValueError:
        if not _actions_runner():
            raise SystemExit(f"Invalid {name}: {raw}") from None
        return raw  # e.g. astral-restore.yml


@dataclass
class RestoreRequest:
    """Parameters for one run of the restore pipeline."""

    definition_id: int | str
    baseline_branch: str
    include_entra_update: bool = False
    dry_run: bool = True  # fail safe
    update_assignments: bool = True
    remove_unmanaged: bool = False
    max_workers: int = 10
    exclude_csv: str = ""
    restore_mode: str = "full"
    restore_paths_csv: str = ""

    def template_parameters(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "dryRun": self.dry_run,
            "updateAssignments": self.update_assignments,
            "removeObjectsNotInBaseline": self.remove_unmanaged,
            "includeEntraUpdate": self.include_entra_update,
            "baselineBranch": self.baseline_branch,
            "maxWorkers": self.max_workers,
            "restoreMode": self.restore_mode,
        }
        if self.restore_mode == "selective" and self.restore_paths_csv.strip():
            params["restorePathsCsv"] = self.restore_paths_csv.strip()
        exclude = normalize_exclude_csv(self.exclude_csv)
        if exclude:
            params["excludeCsv"] = exclude
        return params


def queue_restore_pipeline(
    collection_uri: str,
    project: str,
    headers: dict[str, str],
    request: RestoreRequest,
) -> dict[str, Any]:
    """Queue the restore pipeline; returns the Azure DevOps build payload."""
    body = {
        "definition": {"id": request.definition_id},
        "sourceBranch": ref_from_branch(request.baseline_branch),
        "templateParameters": request.template_parameters(),
    }
    return request_json(
        f"{collection_uri}/{project}/_apis/build/builds?api-version=7.1",
        headers=headers,
        method="POST",
        body=body,
    )


def build_web_url(queued: dict[str, Any], collection_uri: str, project: str) -> str:
    """Browser URL of a queued build ('' when the payload carries no id or link)."""
    url = ((queued.get("_links") or {}).get("web") or {}).get("href", "")
    build_id = queued.get("id")
    if not url and build_id:
        url = f"{collection_uri}/{project}/_build/results?buildId={build_id}"
    return url


def ado_headers(token: str) -> dict[str, str]:
    """Request headers for Azure DevOps REST calls made with a bearer token."""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}


def post_pr_thread(repo_api: str, headers: dict[str, str], pr_id: int, content: str) -> None:
    """Open a new active PR thread with one comment."""
    request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        headers=headers,
        method="POST",
        body={
            "comments": [{"parentCommentId": 0, "content": content, "commentType": 1}],
            "status": THREAD_STATUS_ACTIVE,
        },
    )
