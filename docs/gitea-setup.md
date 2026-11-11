# ASTRAL on Gitea

This guide describes how to run ASTRAL with Gitea Actions. Together with a Linux runner you control, nothing in the CI/CD path depends on Microsoft or GitHub infrastructure. Only Microsoft Graph itself remains, because that is where the tenant configuration lives.

Azure DevOps and GitHub stay supported. All three platforms run the same scripts.

Target: Gitea 1.24 or later with Actions enabled.

## What runs where

| Workflow (`.gitea/workflows/`) | Trigger | Does | Tenant access |
| --- | --- | --- | --- |
| `astral-backup.yml` | daily 02:00 UTC, manual, change probe | Export Intune and Entra, commit drift to `drift/intune` / `drift/entra`, create/refresh one rolling PR per workload | Read (backup identity) |
| `astral-review-sync.yml` | every 20 minutes, manual | Reviewer `/reject` decisions, PR summary, post-merge remediation dispatch | None |
| `astral-probe.yml` | every 5 minutes, manual | Polls audit logs; dispatches a backup when changes settle (15 min quiet, 30 min cooldown) | Read audit logs (backup identity) |
| `astral-restore.yml` | manual or auto-remediation dispatch | Restores an approved baseline to the tenant (dry run by default) | **Write** (restore identity) |

The workflows contain no `uses:` steps. Checkout is plain `git`, so the runner downloads nothing from github.com. The probe's debouncer state lives on the branch `astral-probe-state` as one force-pushed commit.

The PR scripts use Azure DevOps REST calls. On Gitea, `scripts/forge_compat.py` translates them to the Gitea API:

| Azure DevOps | Gitea |
| --- | --- |
| Pull request | Pull request (Gitea has no draft PRs; reviewers are notified when the PR opens) |
| "Change needed" ticket thread | PR comment. To reply in that thread, use **Quote reply** on the comment. |
| Other PR threads (summary, AI narrative, guide) | PR comment, updated in place |
| Reviewer vote **Reject** | Review **Request changes** |
| Queue restore pipeline | `workflow_dispatch` of `astral-restore.yml` |

## 1. Trust model: read this first

Gitea Actions **ignores `environment`**, and it has **no OIDC tokens**. That has two consequences:

1. **Repository secrets are readable by every workflow run in the repository**, including runs from a branch that someone pushed. Anyone with **write access** can therefore use the backup and restore credentials.
2. **No platform approval gate exists for restore.** `astral-restore.yml` checks in the workflow itself: a non-dry-run restore must be dispatched from `BASELINE_BRANCH` by a user listed in `RESTORE_APPROVERS`. A user with write access could change that check on their own branch, so it protects against mistakes and the Actions bot, not against a malicious writer.

So:

- Give **write access only to ASTRAL operators**. They are trusted with the restore credential anyway.
- Give **reviewers read access**. In Gitea they can still comment (`/reject`, quote replies) and submit reviews. Their approvals count for merging when they are in the protected-branch approvals whitelist.
- Keep auto-remediation off (the default). If you enable it, it stays dry-run unless the Actions bot is listed in `RESTORE_APPROVERS`; don't list it.
- Four-eyes approval of a restore (one person requests, another approves) is **not** enforced by Gitea. Use GitHub environments if you need it as a platform control.

## 2. Runners

1. Use `act_runner`, registered **at repository level**, so that other repositories cannot send jobs to it.
2. Register two runners, ideally on separate machines:
   - label `astral`: backup, review, probe
   - label `astral-restore`: restore only
3. Use host mode, or a container image that includes `git`, `python3` (3.11+ with `venv`), PowerShell 7.3+ (`pwsh`), `curl` and `openssl`.
4. Network access from the runners:
   - the Gitea instance
   - `graph.microsoft.com`, `login.microsoftonline.com`
   - `pypi.org` / `files.pythonhosted.org` (IntuneCD) and the PowerShell Gallery (`Microsoft.Graph.DeviceManagement.Administration`), or internal mirrors
5. Optional hardening: set `[actions] DEFAULT_ACTIONS_URL = self` in `app.ini`. The ASTRAL workflows don't need it, but other workflows then can't silently pull code from github.com.

## 3. Identities (Microsoft Graph)

Use two Entra app registrations:

| Identity | Used by | Graph application permissions |
| --- | --- | --- |
| Backup | backup, probe | read permissions from `deploy/bootstrap-tenant.ps1`, plus `AuditLog.Read.All` |
| Restore | restore only | the restore permissions from `deploy/bootstrap-tenant.ps1 -AddRestorePermissions` |

Gitea has no OIDC, so use a **certificate** (recommended) or a client secret:

