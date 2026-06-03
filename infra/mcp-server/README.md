# ASTRAL MCP Server

Azure-hosted Model Context Protocol (MCP) server for ASTRAL. Exposes tenant state, drift history, and configuration data to AI assistants such as Claude Desktop, Cursor, GitHub Copilot, and custom MCP clients.

## Architecture

```
┌─────────────┐      HTTPS/SSE      ┌──────────────────────┐
│ MCP Client  │ ◄─────────────────► │ Azure Container Apps │
│ (Claude,    │   JSON-RPC 2.0      │  ASTRAL MCP Server   │
│  Cursor,    │                     │  (Python / FastMCP)  │
│  Copilot)   │                     └──────────┬───────────┘
└─────────────┘                                │
                                               │ ADO REST API
                                               ▼
                                      ┌──────────────────────┐
                                      │ Azure DevOps Git     │
                                      │ (tenant-state/)      │
                                      └──────────────────────┘
```

## Capabilities

### Tools

| Tool | Description |
|------|-------------|
| `list_workloads()` | List workloads: `intune`, `entra` |
| `list_categories(workload)` | List policy categories for a workload |
| `list_policies(workload, category)` | List policies in a category (recursively) |
| `get_policy(workload, category, name)` | Retrieve current JSON configuration of a policy |
| `get_policy_history(workload, category, name, limit)` | Git commit history for a policy |
| `search_policies(workload, query)` | Search policies by name across categories |
| `get_recent_drift(workload, hours)` | Recent Git commits (drift) for a workload |
| `get_assignment_report(workload)` | Latest assignment report Markdown |
| `get_object_inventory(workload, category)` | Object inventory CSV as JSON rows |

### Prompts

| Prompt | Description |
|--------|-------------|
| `audit_briefing(workload)` | Template for generating a 7-day drift audit summary |
| `policy_deep_dive(workload, category, name)` | Template for deep-diving into a single policy |

## Configuration

The server reads configuration from environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ASTRAL_REPO_ROOT` | No | — | Local Git repo path (overrides ADO) |
| `ADO_ORGANIZATION` | Yes* | — | Azure DevOps organization |
| `ADO_PROJECT` | Yes* | — | Azure DevOps project |
| `ADO_REPO_NAME` | No | — | Repository name (auto-detected if omitted) |
| `ADO_BRANCH` | No | `main` | Git branch to read from |
| `ADO_TOKEN` | Yes* | — | Azure DevOps PAT (Build read scope) |

\* Required when `ASTRAL_REPO_ROOT` is not set.

## Deployment (Standard)

The MCP server is deployed automatically by the ASTRAL provisioning script. During `deploy/provision-change-probe.ps1`, you are prompted whether to deploy the MCP server to Azure Container Apps.

### Automated deployment via provisioning script

```powershell
# Deploy everything including MCP server (interactive prompt)
.\deploy\provision-change-probe.ps1

# Explicitly deploy MCP server (skip interactive prompt)
.\deploy\provision-change-probe.ps1 -DeployMcpServer

# Skip MCP server deployment entirely
.\deploy\provision-change-probe.ps1 -SkipMcpServer

# Deploy ONLY the MCP server (change probe already exists)
.\deploy\provision-change-probe.ps1 -DeployMcpOnly
```

The script provisions:
- Azure Container Registry (Basic SKU)
- Container Apps Environment
- Container App with the MCP server image
- Environment variables for Azure DevOps connectivity
- Optional Microsoft Entra ID authentication

### Manual Azure deployment

If you prefer to deploy the MCP server separately:

```bash
# Build image
az acr build \
  --registry <acr-name> \
  --image astral-mcp:latest \
  --file infra/mcp-server/Dockerfile .

# Deploy to Container Apps
az containerapp create \
  --name astral-mcp \
  --resource-group rg-astral \
  --image <acr>.azurecr.io/astral-mcp:latest \
  --target-port 8080 \
  --ingress external \
  --environment cae-astral \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars ADO_ORGANIZATION=<org> ADO_PROJECT=<project> ADO_TOKEN=<pat>
