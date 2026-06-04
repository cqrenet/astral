# Agent Guidance: Intune / Entra Drift Backup

This repository tracks Git-based snapshots of Microsoft Intune and Entra ID configuration, generates review reports, and drives a rolling pull-request workflow for post-change review and remediation.

## Project Overview

The implementation is centered on three Azure DevOps pipelines and an optional MCP server:

- `azure-pipelines.yml`: daily full backup/export pipeline with rolling PR management (previously hourly; now driven primarily by event-driven change probe).
- `azure-pipelines-review-sync.yml`: 20-minute reviewer-decision sync and post-merge remediation queue.
- `azure-pipelines-restore.yml`: manual or auto-queued restore pipeline for approved baseline rollback.

Workflow at a high level:
1. Export Intune and Entra configuration into `tenant-state/`.
2. Generate Markdown/CSV reports in `tenant-state/reports/`.
3. Filter known non-actionable drift noise before commit.
4. Commit workload drift to `drift/intune` and `drift/entra`.
5. Create or update one rolling PR per workload into `main`.
6. Refresh the PR description with deterministic change/risk summary and optional Azure OpenAI narrative.
7. Apply reviewer `/reject` or `/accept` decisions and queue restore when needed.
8. Query tenant state and drift history via the ASTRAL MCP Server (optional premium feature).

## Technology Stack

- **Python 3**: primary language for all automation scripts.
- **Azure DevOps Pipelines**: YAML-based CI/CD (`azure-pipelines.yml`, `azure-pipelines-review-sync.yml`, `azure-pipelines-restore.yml`).
- **PowerShell & Bash**: inline pipeline steps for Git operations, token retrieval, and conditional logic.
- **IntuneCD** (Python package, pinned to `2.6.0`): exports Intune configuration and restores baseline state.
- **Microsoft Graph API**: reads/writes tenant configuration and resolves references.
- **Node.js / md-to-pdf** (v5.2.5): generates HTML and PDF documentation artifacts from Markdown on full runs.
- **Azure OpenAI**: optional PR narrative generation.
- **MCP (Model Context Protocol)**: optional AI assistant integration for natural-language tenant queries.

## Repository Layout

```
.
├── azure-pipelines.yml              # Main backup pipeline (daily snapshot + event-driven trigger)
├── azure-pipelines-review-sync.yml  # 20-minute review sync
├── azure-pipelines-restore.yml      # Baseline restore pipeline
├── scripts/                         # Python automation helpers
├── tests/                           # unittest coverage for scripts
├── tenant-state/                    # Committed JSON exports and reports
│   ├── intune/
│   ├── entra/
│   └── reports/
├── infra/                           # Azure infrastructure
│   ├── change-probe/                # Event-driven backup trigger (Function App)
│   └── mcp-server/                  # Model Context Protocol server (Container Apps)
├── deploy/                          # Infrastructure provisioning scripts
├── docs/                            # Security review docs and roadmap
├── md2pdf/                          # HTML/PDF styling and configs
├── prod-as-built.md                 # Generated as-built source
└── README.md                        # Operational overview for humans
```

### Key Scripts

- `export_entra_baseline.py`: Graph API export for Entra objects (Named Locations, Authentication Strengths, Conditional Access, App Registrations, Enterprise Applications).
- `commit_entra_drift.py`: commits Entra drift with author attribution from audit logs.
- `resolve_ca_references.py`: resolves Conditional Access GUID references to human-readable names.
- `filter_entra_enrichment_noise.py`: reverts JSON churn caused by best-effort Graph enrichment (owners, app roles).
- `filter_intune_partial_settings_noise.py`: reverts partial Settings Catalog exports.
- `generate_assignment_report.py`: produces Markdown and CSV assignment inventories.
- `generate_app_inventory_report.py`: produces Entra apps inventory CSV.
- `generate_object_inventory_reports.py`: produces per-category object inventory CSVs.
- `validate_backup_outputs.py`: asserts required files exist after export.
- `ensure_rolling_pr.py`: creates or updates one rolling drift PR per workload.
- `update_pr_review_summary.py`: refreshes PR descriptions with change counts, risk assessment, and optional AI narrative.
- `apply_reviewer_rejections.py`: processes `/reject` and `/accept` reviewer thread commands.
- `queue_post_merge_restore.py`: queues restore pipeline after merged PRs that contained `/reject` decisions.
- `probe_tenant_changes.py`: polls Intune/Entra audit logs via Graph, implements debouncer (idle → armed → cooldown), and decides whether to trigger a backup.
- `trigger_backup_pipeline.py`: thin ADO REST API wrapper to queue the backup pipeline on demand.
- `astral_mcp_tools.py`: core query layer for the MCP server; reads tenant state from Azure DevOps Git or a local clone.

