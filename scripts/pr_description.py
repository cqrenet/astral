#!/usr/bin/env python3
"""PR description block handling: markers, compact levels and size-limit fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pr_ai import _format_ai_narrative_markdown
from pr_base import _env_bool, _request_json

AUTO_BLOCK_START = "<!-- AUTO-REVIEW-SUMMARY:START -->"


AUTO_BLOCK_END = "<!-- AUTO-REVIEW-SUMMARY:END -->"


AUTO_SUMMARY_VERSION = "2026-04-08a"


# Legacy marker retained for one-time cleanup in PR descriptions.
TICKET_BLOCK_START = "<!-- AUTO-CHANGE-TICKETS:START -->"


TICKET_BLOCK_END = "<!-- AUTO-CHANGE-TICKETS:END -->"


COMPACT_AI_THREAD_NOTE = "_Full AI reviewer narrative is posted in a dedicated PR thread due PR description limits._"


COMPACT_DETERMINISTIC_THREAD_NOTE = (
    "_Full deterministic summary (including Top Risk Items) is posted in a dedicated PR thread "
    "due to Azure DevOps description size limits._"
)


ADO_PR_DESCRIPTION_MAX_LEN = 4000


COMPACT_REVIEWER_GUIDE_NOTE = "> 📋 Full **reviewer guide** is posted in a dedicated PR thread."


def _is_description_limit_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if "http 413" in text or "request entity too large" in text:
        return True
    size_terms = (
        "too large",
        "too long",
        "maximum length",
        "max length",
        "exceeds the maximum",
        "exceeds maximum",
        "exceeds the limit",
        "payload too large",
        "content length",
        "description",
    )
    limit_terms = ("limit", "length", "size", "large", "long")
    return any(term in text for term in size_terms) and any(term in text for term in limit_terms)


def _upsert_marked_block(description: str, block: str, start_marker: str, end_marker: str) -> str:
    description = description or ""
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        flags=re.DOTALL,
    )
    if pattern.search(description):
        return pattern.sub(block, description)
    if description.endswith("\n"):
        return description + "\n" + block + "\n"
    if description:
        return description + "\n\n" + block + "\n"
    return block + "\n"


def _upsert_auto_block(description: str, auto_block: str) -> str:
    description = description or ""
    cleaned = _remove_marked_block(description, AUTO_BLOCK_START, AUTO_BLOCK_END)
    marker = "## Reviewer Quick Actions"
    idx = cleaned.find(marker)
    block = auto_block.strip()
    if idx == -1:
        if not cleaned:
            return block + "\n"
        if cleaned.endswith("\n"):
            return cleaned + "\n" + block + "\n"
        return cleaned + "\n\n" + block + "\n"

    prefix = cleaned[:idx].rstrip()
    suffix = cleaned[idx:].lstrip()
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    parts.append(block)
    if suffix:
        parts.append(suffix)
    return "\n\n".join(parts).strip() + "\n"


def _existing_change_fingerprint(description: str) -> str:
    description = description or ""
    block_pattern = re.compile(
        re.escape(AUTO_BLOCK_START) + r"(?P<body>.*?)" + re.escape(AUTO_BLOCK_END),
        flags=re.DOTALL,
    )
    match = block_pattern.search(description)
    if not match:
        return ""

    body = match.group("body")
    fingerprint_pattern = re.compile(r"\*\*Change Fingerprint:\*\*\s*`(?P<fp>[a-fA-F0-9]+)`")
    fingerprint_match = fingerprint_pattern.search(body)
    if not fingerprint_match:
        return ""
    return fingerprint_match.group("fp").strip().lower()


def _existing_summary_version(description: str) -> str:
    body = _auto_block_body(description)
    if not body:
        return ""
    version_pattern = re.compile(r"\*\*Summary Version:\*\*\s*`(?P<version>[^`]+)`")
    version_match = version_pattern.search(body)
    if not version_match:
        return ""
    return version_match.group("version").strip()


def _auto_block_body(description: str) -> str:
    description = description or ""
    block_pattern = re.compile(
        re.escape(AUTO_BLOCK_START) + r"(?P<body>.*?)" + re.escape(AUTO_BLOCK_END),
        flags=re.DOTALL,
    )
    match = block_pattern.search(description)
    if not match:
        return ""
    return match.group("body")


def _auto_block_contains_ai_fallback(body: str) -> bool:
    if not body:
        return False
    lowered = body.lower()
    return ("ai fallback used:" in lowered) or ("ai summary unavailable:" in lowered)


def _compact_deterministic_summary(deterministic_summary: str) -> str:
    marker = "\n### Top Risk Items"
    idx = deterministic_summary.find(marker)
    if idx == -1:
        return deterministic_summary.strip()
    return deterministic_summary[:idx].strip()


def _compact_reviewer_guide(description: str) -> str:
    """Replace the legacy long reviewer guide with a compact reference."""
    description = description or ""
    marker = "## Reviewer Quick Actions"
    idx = description.find(marker)
    if idx == -1:
        return description
    prefix = description[:idx].rstrip()
    if not prefix:
        return COMPACT_REVIEWER_GUIDE_NOTE + "\n"
    return prefix + "\n\n" + COMPACT_REVIEWER_GUIDE_NOTE + "\n"


def _append_reviewer_guide_note(description: str) -> str:
    """Append the compact reviewer guide note if not already present."""
    description = description or ""
    if COMPACT_REVIEWER_GUIDE_NOTE in description:
        return description
    if description.endswith("\n"):
        return description + COMPACT_REVIEWER_GUIDE_NOTE + "\n"
    return description + "\n\n" + COMPACT_REVIEWER_GUIDE_NOTE + "\n"


def _remove_marked_block(description: str, start_marker: str, end_marker: str) -> str:
    description = description or ""
    pattern = re.compile(
        r"\n*"
        + re.escape(start_marker)
        + r".*?"
        + re.escape(end_marker)
        + r"\n*",
        flags=re.DOTALL,
    )
    cleaned = pattern.sub("\n\n", description)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n")


def _build_summary_block(
    workload: str,
    deterministic: str,
    ai_summary: str | None,
    ai_error: str | None,
    level: str,
) -> str:
    """Auto-summary block for the PR description.

    level: "full" (everything inline), "compact" (short deterministic summary, AI inline),
    "compact_thread" (short deterministic summary, AI narrative in a thread) or
    "ultra" (short deterministic summary, everything else in threads).
    """
    lines = [
        AUTO_BLOCK_START,
        f"## Automated Review Summary ({workload})",
        "",
        f"- **Summary Version:** `{AUTO_SUMMARY_VERSION}`",
    ]
    if level == "full":
        lines.append(deterministic)
        if ai_summary:
            lines.extend(["", "### AI Reviewer Narrative", _format_ai_narrative_markdown(ai_summary)])
        elif ai_error:
            lines.extend(["", f"_AI summary unavailable: {ai_error}_"])
        lines.append(AUTO_BLOCK_END)
        return "\n".join(lines)

    if level == "ultra":
        ai_block = COMPACT_AI_THREAD_NOTE
    elif ai_summary:
        narrative = _format_ai_narrative_markdown(ai_summary) if level == "compact" else COMPACT_AI_THREAD_NOTE
        ai_block = "\n### AI Reviewer Narrative\n" + narrative
    elif ai_error:
        ai_block = f"\n_AI summary unavailable: {ai_error}_"
    else:
        ai_block = ""
    lines.extend(
        [
            _compact_deterministic_summary(deterministic),
            "",
            COMPACT_DETERMINISTIC_THREAD_NOTE,
            ai_block,
            AUTO_BLOCK_END,
        ]
    )
    return "\n".join(lines)


def _description_with_block(current_description: str, block: str, tidy_reviewer_guide: bool) -> str:
    description = _upsert_auto_block(current_description, block)
    # Cleanup legacy description-based ticket checklist if present.
    description = _remove_marked_block(description, TICKET_BLOCK_START, TICKET_BLOCK_END)
    if tidy_reviewer_guide:
        # Strip legacy long reviewer guide and ensure compact note is present.
        description = _compact_reviewer_guide(description)
        description = _append_reviewer_guide_note(description)
    return description


@dataclass
class DescriptionUpdate:
    summary_updated: bool
    final_description: str
    compacted: bool  # description could not hold the full summary
    ai_moved_to_thread: bool


def _publish_description(
    *,
    workload: str,
    deterministic: str,
    ai_summary: str | None,
    ai_error: str | None,
    current_description: str,
    patch_url: str,
    patch_title: str,
    token: str,
) -> DescriptionUpdate:
    """Write the summary into the PR description, degrading full -> compact -> ultra on size limits."""
    limit = ADO_PR_DESCRIPTION_MAX_LEN - 100

    def _description(level: str) -> str:
        block = _build_summary_block(workload, deterministic, ai_summary, ai_error, level)
        return _description_with_block(current_description, block, tidy_reviewer_guide=level in {"full", "compact"})

    def _patch(description: str) -> None:
        _request_json(patch_url, token=token, method="PATCH", body={"title": patch_title, "description": description})

    updated = _description("full")
    if updated == current_description:
        return DescriptionUpdate(False, updated, len(updated) > limit, False)

    if len(updated) <= limit:
        try:
            _patch(updated)
            return DescriptionUpdate(True, updated, False, False)
        except RuntimeError as exc:
            if not _is_description_limit_error(exc):
                raise

    # Compact deterministic summary first; the AI narrative stays inline when it fits.
    ai_moved_to_thread = False
    compact = _description("compact")
    if len(compact) > limit:
        ai_moved_to_thread = True
        compact = _description("compact_thread")

    if ai_moved_to_thread:
        print(
            "INFO: Full PR summary exceeds Azure DevOps description limit; "
            "using compact summary in description and posting full details to PR threads."
        )
    else:
        print(
            "INFO: Full PR summary exceeds Azure DevOps description limit; "
            "using compact deterministic summary in description, keeping AI narrative inline, "
            "and posting full deterministic details to a PR thread."
        )
    try:
        _patch(compact)
        return DescriptionUpdate(True, compact, True, ai_moved_to_thread)
    except RuntimeError as exc:
        if not _is_description_limit_error(exc):
            raise
    print("WARNING: Compact summary still too large; retrying with ultra-compact block.")
    ultra = _description("ultra")
    _patch(ultra)
    return DescriptionUpdate(True, ultra, True, ai_moved_to_thread)


def _summary_is_current(
    *,
    workload: str,
    pr_id: Any,
    current_description: str,
    changes_fingerprint: str,
    deterministic: str,
) -> bool:
    """True when the description already holds this summary; otherwise logs why it is refreshed."""
    existing_fingerprint = _existing_change_fingerprint(current_description)
    if not existing_fingerprint or existing_fingerprint != changes_fingerprint:
        return False
    existing_summary_version = _existing_summary_version(current_description)
    current_auto_body = _auto_block_body(current_description)
    deterministic_present = bool(current_auto_body) and (
        deterministic in current_auto_body or _compact_deterministic_summary(deterministic) in current_auto_body
    )
    ai_fallback_present = _auto_block_contains_ai_fallback(current_auto_body)
    refresh_on_fallback = _env_bool("PR_AI_FORCE_REFRESH_ON_FALLBACK", default=True)
    version_matches = existing_summary_version == AUTO_SUMMARY_VERSION
    retry_ai = deterministic_present and refresh_on_fallback and ai_fallback_present

    prefix = f"Automated review summary fingerprint unchanged for PR #{pr_id} ({workload})"
    if deterministic_present and version_matches and not (refresh_on_fallback and ai_fallback_present):
        return True
    if retry_ai:
        print(f"{prefix}, but prior AI fallback marker detected; retrying AI narrative refresh.")
    elif not version_matches:
        print(
            f"{prefix}, but summary version changed "
            f"({existing_summary_version or 'unversioned'} -> {AUTO_SUMMARY_VERSION}); refreshing description."
        )
    else:
        print(f"{prefix}, but summary format/content changed; refreshing description.")
    return False
