# ASTRAL Onboarding Runbook

This guide walks through deploying ASTRAL into a new Azure DevOps organization and Microsoft 365 tenant.

## Prerequisites

- Azure DevOps organization and project created.
- Owner or Contributor access to the target Microsoft 365 tenant.
- Permission to create app registrations and grant admin consent in Entra ID.
- PowerShell 7+ or Windows PowerShell 5.1 with the `Microsoft.Graph` module (for the bootstrap script).

## Step 1: Import the repository

1. In Azure DevOps, create a new Git repository in your project.
2. Push the contents of this repository into it, or use **Import repository** from a public Git URL.

## Step 2: Create the tenant variable group

1. In Azure DevOps, go to **Pipelines > Library** and create a new Variable Group.
2. Recommended name: `vg-astral-tenant` (you can choose any name).
3. Add the variables from `templates/variables-tenant.yml`. Use your real tenant values:

   | Variable | Example value | Notes |
   | --- | --- | --- |
   | `TENANT_NAME` | `contoso.onmicrosoft.com` | Your M365 tenant domain |
   | `SERVICE_CONNECTION_NAME` | `sc-astral-backup` | Name you will use for the service connection |
   | `USER_NAME` | `ASTRAL Backup Service` | Git committer name |
   | `USER_EMAIL` | `astral-backup@contoso.com` | Git committer email |
   | `AGENT_POOL_NAME` | `Azure Pipelines` | Change if using a self-hosted pool |
   | `BACKUP_TIMEZONE` | `Europe/Prague` | Valid tz database name |
   | `FULL_RUN_HOUR` | `00` | Hour that triggers full export |
   | `AUTO_REMEDIATE_RESTORE_PIPELINE_ID` | *(leave empty)* | Filled in Step 8 |

4. If you plan to use Azure OpenAI summaries, also add:
   - `ENABLE_PR_AI_SUMMARY` = `true`
   - `AZURE_OPENAI_ENDPOINT`
   - `AZURE_OPENAI_DEPLOYMENT`
   - `AZURE_OPENAI_API_KEY` *(mark as secret)*

## Step 3: Link the variable group to the pipelines

Open each pipeline YAML and uncomment the variable group line near the top:

```yaml
variables:
  - group: vg-astral-tenant   # <-- uncomment this line
  - template: templates/variables-common.yml
```

Do this for:
- `azure-pipelines.yml`
- `azure-pipelines-review-sync.yml`
- `azure-pipelines-restore.yml`

Commit and push the changes.

## Step 4: Run the tenant bootstrap script

Run `deploy/bootstrap-tenant.ps1` in a PowerShell session authenticated to your target tenant.

```powershell
# Example
.\deploy\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ServiceConnectionName "sc-astral-backup"
```

The script will:
1. Create a single-tenant app registration.
2. Add required Microsoft Graph application permissions.
3. Grant admin consent.
4. Create a workload federated credential for Azure DevOps.
5. Print the App ID and instructions for creating the Azure DevOps service connection.

## Step 5: Create the Azure DevOps service connection

1. In Azure DevOps, go to **Project settings > Service connections**.
2. Click **New service connection > Azure Resource Manager > Workload identity federation (manual)**.
3. Fill in:
   - **Subscription**: leave blank or select if you also want ARM access (not required).
   - **Tenant ID**: your Microsoft 365 tenant ID.
   - **Service Connection Name**: the same value you set in `SERVICE_CONNECTION_NAME` (e.g. `sc-astral-backup`).
   - **App ID**: from the bootstrap script output.
4. Save the service connection.

## Step 6: Import the pipelines

1. Go to **Pipelines > Create pipeline > Azure Repos Git**.
2. Select your repository.
3. Choose **Existing Azure Pipelines YAML file**.
4. Import each of the three YAMLs one by one:
   - `azure-pipelines.yml` (main backup)
   - `azure-pipelines-review-sync.yml` (review sync)
   - `azure-pipelines-restore.yml` (restore)

## Step 7: Grant repository permissions to the build identity

1. Go to **Project settings > Repositories**.
2. Select your repository.
3. Under **Security**, grant the **Build Service** account:
   - Contribute
   - Create branch
   - Force push
   - Create pull request
   - Edit pull request
   - Tag creation (if you enable tagging)

4. Under **Pipelines**, grant the build service **Queue builds** permission on `azure-pipelines-restore.yml` if you plan to use auto-remediation.

## Step 8: Set the restore pipeline definition ID

After importing `azure-pipelines-restore.yml`, find its definition ID:

1. Open the restore pipeline in Azure DevOps.
2. The URL contains `definitionId=XX`. Note the number.
3. Go back to your variable group (`vg-astral-tenant`) and set:
   - `AUTO_REMEDIATE_RESTORE_PIPELINE_ID` = `XX`

## Step 9: Validate the deployment

1. Import `deploy/validate-deployment.yml` as a one-time pipeline.
2. Run it.
3. Verify that all checks pass:
   - Graph token acquisition
   - Required roles present
   - Test read from Graph
   - Test PR creation and abandonment

## Step 10: Run the first backup

1. Queue a manual run of `azure-pipelines.yml`.
2. Set `forceFullRun=true` to get a complete initial snapshot.
3. Verify that `tenant-state/` is populated and a rolling PR is created.

## Step 11: Provision the event-driven change probe (optional but recommended)

The change probe replaces the previous hourly polling model with responsive, event-driven backup triggers.

### Option A: Automated provisioning

