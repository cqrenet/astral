#!/usr/bin/env python3
"""Environment, git and HTTP helpers shared by the PR review summary modules."""

from __future__ import annotations

import os
from typing import Any

import common

log = common.log

_env_bool = common.env_bool
_run_git = common.run_git


def _env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return default


def _delay_reviewer_notifications_enabled() -> bool:
    return _env_bool("ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS", False)


def _debug_enabled() -> bool:
    return (
        _env_bool("DEBUG_CHANGE_TICKET_THREADS")
        or _env_bool("DEBUG_CHANGE_TICKETS")
        or _env_bool("SYSTEM_DEBUG")
    )


def _debug(msg: str) -> None:
    if _debug_enabled():
        log.info(f"[change-ticket-debug] {msg}")


def _request_json(url: str, token: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        result = common.request_json(
            url,
            method=method,
            body=body,
            token=token,
            timeout=45,
            max_retries=3,
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    if isinstance(result, dict):
        return result
    return {"value": result} if result is not None else {}


def _load_policy_excerpt(repo_root: str, branch: str, path: str, max_chars: int = 0) -> str:
    """Contents of *path* on origin/<branch> (stripped, optionally truncated); '' when unavailable."""
    try:
        content = common.git_show_text(repo_root, f"origin/{common.normalize_branch(branch)}", path)
    except Exception:
        return ""
    if content is None:
        return ""
    content = content.strip()
    if max_chars and len(content) > max_chars:
        return content[:max_chars] + "...(truncated)"
    return content