```

## Local Development

### Prerequisites

- Python 3.11+
- `mcp` Python SDK

### Install dependencies

```bash
cd infra/mcp-server
pip install -r requirements.txt
```

### Run with stdio transport (for Claude Desktop / local testing)

```bash
export ASTRAL_REPO_ROOT=/path/to/astral-clone
python3 mcp_server.py --transport stdio
```

### Run with SSE transport (for remote clients)

```bash
export ASTRAL_REPO_ROOT=/path/to/astral-clone
python3 mcp_server.py --transport sse --host 127.0.0.1 --port 8080
```

### Configure Claude Desktop

Claude Desktop config file: `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.

**Option A — stdio (local repo, no auth needed)**

```json
{
  "mcpServers": {
    "astral": {
      "command": "python3",
      "args": [
        "/path/to/astral/infra/mcp-server/mcp_server.py",
        "--transport", "stdio"
      ],
      "env": {
        "ASTRAL_REPO_ROOT": "/path/to/astral-clone"
      }
    }
  }
}
```

**Option B — SSE with API key (local or remote server)**

```bash
# Start the server
export ASTRAL_REPO_ROOT=/path/to/astral-clone
export MCP_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
python3 mcp_server.py --transport sse --host 127.0.0.1 --port 8080
```

```json
{
  "mcpServers": {
    "astral": {
      "url": "http://127.0.0.1:8080/sse",
      "headers": {
        "Authorization": "Bearer <your-MCP_API_KEY-value>"
      }
    }
  }
}
```

## Azure Deployment

### Azure Container Apps

Build and push the container image:

```bash
cd /path/to/astral

az acr build \
  --registry <acr-name> \
  --image astral-mcp:latest \
  --file infra/mcp-server/Dockerfile .
```

Deploy to Container Apps with built-in Entra ID authentication:

```bash
az containerapp create \
  --name astral-mcp \
  --resource-group rg-astral \
  --image <acr-name>.azurecr.io/astral-mcp:latest \
  --target-port 8080 \
  --ingress external \
  --environment cae-astral \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
    ADO_ORGANIZATION=<org> \
    ADO_PROJECT=<project> \
    ADO_REPO_NAME=<repo> \
    ADO_BRANCH=main \
    ADO_TOKEN=<pat>
```

Enable Microsoft Entra ID authentication:

```bash
az containerapp auth update \
  --name astral-mcp \
  --resource-group rg-astral \
  --enabled true \
  --unauthenticated-client-action RedirectToLoginPage \
  --identity-provider azureactivedirectory
```

### Authentication

The MCP server supports two authentication modes, which can be active simultaneously.

**1. API key — recommended for Claude Desktop and AURORA**

Set `MCP_API_KEY` to a random secret (the provisioning script does this automatically; you can also set it manually). Pass the key as a Bearer token or via `x-api-key`:

```json
{
  "mcpServers": {
    "astral": {
      "url": "https://astral-mcp.<region>.azurecontainerapps.io/sse",
      "headers": {
        "Authorization": "Bearer <your-mcp-api-key>"
      }
    }
  }
}
```

The `x-api-key: <key>` header is accepted as an alternative.

To generate a key manually:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**2. Microsoft Entra ID (enterprise / human users)**

The server can publish an OAuth 2.0 discovery document (`/.well-known/oauth-authorization-server`, RFC 8414) so that MCP clients discover the Entra auth endpoints automatically. Set `ENTRA_TENANT_ID` and `MCP_CLIENT_ID` to activate it. Actual token enforcement is handled by Azure Container Apps built-in authentication (`az containerapp auth update ...`).

With discovery active, Claude Desktop initiates the OAuth flow on its own — no `headers` entry needed in the config:

```json
{
  "mcpServers": {
    "astral": {
      "url": "https://astral-mcp.<region>.azurecontainerapps.io/sse"
    }
  }
}
```

Both modes can be active at the same time — service clients use the key, human users go through Entra.

## Security Notes

- **Never commit `ADO_TOKEN`** to Git. Use Azure Key Vault or Container Apps secrets.
- Enable **Entra ID authentication** on the Container Apps ingress for production.
- Consider placing the MCP server behind **Azure API Management** for rate limiting and additional policy governance.
- The ADO token only needs **Build (read)** scope for reading repository contents.

## Data Flow

1. MCP client sends a natural-language query (e.g., *"What changed in Intune yesterday?"*)
2. AI assistant maps the intent to MCP tools:
   - `get_recent_drift(workload="intune", hours=24)`
3. MCP server calls Azure DevOps REST API to read `tenant-state/intune` commit history
4. Results are returned to the AI assistant as structured JSON
5. AI assistant synthesizes a human-readable answer
