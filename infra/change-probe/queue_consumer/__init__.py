#!/usr/bin/env python3
"""Azure Function queue trigger that calls the Azure DevOps REST API to queue a backup pipeline run."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import azure.functions as func


def _repo_root() -> str:
    """Resolve the repository root so we can invoke scripts/trigger_backup_pipeline.py."""
    env_root = os.environ.get("REPO_ROOT", "").strip()
    if env_root:
        return os.path.abspath(env_root)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _ado_settings() -> dict[str, str]:
    """ADO connection settings from the environment; raises (so the runtime retries) when incomplete."""
    settings = {
        "organization": os.environ.get("ADO_ORGANIZATION", "").strip(),
        "project": os.environ.get("ADO_PROJECT", "").strip(),
        "pipeline-id": os.environ.get("ADO_PIPELINE_ID", "").strip(),
        "token": os.environ.get("ADO_TOKEN", "").strip(),
        "branch": os.environ.get("ADO_BRANCH", "main").strip(),
    }
    if not all(value for name, value in settings.items() if name != "branch"):  # branch may be empty
        logging.error("Missing one or more ADO configuration variables (ADO_ORGANIZATION, ADO_PROJECT, ADO_PIPELINE_ID, ADO_TOKEN).")
        # Re-raising causes the Functions runtime to retry the message after the visibility timeout.
        raise RuntimeError("Incomplete ADO configuration")
    return settings


def main(msg: func.QueueMessage) -> None:
    logging.info(f"Queue consumer received message: {msg.get_body().decode('utf-8')}")
    settings = _ado_settings()

    trigger_script = os.path.join(_repo_root(), "scripts", "trigger_backup_pipeline.py")
    if not os.path.exists(trigger_script):
        logging.error(f"Trigger script not found at {trigger_script}")
        raise RuntimeError("Trigger script missing")

    cmd = [sys.executable, trigger_script]
    for name, value in settings.items():
        if name != "token":
            cmd.extend([f"--{name}", value])
    child_env = {"PATH": os.environ.get("PATH", "")}
    child_env["ADO_TOKEN"] = settings["token"]

    logging.info(f"Triggering ADO pipeline {settings['pipeline-id']} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=child_env)
    except subprocess.TimeoutExpired:
        logging.error("Trigger script timed out after 60 seconds.")
        raise
    except Exception as exc:
        logging.error(f"Failed to run trigger script ({exc}).")
        raise

    if result.returncode != 0:
        logging.error(f"Trigger script failed (exit {result.returncode}): {result.stderr}")
        raise RuntimeError(f"Trigger script failed: {result.stderr}")
    logging.info(f"Trigger script succeeded: {result.stdout.strip()}")
