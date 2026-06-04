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
2. Recommended name: `vg-astral` (you can choose any name).
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
  - group: vg-astral   # <-- uncomment this line
  - template: templates/variables-common.yml
```

Do this for:
- `azure-pipelines.yml`
- `azure-pipelines-review-sync.yml`
- `azure-pipelines-restore.yml`
- `deploy/validate-deployment.yml`
- `deploy/update-from-upstream.yml`

Commit and push the changes.

## Steps 4 & 5: Bootstrap and service connection

There are two paths. **Path A (automatic)** is simpler — ADO creates the app registration for you. **Path B (manual)** gives you full control and works if you don't have Azure subscription Owner role.

---

### Path A: Automatic service connection (recommended)

**Step 4A — Create the service connection**

1. In Azure DevOps, go to **Project settings → Service connections**.
2. Click **New service connection → Azure Resource Manager → Next**.
3. Select **App registration (automatic)** with **Workload identity federation**. Click **Next**.
4. Select **Scope level: Subscription** and choose any subscription in your tenant (ASTRAL uses Microsoft Graph, not ARM resources — the subscription is required by the form only).
5. Enter the **Service connection name** matching `SERVICE_CONNECTION_NAME` (e.g. `sc-astral-backup`).
6. **Do not tick "Grant access permission to all pipelines"** — leave it unchecked. You will authorize only the three ASTRAL pipelines individually in Step 7.
7. Click **Save**. ADO creates the app registration and federated credential automatically.
8. On the service connection overview page, click **Manage App registration** — this opens the Entra app registration. Copy the **Application (client) ID** from there.

**Step 5A — Assign Graph permissions**

Run `deploy/bootstrap-tenant.ps1` with `-ExistingAppId` pointing to the ADO-created app:

```powershell
.\deploy\bootstrap-tenant.ps1 `
  -TenantName "contoso.onmicrosoft.com" `
  -ExistingAppId "<app-id-from-service-connection>"
```

The script assigns all required Microsoft Graph application permissions and grants admin consent. No app registration or federated credential is created — those already exist.

---

### Path B: Manual service connection

**Step 4B — Create the service connection draft**

1. In Azure DevOps, go to **Project settings → Service connections**.
2. Click **New service connection → Azure Resource Manager → Next**.
3. Select **App registration or Managed identity (manual)** with **Workload identity federation**. Click **Next**.
4. Enter the **Service connection name** matching `SERVICE_CONNECTION_NAME` (e.g. `sc-astral-backup`). Click **Next**.
5. In Step 2 (App registration details):
   - **Environment**: Azure Cloud
   - **Scope level**: Subscription — enter any valid subscription in your tenant
   - Leave **Application (client) ID** and **Directory (tenant) ID** blank for now
   - **Do not tick "Grant access permission to all pipelines"** — leave it unchecked. You will authorize only the three ASTRAL pipelines individually in Step 7.
6. **Copy the generated Issuer and Subject identifier** values shown by ADO.
7. Click **Keep as draft**.

**Step 5B — Bootstrap and add the federated credential**

Run `deploy/bootstrap-tenant.ps1` to create the app registration, assign permissions, and grant admin consent:

```powershell
.\deploy\bootstrap-tenant.ps1 `
  -TenantName "contoso.onmicrosoft.com" `
  -ServiceConnectionName "sc-astral-backup"
