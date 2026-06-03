#!/usr/bin/env python3
"""Stdio-to-SSE proxy for ASTRAL MCP Server (Entra ID auth).

Configure Claude Desktop with:

    {
      "mcpServers": {
        "astral": {
          "command": "python3",
          "args": [
            "/Users/avedelphina/Local/CQRE-Product/ASTRAL-CQRE/infra/mcp-server/claude_mcp_proxy.py"
          ]
        }
      }
    }

On first run the script will print a device-code URL to stderr.
Open the URL in a browser, sign in, and enter the code.
The token is cached in ~/.astral-mcp-token-cache.json so subsequent
starts are fully silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx
import msal
from httpx_sse import aconnect_sse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_TENANT_ID = "0ec9f34c-17c8-4541-b084-7d64ecdcc997"
_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Azure CLI public client
_SCOPE = "api://db877bb2-ed35-44ec-8c1a-faf9667fd29f/user_impersonation"
_SSE_URL = "https://ca-astral-mcp.thankfulpebble-08beaacd.swedencentral.azurecontainerapps.io/sse"
_TOKEN_CACHE = os.path.expanduser("~/.astral-mcp-token-cache.json")


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------
def _get_token_from_az() -> str | None:
    """Try to reuse the existing Azure CLI authenticated session."""
    import shutil
    import subprocess

    az = shutil.which("az")
    if az is None:
        # Common macOS Homebrew fallback paths
        for p in ("/opt/homebrew/bin/az", "/usr/local/bin/az", "/usr/bin/az"):
            if os.path.isfile(p):
                az = p
                break
    if az is None:
        return None

    try:
        result = subprocess.run(
            [az, "account", "get-access-token", "--resource", "api://db877bb2-ed35-44ec-8c1a-faf9667fd29f"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)["accessToken"]
    except Exception:
        pass
    return None


def _get_token() -> str:
    # Fast path: reuse Azure CLI session
    token = _get_token_from_az()
    if token:
        return token

    # Fallback: MSAL device code flow
    cache = msal.SerializableTokenCache()
    if os.path.exists(_TOKEN_CACHE):
        cache.deserialize(open(_TOKEN_CACHE).read())

    app = msal.PublicClientApplication(
        _CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{_TENANT_ID}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent([_SCOPE], account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    flow = app.initiate_device_flow(scopes=[_SCOPE])
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to initiate device flow: {flow.get('error_description')}")

    print("\n" + "=" * 60, file=sys.stderr)
    print("ASTRAL MCP Server — authentication required", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(flow["message"], file=sys.stderr)
    print("=" * 60 + "\n", file=sys.stderr)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            f"Authentication failed: {result.get('error_description', result)}"
        )

    _save_cache(cache)
    return result["access_token"]


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    with open(_TOKEN_CACHE, "w") as f:
        f.write(cache.serialize())


# ---------------------------------------------------------------------------
# Stdio ↔ SSE bridge
# ---------------------------------------------------------------------------
async def _bridge() -> None:
    token = _get_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=None, headers=headers) as client:
        async with aconnect_sse(client, "GET", _SSE_URL, headers=headers) as event_source:
            # Read the endpoint event
            endpoint_url: str | None = None
            async for sse in event_source.aiter_sse():
                if sse.event == "endpoint":
                    endpoint_url = sse.data
                    logger.info("SSE endpoint acquired: %s", endpoint_url)
                    break

            if endpoint_url is None:
                raise RuntimeError("No endpoint event received from SSE stream")

            # Resolve relative endpoint against base URL
            if endpoint_url.startswith("/"):
                endpoint_url = _SSE_URL.rsplit("/", 1)[0] + endpoint_url

            async def stdin_reader():
                """Read JSON-RPC lines from stdin and POST to message endpoint."""
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)

                try:
                    while True:
                        line = await reader.readline()
                        if not line:
                            break
                        line_str = line.decode().strip()
                        if not line_str:
                            continue

                        logger.debug("→ %s", line_str[:200])
                        resp = await client.post(
                            endpoint_url,
                            headers={"Content-Type": "application/json"},
                            content=line_str.encode(),
                        )
                        # HTTP 202 Accepted is expected for fire-and-forget POSTs
                        if resp.status_code not in (202, 200):
                            logger.warning(
                                "POST returned %s: %s", resp.status_code, resp.text[:200]
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("stdin reader error")
                    raise

            async def sse_writer():
                """Read SSE message events and write JSON-RPC to stdout."""
                try:
                    async for sse in event_source.aiter_sse():
                        if sse.event == "message":
                            logger.debug("← %s", sse.data[:200])
                            sys.stdout.write(sse.data + "\n")
                            sys.stdout.flush()
                        elif sse.event == "endpoint":
                            # ignore duplicate endpoint events
                            pass
                        else:
                            logger.debug("SSE event: %s = %s", sse.event, sse.data[:200])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("sse writer error")
                    raise

            async with asyncio.TaskGroup() as tg:
                tg.create_task(stdin_reader())
                tg.create_task(sse_writer())


def main() -> int:
    try:
        asyncio.run(_bridge())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as exc:
        logger.exception("Proxy failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