## Code Style and Conventions

- Every Python file starts with `#!/usr/bin/env python3` and `from __future__ import annotations`.
- Type hints are used throughout (`typing.Any`, `argparse.Namespace`, etc.).
- Internal helper functions are prefixed with `_`.
- Common environment parsing helpers appear in multiple scripts:
  - `_env_text(name, default="")` – reads and sanitizes env vars, treating unresolved Azure DevOps macros `$(...)` as empty.
  - `_env_bool(name, default=False)` – interprets `1`, `true`, `yes`, `on` as boolean true.
- Arguments use `argparse` with typed flags; pipeline variables are passed as env vars or CLI args.
- JSON is written with `indent=4` or `indent=5` and `ensure_ascii=False`.
- HTTP calls to Graph or Azure DevOps REST APIs use `urllib.request` (no external HTTP library).

## Testing

Tests are written with the Python standard library `unittest` framework. There is **no pytest configuration** (`pyproject.toml`, `setup.py`, or `pytest.ini` are absent). Modules are loaded dynamically in tests using `importlib.util.spec_from_file_location` so that scripts in `scripts/` do not need to be on `PYTHONPATH`.

### Run Tests

```bash
python3 -m unittest discover -s tests -v
```

### Test Coverage Areas

- `test_ensure_rolling_pr.py`: rolling PR creation, draft publishing, merge strategy logic.
- `test_export_entra_baseline.py`: Entra export parsing, concurrent export behavior, error handling.
- `test_filter_entra_enrichment_noise.py`: enrichment-only churn detection and reversion.
- `test_filter_intune_partial_settings_noise.py`: partial Settings Catalog export filtering.
- `test_queue_post_merge_restore.py`: post-merge restore queueing logic.
- `test_update_pr_review_summary.py`: semantic diffing, AI thread management, PR description upserts.
- `test_validate_backup_outputs.py`: validation rules for Intune and Entra outputs.
- `test_astral_mcp_tools.py`: MCP client behavior for local and remote data sources.

## Build and Runtime Architecture

There is no traditional build step for the Python code. The pipelines install runtime dependencies on each run:

```bash
pip3 install "IntuneCD==2.6.0"
```

For local development, only a Python 3 interpreter is required; scripts use the standard library except for the optional IntuneCD package.

### MCP Server (Standard AI Assistant Integration)

An Azure Container Apps-hosted MCP server exposes ASTRAL tenant state to AI assistants. This is a **standard** feature deployed alongside the change probe:

- **Location**: `infra/mcp-server/`
- **Runtime**: Python FastMCP with stdio or SSE transport.
- **Data source**: Azure DevOps REST API (reads `tenant-state/` from Git) or local filesystem clone.
- **Tools**: policy lookup, search, drift history, assignment reports, object inventory.
- **Prompts**: audit briefing and policy deep-dive templates.
- **Auth**: Microsoft Entra ID via Container Apps built-in authentication (recommended for production).
- **Deployment**: Integrated into `deploy/provision-change-probe.ps1` (optional, prompted interactively or controlled via `-DeployMcpServer` / `-SkipMcpServer` / `-DeployMcpOnly` flags). Container image built from `infra/mcp-server/Dockerfile` via Azure Container Registry Tasks.

### Change Probe (Event-Driven Backup Trigger)

Because Microsoft Graph change notifications and delta queries do not support Intune device management or Conditional Access resources, an audit-log polling architecture is used instead:

- **Azure Function App** (`infra/change-probe/`):
  - `probe_timer`: 5-minute timer trigger. Loads debouncer state from Azure Table Storage, runs `probe_tenant_changes.py`, writes state back, and emits a queue message when the debouncer triggers.
  - `queue_consumer`: queue trigger. Dequeues messages and calls `trigger_backup_pipeline.py` to queue the ADO backup pipeline.
- **Debouncer**: 15-minute quiet window (idle → armed) + 30-minute cooldown. Prevents backup storms during bulk changes.
- **State**: stored in Azure Table Storage (`ProbeState` table).
- **Provisioning**: `deploy/provision-change-probe.ps1` creates the Entra app, grants admin consent, provisions Resource Group / Storage Account / Function App, and configures app settings.

