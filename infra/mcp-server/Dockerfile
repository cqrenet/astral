# ASTRAL MCP Server — Azure Container Apps image
# Build context should be the repository root so we can COPY scripts/

FROM python:3.11-slim

WORKDIR /app

# git: ASTRAL_REPO_URL mode keeps a clone of the tenant-state repository (any Git forge)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY infra/mcp-server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared scripts and MCP server entry point
COPY scripts/common.py ./scripts/
COPY scripts/astral_mcp_tools.py ./scripts/
COPY infra/mcp-server/mcp_server.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Run unprivileged; the server only reads files and talks HTTPS.
USER nobody

# Default to SSE transport for cloud hosting
CMD ["python", "mcp_server.py", "--transport", "sse", "--host", "0.0.0.0", "--port", "8080"]
