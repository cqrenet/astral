<img src="./assets/astral-logo.svg" alt="ASTRAL logo" width="760" />

# ASTRAL Security Review Package

Prepared: 2026-04-20

## Purpose

This document describes the security posture of ASTRAL, an Intune / Entra drift backup, review, and remediation platform implemented in this repository.

ASTRAL stands for:

- Admin Security: Tenant Review, Automation & Lifecycle

The goal of the platform is to:

- export Microsoft Intune and selected Entra ID configuration from a production tenant,
- store approved configuration snapshots in Git,
- surface drift through rolling pull requests,
- optionally restore tenant configuration back to the approved baseline.

This package is intended for customer security review of the full product and its available deployment modes.

## Executive Summary

ASTRAL is an Azure DevOps pipeline based administrative workflow, not a customer-facing application and not an endpoint agent.

Key characteristics:

- No inbound listener or public application endpoint is exposed by this repository.
- The normal operating mode is outbound-only scheduled jobs from Azure DevOps to Microsoft Graph and Azure DevOps APIs.
- The default backup/review path is read-oriented against Microsoft Graph.
- A separate restore path can write configuration back to the tenant, but only through the dedicated restore pipeline and only when enabled and authorized.
- AI-assisted PR summaries are optional and are not required for backup, review, or restore.

## Deployment Modes

The repository can be deployed progressively. It does not need to be introduced as an all-or-nothing package.

| Mode | Scope | Graph Access Profile | Azure DevOps Scope | AI |
| --- | --- | --- | --- | --- |
| Backup-only | Export tenant configuration, generate reports, retain Git-tracked snapshots | Read-only | Repository and scheduled pipeline only | Disabled |
| Review package | Backup-only plus rolling PR review, reviewer summaries, optional change-ticket threads, reviewer `/accept` and `/reject` processing | Read-only | Repository, PR workflows, review-sync pipeline | Optional |
| Full package | Review package plus restore pipeline, rollback support, selective remediation, and optional auto-remediation | Read + Write for restore path only | Repository, PR workflows, review-sync, restore pipeline | Optional |

Important clarifications:

- AI is an add-on, not a core dependency.
- Restore is a separate capability, not a requirement for backup or review.
- Organizations can adopt the platform progressively, starting with backup-only and adding review or restore capabilities later.
- AI can be enabled or disabled independently of the backup, review, and restore layers.

## System Overview

### In-Scope Components

| Component | Function | Security Relevance |
| --- | --- | --- |
| Azure DevOps pipeline `azure-pipelines.yml` | Scheduled backup, drift commit, rolling PR management, documentation artifact publishing | Main execution path |
| Azure DevOps pipeline `azure-pipelines-review-sync.yml` | Processes reviewer `/reject` and `/accept` decisions and refreshes PR summaries | Uses Azure DevOps API token |
| Azure DevOps pipeline `azure-pipelines-restore.yml` | Restores approved baseline to tenant | Write-capable path |
| Azure Function App (`infra/change-probe`) | Event-driven probe: polls audit logs, debounces, triggers backup pipeline on demand | Outbound-only; uses separate Entra app registration |
| Azure Table Storage | Persists probe debouncer state (`ProbeState` table) | No sensitive tenant data |
| Azure Queue Storage | Receives trigger messages from probe timer for queue consumer | No sensitive tenant data |
| Azure DevOps Git repository | Stores approved baseline, drift branches, JSON exports, reports, docs | Primary configuration store |
| Microsoft Graph | Source of Intune and Entra configuration; optional target for restore; audit log source for probe | Production tenant access |
| Azure DevOps REST APIs | PR creation/update, review thread sync, restore queueing, pipeline trigger | Change-management control plane |
| Optional Azure OpenAI | PR summary generation only | Optional data egress path |

### High-Level Flow

```mermaid
flowchart LR
    A["Azure Function App<br/>probe_timer"] --> B["Microsoft Graph<br/>audit logs"]
    A --> C["Azure Table Storage<br/>ProbeState"]
    A --> D["Azure Queue Storage<br/>backup-trigger-queue"]
    E["Azure Function App<br/>queue_consumer"] --> D
    E --> F["Azure DevOps REST API<br/>queue pipeline run"]
    G["Azure DevOps scheduled pipeline<br/>daily snapshot + reports"] --> H["Federated service connection"]
    H --> B
    G --> I["Git repo: main + drift branches"]
    G --> J["Azure DevOps PR and thread APIs"]
    G --> K["Build artifacts: markdown / HTML / PDF"]
    G -. optional .-> L["Azure OpenAI"]
    M["Reviewer in Azure DevOps"] --> J
    J --> N["Rolling PR approval / rejection"]
    N -. optional remediation .-> O["Restore pipeline"]
    O --> B
```

