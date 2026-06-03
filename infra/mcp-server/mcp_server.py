#!/usr/bin/env python3
"""ASTRAL MCP Server — exposes tenant state and drift history to AI assistants.

Supports stdio transport (local development) and SSE transport (Azure hosting).

Usage:
    python mcp_server.py --transport stdio
    python mcp_server.py --transport sse --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Resolve script imports (bundled scripts/ directory or repo-root scripts/)
# ---------------------------------------------------------------------------
_script_dir = os.path.join(os.path.dirname(__file__), "scripts")
if os.path.isfile(os.path.join(_script_dir, "astral_mcp_tools.py")):
    sys.path.insert(0, _script_dir)
else:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

try:
    from astral_mcp_tools import AstralMcpClient, client_from_env
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Unable to import astral_mcp_tools. Ensure scripts/ directory is on PYTHONPATH."
    ) from exc

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("astral")

# ---------------------------------------------------------------------------
# Optional API key authentication (lightweight alternative to Entra ID)
# ---------------------------------------------------------------------------
_api_key = os.environ.get("MCP_API_KEY", "").strip()
if _api_key:
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import PlainTextResponse

        class _ApiKeyMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                # Allow health probes without a key.
                if request.url.path in ("/health", "/"):
                    return await call_next(request)
                header = request.headers.get("x-api-key", "")
                auth = request.headers.get("authorization", "")
                provided = header
                if auth.lower().startswith("bearer "):
                    provided = auth[7:]
                if provided != _api_key:
                    return PlainTextResponse("Unauthorized", status_code=401)
                return await call_next(request)

        _orig_sse_app = mcp.sse_app

        def _wrapped_sse_app(*args, **kwargs):
            app = _orig_sse_app(*args, **kwargs)
            app.add_middleware(_ApiKeyMiddleware)
            return app

        mcp.sse_app = _wrapped_sse_app
        logger.info("API key authentication enabled.")
    except Exception as exc:
        logger.warning(f"Could not enable API key middleware: {exc}")

_client: AstralMcpClient | None = None


def _get_client() -> AstralMcpClient:
    global _client
    if _client is None:
        _client = client_from_env()
    return _client


def _jsonify(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


@mcp.tool()
def list_workloads() -> str:
    """List available ASTRAL workloads (intune, entra)."""
    return _jsonify(_get_client().list_workloads())


@mcp.tool()
def list_categories(workload: str) -> str:
    """List policy categories for a workload.

    Args:
        workload: Either "intune" or "entra".
    """
    try:
        return _jsonify(_get_client().list_categories(workload))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def list_policies(workload: str, category: str) -> str:
    """List policies in a category.

    Args:
        workload: Either "intune" or "entra".
        category: A category name (e.g. "Compliance Policies", "Conditional Access").
    """
    try:
        return _jsonify(_get_client().list_policies(workload, category))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_policy(workload: str, category: str, name: str) -> str:
    """Retrieve the current JSON configuration of a specific policy.

    Args:
        workload: Either "intune" or "entra".
        category: A category name.
        name: Policy file name without the .json extension.
    """
    try:
        return _jsonify(_get_client().get_policy(workload, category, name))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_policy_history(workload: str, category: str, name: str, limit: int = 10) -> str:
    """Get Git commit history for a specific policy.

    Args:
        workload: Either "intune" or "entra".
        category: A category name.
        name: Policy file name without the .json extension.
        limit: Maximum number of commits to return (default 10).
    """
    try:
        return _jsonify(_get_client().get_policy_history(workload, category, name, limit))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def search_policies(workload: str, query: str) -> str:
    """Search policies by name across all categories in a workload.

    Args:
        workload: Either "intune" or "entra".
        query: Case-insensitive substring to match against policy names.
    """
    try:
        return _jsonify(_get_client().search_policies(workload, query))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_recent_drift(workload: str, hours: int = 24) -> str:
    """Get recent Git commits (drift) for a workload.

    Args:
        workload: Either "intune" or "entra".
        hours: Lookback window in hours (default 24).
    """
    try:
        return _jsonify(_get_client().get_recent_drift(workload, hours))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_assignment_report(workload: str = "intune") -> str:
    """Retrieve the latest assignment report Markdown for a workload.

    Args:
        workload: Either "intune" or "entra" (default "intune").
    """
    try:
        result = _get_client().get_assignment_report(workload)
        if not result:
            return "No assignment report found."
        return result
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_object_inventory(workload: str, category: str) -> str:
    """Retrieve the latest object inventory CSV for a category as JSON rows.

    Args:
        workload: Either "intune" or "entra".
        category: A category name (e.g. "Compliance Policies").
    """
    try:
        result = _get_client().get_object_inventory(workload, category)
        if not result:
            return "No inventory report found."
        return _jsonify(result)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.prompt()
def audit_briefing(workload: str = "intune") -> str:
    """Generate a natural-language audit briefing for recent drift."""
    return (
        f"You are an ASTRAL tenant configuration analyst. "
        f"Review the {workload} workload for changes in the last 7 days. "
        f"Use get_recent_drift(workload='{workload}', hours=168) to fetch commits, "
        f"then summarize what changed, who changed it, and highlight any risky or "
        f"unusual modifications (e.g., Conditional Access rule relaxations, new app "
        f"registrations with broad permissions, compliance policy downgrades)."
    )


@mcp.prompt()
def policy_deep_dive(workload: str, category: str, name: str) -> str:
    """Deep-dive into a single policy: current state + history + assignments."""
    return (
        f"You are an ASTRAL tenant configuration analyst. "
        f"Perform a deep-dive on the {workload} policy '{name}' in category '{category}'. "
        f"1) Fetch its current configuration with get_policy. "
        f"2) Fetch its change history with get_policy_history. "
        f"3) If workload is intune, check the assignment report for this policy. "
        f"Summarize what the policy does, when it last changed, who changed it, and "
        f"any potential misconfigurations or security concerns."
    )


# ---------------------------------------------------------------------------
# OAuth 2.0 authorization server metadata (RFC 8414)
# Enables Claude Desktop and other MCP clients to discover Entra ID auth.
# Required env vars: ENTRA_TENANT_ID, MCP_CLIENT_ID
# ---------------------------------------------------------------------------

_entra_tenant_id = os.environ.get("ENTRA_TENANT_ID", "").strip()
_mcp_client_id   = os.environ.get("MCP_CLIENT_ID", "").strip()


def _build_oauth_metadata() -> dict | None:
    if not _entra_tenant_id or not _mcp_client_id:
        return None
    base = f"https://login.microsoftonline.com/{_entra_tenant_id}"
    return {
        "issuer":                             f"{base}/v2.0",
        "authorization_endpoint":             f"{base}/oauth2/v2.0/authorize",
        "token_endpoint":                     f"{base}/oauth2/v2.0/token",
        "scopes_supported":                   [f"api://{_mcp_client_id}/user_impersonation", "openid", "profile", "offline_access"],
        "response_types_supported":           ["code"],
        "grant_types_supported":              ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported":   ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ASTRAL MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for SSE transport")
    parser.add_argument("--port", type=int, default=8080, help="Bind port for SSE transport")
    args = parser.parse_args()

    logger.info(
        "Starting ASTRAL MCP Server (transport=%s, host=%s, port=%s)",
        args.transport,
        args.host,
        args.port,
    )

    if args.transport == "sse":
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route

        oauth_meta = _build_oauth_metadata()

        async def oauth_metadata_handler(request):
            return JSONResponse(oauth_meta)

        async def health(request):
            return JSONResponse({"status": "ok"})

        sse_app = mcp.sse_app()

        routes = []
        if oauth_meta:
            routes.append(Route("/.well-known/oauth-authorization-server", oauth_metadata_handler))
            logger.info("OAuth discovery endpoint enabled (tenant=%s, client=%s)", _entra_tenant_id, _mcp_client_id)
        else:
            logger.info("OAuth discovery endpoint disabled — set ENTRA_TENANT_ID and MCP_CLIENT_ID to enable.")
        routes.append(Route("/health", health))
        routes.append(Mount("/", app=sse_app))

        app = Starlette(routes=routes)
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
