# ASTRAL Change Probe

Event-driven backup trigger for ASTRAL. Monitors Intune and Entra ID audit logs via Microsoft Graph, debounces change bursts, and queues the Azure DevOps backup pipeline only when actual drift is detected.

## Why this exists

Microsoft Graph change notifications and delta queries do **not** support Intune device management or Conditional Access resources. The only viable event-driven approach is polling the Graph audit log APIs, which have a 5–15 minute propagation delay. This probe implements a debouncer on top of that polling to avoid backup storms during bulk changes.

## Architecture

```
┌─────────────────┐     5 min      ┌──────────────┐    quiet window    ┌─────────────────┐
│  Timer Trigger  │ ─────────────► │  probe_timer │ ─────────────────► │  backup-trigger │
│  (probe_timer)  │                │  (debouncer) │   (15 min armed)   │  -queue         │
└─────────────────┘                └──────────────┘                    └────────┬────────┘
        │                                                                        │
        │  load/save state                                                       │ dequeue
        │  (Azure Table Storage)                                                 ▼
        │                                                               ┌─────────────────┐
        │                                                               │ queue_consumer  │
        └──────────────────────────────────────────────────────────────►│  (ADO REST API) │
                                                                        └─────────────────┘
                                                                                │
                                                                                ▼
                                                                        ┌─────────────────┐
                                                                        │  Azure DevOps   │
                                                                        │  backup pipeline│
                                                                        └─────────────────┘

ADO PR comment
  (reviewer posts /accept or /reject)
        │
        │  ADO Service Hook
        ▼
┌───────────────────┐    /accept|/reject     ┌─────────────────────┐
│ pr_comment_webhook│ ──────────────────────►│  ADO REST API       │
│  (HTTP Trigger)   │                        │  review-sync        │
└───────────────────┘                        │  pipeline (on-demand│
                                             └─────────────────────┘
20-min schedule in azure-pipelines-review-sync.yml remains as fallback.
```

## Components

### `probe_timer` (Timer Trigger)

- **Schedule**: every 5 minutes (`0 */5 * * * *`)
- **Input**: `TimerRequest` from Functions runtime
- **Output**: queue message to `backup-trigger-queue` (via `func.Out[str]`)
- **Actions**:
  1. Load debouncer state from Azure Table Storage (`ProbeState` / `singleton` / `default`).
  2. Run `scripts/probe_tenant_changes.py` via subprocess.
  3. Save updated state back to Table Storage.
  4. If `trigger=true`, emit a queue message.

### `queue_consumer` (Queue Trigger)

- **Input**: `QueueMessage` from `backup-trigger-queue`
- **Actions**:
  1. Parse JSON payload (`reason`, `checked_at`).
  2. Call Azure DevOps REST API to queue the backup pipeline run.
  3. Raise on failure so the Functions runtime handles retry and poison-queue logic.

### `pr_comment_webhook` (HTTP Trigger)

- **Trigger**: POST from ADO service hook on "Pull request commented on"
- **Auth**: `Authorization: Basic :<WEBHOOK_SECRET>` (if `WEBHOOK_SECRET` is set)
- **Actions**:
  1. Validate the shared secret (if configured).
  2. Extract `resource.comment.content` from the ADO service hook payload.
  3. If the comment matches `^/accept` or `^/reject` (case-insensitive), call the ADO REST API to queue `ADO_REVIEW_SYNC_PIPELINE_ID` immediately.
  4. Always return `200 OK` to prevent ADO from retrying; the 20-min schedule is the fallback.

### `scripts/probe_tenant_changes.py`

Standalone CLI script that can also be run locally. It:

- Queries Intune (`deviceManagement/auditEvents`) and Entra (`directoryAudits`) audit logs.
- Implements a three-state debouncer: `idle` → `armed` → `cooldown`.
- Returns JSON with `trigger`, `reason`, and `new_state`.

### `scripts/trigger_backup_pipeline.py`

Standalone CLI script that queues an Azure DevOps pipeline run via REST API. Can be used locally or from the queue consumer.

## Debouncer State Machine

| State | Condition to transition | Output |
|---|---|---|
| **idle** | Audit log shows a new change | → `armed` |
| **armed** | Quiet window elapsed (default 15 min) with no newer events | → `cooldown`, `trigger=true` |
| **armed** | Newer event arrives while armed | Stay `armed`, extend quiet window |
| **cooldown** | Cooldown elapsed (default 30 min) | → `idle` |
| **cooldown** | New event arrives | Stay `cooldown` (change is buffered until cooldown ends) |

## Configuration

All settings are provided via Function App application settings (environment variables):

| Setting | Required | Default | Description |
|---|---|---|---|
| `AzureWebJobsStorage` | Yes | — | Storage account connection string (tables + queues) |
| `PROBE_APP_ID` | Yes* | — | Entra app registration client ID |
| `PROBE_APP_SECRET` | Yes* | — | Entra app client secret |
| `TENANT_ID` | Yes* | — | Microsoft 365 tenant ID |
| `GRAPH_TOKEN` | No | — | Optional passthrough token ( skips client credentials flow ) |
| `ADO_ORGANIZATION` | Yes | — | Azure DevOps organization name |
| `ADO_PROJECT` | Yes | — | Azure DevOps project name |
| `ADO_PIPELINE_ID` | Yes | — | Backup pipeline definition ID |
| `ADO_TOKEN` | Yes | — | Azure DevOps PAT with **Build (read & execute)** |
| `ADO_BRANCH` | No | `main` | Git ref to queue the pipeline against |
| `ADO_REVIEW_SYNC_PIPELINE_ID` | No | — | Pipeline definition ID for `azure-pipelines-review-sync.yml`; required for `pr_comment_webhook` |
| `WEBHOOK_SECRET` | No | — | Shared secret for `pr_comment_webhook`; validated as `Authorization: Basic :<secret>` |
| `PROBE_QUIET_WINDOW_MINUTES` | No | `15` | Minutes to wait for change burst to settle |
| `PROBE_COOLDOWN_MINUTES` | No | `30` | Minutes between successive triggers |