```

Note the **App ID** and **Tenant ID** from the output.

Then add the federated credential to the new app:

1. In the [Azure portal](https://portal.azure.com), open **Entra ID → App registrations** and find the app created by the script.
2. Go to **Certificates & secrets → Federated credentials → Add credential**.
3. Select scenario **Other issuer**.
4. Paste the **Issuer** and **Subject identifier** copied from ADO in Step 4B.
5. Set **Type** to **Explicit subject identifier**, give it a name (e.g. `astral-ado-federation`), and click **Add**.

**Complete the service connection**

1. Return to the ADO draft service connection.
2. Fill in **Application (client) ID** and **Directory (tenant) ID** from the bootstrap output.
3. Click **Finish setup → Verify and save**.

---

## Step 6: Import the pipelines

For each pipeline: **Pipelines → New pipeline → Azure Repos Git → select your repository → Existing Azure Pipelines YAML file → select the file → Save** (do not run yet). After saving, immediately rename the pipeline via **⋮ → Rename/move** — ADO defaults to the filename which is not descriptive.

| YAML file | Suggested pipeline name |
| --- | --- |
| `azure-pipelines.yml` | `ASTRAL — Backup` |
| `azure-pipelines-review-sync.yml` | `ASTRAL — Review Sync` |
| `azure-pipelines-restore.yml` | `ASTRAL — Restore` |

## Step 7: Grant repository permissions to the build identity

1. Go to **Project settings > Repositories**.
2. Select your repository.
3. Under **Security**, find **ASTRAL-[project] Build Service** and grant:
   - Contribute
   - Create branch
   - Force push (rewrite history, delete branches and tags)
   - Contribute to pull requests
   - Create tag (only if you enable snapshot tagging)

4. On the same **Security** page, scroll down to the **Pipeline permissions** section.
   Click **+** and add all three pipelines:
   - `ASTRAL — Main Backup`
   - `ASTRAL — Review Sync`
   - `ASTRAL — Restore`

   This is required because you did not tick "Grant access to all pipelines" when creating the service connection.

5. If you plan to use auto-remediation, also grant the build service **Queue builds** permission on the `ASTRAL — Restore` pipeline:
   Go to **Pipelines → ASTRAL — Restore → ⋮ → Manage security**, find the Build Service account and set **Queue builds** to Allow.

## Step 8: Set the restore pipeline definition ID

After importing `azure-pipelines-restore.yml`, find its definition ID:

1. Open the restore pipeline in Azure DevOps.
2. The URL contains `definitionId=XX`. Note the number.
3. Go back to your variable group (`vg-astral`) and set:
   - `AUTO_REMEDIATE_RESTORE_PIPELINE_ID` = `XX`

## Step 9: Validate the deployment

1. Import `deploy/validate-deployment.yml` as a one-time pipeline. Rename it to `ASTRAL — Validate`.
2. Run it.
3. The run will appear to hang in the pipeline list. Open the run by clicking on it, then look for the yellow banner **"This pipeline needs permission to access N resources before this run can continue"**. Click **View**, then **Permit** for each resource (typically the service connection and the variable group). The run will then proceed automatically.
4. Verify that all checks pass:
   - Graph token acquisition
   - Required roles present
   - Test read from Graph
   - Test PR creation and abandonment

> This permission prompt appears once per pipeline per resource. The three main pipelines will show the same prompt on their first run — approve them the same way.

## Step 10: Configure branch policies on main

Without branch policies, rolling PRs can be merged without any review, which defeats the purpose of the platform. This step protects `main` so that drift can only land after a human approves it.

1. Go to **Project settings → Repositories → ASTRAL → Policies**.
2. Under **Branch policies**, click **main**.
3. Enable and configure the following:

   **Require a minimum number of reviewers**
   - Minimum reviewers: `1` (or more depending on your team)
   - Check **Allow requestors to approve their own changes**: leave off — the pipeline creates the PR, reviewers should be humans
   - Check **Reset all approval votes when there are new changes**: on — ensures a re-review if drift is updated

   **Limit merge types**
   - Enable **Rebase and fast-forward** only (or whichever strategy matches `ROLLING_PR_MERGE_STRATEGY` in your variable group — default is `rebase`)
   - Disabling squash and basic merge keeps the Git history clean and preserves individual drift commits

4. Optionally, add **Required reviewers** to automatically add specific people or groups to every drift PR.

> The build service identity creates the rolling PRs. It is not a human reviewer — do not add it to any required reviewer group.

## Step 10b: Configure branch policies on drift branches (optional)

The drift branches (`drift/intune`, `drift/entra`) are written to directly by the pipeline. They should **not** have reviewer policies — the pipeline needs to force-push to them. No action needed here; this is just a reminder not to accidentally apply `main` policies to the drift branches.

## Step 11: Run the first backup

1. Queue a manual run of `azure-pipelines.yml`.
2. Set `forceFullRun=true` to get a complete initial snapshot.
3. Verify that `tenant-state/` is populated and a rolling PR is created.

## Step 12: Provision the event-driven change probe (optional but recommended)

The change probe replaces the previous hourly polling model with responsive, event-driven backup triggers.

### Option A: Automated provisioning

Run the unified provisioning script. It provisions the change probe (Azure Function App) and optionally the MCP server (Azure Container Apps) in the same run:

```powershell
# Deploy change probe + MCP server (recommended)
.\deploy\provision.ps1 `
  -ResourceGroup "rg-astral-probe" `
  -Location "westeurope" `
  -DeployMcpServer

# Deploy change probe only (skip MCP server)
.\deploy\provision.ps1 `
  -ResourceGroup "rg-astral-probe" `
  -Location "westeurope" `
  -SkipMcpServer

# Deploy MCP server only (change probe already exists)
.\deploy\provision.ps1 `
  -ResourceGroup "rg-astral-probe" `
  -Location "westeurope" `
  -DeployMcpOnly
```

If you run the script without `-DeployMcpServer` or `-SkipMcpServer` it will prompt interactively. The script creates an Entra app, grants admin consent, provisions Azure resources, and deploys the selected components.

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