```bash
openssl req -x509 -newkey rsa:3072 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=astral-backup"
# Upload cert.pem to the app registration (Certificates & secrets).
cat key.pem cert.pem   # store this output as the Gitea secret AZURE_CLIENT_CERTIFICATE
```

Do the same for the restore app and store the result as `RESTORE_AZURE_CLIENT_CERTIFICATE`. Delete the local key files afterwards. Give the certificates an expiry date and rotate them.

If the runner is an Azure VM, `GRAPH_AUTH_METHOD=managed-identity` also works and needs no secret.

## 4. Repository settings

1. **Actions:** enabled. Under the repository's Actions settings, allow the Actions token to write. The workflows then limit it per job with `permissions:`.
2. **Branch protection for `main`:** require pull requests, require 1 approval from the approvals whitelist (your reviewers team), block merging on rejected reviews, dismiss stale approvals, and disable force push.
3. Leave `drift/*` and `astral-probe-state` unprotected. The Actions token force-pushes them.

## 5. Variables and secrets

Repository **variables**:

| Variable | Default | Notes |
| --- | --- | --- |
| `GRAPH_AUTH_METHOD` | — | `client-certificate`, `client-secret` or `managed-identity` |
| `AZURE_TENANT_ID` | — | |
| `AZURE_CLIENT_ID` | — | Backup app |
| `RESTORE_AZURE_CLIENT_ID` | — | Restore app |
| `RESTORE_GRAPH_AUTH_METHOD` | `GRAPH_AUTH_METHOD` | Only if restore authenticates differently |
| `RESTORE_APPROVERS` | — | Gitea usernames allowed to run a non-dry-run restore. Empty = only dry runs. |
| `PR_DECISION_ALLOWED_REVIEWERS` | — | Gitea usernames whose `/reject`, `/accept` and *Request changes* reviews count. Empty = decisions ignored. Edited comments never count. |
| `BASELINE_BRANCH` | `main` | Also the only branch that restore accepts history from |
| `ENABLE_WORKLOAD_INTUNE` / `ENABLE_WORKLOAD_ENTRA` | `true` | |
| `BACKUP_TIMEZONE` / `FULL_RUN_HOUR` | `UTC` / `02` | The scheduled 02:00 UTC run is the full run |
| `AUTO_REMEDIATE_ON_PR_REJECTION` / `AUTO_REMEDIATE_AFTER_MERGE` | `false` | |
| `AUTO_REMEDIATE_DRY_RUN` | `true` | |
| `ENABLE_PR_AI_SUMMARY` | `false` | Also `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` |

Repository **secrets**:

| Secret | Notes |
| --- | --- |
| `AZURE_CLIENT_CERTIFICATE` or `AZURE_CLIENT_SECRET` | Backup app |
| `RESTORE_AZURE_CLIENT_CERTIFICATE` or `RESTORE_AZURE_CLIENT_SECRET` | Restore app |
| `AZURE_OPENAI_API_KEY` | Only with AI summaries |
| `ASTRAL_DISPATCH_TOKEN` | Optional. Only if your Gitea version does not let the Actions token dispatch workflows: a token with write access to this repository. |

## 6. Reviewing on Gitea

- **Accept all:** approve and merge the rolling PR.
- **Reject one file:** with `REQUIRE_CHANGE_TICKETS=true`, each changed policy gets a "Change needed" comment. Use **Quote reply** on it and write `/reject` (or `/accept`) on its own line. The quoted marker links your reply to that file. Review sync resets the file on the drift branch within about 20 minutes.
- **Reject the whole PR:** submit a review with **Request changes** (as an allowlisted reviewer), then close the PR.

## 7. Move an instance from Azure DevOps to Gitea

1. Merge or abandon the open rolling PRs in Azure DevOps.
2. Mirror the repository with history, branches and tags:

   ```bash
   git clone --mirror https://dev.azure.com/<org>/<project>/_git/<repo> astral.git
   cd astral.git && git push --mirror https://<gitea>/<owner>/astral.git
   ```

3. Configure runners, variables and secrets (sections 2–5) and branch protection.
4. Disable the Azure DevOps pipelines and the change-probe Function App, so that only one system writes the drift branches.
5. Run `astral-backup.yml` manually with `forceFullRun`, then `astral-restore.yml` with the default dry run.
6. MCP server: point it at Gitea with `ASTRAL_REPO_URL=https://<gitea>/<owner>/astral.git` and `ASTRAL_REPO_TOKEN=<read-only token>` (see `infra/mcp-server/README.md`).

## 8. Not yet available on Gitea

- HTML/PDF as-built documentation artifacts and snapshot tagging.
- Platform-enforced restore approval (see section 1).