## Deployment Model

### Backup and Review

The main pipeline runs daily at 02:00 on `main` to generate a full tenant snapshot, reports, and documentation artifacts. The primary trigger is the event-driven change probe, which queues the pipeline on demand when drift is detected.

- On change detection: the probe timer polls audit logs every 5 minutes. After a 15-minute quiet window with no new events, it queues the backup pipeline.
- Daily at 02:00: export Intune and Entra configuration, generate reports, commit drift to rolling workload branches, and update one rolling PR per workload.
- When delayed reviewer notifications are enabled, newly created rolling PRs are opened as Azure DevOps draft PRs, the automated summary is inserted, and the PR is then published for reviewer notification.
- At the configured full-run hour: perform the same work plus documentation artifact generation (Markdown, and optionally HTML/PDF if browser dependencies are available).

The workload branches are:

- `drift/intune`
- `drift/entra`

Reviewers approve or reject drift through Azure DevOps pull requests. The system is intentionally ex-post change management: admins may make changes in the Microsoft admin portals, and this system detects, records, and routes those changes for review.

### Review Sync

The review-sync pipeline runs every 20 minutes on `main`.

It can:

- refresh automated PR summaries,
- process reviewer `/reject` or `/accept` commands in policy threads,
- optionally queue remediation after merge if rejected items were merged out of the PR scope.

### Restore

The restore pipeline is the only path that writes configuration back to the tenant.

It supports:

- full restore from `main`,
- selective restore of specific policy files,
- restore from a historical Git ref for rollback use cases,
- dry-run mode for report-only validation.

## Data Processed

### Data Categories

| Category | Examples | Source | Stored In |
| --- | --- | --- | --- |
| Intune configuration objects | compliance policies, device configurations, settings catalog, enrollment profiles, apps, scripts, filters, scope tags | Microsoft Graph / IntuneCD export | Git repo under `tenant-state/intune/**` |
| Entra configuration objects | conditional access, named locations, authentication strengths, app registrations, enterprise applications | Microsoft Graph | Git repo under `tenant-state/entra/**` |
| Generated reports | assignment inventories, object inventories, app inventories | Derived from exported configuration | `tenant-state/reports/**` and build artifacts |
| Documentation artifacts | split markdown, optional HTML/PDF | Derived from exported configuration | build artifacts |
| Review metadata | PR descriptions, review threads, accept/reject commands | Azure DevOps reviewers | Azure DevOps PR APIs |
| Probe state | debouncer state (timestamps, enum values) | Derived from audit log evaluation | Azure Table Storage (`ProbeState`) |
| Optional AI summary payload | sampled changed paths, semantic change descriptions, deterministic summary, fingerprints | Derived from repo diff | Azure OpenAI request payload |

### Data Sensitivity Notes

- The system is designed for administrative configuration data, not end-user business content.
- The repository can still contain sensitive operational material, including policy logic, group names, app identifiers, script bodies, custom configuration payloads, and administrator email addresses present in tenant configuration.
- If tenant-authored scripts or custom payloads contain embedded secrets, those secrets would also be captured. This is a customer governance risk, not something the exporter can reliably prevent.
- For that reason, the repository, drift branches, build logs, and published artifacts should all be treated as confidential administrative data.
- The same sensitivity assumptions apply to any AI summary payload because it is derived from the same administrative configuration changes.

## Authentication and Authorization

### Azure to Microsoft Graph

The pipelines obtain a Microsoft Graph access token at runtime using the Azure DevOps service connection configured in `SERVICE_CONNECTION_NAME` (e.g. `sc-astral-backup`).

The change probe uses a **separate Entra app registration** (`ASTRAL Change Probe`) with its own client credentials to authenticate to Microsoft Graph for audit log polling. This app is created by `deploy/provision-change-probe.ps1` and is distinct from the pipeline service connection identity.

Observed controls in the implementation:

- token acquisition is performed at runtime with `Get-AzAccessToken`,
- token role claims are inspected before proceeding,
- the token is stored as a secret pipeline variable (`issecret=true`),
- missing required Graph roles cause early failure.

### Azure DevOps API Access

The pipelines use `System.AccessToken` for:

- creating and updating rolling PRs,
- reading and updating PR threads,
- queuing the restore pipeline.

The repository permissions documented in the implementation are:

- contribute,
- create branch,
- force push,
- create/update pull requests,
- optional create tag.

If restore auto-queue is enabled, the pipeline identity also needs:

- `View builds`,
- `Queue builds`,
- explicit pipeline authorization when enforced by the project.

### Graph Permissions by Mode

#### Backup / Review Mode

Read-oriented Graph application permissions documented in the repository:

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
- `Application.Read.All` for Entra app exports
- `RoleManagement.Read.Directory` or `Directory.Read.All` for richer enrichment
- `AuditLog.Read.All` if commit author attribution is desired

