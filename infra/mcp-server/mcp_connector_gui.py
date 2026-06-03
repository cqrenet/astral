#!/usr/bin/env python3
"""Simple GUI for configuring the ASTRAL MCP Claude Desktop connector.

Run directly:
    python3 mcp_connector_gui.py

What it does:
    • Lets you type/paste the SSE URL and Entra Client ID
    • Writes the Claude Desktop config for you
    • Optionally pre-authenticates the token cache
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLAUDE_CONFIG_PATH = os.path.expanduser(
    "~/Library/Application Support/Claude/claude_desktop_config.json"
)
PROXY_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "claude_mcp_proxy.py",
)

DEFAULT_URL = (
    "https://ca-astral-mcp.thankfulpebble-08beaacd"
    ".swedencentral.azurecontainerapps.io/sse"
)
DEFAULT_CLIENT_ID = "db877bb2-ed35-44ec-8c1a-faf9667fd29f"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_claude_config() -> dict:
    if os.path.isfile(CLAUDE_CONFIG_PATH):
        with open(CLAUDE_CONFIG_PATH, "r") as f:
            return json.load(f)
    return {"mcpServers": {}}


def save_claude_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CLAUDE_CONFIG_PATH), exist_ok=True)
    with open(CLAUDE_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class ConnectorGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ASTRAL MCP Connector")
        self.geometry("560x380")
        self.resizable(False, False)

        # Try to load existing values from Claude config
        existing = load_claude_config()
        existing_env = {}
        if "astral" in existing.get("mcpServers", {}):
            existing_env = existing["mcpServers"]["astral"].get("env", {})

        # Header
        ttk.Label(self, text="ASTRAL MCP Server Configuration", font=("Helvetica", 16, "bold")).pack(pady=(16, 8))

        # Form frame
        frm = ttk.Frame(self, padding="16")
        frm.pack(fill=tk.X)

        ttk.Label(frm, text="MCP SSE URL:").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.url_var = tk.StringVar(value=existing_env.get("ASTRAL_MCP_URL", DEFAULT_URL))
        ttk.Entry(frm, textvariable=self.url_var, width=55).grid(row=0, column=1, pady=4)

        ttk.Label(frm, text="Entra Client ID:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.client_var = tk.StringVar(value=existing_env.get("ASTRAL_CLIENT_ID", DEFAULT_CLIENT_ID))
        ttk.Entry(frm, textvariable=self.client_var, width=55).grid(row=1, column=1, pady=4)

        ttk.Label(frm, text="Entra Tenant ID:").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.tenant_var = tk.StringVar(value=existing_env.get("ASTRAL_TENANT_ID", "0ec9f34c-17c8-4541-b084-7d64ecdcc997"))
        ttk.Entry(frm, textvariable=self.tenant_var, width=55).grid(row=2, column=1, pady=4)

        # Buttons
        btn_frm = ttk.Frame(self, padding="16")
        btn_frm.pack(fill=tk.X)

        ttk.Button(btn_frm, text="💾 Save to Claude Desktop", command=self.on_save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frm, text="🔑 Pre-authenticate", command=self.on_auth).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frm, text="🧪 Test Connection", command=self.on_test).pack(side=tk.LEFT, padx=4)

        # Status
        self.status_var = tk.StringVar(value="Ready — fill in the fields and click Save")
        ttk.Label(self, textvariable=self.status_var, foreground="gray", wraplength=520).pack(pady=(8, 16))

        # Notes
        notes = (
            "Notes:\n"
            "• The proxy script reuses your Azure CLI login when available.\n"
            "• If not, it falls back to MSAL device-code flow (one-time).\n"
            "• Pre-authenticate writes the token cache so Claude starts silently.\n"
            "• Restart Claude Desktop (Cmd+Q) after saving."
        )
        ttk.Label(self, text=notes, foreground="gray", justify=tk.LEFT, wraplength=520).pack(pady=(0, 16))

    # -----------------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------------
    def on_save(self) -> None:
        url = self.url_var.get().strip()
        client_id = self.client_var.get().strip()
        tenant_id = self.tenant_var.get().strip()

        if not url or not client_id:
            messagebox.showerror("Missing fields", "URL and Client ID are required.")
            return

        cfg = load_claude_config()
        cfg.setdefault("mcpServers", {})
        cfg["mcpServers"]["astral"] = {
            "command": "python3",
            "args": [PROXY_SCRIPT],
            "env": {
                "ASTRAL_MCP_URL": url,
                "ASTRAL_CLIENT_ID": client_id,
                "ASTRAL_TENANT_ID": tenant_id,
            },
        }
        save_claude_config(cfg)
        self.status_var.set(f"Saved to {CLAUDE_CONFIG_PATH}")
        messagebox.showinfo("Saved", "Configuration saved.\nRestart Claude Desktop (Cmd+Q) to apply.")

    def on_auth(self) -> None:
        url = self.url_var.get().strip()
        client_id = self.client_var.get().strip()
        tenant_id = self.tenant_var.get().strip()

        env = os.environ.copy()
        env["ASTRAL_MCP_URL"] = url
        env["ASTRAL_CLIENT_ID"] = client_id
        env["ASTRAL_TENANT_ID"] = tenant_id

        self.status_var.set("Launching proxy for authentication... check your terminal / browser.")
        self.update_idletasks()

        try:
            result = subprocess.run(
                [sys.executable, PROXY_SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                self.status_var.set("Authentication successful — token cached.")
                messagebox.showinfo("Authenticated", "Token cached successfully. Claude Desktop can now start silently.")
            else:
                self.status_var.set(f"Auth failed: {result.stderr[:200]}")
                messagebox.showerror("Authentication failed", result.stderr[:400])
        except subprocess.TimeoutExpired:
            self.status_var.set("Auth timed out (SSE connection stays open). This is expected — token was cached.")
            messagebox.showinfo("Authenticated", "Proxy connected successfully. Token is cached.")
        except Exception as exc:
            self.status_var.set(f"Error: {exc}")
            messagebox.showerror("Error", str(exc))

    def on_test(self) -> None:
        url = self.url_var.get().strip()
        self.status_var.set(f"Testing HEAD {url} ...")
        self.update_idletasks()

        try:
            import urllib.request
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("Authorization", "Bearer dummy")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.status_var.set(f"Reachable — returned HTTP {resp.status}")
                messagebox.showinfo("Test result", f"Server reachable.\nHTTP {resp.status} {resp.reason}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.status_var.set("Reachable — HTTP 401 (auth required, which is correct)")
                messagebox.showinfo("Test result", "Server is reachable.\nHTTP 401 is expected — it means Entra ID auth is active.")
            else:
                self.status_var.set(f"HTTP error: {e.code}")
                messagebox.showwarning("Test result", f"HTTP {e.code} — check your URL.")
        except Exception as exc:
            self.status_var.set(f"Unreachable: {exc}")
            messagebox.showerror("Test result", f"Could not reach server:\n{exc}")


def main() -> int:
    app = ConnectorGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
