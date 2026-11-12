"""Start the real MCP server over stdio with the installed `mcp` package and call a tool.

The unit tests in test_mcp_server.py replace the mcp package with a stub, so they cannot notice an incompatible
major release of the library. This test runs the server in a child process against the real package and is skipped
when `mcp` is not installed.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CLIENT = r"""
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = dict(os.environ, ASTRAL_REPO_ROOT=sys.argv[1])
    params = StdioServerParameters(command=sys.executable, args=["infra/mcp-server/mcp_server.py", "--transport", "stdio"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("list_report_definitions", {})
            print("TOOLS", len(tools.tools), "ERROR", bool(result.is_error))

asyncio.run(main())
"""


def _mcp_installed() -> bool:
    return subprocess.run([sys.executable, "-c", "import mcp.server.mcpserver"], capture_output=True).returncode == 0


@unittest.skipUnless(_mcp_installed(), "mcp 2.x is not installed")
class McpServerSmokeTests(unittest.TestCase):
    def test_server_lists_tools_and_answers_a_call_over_stdio(self) -> None:
        proc = subprocess.run([sys.executable, "-c", CLIENT, str(ROOT)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        line = next(l for l in proc.stdout.splitlines() if l.startswith("TOOLS"))
        _, count, _, error = line.split()
        self.assertGreaterEqual(int(count), 15)
        self.assertEqual(error, "False")


if __name__ == "__main__":
    unittest.main()
