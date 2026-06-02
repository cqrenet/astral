#!/usr/bin/env python3
"""Shared utilities for Intune / Entra drift backup scripts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any


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


def env_bool(name: str, default: bool = False) -> bool:
    """Interpret an environment variable as a boolean."""
    raw = env_text(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


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


def _get_retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    try:
        retry_after = error.headers.get("Retry-After")
        if retry_after:
            return float(retry_after)
    except Exception:
        pass
    return None


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
    502, 503, 504) are retried with exponential back-off.
    """
    req_headers: dict[str, str] = {
        "Accept": "application/json",
    }
    if token is not None:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers is not None:
        req_headers.update(headers)

    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    retry_codes = {429, 500, 502, 503, 504}
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers=req_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in retry_codes or attempt == max_retries:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:2048]
                except Exception:
                    pass
                if body:
                    raise RuntimeError(
                        f"{method} {url} failed: HTTP Error {exc.code}: {exc.reason} — {body}"
                    ) from exc
                raise
            retry_after = _get_retry_after_seconds(exc)
            sleep = retry_after if retry_after is not None else (2 ** attempt)
            time.sleep(sleep)
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == max_retries:
                raise
            time.sleep(2 ** attempt)

    # Should never be reached; satisfy type checker.
    if last_error is not None:
        raise last_error
    raise RuntimeError("request_json exhausted all retries")


def run_git(repo_root: str | os.PathLike[str], args: list[str], check: bool = True) -> str:
    """Run a git command and return stdout as a stripped string."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {stderr}")
    return proc.stdout.strip()


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