\* Required unless `GRAPH_TOKEN` is provided.

## Local Development

### Prerequisites

- Python 3.11+
- [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- An Azure Storage account (or Azurite for local emulation)

### Install dependencies

```bash
cd infra/change-probe
pip install -r requirements.txt
```

### Copy shared scripts

The probe reuses scripts from the repository root. Copy them into this directory before building or running locally:

```bash
cp ../../scripts/common.py scripts/
cp ../../scripts/probe_tenant_changes.py scripts/
cp ../../scripts/trigger_backup_pipeline.py scripts/
```

### Run locally

```bash
# Start Azurite (Storage emulator)
azurite --silent --location ./azurite --debug ./azurite/debug.log

# Copy local settings template
cp local.settings.json.example local.settings.json
# Edit local.settings.json with your values

# Start the Functions host
func start
```

### Run the probe script standalone

```bash
cd ../..
python3 scripts/probe_tenant_changes.py \
  --client-id "$PROBE_APP_ID" \
  --client-secret "$PROBE_APP_SECRET" \
  --tenant-id "$TENANT_ID" \
  --state-file ./probe-state.json \
  --output ./probe-result.json
```

### Trigger the backup pipeline standalone

```bash
python3 scripts/trigger_backup_pipeline.py \
  --organization "contoso" \
  --project "Intune" \
  --pipeline-id 1 \
  --token "$ADO_TOKEN" \
  --branch refs/heads/main
```

## Wiring up the ADO service hook

After deploying the Function App, configure the service hook in ADO so PR comments trigger an immediate review-sync run:

1. Go to **Azure DevOps → Project Settings → Service hooks → + Create subscription**.
2. Select **Web Hooks** and click **Next**.
3. **Event**: *Pull request commented on*. Optionally filter to the specific repository.
4. **Action — URL**: `https://<func-app>.azurewebsites.net/api/pr_comment_webhook`
5. **Basic authentication — Username**: any value (e.g. `astral`)
6. **Basic authentication — Password**: the value you set for `WEBHOOK_SECRET`
7. Leave **HTTP headers** empty and click **Finish**.

> **Note:** If `WEBHOOK_SECRET` is left empty in the Function App settings, the function skips auth entirely — useful during local testing but not recommended for production.

## Deployment

Use the unified provisioning script:

```powershell
.\deploy\provision-change-probe.ps1 `
  -TenantName "contoso.onmicrosoft.com" `
  -ResourceGroupName "rg-astral-probe" `
  -Location "westeurope" `
  -DeployFunctionApp
```

The script will:

1. Register an Entra app (or reuse an existing one).
2. Grant admin consent for Graph permissions.
3. Create a client secret.
4. Provision Resource Group, Storage Account, and Function App (Linux Consumption, Python 3.11).
5. Configure application settings.
6. Build and deploy the function package.

### Manual deployment (zip package)

If you prefer to deploy manually:

```bash
cd infra/change-probe

# Copy shared scripts into the package directory
cp ../../scripts/common.py scripts/
cp ../../scripts/probe_tenant_changes.py scripts/
cp ../../scripts/trigger_backup_pipeline.py scripts/

# Install production dependencies into the package
pip install -r requirements.txt --target .python_packages/lib/site-packages

# Build the zip (Linux Consumption requires .python_packages/lib/site-packages, NOT python3.11/)
zip -r function-package.zip \
  probe_timer/ queue_consumer/ scripts/ .python_packages/ \
  host.json requirements.txt \
  -x "*.pyc" -x "__pycache__/*"

# Upload and set WEBSITE_RUN_FROM_PACKAGE
az functionapp deployment source config-zip \
  --resource-group rg-astral-probe \
  --name func-astral-probe \
  --src function-package.zip
```

## Permissions

### Entra App (Graph access)

The probe requires the same read permissions as the main backup pipeline:

- `DeviceManagementConfiguration.Read.All`
- `DeviceManagementApps.Read.All`
- `AuditLog.Read.All`
- `Directory.Read.All`

### Azure DevOps PAT

The `ADO_TOKEN` must have:

- **Build** → *Read & execute*

## Monitoring

Check the `ProbeState` table for current debouncer state:

```bash
az storage entity query --table-name ProbeState --account-name <storage>
```

Check the queue depth:

```bash
az storage queue list --account-name <storage>
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Timer fires but no state update | `schedule_status["last"]` case mismatch (fixed in current version) | Ensure deployed code uses `.get("Last")` |
| Probe script `ModuleNotFoundError` | Bundled packages in wrong path | Use `.python_packages/lib/site-packages`, not `python3.11/site-packages` |
| Queue message lands in poison queue | `ADO_TOKEN` missing or invalid | Verify token in Function App settings and restart |
| Probe never triggers | No audit events in Graph window | Normal if tenant is idle; verify `AuditLog.Read.All` permission |
| Duplicate pipeline runs | Multiple messages queued | Check debouncer state; cooldown should prevent this |