### Pipeline Jobs

- **Intune backup job** (`backup_intune`):
  1. Prepare `drift/intune` branch from `main`.
  2. Decide light vs full mode (configured full-run hour or `forceFullRun=true`).
  3. Run `IntuneCD-startbackup`.
  4. Filter partial Settings Catalog exports.
  5. Resolve assignment group names from Graph.
  6. Generate assignment and object inventory reports.
  7. Validate outputs.
  8. Commit drift and update rolling PR.

- **Entra backup job** (`backup_entra`):
  1. Prepare `drift/entra` branch from `main`.
  2. Export selected categories with `export_entra_baseline.py`.
  3. Resolve Conditional Access references.
  4. Generate reports.
  5. Validate outputs.
  6. Filter enrichment noise and commit drift.

- **Review sync jobs** (`sync_intune_review_decisions`, `sync_entra_review_decisions`):
  1. Apply `/reject` decisions.
  2. Update automated PR summary.
  3. Queue post-merge restore when needed.

- **Restore job** (`restore_from_baseline`):
  1. Checkout approved baseline snapshot (branch, tag, or commit).
  2. Prepare restore scope (`full` or `selective`).
  3. Normalize payload JSON and strip display-only assignment labels.
  4. Run `IntuneCD-startupdate` with optional `--entraupdate`.

## Security Considerations

- **Token handling**: Graph tokens are obtained via `Get-AzAccessToken` in PowerShell and passed as secret pipeline variables. Token payload is decoded to validate required application permissions before use.
- **Service connection**: Azure DevOps service connection (e.g. `sc-astral-backup`) uses workload federated credentials.
- **Permissions**: read-only permissions for backup; read-write permissions (`...ReadWrite.All`) for restore. Missing roles are surfaced as pipeline errors before any Graph mutations occur.
- **Path traversal**: selective restore paths are normalized and validated against `..` segments before file copy.
- **Dry run**: restore pipeline defaults to `dryRun=true` and must be explicitly overridden to push changes.
- **Access token scope**: `System.AccessToken` is required for PR and thread management via Azure DevOps REST APIs.

## Common Development Tasks

### Generate Entra export locally

```bash
python3 ./scripts/export_entra_baseline.py \
  --root ./tenant-state/entra \
  --token "$GRAPH_TOKEN" \
  --enterprise-app-workers 8
```

### Resolve Conditional Access references locally

```bash
python3 ./scripts/resolve_ca_references.py \
  --root ./tenant-state/entra \
  --token "$GRAPH_TOKEN"
```

### Generate assignment report locally

```bash
python3 ./scripts/generate_assignment_report.py \
  --root ./tenant-state/intune \
  --output-dir ./tenant-state/reports/intune
```

### Run the MCP server locally (development only)

```bash
export ASTRAL_REPO_ROOT=/path/to/astral-clone
python3 ./infra/mcp-server/mcp_server.py --transport stdio
```

For production deployment, use the integrated provisioning script (`deploy/provision-change-probe.ps1`).

### Validate backup outputs locally

```bash
python3 ./scripts/validate_backup_outputs.py \
  --workload intune \
  --mode light \
  --root ./tenant-state/intune \
  --reports-root ./tenant-state/reports/intune
```

## Key Environment / Pipeline Variables

- `BASELINE_BRANCH` (default: `main`)
- `DRIFT_BRANCH_INTUNE` (default: `drift/intune`)
- `DRIFT_BRANCH_ENTRA` (default: `drift/entra`)
- `BACKUP_FOLDER` (default: `tenant-state`)
- `ENABLE_WORKLOAD_INTUNE` / `ENABLE_WORKLOAD_ENTRA`
- `ENABLE_PR_REVIEW_SUMMARY` / `ENABLE_PR_REVIEWER_DECISIONS`
- `AUTO_REMEDIATE_AFTER_MERGE` / `AUTO_REMEDIATE_DRY_RUN`
- `ENABLE_PR_AI_SUMMARY` + `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY`
- `ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS` / `ROLLING_PR_MERGE_STRATEGY`

### MCP Server Environment Variables

- `ASTRAL_REPO_ROOT` — local Git repo path (overrides ADO; used for local dev)
- `ADO_ORGANIZATION` / `ADO_PROJECT` / `ADO_REPO_NAME` / `ADO_BRANCH` / `ADO_TOKEN`

See `infra/mcp-server/README.md` for the full configuration reference.