#### Change Probe Mode

The probe app registration requires these read-only Graph application permissions:

- `AuditLog.Read.All` (reads directory and Intune audit logs)
- `DeviceManagementApps.Read.All`
- `DeviceManagementConfiguration.Read.All`
- `DeviceManagementManagedDevices.Read.All`
- `Policy.Read.All`
- `Policy.Read.ConditionalAccess`
- `Application.Read.All`

The probe does **not** require write permissions. It only polls audit logs and queues the backup pipeline.

#### Restore Mode

Write-capable Graph application permissions documented in the repository:

- `DeviceManagementApps.ReadWrite.All`
- `DeviceManagementConfiguration.ReadWrite.All`
- `DeviceManagementManagedDevices.ReadWrite.All`
- `DeviceManagementRBAC.ReadWrite.All`
- `DeviceManagementScripts.ReadWrite.All`
- `DeviceManagementServiceConfig.ReadWrite.All`
- `Group.Read.All`
- `Policy.Read.All`
- `Policy.ReadWrite.ConditionalAccess` when Entra updates are included

## Security Controls Present in the Implementation

### Network Exposure

- No inbound application endpoint is created by this repository.
- The system is pipeline-driven and relies on outbound HTTPS calls.
- Required outbound destinations are:
  - `graph.microsoft.com`
  - Azure DevOps organization APIs
  - Azure Table Storage (for probe state)
  - Azure Queue Storage (for probe trigger messages)
  - optional Azure OpenAI endpoint
  - Python package registry for `IntuneCD`
  - npm registry for `md-to-pdf`
  - optional OS package repositories when HTML/PDF conversion needs Chromium libraries

### Secrets Handling

- Graph tokens are obtained just-in-time rather than stored in the repository.
- The pipeline marks the Graph token as a secret variable.
- The implementation logs token claims and roles for diagnostics, but not the token value itself.
- The change probe app secret is stored as an Azure Function App setting (`PROBE_APP_SECRET`), not in the repository.
- Azure OpenAI uses a pipeline secret variable when enabled.
- The pipeline logic itself does not depend on repository-stored application secrets; separate secret scanning of exported tenant content is still recommended.

### Change Control

- Drift is committed to dedicated rolling branches rather than directly to `main`.
- Review happens through rolling pull requests into `main`.
- The implementation can delay reviewer notification by creating new rolling PRs as drafts until the automated summary block is present, reducing generic first-notification content.
- Optional file-level change tickets can be enforced through auto-created PR threads.
- Reviewers can explicitly accept or reject individual configuration files.
- Generated reports are excluded from drift commits and PR diffs to reduce review noise.

### Safety Checks

- Backup jobs validate expected outputs before committing drift.
- Intune backup logic checks for unauthorized Graph 403 responses and fails unless the failure is explicitly allowed by configuration.
- Entra export logic is configured to fail on requested export errors to avoid partial snapshots.
- Restore validates required write permissions before running.
- Selective restore sanitizes requested paths and rejects path traversal or missing-file conditions.
- Restore supports dry-run mode before any tenant change is applied.

### Auditability

- Git history retains approved baseline snapshots.
- Rolling PR history provides reviewer decisions and rationale.
- Azure DevOps build history records pipeline runs and restoration events.
- Optional tags can be created for snapshots.

## Optional Azure OpenAI Integration

Azure OpenAI is used only for PR review narrative generation.

Important scoping facts from the implementation:

- the feature is optional and controlled by pipeline variables,
- the core backup/review/restore workflow does not depend on it,
- it can remain disabled in every deployment mode,
- only a reduced, budget-limited change payload is sent,
- the payload contains changed paths, semantic summaries, risk labels, fingerprints, and deterministic summary text,
- it does not need direct Microsoft Graph access,
- it can be disabled with `ENABLE_PR_AI_SUMMARY=false`.

### Intended AI Deployment Posture

