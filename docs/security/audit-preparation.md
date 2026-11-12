# Preparing for a security audit

What an auditor can verify from this repository is in [controls-and-evidence.md](./controls-and-evidence.md). The
items below depend on your environment. Collect evidence for each before the audit and keep it outside the repository
if it names your tenant.

## Evidence to collect

| Area | Show that | How to produce it |
|---|---|---|
| Repository | `main` is protected: reviews required, no force-push, no direct push, status checks required | Export of the branch protection or branch policy settings |
| Repository | Tags `v*` are protected | Tag protection or ruleset export |
| Repository | `tenant-state/` lives in a private repository with a named collaborator list, no public forks | Collaborator list and visibility setting |
| Reviewers | `PR_DECISION_ALLOWED_REVIEWERS` contains only intended people | Variable value, reviewed against the reviewer group |
| Pipelines | The restore pipeline or environment needs approval, and only the restore identity can write to the tenant | Environment checks, service connection permissions |
| Identities | The backup identity has no write permissions; the restore identity has no other use | Entra app permission list and role assignments for both |
| Federation | OIDC subject claims limit the identity to the intended repository, branch and environment | Federated credential configuration |
| Runners | Self-hosted runners are dedicated, ephemeral or cleaned, and cannot reach unrelated networks | Runner group settings, host baseline, egress rules |
| MCP | The endpoint rejects unauthenticated requests from the internet; internal ingress if clients are private | `curl` against `/sse` without credentials returns 401; ingress setting |
| MCP | The MCP token has Code (Read) only | PAT scope screenshot |
| Probe | The ADO token has only Build (Read & Execute) and an expiry date | PAT scope and expiry |
| Secrets | Function App settings and Container App secrets are not readable by wide groups; rotation dates are recorded | Role assignments on the resource group, rotation log |
| Restore | A restore was tested in a disposable tenant with dry run first, and object state was verified afterwards | Test record |
| Data | Which tenant data goes to Azure OpenAI, and that the deployment region and retention match policy | AI payload description in the README, Azure OpenAI configuration |
| Updates | `update-from-upstream` is run with a release tag and dry run first, and the diff is reviewed | Pipeline run history |

## Known limits to state openly

- Tenant exports can contain secrets that administrators embedded in scripts and policies. ASTRAL does not redact
  them. Protect the repository accordingly.
- The Function App stores the probe client secret, the Azure DevOps token and the storage key as app settings, and
  serves its code from a read-only SAS URL with a long expiry. Key Vault references are the recommended next step.
- `deploy/provision.ps1` passes the Container App secret values to the Azure CLI once, on the operator's machine.
  Run it from a trusted workstation or Cloud Shell.
- IntuneCD and its transitive dependencies are installed by version, not by hash, in the backup and restore
  pipelines. Use an internal package mirror if your policy requires hashes.
- The GitHub and Gitea workflows depend on the runner environment. Their isolation is a deployment property.

## Run the scanners yourself first

```bash
pip install bandit pip-audit shellcheck-py
bandit -r scripts infra deploy            # review medium findings; high must be zero
pip-audit -r infra/mcp-server/requirements.txt --no-deps --disable-pip
pip-audit -r infra/change-probe/requirements.txt --no-deps --disable-pip
shellcheck -x scripts/*.sh
git log -p | gitleaks stdin               # or: trufflehog git file://. --results=verified,unknown
```

Fix or document every hit. Findings you already triaged are cheaper for the audit than findings they discover.

## Rotating credentials

| Credential | Where used | Rotate by |
|---|---|---|
| Probe app client secret | Function App `PROBE_APP_SECRET` | Create a new secret on the Entra app, update the Function App setting, delete the old secret |
| Azure DevOps PAT for the probe | Function App `ADO_TOKEN` | Issue a new PAT, update the setting |
| Azure DevOps PAT for MCP | Container App secret `ado-token` | `az containerapp secret set`, then restart the revision |
| MCP API key | Container App secret `mcp-api-key` | Same as above, then update the clients |
| Webhook secret | Function App `WEBHOOK_SECRET` and the ADO service hook | Update both together |
