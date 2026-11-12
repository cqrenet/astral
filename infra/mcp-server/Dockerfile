# ASTRAL MCP Server — Azure Container Apps image
# Build context should be the repository root so we can COPY scripts/

# Pinned by digest; Dependabot proposes updates. Tag: python:3.14-slim
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

# git: ASTRAL_REPO_URL mode keeps a clone of the tenant-state repository (any Git forge)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY infra/mcp-server/requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

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
