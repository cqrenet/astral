#!/usr/bin/env python3
"""Trigger an Azure DevOps pipeline run via REST API.

Intended to be invoked from the queue-consumer Azure Function or locally for testing.

Usage:
    python3 scripts/trigger_backup_pipeline.py \
        --organization "my-org" \
        --project "my-project" \
        --pipeline-id 123 \
        --token "$ADO_PAT" \
        --branch "main" \
        --parameters '{"forceFullRun": false}'
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

try:
    from scripts.common import request_json
except ImportError:
    from common import request_json  # type: ignore[no-redef]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--pipeline-id", type=int, required=True)
    parser.add_argument("--token", required=True, help="Azure DevOps PAT or OAuth token.")
    parser.add_argument("--branch", default="main", help="Git ref to run against.")
    parser.add_argument(
        "--parameters",
        default="{}",
        help='JSON object of pipeline template parameters (e.g. \'{"forceFullRun": true}\').',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    base_url = (
        f"https://dev.azure.com/{args.organization}/{args.project}"
        f"/_apis/pipelines/{args.pipeline_id}/runs?api-version=7.1"
    )

    body: dict[str, Any] = {
        "resources": {
            "repositories": {
                "self": {"refName": f"refs/heads/{args.branch.lstrip('refs/heads/')}"}
            }
        },
    }

    params = json.loads(args.parameters)
    if isinstance(params, dict) and params:
        body["templateParameters"] = params

    # ADO REST API accepts Basic auth with an empty username and the PAT as password.
    import base64
    encoded = base64.b64encode(f":{args.token}".encode("utf-8")).decode("utf-8")
    auth_header = f"Basic {encoded}"

    print(f"Triggering pipeline {args.pipeline_id} on branch {args.branch} ...")
    response = request_json(
        base_url,
        method="POST",
        body=body,
        headers={"Authorization": auth_header},
        timeout=30,
        max_retries=2,
    )

    run_id = response.get("id")
    run_url = response.get("url")
    print(f"Queued run id={run_id} url={run_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