Run the unified provisioning script:

```powershell
.\deploy\provision-change-probe.ps1 `
  -TenantName "contoso.onmicrosoft.com" `
  -ResourceGroupName "rg-astral-probe" `
  -Location "westeurope" `
  -DeployFunctionApp
```

The script will create an Entra app, grant admin consent, provision Azure resources, and deploy the Function App.

### Option B: Manual provisioning

If you prefer manual setup:

1. **Create an app registration** in Entra ID for the probe.
2. **Grant admin consent** for:
   - `DeviceManagementConfiguration.Read.All`
   - `DeviceManagementApps.Read.All`
   - `AuditLog.Read.All`
   - `Directory.Read.All`
3. **Create a client secret** and note the value.
4. **Provision Azure resources**:
   - Resource Group
   - Storage Account (Standard LRS)
   - Function App (Linux Consumption, Python 3.11)
5. **Configure Function App settings**:
   | Setting | Value |
   |---|---|
   | `AzureWebJobsStorage` | Storage account connection string |
   | `PROBE_APP_ID` | App registration client ID |
   | `PROBE_APP_SECRET` | App registration client secret |
   | `TENANT_ID` | Your Microsoft 365 tenant ID |
   | `ADO_ORGANIZATION` | Your Azure DevOps org name |
   | `ADO_PROJECT` | Your Azure DevOps project name |
   | `ADO_PIPELINE_ID` | Definition ID of `azure-pipelines.yml` |
   | `ADO_TOKEN` | Azure DevOps PAT with **Build (read & execute)** |
   | `ADO_BRANCH` | `main` (or your baseline branch) |
6. **Deploy the function package** using `WEBSITE_RUN_FROM_PACKAGE` (see `infra/change-probe/README.md`).

### Verify the probe

1. Make a test change in Intune (e.g., create a temporary device configuration profile).
2. Wait 5–20 minutes for the audit log to propagate.
3. Check the `ProbeState` table in your Storage Account — the `singleton/default` entity should show `debouncer.state = armed`.
4. After the quiet window (default 15 min) elapses, a queue message will be emitted.
5. The `queue_consumer` will dequeue it and queue the backup pipeline.
6. Verify the pipeline run appears in Azure DevOps with reason `manual` (API-triggered runs show as manual).

> **Note:** The probe uses the same Entra app as the main backup pipeline. You can reuse the app registration created by `bootstrap-tenant.ps1` if you add the `AuditLog.Read.All` permission and create a client secret for it.

## Optional: progressive feature rollout

| Phase | What to enable |
| --- | --- |
| Backup-only | `ENABLE_PR_REVIEW_SUMMARY=false`, `ENABLE_PR_REVIEWER_DECISIONS=false`, `AUTO_REMEDIATE_AFTER_MERGE=false` |
| Review package | `ENABLE_PR_REVIEW_SUMMARY=true`, `ENABLE_PR_REVIEWER_DECISIONS=true` |
| Full package | Also enable restore and set `AUTO_REMEDIATE_AFTER_MERGE=true` if desired |
| AI summaries | `ENABLE_PR_AI_SUMMARY=true` plus Azure OpenAI variables |

## Staying up to date

ASTRAL releases are published to [https://github.com/cqrenet/astral/releases](https://github.com/cqrenet/astral/releases). Watch the repository on GitHub to receive release notifications.

### Import the update pipeline (one-time setup)

Import `deploy/update-from-upstream.yml` into your ADO project as a pipeline. You only need to do this once — the pipeline is then available to run whenever you want to apply an update.

In Azure DevOps: **Pipelines → Create pipeline → Azure Repos Git → Existing YAML file** → select `deploy/update-from-upstream.yml`.

Grant the build service **Contribute** and **Force push** permissions on the repository if not already granted (same permissions used by the backup pipeline).

### Applying an update

1. Check the [release notes](https://github.com/cqrenet/astral/releases) for breaking changes or required variable group changes.
2. Queue the `update-from-upstream.yml` pipeline:
   - Leave **Upstream ref** blank to pull the latest `main`.
   - Or enter a specific tag (e.g. `v1.2.0`) to pin to a release.
   - Run with **Dry run** checked first to see what will change before committing.
3. If the merge completes cleanly, the pipeline pushes directly to your `main` branch.
4. If there are conflicts (typically in pipeline YAML files where your variable group name differs from upstream defaults), the pipeline stops and lists the conflicting files. Resolve them locally and push manually.

### Pinned version upgrades

If you prefer explicit version control over tracking `main`, always specify a tag in the **Upstream ref** parameter. This means updates only happen when you decide to upgrade, and you can review the full changelog between your current version and the target tag before applying.

To see what tag you are currently on:
```bash
git describe --tags --abbrev=0
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Pipeline fails at "Get Graph Token" | Wrong service connection name or missing federated credential | Verify `SERVICE_CONNECTION_NAME` matches the service connection exactly |
| "Missing required Graph roles" | Admin consent not granted | Run bootstrap script again or grant consent manually in Entra ID |
| Rolling PR not created | Build identity lacks PR permissions | Add **Create pull request** and **Edit pull request** permissions |
| Restore pipeline queue fails | `AUTO_REMEDIATE_RESTORE_PIPELINE_ID` wrong or missing queue permission | Verify the ID and grant **Queue builds** on the restore pipeline |
| Empty `tenant-state/` after run | First run may have no data if Graph returns nothing; also check `BACKUP_FOLDER` path | Verify Graph permissions and re-run |
