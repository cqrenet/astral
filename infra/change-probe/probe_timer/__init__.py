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


def _auth_args() -> list[str] | None:
    """Probe CLI arguments for Graph authentication: a ready token, else the app credentials; None if neither."""
    token = os.environ.get("GRAPH_TOKEN", "").strip()
    if token:
        return ["--token", token]
    client_id = os.environ.get("PROBE_APP_ID", "").strip()
    client_secret = os.environ.get("PROBE_APP_SECRET", "").strip()
    tenant_id = os.environ.get("TENANT_ID", "").strip()
    if client_id and client_secret and tenant_id:
        return ["--client-id", client_id, "--client-secret", client_secret, "--tenant-id", tenant_id]
    return None


def _run_probe(cmd: list[str]) -> dict[str, Any] | None:
    """Run the probe script and parse its JSON result; None (after logging why) when it did not produce one."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        logging.error("Probe script timed out after 60 seconds.")
        return None
    except Exception as exc:
        logging.error(f"Failed to run probe script ({exc}).")
        return None
    if result.returncode != 0:
        logging.error(f"Probe script failed (exit {result.returncode}): {result.stderr}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logging.error(f"Probe script returned invalid JSON ({exc}): {result.stdout[:500]}")
        return None


def main(mytimer: func.TimerRequest, msg: func.Out[str]) -> None:
    last_run = mytimer.schedule_status.get("Last", "n/a") if mytimer.schedule_status else "n/a"
    logging.info(f"Probe timer triggered at {last_run}")

    auth_args = _auth_args()
    if auth_args is None:
        logging.error("No Graph authentication configured (PROBE_APP_ID/SECRET/TENANT_ID or GRAPH_TOKEN).")
        return
    connection_string = os.environ.get("AzureWebJobsStorage", "").strip()
    if not connection_string:
        logging.error("AzureWebJobsStorage connection string is missing.")
        return
    probe_script = os.path.join(_repo_root(), "scripts", "probe_tenant_changes.py")
    if not os.path.exists(probe_script):
        logging.error(f"Probe script not found at {probe_script}")
        return

    state = _load_state(connection_string)
    cmd = [
        sys.executable,
        probe_script,
        *auth_args,
        "--quiet-window-minutes", os.environ.get("PROBE_QUIET_WINDOW_MINUTES", "15"),
        "--cooldown-minutes", os.environ.get("PROBE_COOLDOWN_MINUTES", "30"),
    ]
    if state:
        cmd.extend(["--state-json", json.dumps(state)])

    logging.info(f"Running probe script: {probe_script}")
    output = _run_probe(cmd)
    if output is None:
        return

    _save_state(connection_string, output.get("new_state", state))
    reason = output.get("reason", "no reason given")
    logging.info(f"Probe result: trigger={output.get('trigger', False)}, reason={reason}")
    if output.get("trigger", False):
        msg.set(json.dumps({"reason": reason, "checked_at": output.get("checked_at", "")}))
        logging.info("Queued backup trigger message.")
