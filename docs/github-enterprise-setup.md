# ASTRAL on GitHub Enterprise Server

This guide describes how to run ASTRAL with GitHub Actions on GitHub Enterprise Server (GHES), for example `https://github.ssd.sk`. Azure DevOps stays supported; both platforms run the same scripts.

Tested target: GHES 3.22 behind Microsoft Entra Application Proxy.

## What runs where

| Workflow | Trigger | Does | Tenant access |
| --- | --- | --- | --- |
| `astral-backup.yml` | daily 02:00 UTC, manual, change probe | Export Intune and Entra, commit drift to `drift/intune` / `drift/entra`, create/refresh one rolling PR per workload | Read (backup identity) |
| `astral-review.yml` | called by backup and review sync | Rolling PR, review summary, reviewer `/reject` decisions, post-merge remediation dispatch | None |
| `astral-review-sync.yml` | every 20 minutes, manual | Calls `astral-review.yml` for both workloads | None |
| `astral-probe.yml` | every 5 minutes, manual | Polls audit logs; dispatches a backup when changes settle (15 min quiet, 30 min cooldown) | Read audit logs (backup identity) |
| `astral-restore.yml` | manual or auto-remediation dispatch | Restores an approved baseline to the tenant (dry run by default) | **Write** (restore identity) |

The PR scripts use Azure DevOps REST calls. On GitHub, `scripts/forge_compat.py` translates them to the GitHub API:

| Azure DevOps | GitHub |
| --- | --- |
| Pull request | Pull request |
| "Change needed" ticket thread | File-level review comment thread (reply `/reject` or `/accept` in the thread) |
| Other PR threads (summary, AI narrative, guide) | PR comment, updated in place |
| Reviewer vote **Reject** | Review **Request changes** |
| Queue restore pipeline | `workflow_dispatch` of `astral-restore.yml` |

The change probe runs as a workflow, not as an Azure Function. Application Proxy pre-authentication blocks inbound calls to the GHES API from Azure, and the workflow needs no PAT and no client secret.

## 1. Runners

GHES has no GitHub-hosted runners. Use self-hosted runners.

1. Use **ephemeral** runners (one job per runner instance). IntuneCD receives the Graph token as a command-line argument, so a shared runner exposes it to other processes on the host.
2. Create two runner groups:
   - `astral`: label `astral`. Used by backup, review and probe.
   - `astral-restore`: label `astral-restore`. Used by restore only.
3. Restrict each runner group to this repository. Also restrict it to the ASTRAL workflows on `refs/heads/main` (runner group → *Workflow access* → selected workflows).
4. Install on the runner image: `git`, `python3` (3.11+ with `venv`), PowerShell 7.3+ (`pwsh`), `curl`.
5. Network access from the runners:
   - GHES on the internal network (split DNS). Runners must not go through Application Proxy.
   - `graph.microsoft.com`, `login.microsoftonline.com`
   - `pypi.org` / `files.pythonhosted.org` (IntuneCD), PowerShell Gallery (`Microsoft.Graph.DeviceManagement.Administration`), or internal mirrors

To use other labels, set the `ASTRAL_RUNS_ON` and `ASTRAL_RESTORE_RUNS_ON` variables to a JSON array, for example `["self-hosted","linux","astral"]`.

## 2. Identities (Microsoft Graph)

Use **two** Entra identities:

| Identity | Used by | Graph application permissions |
| --- | --- | --- |
| Backup | backup, probe | read permissions from `deploy/bootstrap-tenant.ps1`, plus `AuditLog.Read.All` |
| Restore | restore only | the restore permissions from `deploy/bootstrap-tenant.ps1 -AddRestorePermissions` |

The restore identity can change device configuration, run scripts on managed devices, and change Conditional Access. Give it to nothing else.

Choose one way to get tokens. Set `GRAPH_AUTH_METHOD` accordingly.

### Option A: managed identity (runners on Azure VMs)

`GRAPH_AUTH_METHOD=managed-identity`

