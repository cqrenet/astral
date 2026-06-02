# ASTRAL MCP Server — Azure Container Apps image
# Build context should be the repository root so we can COPY scripts/

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY infra/mcp-server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared scripts and MCP server entry point
COPY scripts/common.py ./scripts/
COPY scripts/astral_mcp_tools.py ./scripts/
COPY infra/mcp-server/mcp_server.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Default to SSE transport for cloud hosting
CMD ["python", "mcp_server.py", "--transport", "sse", "--host", "0.0.0.0", "--port", "8080"]
