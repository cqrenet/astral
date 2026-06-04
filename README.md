<img src="docs/assets/astral-logo.svg" alt="ASTRAL — Admin Security: Tenant Review, Automation &amp; Lifecycle" width="600"/>

> Part of the [CQRE](https://cqre.net) M365 governance suite alongside [PULSAR](https://github.com/cqrenet/pulsar).

ASTRAL keeps Git-tracked snapshots of Microsoft Intune and Entra ID configuration, surfaces configuration drift through pull requests, and provides a review and rollback path. It answers the question: *"what changed in my tenant, was it authorised, and can we revert it?"*

## Getting Started

This repository is designed to be forked or downloaded into your own Azure DevOps organization. Each tenant gets its own project and pipeline instance.

Quick start:

1. Fork or import this repository into an Azure DevOps project.
2. Review `templates/variables-tenant.yml` and create a matching Azure DevOps Variable Group in your project (e.g. `vg-astral`).
3. Uncomment the variable group reference in the three pipeline YAMLs.
4. Run `deploy/provision.ps1` to create the Azure AD app registration, assign Graph permissions, configure the federated credential, and provision the event-driven change probe (Azure Function App) and optionally the MCP server (Azure Container Apps).
5. Create the Azure DevOps service connection using the app registration details from the bootstrap script.
6. Import the three pipelines (`azure-pipelines.yml`, `azure-pipelines-review-sync.yml`, `azure-pipelines-restore.yml`) into Azure DevOps.
7. Run `deploy/validate-deployment.yml` to verify connectivity and permissions.
8. Set `AUTO_REMEDIATE_RESTORE_PIPELINE_ID` in your variable group after the restore pipeline is imported.

See [`deploy/onboarding-runbook.md`](deploy/onboarding-runbook.md) for the full step-by-step guide.

## Updates

Import `deploy/update-from-upstream.yml` as a pipeline once after initial setup. Queue it to pull the latest `main` from GitHub, or specify a release tag (e.g. `v1.2.0`) to do a pinned version upgrade. The pipeline shows incoming changes, merges cleanly if there are no conflicts, and pushes — or stops and reports conflicting files if manual resolution is needed.

Watch [GitHub releases](https://github.com/cqrenet/astral/releases) for new versions and changelogs.

## What The Repository Does

The implementation is centered on three Azure DevOps pipelines and an MCP server:

- `azure-pipelines.yml`: backup/export pipeline with rolling PR management. Runs daily at 02:00 to generate a full tenant snapshot, reports, and documentation artifacts, and is also triggered on-demand by the event-driven change probe.
- `azure-pipelines-review-sync.yml`: 20-minute reviewer-decision sync and post-merge remediation queue.
- `azure-pipelines-restore.yml`: manual or auto-queued restore pipeline for approved baseline rollback.
- `infra/mcp-server/`: Azure Container Apps-hosted MCP server exposing tenant state and drift history to AI assistants via natural-language queries.

The main workflow is:

1. Export Intune and Entra configuration into `tenant-state/`.
2. Generate Markdown/CSV reports in `tenant-state/reports/`.
3. Filter known non-actionable drift noise before commit.
4. Commit workload drift to `drift/intune` and `drift/entra`.
5. Create or update one rolling PR per workload into `main`.
6. Refresh the PR description with deterministic change/risk summary and optional Azure OpenAI narrative.
7. Apply reviewer `/reject` or `/accept` decisions and queue restore when needed.
8. Query tenant state and drift history via the ASTRAL MCP Server.

An **event-driven change probe** monitors Intune and Entra audit logs and triggers the backup pipeline when actual changes are detected, replacing the previous hourly polling model with a responsive event-driven approach.

This is an ex-post change-management model: admins can change settings in the Microsoft admin portals, and the repo turns those changes into auditable Git drift with a review and rollback path.

## Current Baseline Coverage

Intune currently tracks:

- App Configuration
- App Protection
- Apple Push Notification
- Apple VPP Tokens
- Applications
- Compliance Policies
- Device Configurations
- Device Management Settings
- Enrollment Configurations
- Enrollment Profiles
- Filters
- Scope Tags
- Scripts
- Settings Catalog

Entra currently tracks:

- Named Locations
- Authentication Strengths
- Conditional Access
- App Registrations
- Enterprise Applications

Current scope behavior:

- Named Locations, Authentication Strengths, and Conditional Access run on hourly light runs and midnight full runs.
- App Registrations and Enterprise Applications are enabled in the pipeline but exported only on full runs.
- During light runs, the previous drift-branch snapshot of `App Registrations` and `Enterprise Applications` is preserved to avoid churn and heavy export cost.

## Repository Layout

- `README.md`: operational overview.
- `azure-pipelines.yml`: backup/export, report generation, drift commit, rolling PR, and docs/artifact flow.
- `azure-pipelines-review-sync.yml`: reviewer decision sync and post-merge remediation helper.
- `azure-pipelines-restore.yml`: baseline restore pipeline with full or selective scope.
- `infra/change-probe/`: Azure Function App for event-driven change detection.
- `infra/mcp-server/`: MCP server (Azure Container Apps) for AI assistant integration — policy lookup, search, drift history, and assignment reports.
- `deploy/provision.ps1`: unified provisioning script for the change probe and MCP server infrastructure.
- `docs/m365-baseline-roadmap.md`: expansion roadmap beyond current workload scope.
- `docs/security-review-package.md`: implementation-focused security review package.
- `docs/security-review-questionnaire.md`: short-form security review answers.
- `scripts/`: export, reporting, PR automation, validation, remediation helpers, change probe logic, and MCP query layer.
- `tests/`: focused unit coverage for the Python helpers.
- `tenant-state/intune`: committed Intune JSON export.
- `tenant-state/entra`: committed Entra JSON export.
- `tenant-state/reports/intune`: Intune CSV/Markdown reports.
- `tenant-state/reports/entra`: Entra CSV/Markdown reports.
- `prod-as-built.md`: generated as-built document source.
- `md2pdf/`: HTML/PDF styling and config for documentation publish.

## Pipeline Model

### Main Backup Pipeline

`azure-pipelines.yml` runs daily at 02:00 on `main` to generate a full tenant snapshot, reports, and documentation artifacts. It is also triggered on-demand by the change probe when drift is detected.

For Intune it:

1. Prepares `drift/intune` from `main`.
2. Chooses light vs full mode from the configured local timezone, with `forceFullRun=true` override.
3. Runs IntuneCD export.
4. Reverts partial Settings Catalog exports with `scripts/filter_intune_partial_settings_noise.py`.
5. Resolves assignment group names from Graph when needed.
6. Generates assignment and object inventory reports.
7. Validates outputs with `scripts/validate_backup_outputs.py`.
8. Commits drift and updates the rolling PR flow.

For Entra it:

1. Prepares `drift/entra` from `main`.
2. Chooses effective export scope per mode.
3. Exports selected categories with `scripts/export_entra_baseline.py`.
4. Resolves Conditional Access reference names with `scripts/resolve_ca_references.py`.
5. Generates assignment, app, and object inventory reports.
6. Validates outputs with `scripts/validate_backup_outputs.py`.
7. Reverts enrichment-only JSON churn with `scripts/filter_entra_enrichment_noise.py`.
8. Commits drift with `scripts/commit_entra_drift.py`.

### Review Sync Pipeline

`azure-pipelines-review-sync.yml` runs every 20 minutes on `main` and exists to shorten the reviewer feedback loop.

Per workload it can:

- apply reviewer `/reject` and `/accept` decisions with `scripts/apply_reviewer_rejections.py`
- refresh the automated PR summary
- queue restore after merged PRs that contained reviewer `/reject` decisions using `scripts/queue_post_merge_restore.py`

### Restore Pipeline

`azure-pipelines-restore.yml` restores from approved baseline (`main` by default) or from a historical branch, tag, or commit.

Supported restore modes:

- `full`: restore the full committed Intune baseline
- `selective`: restore only selected file paths

It also supports optional Entra update when restore automation is triggered for Entra review outcomes.

## Schedule And Run Modes

- Main backup schedule: daily at 02:00, `0 2 * * *`, on `main` (full snapshot, reports, and docs)
- Change probe trigger: event-driven, on-demand via Azure Function App
- Review sync schedule: every 20 minutes, `*/20 * * * *`, on `main`
- Full mode: configured full-run hour (default 00:00) or manual queue with `forceFullRun=true`
- Light mode: all probe-triggered runs except the daily full run

### Change Probe (Event-Driven Backup Trigger)

Because Microsoft Graph change notifications and delta queries do not support Intune device management or Conditional Access resources, an audit-log polling architecture is used:

- **`probe_timer`** (5-minute timer trigger): polls Intune and Entra audit logs via Microsoft Graph, evaluates a debouncer state machine (idle → armed → cooldown), and emits a queue message when the quiet window elapses.
- **`queue_consumer`** (queue trigger): dequeues messages and calls the Azure DevOps REST API to queue the backup pipeline.
- **Debouncer**: 15-minute quiet window + 30-minute cooldown prevents backup storms during bulk changes.
- **State**: stored in Azure Table Storage (`ProbeState` table).
- **Provisioning**: `deploy/provision.ps1` creates the Entra app, grants admin consent, provisions Resource Group / Storage Account / Function App, and configures app settings.

Full mode adds:

- full Entra scope, including App Registrations and Enterprise Applications
- Intune split-documentation generation
- HTML/PDF artifact generation when browser dependencies are available
- optional tagging and documentation publish steps

## Branch And PR Model

- Baseline branch: `main`
- Drift branches:
  - `drift/intune`
  - `drift/entra`

Each workload keeps one rolling PR open to `main`.

Key behavior:

- reports are generated in `tenant-state/reports/*` but excluded from rolling drift commits and PR diffs
- rolling PRs can be created as draft first, then published after automated summary generation when `ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS=true`
- merge strategy for the rolling PR is controlled by `ROLLING_PR_MERGE_STRATEGY` and defaults to `rebase`

## Review, Tickets, And Remediation

The PR automation currently supports:

- deterministic operation counts and risk assessment
- rename-aware semantic comparison
- stable change fingerprinting for idempotent summary refresh
- optional Azure OpenAI reviewer narrative
- optional per-file `Change Needed` review threads when `REQUIRE_CHANGE_TICKETS=true`

Reviewer thread commands:

- `/reject`: remove that file-level drift from the rolling PR by resetting it to baseline
- `/accept`: keep that file in PR scope

Supported remediation paths:

1. Reject and abandon a whole rolling PR.
   The next run can detect the matching rejected snapshot and queue restore automatically.
2. Reject selected files in ticket threads, then merge the accepted remainder.
   The review-sync pipeline can queue restore after merge so the tenant is reconciled to the merged baseline.
3. Queue `azure-pipelines-restore.yml` manually for full or selective historical rollback.

## Key Variables

Core repo and branch settings:

- `BASELINE_BRANCH`
- `DRIFT_BRANCH_INTUNE`
- `DRIFT_BRANCH_ENTRA`
- `ROLLING_PR_TITLE_INTUNE`
- `ROLLING_PR_TITLE_ENTRA`
- `BACKUP_FOLDER`
- `INTUNE_BACKUP_SUBDIR`
- `ENTRA_BACKUP_SUBDIR`
- `REPORTS_SUBDIR`

Workload toggles:

- `ENABLE_WORKLOAD_INTUNE`
- `ENABLE_WORKLOAD_ENTRA`
- `ENABLE_ENTRA_CONDITIONAL_ACCESS`

Intune behavior:

- `INTUNECD_VERSION`
- `EXCLUDE_SCRIPT_BACKUP`
- `INTUNE_EXCLUDE_CSV`
- `SPLIT_DOCUMENTATION`

Entra behavior:

- `ENTRA_INCLUDE_NAMED_LOCATIONS`
- `ENTRA_INCLUDE_AUTHENTICATION_STRENGTHS`
- `ENTRA_INCLUDE_CONDITIONAL_ACCESS`
- `ENTRA_INCLUDE_APP_REGISTRATIONS`
- `ENTRA_INCLUDE_ENTERPRISE_APPS`
- `ENTRA_ENTERPRISE_APP_WORKERS`

PR and reviewer automation:

- `ENABLE_PR_REVIEW_SUMMARY`
- `ENABLE_PR_REVIEWER_DECISIONS`
- `ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS`
- `ROLLING_PR_MERGE_STRATEGY`
- `REQUIRE_CHANGE_TICKETS`
- `CHANGE_TICKET_REGEX`
- `DEBUG_CHANGE_TICKET_THREADS`

Auto-remediation:

- `AUTO_REMEDIATE_ON_PR_REJECTION`
- `AUTO_REMEDIATE_AFTER_MERGE`
- `AUTO_REMEDIATE_AFTER_MERGE_LOOKBACK_HOURS`
- `AUTO_REMEDIATE_RESTORE_PIPELINE_ID`
- `AUTO_REMEDIATE_DRY_RUN`
- `AUTO_REMEDIATE_UPDATE_ASSIGNMENTS`
- `AUTO_REMEDIATE_REMOVE_OBJECTS`
- `AUTO_REMEDIATE_MAX_WORKERS`
- `AUTO_REMEDIATE_EXCLUDE_CSV`

Change probe settings:

- `PROBE_APP_ID`
- `PROBE_APP_SECRET`
- `PROBE_QUIET_WINDOW_MINUTES` (default: 15)
- `PROBE_COOLDOWN_MINUTES` (default: 30)
- `GRAPH_TOKEN` (optional passthrough)

Azure OpenAI integration:

- `ENABLE_PR_AI_SUMMARY`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_API_VERSION`
- `PR_AI_PAYLOAD_MAX_BYTES`
- `PR_AI_MAX_TOKENS`
- `PR_AI_COMPACT_MAX_CHARS`

MCP server settings:

- `ASTRAL_REPO_ROOT` — local Git repo path (overrides ADO source; used for local dev)
- `ADO_ORGANIZATION` / `ADO_PROJECT` / `ADO_REPO_NAME` / `ADO_BRANCH` / `ADO_TOKEN`

See `infra/mcp-server/README.md` for the full MCP server configuration reference.

## Required Azure DevOps Permissions

The pipeline build identity should have repository permissions to:

- contribute
- create branch
- force push
- create and update pull requests
- create tag if tagging is enabled

For auto-queued restore, the same identity also needs on `azure-pipelines-restore.yml`:

- `View builds`
- `Queue builds`
- pipeline authorization if explicit pipeline permissions are enforced

Also enable script access to `System.AccessToken`.

## Required Microsoft Graph Application Permissions

Baseline read permissions used by the current implementation:

- `Device.Read.All`
- `DeviceManagementApps.Read.All`
- `DeviceManagementConfiguration.Read.All`
- `DeviceManagementManagedDevices.Read.All`
- `DeviceManagementRBAC.Read.All`
- `DeviceManagementScripts.Read.All`
- `DeviceManagementServiceConfig.Read.All`
- `Group.Read.All`
- `Policy.Read.All`
- `Policy.Read.ConditionalAccess`
- `Policy.Read.DeviceConfiguration`
- `User.Read.All`

Additional read permissions used by the current Entra scope:

- `Application.Read.All`
- `RoleManagement.Read.Directory` or `Directory.Read.All` for richer name resolution
- `AuditLog.Read.All` for best-effort Entra drift author attribution

Restore pipeline write permissions:

- `DeviceManagementApps.ReadWrite.All`
- `DeviceManagementConfiguration.ReadWrite.All`
- `DeviceManagementManagedDevices.ReadWrite.All`
- `DeviceManagementRBAC.ReadWrite.All`
- `DeviceManagementScripts.ReadWrite.All`
- `DeviceManagementServiceConfig.ReadWrite.All`
- `Group.Read.All`

Additional restore permission when `includeEntraUpdate=true`:

- `Policy.Read.All`
- `Policy.ReadWrite.ConditionalAccess`

## Outputs

Intune outputs:

- JSON backup under `tenant-state/intune/**`
- `tenant-state/reports/intune/policy-assignments.md`
- `tenant-state/reports/intune/policy-assignments.csv`
- `tenant-state/reports/intune/object-inventory-all.csv`
- `tenant-state/reports/intune/Object Inventory/*-inventory.csv`

Entra outputs:

- JSON backup under `tenant-state/entra/**`
- `tenant-state/reports/entra/policy-assignments.md`
- `tenant-state/reports/entra/policy-assignments.csv`
- `tenant-state/reports/entra/apps-inventory.csv`
- `tenant-state/reports/entra/object-inventory-all.csv`
- `tenant-state/reports/entra/Object Inventory/*-inventory.csv`

Full-run documentation artifacts:

- `prod-as-built-split-markdown`
- `prod-as-built-split-html`
- `prod-as-built-split-pdf`

## Local Script Usage

Generate Entra export locally:

```bash
python3 ./scripts/export_entra_baseline.py \
  --root ./tenant-state/entra \
  --token "$GRAPH_TOKEN" \
  --enterprise-app-workers 8
```

Resolve Conditional Access references:

```bash
python3 ./scripts/resolve_ca_references.py \
  --root ./tenant-state/entra \
  --token "$GRAPH_TOKEN"
```

Generate Intune assignment report:

```bash
python3 ./scripts/generate_assignment_report.py \
  --root ./tenant-state/intune \
  --output-dir ./tenant-state/reports/intune
```

Generate Entra assignment report:

```bash
python3 ./scripts/generate_assignment_report.py \
  --root ./tenant-state/entra \
  --output-dir ./tenant-state/reports/entra
```

Generate Entra apps inventory:

```bash
python3 ./scripts/generate_app_inventory_report.py \
  --root ./tenant-state/entra \
  --output-dir ./tenant-state/reports/entra
```

Generate workload object inventories:

```bash
python3 ./scripts/generate_object_inventory_reports.py \
  --root ./tenant-state/intune \
  --output-dir ./tenant-state/reports/intune

python3 ./scripts/generate_object_inventory_reports.py \
  --root ./tenant-state/entra \
  --output-dir ./tenant-state/reports/entra
```

Validate backup outputs:

```bash
python3 ./scripts/validate_backup_outputs.py \
  --workload intune \
  --mode light \
  --root ./tenant-state/intune \
  --reports-root ./tenant-state/reports/intune
```

Run the change probe locally:

```bash
python3 ./scripts/probe_tenant_changes.py \
  --app-id "$PROBE_APP_ID" \
  --app-secret "$PROBE_APP_SECRET" \
  --tenant-id "$TENANT_ID" \
  --state-file ./probe-state.json \
  --output ./probe-result.json
```

Trigger the backup pipeline manually:

```bash
python3 ./scripts/trigger_backup_pipeline.py \
  --organization cqre \
  --project Intune \
  --pipeline-id 1 \
  --token "$ADO_TOKEN" \
  --branch refs/heads/main
```

## Tests

The repository includes focused unit tests for:

- Entra export behavior
- backup output validation
- rolling PR creation/update logic
- PR summary generation
- reviewer rejection processing
- post-merge restore queueing
- Intune partial-export noise filtering
- Entra enrichment-noise filtering
- MCP server query layer and data source behavior

## Acknowledgements

ASTRAL's Intune export is built on [IntuneCD](https://github.com/almenscorner/IntuneCD) by [Tobias Almén](https://github.com/almenscorner). IntuneCD handles the export of Intune configuration — device configurations, compliance policies, applications, scripts, and more — directly from the Microsoft Graph API. Without it, ASTRAL would need to reimplement a large surface area of the Intune export layer from scratch. If you find ASTRAL useful, IntuneCD is where the Intune export work actually happens — it deserves your support, or at least a star.

The Entra export (Conditional Access, named locations, app registrations, and related policies) is implemented directly in ASTRAL using the Microsoft Graph API.