1. Create one user-assigned managed identity per runner group. Attach the backup identity only to `astral` runners and the restore identity only to `astral-restore` runners.
2. Grant the Graph application permissions to each managed identity (app role assignments on its service principal).
3. Set `AZURE_CLIENT_ID` to the managed identity client ID: in environment `astral-backup` for the backup identity and in environment `astral-restore` for the restore identity.

**Important:** every job that runs on a runner can get that runner's token. With this option, the runner group restrictions in section 1 are the control that protects the identity.

### Option B: GitHub OIDC federation (runners anywhere)

`GRAPH_AUTH_METHOD=github-oidc`

1. Entra must read the GHES OIDC discovery document without signing in. In Application Proxy, publish **only** `https://github.ssd.sk/_services/token/.well-known/` with pre-authentication set to *Passthrough*. This path contains only the discovery document and public signing keys. Keep pre-authentication on everything else.
2. Add a federated credential to each app registration:
   - Issuer: `https://github.ssd.sk/_services/token`
   - Audience: `api://AzureADTokenExchange`
   - Subject (backup app): `repo:<org>/<repo>:environment:astral-backup`
   - Subject (restore app): `repo:<org>/<repo>:environment:astral-restore`
3. Set `AZURE_TENANT_ID` (repository variable) and `AZURE_CLIENT_ID` (environment variable, per environment).

A token is then issued only to jobs that run in the named environment, and the environments in section 4 accept only `main`. This is stronger than option A, because it does not depend on runner placement.

## 3. Repository settings

1. **Actions → General → Workflow permissions:** *Read repository contents*. The workflows ask for more per job.
2. Enable **Allow GitHub Actions to create and approve pull requests**. This is required to open the rolling PRs. Because it also lets workflows approve PRs, require Code Owner review (step 4); the bot is not a code owner.
3. **Ruleset for `main`:** require a pull request and 1 approval, require review from Code Owners, dismiss stale approvals, block force pushes and deletion, no bypass list.
4. **CODEOWNERS** (`.github/CODEOWNERS`), for example:

   ```text
   *            @<org>/astral-reviewers
   /.github/    @<org>/astral-admins
   /scripts/    @<org>/astral-admins
   /templates/  @<org>/astral-admins
   ```

5. **Ruleset for `drift/*`:** restrict updates and deletion to repository admins (the workflows push with `GITHUB_TOKEN`).

## 4. Environments

| Environment | Deployment branches | Protection | Variables | Secrets |
| --- | --- | --- | --- | --- |
| `astral-backup` | `main` only | none | `AZURE_CLIENT_ID` (backup identity) | `AZURE_OPENAI_API_KEY` (if AI summaries are on) |
| `astral-restore` | `main` only | required reviewers (restore approvers), prevent self-review | `AZURE_CLIENT_ID` (restore identity) | none |

Every restore, including an auto-remediation dispatch, waits for an approval in `astral-restore`.

Backup, review and probe runs appear as deployments to `astral-backup`. That is expected.

## 5. Repository variables

Only repository admins can change these. Change them as carefully as code.

