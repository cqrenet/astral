#!/usr/bin/env python3
"""Probe tenant audit logs to detect configuration changes and decide whether to trigger a backup pipeline.

This script is designed to run inside an Azure Function timer trigger or locally for testing.
It queries Microsoft Graph audit endpoints for the cheapest possible signal that a configuration
change occurred since the last check, then applies a debouncer so that a burst of changes during
an admin sprint results in a single backup run after a configurable quiet window.

Usage (local testing):
    python3 scripts/probe_tenant_changes.py \
        --token "$GRAPH_TOKEN" \
        --state-path ./probe-state.json \
        --quiet-window-minutes 15 \
        --cooldown-minutes 30

Usage (Azure Function wrapper):
    python3 scripts/probe_tenant_changes.py \
        --token "$GRAPH_TOKEN" \
        --state-json '{"intune":{"last_check":"2026-04-20T10:00:00+00:00"},...}' \
        --quiet-window-minutes 15 \
        --cooldown-minutes 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import urllib.parse
from typing import Any

# scripts/ is not guaranteed to be on PYTHONPATH when loaded by the Function wrapper,
# so we tolerate a relative import failure and fall back to an absolute import.
try:
    from scripts import common
except ImportError:
    import common  # type: ignore[no-redef]

log = common.log
request_json = common.request_json


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTUNE_AUDIT_URL = "https://graph.microsoft.com/beta/deviceManagement/auditEvents"
_ENTRA_AUDIT_URL = "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits"

# Target resource types in Entra that map to the categories exported by export_entra_baseline.py.
_ENTRA_TARGET_TYPES = (
    "ConditionalAccessPolicy",
    "NamedLocation",
    "AuthenticationStrengthPolicy",
    "Application",
    "ServicePrincipal",
)

_DEFAULT_STATE: dict[str, Any] = {
    "intune": {"last_check": None},
    "entra": {"last_check": None},
    "debouncer": {
        "state": "idle",
        "first_event_at": None,
        "trigger_after": None,
        "cooldown_until": None,
    },
}


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------

def _acquire_graph_token(client_id: str, client_secret: str, tenant_id: str) -> str:
    """Acquire a Graph access token via client credentials flow."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("Token endpoint did not return an access_token.")
    return str(access_token)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=os.environ.get("GRAPH_TOKEN", ""), help="Microsoft Graph bearer token (default: $GRAPH_TOKEN).")
    parser.add_argument("--client-id", default="", help="Entra app client ID (alternative to --token).")
    parser.add_argument("--client-secret", default="", help="Entra app client secret (alternative to --token).")
    parser.add_argument("--tenant-id", default="", help="Entra tenant ID (alternative to --token).")
    parser.add_argument(
        "--state-path",
        default="",
        help="Path to a local JSON state file (used for local testing).",
    )
    parser.add_argument(
        "--state-json",
        default="",
        help="Raw JSON state string (used when the caller manages persistence, e.g. Azure Table Storage).",
    )
    parser.add_argument(
        "--quiet-window-minutes",
        type=int,
        default=15,
        help="Minutes of silence after the last detected change before triggering a backup.",
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=int,
        default=30,
        help="Minimum minutes between two triggered backup runs.",
    )
    parser.add_argument(
        "--now",
        default="",
        help="Override the current time (ISO 8601). Useful for tests.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state(path: str, json_str: str) -> dict[str, Any]:
    if json_str:
        return json.loads(json_str)
    if path:
        p = pathlib.Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(json.dumps(_DEFAULT_STATE))


def _save_state(path: str, state: dict[str, Any]) -> None:
    if path:
        pathlib.Path(path).write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _format_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Graph queries
# ---------------------------------------------------------------------------

def _build_intune_filter(since: dt.datetime, until: dt.datetime) -> str:
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"activityDateTime ge {since_str}"
        f" and activityDateTime le {until_str}"
        f" and activityResult eq 'Success'"
        f" and ActivityOperationType ne 'Get'"
    )


