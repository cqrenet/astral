#!/usr/bin/env python3
"""ASTRAL MCP Server — exposes tenant state and drift history to AI assistants.

Supports stdio transport (local development) and SSE transport (Azure hosting).

Usage:
    python mcp_server.py --transport stdio
    python mcp_server.py --transport sse --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
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
# Auth configuration
# ---------------------------------------------------------------------------
_api_key         = os.environ.get("MCP_API_KEY", "").strip()
_entra_tenant_id = os.environ.get("ENTRA_TENANT_ID", "").strip()
_mcp_client_id   = os.environ.get("MCP_CLIENT_ID", "").strip()

# ---------------------------------------------------------------------------
# Entra JWT validation (JWKS-based, in-process)
#
# Validates Bearer tokens issued by Entra ID — the same tokens Claude Desktop
# obtains via the OAuth PKCE flow using the discovery endpoint.
#
# Doing this in-process (rather than delegating to ACA EasyAuth) means:
#  - The /.well-known/oauth-authorization-server discovery endpoint is reachable
#    by unauthenticated clients (ACA is set to AllowAnonymous).
#  - Auth behaviour is identical whether deployed to ACA or run locally.
# ---------------------------------------------------------------------------
_jwks_cache: dict = {"keys": [], "exp": 0.0}
_jwks_lock = threading.Lock()


def _fetch_jwks() -> list:
    """Fetch JWKS from Microsoft — blocking, run via asyncio.to_thread."""
    import json as _json
    import urllib.request
    oidc_url = f"https://login.microsoftonline.com/{_entra_tenant_id}/v2.0/.well-known/openid-configuration"
    with urllib.request.urlopen(oidc_url, timeout=10) as resp:  # noqa: S310
        oidc = _json.loads(resp.read())
    with urllib.request.urlopen(oidc["jwks_uri"], timeout=10) as resp:  # noqa: S310
        return _json.loads(resp.read())["keys"]


def _get_jwks() -> list:
    now = time.monotonic()
    with _jwks_lock:
        if _jwks_cache["keys"] and _jwks_cache["exp"] > now:
            return _jwks_cache["keys"]
        keys = _fetch_jwks()
        _jwks_cache["keys"] = keys
        _jwks_cache["exp"] = now + 3600  # cache 1 hour
        return keys


def _validate_entra_token(token: str) -> bool:
    """Return True if token is a valid Entra JWT for this tenant and client. Blocking."""
    try:
        import json as _json
        from jwt import decode, get_unverified_header
        from jwt.algorithms import RSAAlgorithm

        header = get_unverified_header(token)
        kid = header.get("kid")
        keys = _get_jwks()
        key_dict = next((k for k in keys if k.get("kid") == kid), None)
        if not key_dict:
            logger.warning("Entra JWT: signing key not found (kid=%s)", kid)
            return False
        pub_key = RSAAlgorithm.from_jwk(_json.dumps(key_dict))
        bare = _mcp_client_id.removeprefix("api://")
        claims = decode(token, pub_key, algorithms=["RS256"], audience=[bare, f"api://{bare}"])
        tid = claims.get("tid", "")
        iss = claims.get("iss", "")
        if _entra_tenant_id and tid != _entra_tenant_id:
            logger.warning("Entra JWT: wrong tenant (tid=%s)", tid)
            return False
        if _entra_tenant_id and _entra_tenant_id not in iss:
            logger.warning("Entra JWT: wrong issuer (iss=%s)", iss)
            return False
        return True
    except Exception as exc:
        logger.warning("Entra JWT validation failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# MCP auth middleware — pure ASGI (never BaseHTTPMiddleware)
#
# Accepts requests that satisfy at least one of:
#   API key  — Bearer <MCP_API_KEY> or x-api-key: <MCP_API_KEY>
#   Entra JWT — valid token for ENTRA_TENANT_ID / MCP_CLIENT_ID
#
# Certain paths are exempt (health probe, OAuth discovery).
# If neither MCP_API_KEY nor ENTRA_TENANT_ID+MCP_CLIENT_ID are configured,
# the server runs open (useful for local stdio / dev).
# ---------------------------------------------------------------------------
class _McpAuthMiddleware:
    _EXEMPT_PATHS = frozenset({"/health", "/", "/.well-known/oauth-authorization-server"})

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") not in self._EXEMPT_PATHS:
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            key_header = headers.get(b"x-api-key", b"").decode()
            token = key_header or (auth[7:] if auth.lower().startswith("bearer ") else "")

            # Fast path: API key
            if _api_key and token == _api_key:
                await self.app(scope, receive, send)
                return

            # Entra JWT path (blocking JWKS fetch offloaded to thread pool)
            if _entra_tenant_id and _mcp_client_id and token:
                if await asyncio.to_thread(_validate_entra_token, token):
                    await self.app(scope, receive, send)
                    return

            # No auth configured — open access
            if not _api_key and not (_entra_tenant_id and _mcp_client_id):
                await self.app(scope, receive, send)
                return

            body = b"Unauthorized"
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"text/plain; charset=utf-8"],
                    [b"content-length", str(len(body)).encode()],
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)

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

        sse_app = _McpAuthMiddleware(mcp.sse_app())

        if _api_key and (_entra_tenant_id and _mcp_client_id):
            logger.info("Auth: API key + Entra JWT (tenant=%s, client=%s)", _entra_tenant_id, _mcp_client_id)
        elif _api_key:
            logger.info("Auth: API key only.")
        elif _entra_tenant_id and _mcp_client_id:
            logger.info("Auth: Entra JWT only (tenant=%s, client=%s)", _entra_tenant_id, _mcp_client_id)
        else:
            logger.warning("Auth: DISABLED — set MCP_API_KEY or ENTRA_TENANT_ID+MCP_CLIENT_ID.")

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
