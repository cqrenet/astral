#!/usr/bin/env python3
"""Azure OpenAI client and reviewer narrative formatting for the PR review summary."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import common
from pr_change_source import _format_change_source_label
from pr_changes import ChangeItem, _change_scope, _ellipsize, _risk_label

def _reviewer_system_prompt() -> str:
    return (
        "You analyze configuration drift pull requests for enterprise identity and endpoint "
        "management systems such as Microsoft Intune and Entra ID. Your job is to help "
        "reviewers quickly understand operational impact and security implications of "
        "configuration changes, including whether the evidence suggests platform-managed "
        "infrastructure drift, tenant-admin intent, or a mixed/uncertain source."
    )


def _reviewer_instruction() -> str:
    return (
        "Produce a concise PR reviewer summary for configuration changes. "
        "Assume the reviewer may not be a deep technical expert.\n\n"
        "Context signals are provided such as scope, posture change classification, "
        "baseline alignment relative to the approved configuration branch and\n"
        "the active security baseline profile (for example CIS benchmark derived),\n"
        "and the top configuration areas affected.\n\n"
        "Structure the response with sections:\n"
        "Plain-language summary\n"
        "Operational impact\n"
        "Risk assessment rationale\n"
        "Recommended reviewer checks\n"
        "Rollback considerations\n\n"
        "Rules:\n"
        "- Use only the provided change list and facts.\n"
        "- Do not assume settings not present in the input.\n"
        "- Reference the affected policy paths or configuration areas.\n"
        "- Highlight security-relevant changes if present.\n"
        "- Mention major affected areas if provided in top_changed_areas.\n"
        "- Prefer the semantic_change descriptions when explaining what changed.\n"
        "- Use change_source_assessment as a deterministic heuristic, but validate it against the specific changed paths and semantic_change details.\n"
        "- Distinguish probable platform-managed or vendor-driven infrastructure drift from probable tenant-admin changes.\n"
        "- Treat Microsoft/platform-added objects, background service updates, auto-created integrations, and metadata churn as infrastructure changes unless the supplied facts show explicit admin intent.\n"
        "- Treat policy logic, assignments, targeting, access scope, compliance, enrollment, automation, approval, and app configuration choices as admin changes unless the supplied facts suggest they are platform-managed.\n"
        "- For App Registrations and Enterprise Applications: do not downgrade risk to LOW solely because the change source appears platform-driven. New or modified apps that expose requiredResourceAccess, appRoles, oauth2PermissionScopes, passwordCredentials, keyCredentials, redirectUris, or preAuthorizedApplications carry tenant security impact regardless of who created the object.\n"
        "- When the highest deterministic risk is HIGH and the changed paths include app identity objects, the narrative risk rating must be at least MEDIUM unless the specific facts show purely cosmetic metadata changes.\n"
        "- If the evidence is mixed or insufficient, say so explicitly instead of guessing.\n"
        "- Keep PRs for both kinds of changes; explain which category dominates the drift and why.\n"
        "- For assignment filters: treat filter removal as reverting to the base target scope, not 'no devices'.\n"
        "- Only claim policy is unassigned/no devices when assignment targets are removed and none remain.\n"
        "- Keep the summary under 200 words."
    )


def _minimal_reviewer_instruction() -> str:
    return (
        "Write a concise reviewer narrative using only supplied data. "
        "Use sections: Plain-language summary, Operational impact, Risk assessment rationale, "
        "Recommended reviewer checks, Rollback considerations. "
        "State whether the drift looks primarily infrastructure/platform-driven, "
        "primarily admin-driven, or mixed/uncertain based on the evidence. "
        "Keep under 170 words."
    )


def _normalize_aoai_endpoint(endpoint: str) -> str:
    cleaned = endpoint.strip().rstrip("/")
    if not cleaned:
        return cleaned

    parsed = urlsplit(cleaned)
    if parsed.scheme and parsed.netloc:
        cleaned = f"{parsed.scheme}://{parsed.netloc}"

    marker = "/openai"
    idx = cleaned.lower().find(marker)
    if idx != -1:
        return cleaned[:idx]
    return cleaned


def _message_text(content: Any) -> str:
    """Text of a chat message: a string, or a list of strings / {text|content|value} parts."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            candidates = [item]
        elif isinstance(item, dict):
            candidates = [item.get(key) for key in ("text", "content", "value")]
        else:
            continue
        # The first non-empty string field of a part wins.
        text = next((c.strip() for c in candidates if isinstance(c, str) and c.strip()), "")
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _extract_ai_text_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    """(text, error) from a chat-completions response; text is empty when unusable."""
    choices = payload.get("choices", [])
    if not choices:
        return ("", "no choices")

    first = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = str(first.get("finish_reason") or "").strip()
    message = first.get("message", {}) if isinstance(first.get("message"), dict) else {}
    text = _message_text(message.get("content", ""))

    if finish_reason and finish_reason.lower() != "stop":
        detail = f"finish_reason={finish_reason}"
        return ("", detail + (" (partial content suppressed)" if text else ""))
    if text:
        return (text, "")
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return ("", f"model refusal: {refusal.strip()}")
    return ("", f"finish_reason={finish_reason}" if finish_reason else "empty message content")


