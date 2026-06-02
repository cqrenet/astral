#!/usr/bin/env python3
"""
Lightweight Azure OpenAI availability precheck for pipeline diagnostics.

This script is intentionally non-blocking: it always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _set_pipeline_var(name: str, value: str) -> None:
    print(f"##vso[task.setvariable variable={name}]{value}")


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


def _preferred_aoai_token_param(deployment_name: str) -> str:
    override = _env("AZURE_OPENAI_TOKEN_PARAM", "").lower()
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
    override = _env("AZURE_OPENAI_TEMPERATURE", "").lower()
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


def main() -> int:
    enabled = _env("ENABLE_PR_AI_SUMMARY", "true").lower() == "true"
    if not enabled:
        print("Azure OpenAI precheck skipped: ENABLE_PR_AI_SUMMARY=false")
        _set_pipeline_var("AOAI_AVAILABLE", "0")
        return 0

    endpoint = _env("AZURE_OPENAI_ENDPOINT")
    deployment = _env("AZURE_OPENAI_DEPLOYMENT")
    api_key = _env("AZURE_OPENAI_API_KEY")
    api_version = _env("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    if not endpoint or not deployment or not api_key:
        print("Azure OpenAI precheck skipped: missing endpoint/deployment/api-key variable")
        _set_pipeline_var("AOAI_AVAILABLE", "0")
        return 0

    endpoint_raw = endpoint
    endpoint = _normalize_aoai_endpoint(endpoint_raw)
    deployment_url = f"{endpoint}/openai/deployments/{quote(deployment)}/chat/completions?api-version={quote(api_version)}"
    v1_url = f"{endpoint}/openai/v1/chat/completions"

    print("Azure OpenAI precheck: starting")
    print(f"- endpoint(raw): {endpoint_raw}")
    print(f"- endpoint(normalized): {endpoint}")
    print(f"- deployment: {deployment}")
    print(f"- api_version: {api_version}")
    prefer_v1 = endpoint.lower().endswith(".cognitiveservices.azure.com")
    health_messages = [
        {"role": "system", "content": "You are a health-check assistant."},
        {"role": "user", "content": "Reply with: OK"},
    ]

    for temperature in _aoai_temperature_candidates(deployment):
        temperature_unsupported = False
        for token_param in _aoai_token_param_candidates(deployment):
            deployment_payload = {
                "messages": health_messages,
                token_param: 16,
            }
            v1_payload = {
                "model": deployment,
                "messages": health_messages,
                token_param: 16,
            }
            if temperature is not None:
                deployment_payload["temperature"] = temperature
                v1_payload["temperature"] = temperature

            routes = (
                [("v1", v1_url, v1_payload), ("deployments", deployment_url, deployment_payload)]
                if prefer_v1
                else [("deployments", deployment_url, deployment_payload), ("v1", v1_url, v1_payload)]
            )

            token_param_unsupported = False
            for route_name, route_url, payload in routes:
                req = Request(
                    url=route_url,
                    method="POST",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "api-key": api_key,
                    },
                )
                try:
                    with urlopen(req, timeout=45) as resp:
                        _ = json.loads(resp.read().decode("utf-8"))
                    print(f"Azure OpenAI precheck: SUCCESS via {route_name} route")
                    _set_pipeline_var("AOAI_AVAILABLE", "1")
                    return 0
                except HTTPError as exc:
                    raw = ""
                    try:
                        raw = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        raw = ""
                    print(f"Azure OpenAI precheck: HTTP {exc.code} via {route_name} route")
                    if raw:
                        print(raw)
                    if exc.code == 400:
                        raw_lower = raw.lower()
                        if "unsupported parameter" in raw_lower and f"'{token_param}'" in raw_lower:
                            token_param_unsupported = True
                            break
                        if "unsupported value" in raw_lower and "'temperature'" in raw_lower and temperature is not None:
                            temperature_unsupported = True
                            break
                    if exc.code == 404:
                        # Try fallback route first.
                        continue
                    if exc.code in (401, 403):
                        print("Hint: Check AZURE_OPENAI_API_KEY and endpoint/resource pairing.")
                        _set_pipeline_var("AOAI_AVAILABLE", "0")
                        return 0
                    if exc.code == 400:
                        print("Hint: Check model/deployment name and API version compatibility.")
                        _set_pipeline_var("AOAI_AVAILABLE", "0")
                        return 0
                    _set_pipeline_var("AOAI_AVAILABLE", "0")
                    return 0
                except URLError as exc:
                    print(f"Azure OpenAI precheck: network error via {route_name} route: {exc}")
                    _set_pipeline_var("AOAI_AVAILABLE", "0")
                    return 0
                except Exception as exc:  # pragma: no cover
                    print(f"Azure OpenAI precheck: unexpected error via {route_name} route: {exc}")
                    _set_pipeline_var("AOAI_AVAILABLE", "0")
                    return 0
            if temperature_unsupported:
                break
            if not token_param_unsupported:
                break
        if not temperature_unsupported:
            break

    print("Azure OpenAI precheck: no successful response from tested routes/token-params")
    print("Hint: Verify AZURE_OPENAI_ENDPOINT points to the resource root, without /openai path suffix.")
    print("Hint: Verify AZURE_OPENAI_DEPLOYMENT is the deployment name (for v1 this is passed as model).")
    _set_pipeline_var("AOAI_AVAILABLE", "0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