The intended security posture for AI is not an opaque third-party black-box service. The implementation is designed to use a customer-controlled Azure OpenAI deployment defined by:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT`
- `AZURE_OPENAI_API_KEY`

In the intended production design:

- AI requests are sent to the customer's Azure OpenAI resource,
- the model endpoint is explicitly configured by the customer,
- the AI service is a bounded summarization component rather than a system of record,
- Graph access remains with the pipeline and is not delegated to the model.

For formal security documentation, the safest statement is:

- the system is intended to use customer-managed Azure OpenAI infrastructure, typically within the same Azure tenant or controlled Azure environment, rather than an unrelated public AI service.

### AI Security Considerations

From a security perspective, the AI feature changes the system in these specific ways:

- it introduces an additional outbound destination: the configured Azure OpenAI endpoint,
- it sends a derived review payload based on configuration drift rather than raw tenant-wide exports,
- it does not grant the AI service direct credentials to Microsoft Graph or Azure DevOps,
- it is advisory only and does not approve, merge, reject, or restore changes by itself,
- it can be disabled independently of the rest of the platform.

### AI Business Purpose

The AI summaries exist to make technical Intune and Entra drift understandable to non-technical reviewers.

Their intended audience includes:

- project managers,
- delivery leads,
- security managers,
- customer management stakeholders,
- reviewers who own risk acceptance but do not work daily with raw policy JSON.

The purpose is not to replace technical review. The purpose is to provide a manager-readable explanation of:

- what changed,
- why it matters operationally,
- whether the change appears routine, risky, or potentially security-relevant,
- what a reviewer should verify before approval.

This allows management or PM stakeholders to participate meaningfully in review without needing to parse raw technical policy structures.

## Residual Risks and Customer Decisions

The following items are not fully solved by the repository alone and should be addressed in the customer deployment decision:

| Area | Current State | Recommended Position |
| --- | --- | --- |
| Restore capability | Supported by design; can change production tenant state | Keep restore manual only, or disable auto-remediation by default until operational controls are approved |
| Backup vs restore identity separation | Sample config uses the same service connection name in backup and restore pipelines | Use separate service principals: read-only for backup/review, write-enabled only for restore |
| Change probe identity separation | Probe uses a separate Entra app registration from the pipeline service connection | Keep probe app read-only; do not grant write permissions to the probe identity |
| Azure OpenAI egress | Optional and customer-configurable | Enable only when the organization approves the payload scope and Azure OpenAI deployment model |
| Artifact retention | Not defined in repo; inherited from Azure DevOps settings | Set explicit retention for builds, logs, and artifacts |
| Repo access model | Not defined in repo | Restrict repo and artifact access to administrators/reviewers only |
| Build agent hardening | Pool name exists, but agent type and hardening are deployment-specific | Prefer dedicated hardened agent or approved Microsoft-hosted configuration |
| Runtime package download | `pip`, `npm`, and sometimes `apt-get` are used during pipeline runs | Pre-bake dependencies into the agent image if customer forbids runtime internet package fetches |
| Secret content inside exported scripts | Possible if tenant admins embed secrets in Intune scripts or custom payloads | Review tenant script hygiene before onboarding |

## Recommended Deployment Configuration

For a conservative production deployment, use this profile:

1. Enable backup and review workflows.
2. Enable Azure OpenAI summaries only when a customer-controlled Azure OpenAI deployment is approved.
3. Disable automatic remediation queueing.
4. Do not authorize the restore pipeline for automatic queueing.
5. Use a read-only Graph application identity for backup/review.
6. Keep restore on a separate manual path with a separate write-enabled identity.
7. Apply Azure DevOps branch policies so `main` requires reviewer approval.
8. Set explicit retention and access-control policies for:
   - Git repository
   - build logs
   - markdown/HTML/PDF artifacts

Suggested conservative variable posture:

```text
ENABLE_PR_AI_SUMMARY=<true|false according to approved deployment mode>
AUTO_REMEDIATE_ON_PR_REJECTION=false
AUTO_REMEDIATE_AFTER_MERGE=false
REQUIRE_CHANGE_TICKETS=true
```

## Out of Scope

This repository does not provide:

- endpoint malware protection,
- customer device telemetry collection,
- user authentication to a SaaS application,
- network ingress services,
- a standalone secrets vault,
- customer-managed key support within the application itself.

Those controls, where needed, come from Azure DevOps, Microsoft 365 / Entra, the chosen agent environment, and the customer's broader platform governance.

## Customer-Specific Items to Fill Before Sending

The following are deployment-specific and should be completed with the actual customer environment:

- Azure DevOps organization and project name
- whether the agent pool is Microsoft-hosted or self-hosted
- repo retention period
- build log retention period
- artifact retention period
- named reviewer groups and branch policies
- exact service principal names used for backup and restore
- which Azure OpenAI resource and deployment are used, if AI is enabled
- whether restore is manual-only or fully enabled

## Repository Evidence

The statements in this document are based on the implementation in:

- `README.md`
- `azure-pipelines.yml`
- `azure-pipelines-review-sync.yml`
- `azure-pipelines-restore.yml`
- `scripts/update_pr_review_summary.py`
- `scripts/apply_reviewer_rejections.py`
- `scripts/queue_post_merge_restore.py`
- `scripts/export_entra_baseline.py`
- `scripts/probe_tenant_changes.py`
- `scripts/trigger_backup_pipeline.py`
- `infra/change-probe/probe_timer/__init__.py`
- `infra/change-probe/queue_consumer/__init__.py`