def _build_fallback_narrative(
    workload: str,
    changes: list[ChangeItem],
    compact_changes: list[dict[str, Any]],
    top_changed_areas: list[str],
    change_source_assessment: dict[str, Any],
    fallback_reason: str,
) -> str:
    highest_risk = _risk_label(max((item.risk_score for item in changes), default=1))
    change_scope = _change_scope(changes)
    security_sensitive = any(item.risk_score >= 3 for item in changes)
    source_text = _format_change_source_label(
        str(change_source_assessment.get("dominant_source") or "mixed_or_uncertain")
    )
    source_confidence = str(change_source_assessment.get("confidence") or "low")

    top_items = compact_changes[:3]
    bullet_lines: list[str] = []
    for item in top_items:
        path = str(item.get("path") or "")
        op = str(item.get("operation") or "Modified")
        sem = str(item.get("semantic_change") or "Configuration content modified")
        bullet_lines.append(f"- `{op}` `{path}`: {sem}")

    areas_text = ", ".join(top_changed_areas[:3]) if top_changed_areas else "n/a"
    sensitivity_text = "security-relevant" if security_sensitive else "configuration-impacting"

    lines = [
        "#### Plain-Language Summary",
        (
            f"{workload.upper()} drift includes {len(changes)} {change_scope.lower()} "
            f"{sensitivity_text} change(s), with overall risk {highest_risk}. "
            f"Deterministic source assessment: {source_text} ({source_confidence} confidence)."
        ),
        "",
        "#### Operational Impact",
        f"Most affected areas: {areas_text}. Validate expected behavior for impacted policies after merge.",
        "",
        "#### Risk Assessment Rationale",
        f"Risk is driven by changed policy areas and operations (especially deletes/renames in security-critical paths).",
        "",
        "#### Recommended Reviewer Checks",
    ]
    lines.extend(bullet_lines if bullet_lines else ["- Validate changed policy intent against expected baseline behavior."])
    lines.extend(
        [
            "",
            "#### Rollback Considerations",
            "If behavior is not expected, revert the drift commit/PR to restore baseline state.",
            "",
            f"_AI fallback used: {fallback_reason}_",
        ]
    )
    return "\n".join(lines)


def _format_ai_narrative_markdown(text: str) -> str:
    if not text:
        return ""

    section_map = {
        "plain-language summary": "#### Plain-Language Summary",
        "plain language summary": "#### Plain-Language Summary",
        "operational impact": "#### Operational Impact",
        "risk assessment rationale": "#### Risk Assessment Rationale",
        "recommended reviewer checks": "#### Recommended Reviewer Checks",
        "rollback considerations": "#### Rollback Considerations",
    }

    normalized_lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        lowered = stripped.lower().rstrip(":")

        if lowered in section_map:
            normalized_lines.append(section_map[lowered])
            continue

        if stripped.startswith("•"):
            normalized_lines.append("- " + stripped.lstrip("• ").strip())
            continue

        normalized_lines.append(line)

    compact: list[str] = []
    blank_count = 0
    for line in normalized_lines:
        if not line.strip():
            blank_count += 1
            if blank_count > 1:
                continue
        else:
            blank_count = 0
        compact.append(line)

    return "\n".join(compact).strip()


