#!/usr/bin/env python3
"""Azure Function timer trigger that probes tenant audit logs and queues a backup run when changes are detected."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any

import azure.functions as func
from azure.data.tables import TableServiceClient

_TABLE_NAME = "ProbeState"
_PARTITION_KEY = "singleton"
_ROW_KEY = "default"


def _repo_root() -> str:
    """Resolve the repository root so we can invoke scripts/probe_tenant_changes.py."""
    env_root = os.environ.get("REPO_ROOT", "").strip()
    if env_root:
        return os.path.abspath(env_root)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_state(connection_string: str) -> dict[str, Any]:
    """Load persisted probe state from Azure Table Storage."""
    try:
        service = TableServiceClient.from_connection_string(conn_str=connection_string)
        table = service.get_table_client(table_name=_TABLE_NAME)
        entity = table.get_entity(partition_key=_PARTITION_KEY, row_key=_ROW_KEY)
        raw = entity.get("state", "{}")
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception as exc:
        logging.warning(f"Unable to load state from Table Storage ({exc}); starting fresh.")
        return {}


def _save_state(connection_string: str, state: dict[str, Any]) -> None:
    """Persist probe state to Azure Table Storage."""
    service = TableServiceClient.from_connection_string(conn_str=connection_string)
    table = service.get_table_client(table_name=_TABLE_NAME)
    table.upsert_entity(
        {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": _ROW_KEY,
            "state": json.dumps(state),
        }
    )


def main(mytimer: func.TimerRequest, msg: func.Out[str]) -> None:
    utc_now = mytimer.schedule_status.get("Last", "n/a") if mytimer.schedule_status else "n/a"
    logging.info(f"Probe timer triggered at {utc_now}")

    client_id = os.environ.get("PROBE_APP_ID", "").strip()
    client_secret = os.environ.get("PROBE_APP_SECRET", "").strip()
    tenant_id = os.environ.get("TENANT_ID", "").strip()
    token = os.environ.get("GRAPH_TOKEN", "").strip()

    auth_args: list[str] = []
    if token:
        auth_args = ["--token", token]
    elif client_id and client_secret and tenant_id:
        auth_args = [
            "--client-id", client_id,
            "--client-secret", client_secret,
            "--tenant-id", tenant_id,
        ]
    else:
        logging.error("No Graph authentication configured (PROBE_APP_ID/SECRET/TENANT_ID or GRAPH_TOKEN).")
        return

    connection_string = os.environ.get("AzureWebJobsStorage", "").strip()
    if not connection_string:
        logging.error("AzureWebJobsStorage connection string is missing.")
        return

    state = _load_state(connection_string)
    state_json = json.dumps(state) if state else ""
    quiet_window = os.environ.get("PROBE_QUIET_WINDOW_MINUTES", "15")
    cooldown = os.environ.get("PROBE_COOLDOWN_MINUTES", "30")

    probe_script = os.path.join(_repo_root(), "scripts", "probe_tenant_changes.py")
    if not os.path.exists(probe_script):
        logging.error(f"Probe script not found at {probe_script}")
        return

    cmd = [
        sys.executable,
        probe_script,
        *auth_args,
        "--quiet-window-minutes", quiet_window,
        "--cooldown-minutes", cooldown,
    ]
    if state_json:
        cmd.extend(["--state-json", state_json])

    logging.info(f"Running probe script: {probe_script}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        logging.error("Probe script timed out after 60 seconds.")
        return
    except Exception as exc:
        logging.error(f"Failed to run probe script ({exc}).")
        return

    if result.returncode != 0:
        logging.error(f"Probe script failed (exit {result.returncode}): {result.stderr}")
        return

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logging.error(f"Probe script returned invalid JSON ({exc}): {result.stdout[:500]}")
        return

    new_state = output.get("new_state", state)
    _save_state(connection_string, new_state)

    trigger = output.get("trigger", False)
    reason = output.get("reason", "no reason given")
    logging.info(f"Probe result: trigger={trigger}, reason={reason}")

    if trigger:
        queue_payload = json.dumps(
            {
                "reason": reason,
                "checked_at": output.get("checked_at", ""),
            }
        )
        msg.set(queue_payload)
        logging.info("Queued backup trigger message.")