| Variable | Default | Notes |
| --- | --- | --- |
| `GRAPH_AUTH_METHOD` | — | `managed-identity` or `github-oidc` (required) |
| `AZURE_TENANT_ID` | — | Required for `github-oidc` |
| `PR_DECISION_ALLOWED_REVIEWERS` | — | GHES logins whose `/reject`, `/accept` and *Request changes* reviews count. Empty = decisions ignored. Edited comments never count, because users with write access can edit other users' comments. |
| `BASELINE_BRANCH` | `main` | Also the only branch that restore accepts history from |
| `ENABLE_WORKLOAD_INTUNE` / `ENABLE_WORKLOAD_ENTRA` | `true` | |
| `BACKUP_TIMEZONE` / `FULL_RUN_HOUR` | `UTC` / `02` | The scheduled 02:00 UTC run is the full run |
| `ENABLE_PR_AI_SUMMARY` | `false` | Also set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` |
| `AUTO_REMEDIATE_ON_PR_REJECTION` / `AUTO_REMEDIATE_AFTER_MERGE` | `false` | Dispatches `astral-restore.yml`, which still needs approval |
| `AUTO_REMEDIATE_DRY_RUN` | `true` | |
| `USER_NAME` / `USER_EMAIL` | service defaults | Commit identity when no audit-log author is found |
| `ASTRAL_RUNS_ON` / `ASTRAL_RESTORE_RUNS_ON` | `["self-hosted","astral"]` / `["self-hosted","astral-restore"]` | Runner labels |
| `PROBE_QUIET_WINDOW_MINUTES` / `PROBE_COOLDOWN_MINUTES` | `15` / `30` | |
| `ENTRA_INCLUDE_NAMED_LOCATIONS` / `ENTRA_INCLUDE_AUTHENTICATION_STRENGTHS` / `ENTRA_INCLUDE_CONDITIONAL_ACCESS` | `true` | Entra categories to export |
| `ENTRA_INCLUDE_ENTERPRISE_APPS` / `ENTRA_INCLUDE_APP_REGISTRATIONS` | `true` | Exported in full runs only |
| `ENTRA_INCLUDE_GROUPS` / `ENTRA_INCLUDE_ROLE_ASSIGNMENTS` / `ENTRA_INCLUDE_AUTH_METHODS_POLICY` / `ENTRA_INCLUDE_CROSS_TENANT_ACCESS` / `ENTRA_INCLUDE_IDENTITY_PROTECTION` / `ENTRA_INCLUDE_PRIVILEGED_GROUPS` / `ENTRA_INCLUDE_PIM_POLICIES` / `ENTRA_INCLUDE_SECURITY_SETTINGS` | `true` | Entra identity exports (`export_entra_identity.py`) |
| `ENTRA_WATCH_GROUPS_CSV` | empty | Group IDs whose full member list is tracked in Git; other groups get counts only |
| `ENTRA_ENTERPRISE_APP_WORKERS` | `8` | Parallel Graph lookups for Enterprise Applications (1-32) |
| `ENABLE_ENTRA_CONDITIONAL_ACCESS` | `true` | `true` means the Entra workload owns Conditional Access and the Intune export skips it |
| `EXCLUDE_SCRIPT_BACKUP` | `false` | Skip Intune script categories (also relaxes the script permission check) |
| `INTUNE_EXCLUDE_CSV` | — | Extra IntuneCD categories to exclude (comma-separated) |
| `ROLLING_PR_MERGE_STRATEGY` | `rebase` | `rebase`, `squash`, `noFastForward` or `rebaseMerge` |
| `AUTO_REMEDIATE_RESTORE_WORKFLOW` | `astral-restore.yml` | Workflow file the auto-remediation dispatches |

Other variables from `templates/variables-common.yml` and the `variables:` block of `azure-pipelines.yml` (for example `REQUIRE_CHANGE_TICKETS`) can be set with the same names.

## 6. Security controls built into the workflows

- `permissions: {}` at workflow level. Each job asks only for what it needs; restore has `contents: read` and no stored git credentials.
- Workflow inputs and comment text never appear inside `run:` scripts. They reach scripts through `env:` only.
- Restore accepts only commits in the `BASELINE_BRANCH` history, and only plain ref names (no git option injection).
- Graph tokens are masked before any output and checked for the required roles before use.
- Paths, authors and commit messages from tenant data reach git through files and environment variables, never as command-line text.
- Third-party actions are pinned to a commit SHA (`actions/checkout`), not a tag. To update one, resolve the new tag with `git ls-remote https://github.com/actions/checkout 'refs/tags/vX.Y.Z'` and change the SHA and its version comment together.
- No workflow runs on comment or pull request events, so commenters cannot start runs that hold write tokens.

## 7. Not yet available on GitHub

- HTML/PDF as-built documentation artifacts (Azure DevOps `publish` job).
- MCP server hosted in Azure reading from GHES: Application Proxy blocks it. Run the MCP server inside the network with `ASTRAL_REPO_URL` pointing at GHES (see `infra/mcp-server/README.md`).
- Snapshot tagging (`ENABLE_TAGGING`).
