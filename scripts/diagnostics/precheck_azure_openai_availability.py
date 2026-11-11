#!/usr/bin/env python3
"""
Lightweight Azure OpenAI availability precheck for pipeline diagnostics.

This script is intentionally non-blocking: it always exits 0. It reports through the AOAI_AVAILABLE
CI variable ("1" when some endpoint style / parameter combination answered, else "0").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ holds the shared modules

import common  # noqa: E402
from pr_ai import _aoai_temperature_candidates, _aoai_token_param_candidates, _normalize_aoai_endpoint  # noqa: E402

log = common.log

_HEALTH_MESSAGES = [
    {"role": "system", "content": "You are a health-check assistant."},
    {"role": "user", "content": "Reply with: OK"},
]


def _report(available: bool) -> None:
    common.ci_set_env("AOAI_AVAILABLE", "1" if available else "0")


def _routes(
    endpoint: str, deployment: str, api_version: str, token_param: str, temperature: float | None
) -> list[tuple[str, str, dict]]:
    """(name, url, payload) for each endpoint style, in the order worth trying first."""
    body: dict = {"messages": _HEALTH_MESSAGES, token_param: 16}
    if temperature is not None:
        body["temperature"] = temperature
    deployments = (
        "deployments",
        f"{endpoint}/openai/deployments/{quote(deployment)}/chat/completions?api-version={quote(api_version)}",
        body,
    )
    v1 = ("v1", f"{endpoint}/openai/v1/chat/completions", {"model": deployment, **body})
    return [v1, deployments] if endpoint.lower().endswith(".cognitiveservices.azure.com") else [deployments, v1]


def _probe(
    route: tuple[str, str, dict], api_key: str, token_param: str, temperature: float | None
) -> str:
    """Call one route. Returns 'ok', 'next' (try the other route), 'token_param' / 'temperature'
    (the deployment rejects that parameter) or 'fail' (stop: hints are already logged)."""
    name, url, payload = route
    request = Request(
        url=url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "api-key": api_key},
    )
    try:
        with urlopen(request, timeout=45) as resp:
            json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        log.info(f"Azure OpenAI precheck: HTTP {exc.code} via {name} route")
        if raw:
            log.info(raw)
        lowered = raw.lower()
        if exc.code == 400 and "unsupported parameter" in lowered and f"'{token_param}'" in lowered:
            return "token_param"
        if exc.code == 400 and "unsupported value" in lowered and "'temperature'" in lowered and temperature is not None:
            return "temperature"
        if exc.code == 404:
            return "next"
        if exc.code in (401, 403):
            log.info("Hint: Check AZURE_OPENAI_API_KEY and endpoint/resource pairing.")
        elif exc.code == 400:
            log.info("Hint: Check model/deployment name and API version compatibility.")
        return "fail"
    except URLError as exc:
        log.info(f"Azure OpenAI precheck: network error via {name} route: {exc}")
        return "fail"
    except Exception as exc:  # pragma: no cover
        log.info(f"Azure OpenAI precheck: unexpected error via {name} route: {exc}")
        return "fail"
    log.info(f"Azure OpenAI precheck: SUCCESS via {name} route")
    return "ok"


def main() -> int:
    if common.env_text("ENABLE_PR_AI_SUMMARY", "true").lower() != "true":
        log.info("Azure OpenAI precheck skipped: ENABLE_PR_AI_SUMMARY=false")
        _report(False)
        return 0

    endpoint_raw = common.env_text("AZURE_OPENAI_ENDPOINT")
    deployment = common.env_text("AZURE_OPENAI_DEPLOYMENT")
    api_key = common.env_text("AZURE_OPENAI_API_KEY")
    api_version = common.env_text("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    if not endpoint_raw or not deployment or not api_key:
        log.info("Azure OpenAI precheck skipped: missing endpoint/deployment/api-key variable")
        _report(False)
        return 0

    endpoint = _normalize_aoai_endpoint(endpoint_raw)
    log.info("Azure OpenAI precheck: starting")
    log.info(f"- endpoint(raw): {endpoint_raw}")
    log.info(f"- endpoint(normalized): {endpoint}")
    log.info(f"- deployment: {deployment}")
    log.info(f"- api_version: {api_version}")

    for temperature in _aoai_temperature_candidates(deployment):
        temperature_unsupported = False
        for token_param in _aoai_token_param_candidates(deployment):
            outcome = "next"
            for route in _routes(endpoint, deployment, api_version, token_param, temperature):
                outcome = _probe(route, api_key, token_param, temperature)
                if outcome != "next":
                    break
            if outcome in ("ok", "fail"):
                _report(outcome == "ok")
                return 0
            if outcome == "temperature":
                temperature_unsupported = True
                break
            if outcome != "token_param":
                break  # every route answered 404: another token parameter will not help
        if not temperature_unsupported:
            break

    log.info("Azure OpenAI precheck: no successful response from tested routes/token-params")
    log.info("Hint: Verify AZURE_OPENAI_ENDPOINT points to the resource root, without /openai path suffix.")
    log.info("Hint: Verify AZURE_OPENAI_DEPLOYMENT is the deployment name (for v1 this is passed as model).")
    _report(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