def _build_entra_filter(since: dt.datetime, until: dt.datetime) -> str:
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    type_clauses = " or ".join(
        f"targetResources/any(t: t/type eq '{t}')" for t in _ENTRA_TARGET_TYPES
    )
    return (
        f"activityDateTime ge {since_str}"
        f" and activityDateTime le {until_str}"
        f" and result eq 'success'"
        f" and ({type_clauses})"
    )


def _fetch_latest_event(url: str, token: str) -> dict[str, Any] | None:
    """Return the single latest matching audit event, or None if nothing found."""
    try:
        payload = request_json(url, token=token, timeout=30, max_retries=2)
    except Exception as exc:
        # Defensive: log and treat as no event so a transient Graph failure does
        # not wedge the debouncer in an armed state forever.
        log.warning(f"Graph query failed ({exc})")
        return None

    value = payload.get("value")
    if isinstance(value, list) and value:
        event = value[0]
        if isinstance(event, dict):
            return event
    return None


def _get_latest_intune_event(
    token: str, since: dt.datetime, until: dt.datetime
) -> dict[str, Any] | None:
    filter_str = _build_intune_filter(since, until)
    params = {
        "$filter": filter_str,
        "$orderby": "activityDateTime desc",
        "$top": "1",
        "$select": "id,activityDateTime,activityType,activityOperationType",
    }
    url = f"{_INTUNE_AUDIT_URL}?{urllib.parse.urlencode(params)}"
    return _fetch_latest_event(url, token)


def _get_latest_entra_event(
    token: str, since: dt.datetime, until: dt.datetime
) -> dict[str, Any] | None:
    filter_str = _build_entra_filter(since, until)
    params = {
        "$filter": filter_str,
        "$orderby": "activityDateTime desc",
        "$top": "1",
        "$select": "id,activityDateTime,activityDisplayName",
    }
    url = f"{_ENTRA_AUDIT_URL}?{urllib.parse.urlencode(params)}"
    return _fetch_latest_event(url, token)


# ---------------------------------------------------------------------------
# Debouncer
# ---------------------------------------------------------------------------

def _record(
    state: str = "idle",
    first_event_at: str | None = None,
    trigger_after: str | None = None,
    cooldown_until: str | None = None,
) -> dict[str, Any]:
    """A fresh debouncer record."""
    return {
        "state": state,
        "first_event_at": first_event_at,
        "trigger_after": trigger_after,
        "cooldown_until": cooldown_until,
    }


def _on_idle(
    deb: dict[str, Any], latest: dt.datetime | None, now: dt.datetime, quiet: dt.timedelta, cooldown: dt.timedelta,
    workloads: list[str],
) -> tuple[bool, dict[str, Any], str]:
    if latest is None:
        return False, deb, "No changes detected."
    trigger_after = now + quiet
    reason = (
        f"Change detected at {_format_iso(latest)}; "
        f"armed, trigger scheduled for {_format_iso(trigger_after)}."
    )
    return False, _record("armed", _format_iso(latest), _format_iso(trigger_after)), reason


def _on_armed(
    deb: dict[str, Any], latest: dt.datetime | None, now: dt.datetime, quiet: dt.timedelta, cooldown: dt.timedelta,
    workloads: list[str],
) -> tuple[bool, dict[str, Any], str]:
    if latest is None:
        trigger_after = _parse_iso(deb.get("trigger_after"))
        if trigger_after is not None and now >= trigger_after:
            return (
                True,
                _record("cooldown", cooldown_until=_format_iso(now + cooldown)),
                "Quiet window satisfied; no new events since last check.",
            )
        return False, deb, f"Armed, waiting for quiet window until {_format_iso(trigger_after)}."
    # Activity is still ongoing: extend the quiet window.
    trigger_after = now + quiet
    first_event = deb.get("first_event_at") or _format_iso(latest)
    reason = (
        f"Additional change detected at {_format_iso(latest)} "
        f"({'/'.join(workloads)}); quiet window extended to {_format_iso(trigger_after)}."
    )
    return False, _record("armed", first_event, _format_iso(trigger_after)), reason


_DEBOUNCER_STATES = {"idle": _on_idle, "armed": _on_armed}