def _compact_ai_narrative_markdown(text: str, max_chars: int) -> str:
    formatted = _format_ai_narrative_markdown(text)
    if not formatted or max_chars <= 0:
        return ""
    if len(formatted) <= max_chars:
        return formatted

    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for raw in formatted.split("\n"):
        line = raw.rstrip()
        if line.startswith("#### "):
            if current_heading:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line
            current_lines = []
            continue
        current_lines.append(line)
    if current_heading:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    if not sections:
        return _ellipsize(formatted, max_chars)

    weights = {
        "#### Plain-Language Summary": 1.3,
        "#### Operational Impact": 1.2,
        "#### Risk Assessment Rationale": 1.2,
        "#### Recommended Reviewer Checks": 1.0,
        "#### Rollback Considerations": 0.9,
    }
    min_body_chars = 24
    total_weight = sum(weights.get(heading, 1.0) for heading, _ in sections) or 1.0
    body_room = max(len(sections) * min_body_chars, max_chars // 2)
    budgets: list[int] = []
    for heading, body in sections:
        target = int(body_room * (weights.get(heading, 1.0) / total_weight))
        budgets.append(max(min_body_chars, min(len(body), max(min_body_chars, target))))

    def _render(current_budgets: list[int]) -> str:
        parts: list[str] = []
        for idx, (heading, body) in enumerate(sections):
            parts.append(heading)
            if body:
                parts.append(_ellipsize(body, current_budgets[idx]))
            if idx < len(sections) - 1:
                parts.append("")
        return "\n".join(parts).strip()

    compact = _render(budgets)
    while len(compact) > max_chars:
        candidates = [idx for idx, budget in enumerate(budgets) if budget > min_body_chars]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: budgets[i])
        budgets[idx] = max(min_body_chars, budgets[idx] - 20)
        compact = _render(budgets)

    if len(compact) <= max_chars:
        return compact
    return _ellipsize(compact, max_chars)


def _is_timeout_like_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if "timed out" in text or "timeout" in text:
        return True
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, Exception):
        reason_text = str(reason).strip().lower()
        if "timed out" in reason_text or "timeout" in reason_text:
            return True
        if isinstance(reason, TimeoutError):
            return True
    return False


def _preferred_aoai_token_param(deployment_name: str) -> str:
    override = os.environ.get("AZURE_OPENAI_TOKEN_PARAM", "").strip().lower()
    if override in {"max_tokens", "max_completion_tokens"}:
        return override
    if deployment_name.strip().lower().startswith("gpt-5"):
        return "max_completion_tokens"
    return "max_tokens"


def _aoai_token_param_candidates(deployment_name: str) -> list[str]:
    preferred = _preferred_aoai_token_param(deployment_name)
    alternate = "max_completion_tokens" if preferred == "max_tokens" else "max_tokens"
    return [preferred, alternate]


def _preferred_aoai_temperature(deployment_name: str) -> float | None:
    override = os.environ.get("AZURE_OPENAI_TEMPERATURE", "").strip().lower()
    if override in {"default", "none", "omit"}:
        return None
    if override:
        try:
            return float(override)
        except ValueError:
            return None
    if deployment_name.strip().lower().startswith("gpt-5"):
        return None
    return 0.0


def _aoai_temperature_candidates(deployment_name: str) -> list[float | None]:
    preferred = _preferred_aoai_temperature(deployment_name)
    if preferred is None:
        return [None]
    return [preferred, None]


_AOAI_RETRY_HTTP_CODES = {408, 429, 500, 502, 503, 504}


