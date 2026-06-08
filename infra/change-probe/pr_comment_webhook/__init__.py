#!/usr/bin/env python3
"""Azure Function HTTP trigger — ADO service hook receiver for PR comment events.

Fires the review-sync pipeline immediately when a reviewer posts /accept or /reject
on a drift PR, rather than waiting for the 20-minute schedule poll.

ADO service hook config (Project Settings → Service hooks → Web Hooks):
  Event:    Pull request commented on
  Action:   POST  https://<func-app>.azurewebsites.net/api/pr_comment_webhook
  Username: (any value, e.g. "astral")
  Password: value of WEBHOOK_SECRET app setting

The 20-minute schedule in azure-pipelines-review-sync.yml remains as a fallback.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

import azure.functions as func

# Matches /accept or /reject at the start of a line, case-insensitive.
# Trailing word boundary prevents matching /accepted, /rejection, etc.
_DECISION_RE = re.compile(r"^\s*/(accept|reject)\b", re.IGNORECASE | re.MULTILINE)


def _trigger_pipeline(org: str, project: str, pipeline_id: str, token: str, branch: str) -> int:
    """Queue an ADO pipeline run and return the new run ID."""
    url = (
        f"https://dev.azure.com/{org}/{project}"
        f"/_apis/pipelines/{pipeline_id}/runs?api-version=7.1"
    )
    body = json.dumps({
        "resources": {
            "repositories": {
                "self": {"refName": f"refs/heads/{branch.lstrip('refs/heads/')}"}
            }
        }
    }).encode()

    encoded = base64.b64encode(f":{token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result.get("id", -1)


def _authorized(req: func.HttpRequest) -> bool:
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret:
        return True  # no secret configured — allow all (trust network/firewall)
    expected = "Basic " + base64.b64encode(f":{secret}".encode()).decode()
    return req.headers.get("Authorization", "") == expected


def main(req: func.HttpRequest) -> func.HttpResponse:
    if not _authorized(req):
        logging.warning("pr_comment_webhook: rejected — invalid Authorization")
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse("Bad Request: invalid JSON", status_code=400)

    comment_content: str = (
        payload.get("resource", {}).get("comment", {}).get("content", "")
    )
    pr_id: int | None = (
        payload.get("resource", {}).get("pullRequest", {}).get("pullRequestId")
    )

    if not _DECISION_RE.search(comment_content):
        logging.debug("pr_comment_webhook: no /accept|/reject found — ignoring")
        return func.HttpResponse("OK", status_code=200)

    logging.info(
        "pr_comment_webhook: /accept|/reject detected in PR #%s — triggering review-sync",
        pr_id,
    )

    org = os.environ.get("ADO_ORGANIZATION", "").strip()
    project = os.environ.get("ADO_PROJECT", "").strip()
    pipeline_id = os.environ.get("ADO_REVIEW_SYNC_PIPELINE_ID", "").strip()
    token = os.environ.get("ADO_TOKEN", "").strip()
    branch = os.environ.get("ADO_BRANCH", "main").strip()

    if not all([org, project, pipeline_id, token]):
        logging.error(
            "pr_comment_webhook: ADO_REVIEW_SYNC_PIPELINE_ID or other ADO vars not configured"
        )
        # 200 so ADO doesn't retry; schedule will catch up within 20 min.
        return func.HttpResponse("OK (misconfigured — schedule will catch up)", status_code=200)

    try:
        run_id = _trigger_pipeline(org, project, pipeline_id, token, branch)
        logging.info("pr_comment_webhook: queued review-sync run id=%s", run_id)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logging.error("pr_comment_webhook: ADO returned %s: %s", exc.code, body)
        return func.HttpResponse("OK (trigger failed — schedule will catch up)", status_code=200)
    except Exception as exc:
        logging.error("pr_comment_webhook: unexpected error: %s", exc)
        return func.HttpResponse("OK (trigger failed — schedule will catch up)", status_code=200)

    return func.HttpResponse("OK", status_code=200)