def _evaluate_debouncer(
    state: dict[str, Any],
    intune_event: dict[str, Any] | None,
    entra_event: dict[str, Any] | None,
    now: dt.datetime,
    quiet_window: dt.timedelta,
    cooldown: dt.timedelta,
) -> tuple[bool, dict[str, Any], str]:
    """Return (should_trigger, updated_state, human_readable_reason).

    States: idle -> armed (first change) -> cooldown (quiet window passed, backup fired) -> idle.
    """
    deb = dict(state.get("debouncer") or {})
    deb_state = str(deb.get("state") or "idle")
    events = {"intune": intune_event, "entra": entra_event}
    times = [_parse_iso(e.get("activityDateTime")) for e in events.values() if e]
    latest = max((t for t in times if t is not None), default=None)

    if deb_state == "cooldown":
        cooldown_until = _parse_iso(deb.get("cooldown_until"))
        if cooldown_until is not None and now < cooldown_until:
            # Don't drop changes made during cooldown: remember them so a backup runs
            # when cooldown ends (otherwise they stay unseen until the daily run).
            if latest is not None:
                deb["pending_event_at"] = _format_iso(latest)
            state["debouncer"] = deb
            deferred = sum(e is not None for e in events.values())
            return False, state, f"In cooldown until {_format_iso(cooldown_until)}; {deferred} event(s) deferred."
        pending = deb.get("pending_event_at")
        # Cooldown expired: deferred changes are due now, otherwise fall through to idle.
        deb = _record("armed", pending, _format_iso(now)) if pending else _record()
        deb_state = deb["state"]

    handler = _DEBOUNCER_STATES.get(deb_state)
    if handler is None:
        state["debouncer"] = _record()
        return False, state, f"Unexpected debouncer state '{deb_state}'; resetting to idle."
    workloads = [name for name, event in events.items() if event]
    fired, deb, reason = handler(deb, latest, now, quiet_window, cooldown, workloads)
    state["debouncer"] = deb
    return fired, state, reason


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    token = args.token.strip()
    if not token:
        if args.client_id and args.client_secret and args.tenant_id:
            token = _acquire_graph_token(args.client_id, args.client_secret, args.tenant_id)
        else:
            log.error(
                "Provide --token, or all three of --client-id, --client-secret, --tenant-id.",
            )
            raise SystemExit(1)

    quiet_window = dt.timedelta(minutes=args.quiet_window_minutes)
    cooldown = dt.timedelta(minutes=args.cooldown_minutes)

    now = _parse_iso(args.now) or dt.datetime.now(dt.timezone.utc)
    # Truncate to second for cleaner output
    now = now.replace(microsecond=0)

    state = _load_state(args.state_path, args.state_json)

    # Initialise missing last_check values to a safe default (24 hours ago).
    # This prevents a brand-new state file from scanning the entire audit log history.
    default_since = now - dt.timedelta(hours=24)
    intune_since = _parse_iso(state.get("intune", {}).get("last_check")) or default_since
    entra_since = _parse_iso(state.get("entra", {}).get("last_check")) or default_since

    # ------------------------------------------------------------------
    # Query Graph
    # ------------------------------------------------------------------
    intune_event = _get_latest_intune_event(token, intune_since, now)
    entra_event = _get_latest_entra_event(token, entra_since, now)

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------
    trigger, state, reason = _evaluate_debouncer(
        state, intune_event, entra_event, now, quiet_window, cooldown
    )

    # ------------------------------------------------------------------
    # Advance watermarks regardless of trigger decision so the next run
    # does not re-scan the same window.
    # ------------------------------------------------------------------
    state.setdefault("intune", {})["last_check"] = _format_iso(now)
    state.setdefault("entra", {})["last_check"] = _format_iso(now)

    _save_state(args.state_path, state)

    # ------------------------------------------------------------------
    # Emit decision
    # ------------------------------------------------------------------
    result = {
        "trigger": trigger,
        "reason": reason,
        "checked_at": _format_iso(now),
        "intune_event": intune_event,
        "entra_event": entra_event,
        "new_state": state,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