class _AzureOpenAIClient:
    """Chat-completions client for Azure OpenAI with route / parameter fallbacks.

    Tries the endpoint styles ("deployments" and "v1"), retries transient failures, and adapts
    to deployments that reject max_tokens vs max_completion_tokens or a custom temperature.
    """

    def __init__(self, endpoint: str, deployment: str, api_key: str, api_version: str, max_attempts: int) -> None:
        self.endpoint = _normalize_aoai_endpoint(endpoint).rstrip("/")
        self.deployment = deployment
        self.api_key = api_key
        self.api_version = api_version
        self.max_attempts = max_attempts
        self.prefer_v1 = self.endpoint.lower().endswith(".cognitiveservices.azure.com")

    def _routes(
        self, messages: list[dict[str, str]], token_limit: int, token_param: str, temperature: float | None
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"messages": messages, token_param: token_limit}
        if temperature is not None:
            body["temperature"] = temperature
        deployments = {
            "name": "deployments",
            "url": (
                f"{self.endpoint}/openai/deployments/{quote(self.deployment)}"
                f"/chat/completions?api-version={quote(self.api_version)}"
            ),
            "body": body,
        }
        v1 = {
            "name": "v1",
            "url": f"{self.endpoint}/openai/v1/chat/completions",
            "body": {"model": self.deployment, **body},
        }
        return [v1, deployments] if self.prefer_v1 else [deployments, v1]

    def _post(self, route: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        request = Request(
            url=route["url"],
            method="POST",
            data=json.dumps(route["body"]).encode("utf-8"),
            headers={"Content-Type": "application/json", "api-key": self.api_key},
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _try_route(
        self, route: dict[str, Any], timeout_seconds: int, token_param: str, temperature: float | None
    ) -> tuple[str, str, str | None]:
        """One route with retries -> (content, error, signal).

        signal: "token_param" / "temperature" when the deployment rejects that parameter,
        "stop" for credential errors (no point trying other routes), else None.
        """
        name = route["name"]
        error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                payload = self._post(route, timeout_seconds)
            except HTTPError as exc:  # pragma: no cover
                try:
                    raw_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    raw_body = ""
                error = f"{name}: {f'{exc} {raw_body}'.strip()}"
                raw_lower = raw_body.lower()
                if exc.code == 400:
                    if "unsupported parameter" in raw_lower and f"'{token_param}'" in raw_lower:
                        return "", error, "token_param"
                    if "unsupported value" in raw_lower and "'temperature'" in raw_lower and temperature is not None:
                        return "", error, "temperature"
                if exc.code in (401, 403):  # invalid credentials fail fast
                    return "", error, "stop"
                if exc.code == 404:  # endpoint style doesn't match: try the other route
                    break
                if exc.code in _AOAI_RETRY_HTTP_CODES and attempt < self.max_attempts:
                    delay = common.retry_after_seconds(exc)
                    time.sleep(min(2 ** (attempt - 1), 8) if delay is None else delay)
                    continue
                break
            except Exception as exc:  # pragma: no cover  (URLError and anything else)
                timed_out = _is_timeout_like_error(exc)
                if timed_out and attempt < self.max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                error = (
                    f"{name}: timed out after {self.max_attempts} attempts ({exc})" if timed_out else f"{name}: {exc}"
                )
                break
            else:
                content, content_error = _extract_ai_text_from_payload(payload)
                return (content, "", None) if content else ("", f"{name}: {content_error}", None)
        return "", error, None

    def complete(self, messages: list[dict[str, str]], token_limit: int, timeout_seconds: int) -> tuple[str, str]:
        """Return (content, errors); content is empty when every combination failed."""
        errors: list[str] = []
        for temperature in _aoai_temperature_candidates(self.deployment):
            temperature_unsupported = False
            for token_param in _aoai_token_param_candidates(self.deployment):
                signal: str | None = None
                for route in self._routes(messages, token_limit, token_param, temperature):
                    content, error, signal = self._try_route(route, timeout_seconds, token_param, temperature)
                    if content:
                        return content, ""
                    if error:
                        errors.append(error)
                    if signal:
                        break
                if signal == "stop":
                    return "", " | ".join(errors).strip()
                if signal == "temperature":
                    temperature_unsupported = True
                    break
                if signal != "token_param":
                    break  # only try the alternate token parameter when the current one is unsupported
            if not temperature_unsupported:
                break
        return "", " | ".join(errors).strip()


def _chat_messages(system_suffix: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _reviewer_system_prompt() + system_suffix},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
    ]


def _fallback_reason(last_error: str) -> str:
    if "finish_reason=length" in last_error:
        return (
            f"Azure OpenAI response was cut by token limit ({last_error}); "
            "consider increasing PR_AI_MAX_TOKENS"
        )
    if not last_error:
        return "Azure OpenAI returned no usable text output"
    return f"Azure OpenAI unavailable ({last_error})"
