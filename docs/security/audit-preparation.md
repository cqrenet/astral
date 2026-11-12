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
| Identities | The change probe app has only the permissions listed in the security overview | App registration API permissions |
| Federation | OIDC subject claims limit the identity to the intended repository, branch and environment | Federated credential configuration |
| Runners | Self-hosted runners are dedicated, ephemeral or cleaned, and cannot reach unrelated networks | Runner group settings, host baseline, egress rules |
| MCP | The endpoint rejects unauthenticated requests from the internet; internal ingress if clients are private | `curl` against `/sse` without credentials returns 401; ingress setting |
| MCP | The MCP token has Code (Read) only | PAT scope screenshot |
| MCP | *Assignment required* is on for the enterprise application, and only intended users and groups are assigned | Enterprise application properties and the *Users and groups* list |
| MCP | The API key is removed if only Entra sign-in is used, or its holders are known | Container App secret list, key distribution record |
| Webhook | `WEBHOOK_SECRET` is set and the ADO service hook uses it | Function App setting present (value not shown), service hook configuration |
| Pipelines | Force push is allowed on `drift/*` only and denied on `main` | Repository security settings |
| AI | `ENABLE_PR_AI_SUMMARY` has the intended value in the variable group | Variable group export |
| Probe | The ADO token has only Build (Read & Execute) and an expiry date | PAT scope and expiry |
| Secrets | Function App settings and Container App secrets are not readable by wide groups; rotation dates are recorded | Role assignments on the resource group, rotation log |
| Restore | A restore was tested in a disposable tenant with dry run first, and object state was verified afterwards | Test record |
| Data | Which tenant data goes to Azure OpenAI, and that the deployment region and retention match policy | AI payload description in the README, Azure OpenAI configuration |
| Updates | `update-from-upstream` is run with a release tag and dry run first, and the diff is reviewed | Pipeline run history |

## Known limitations

The full list is in the [security overview](./README.md#known-limitations). Read it before the audit and decide,
for each item, whether your deployment accepts it, mitigates it, or fixes it.

## Run the scanners yourself first

```bash
pip install bandit pip-audit shellcheck-py
bandit -r scripts infra deploy            # review medium findings; high must be zero
pip-audit -r infra/mcp-server/requirements.txt --no-deps --disable-pip
pip-audit -r infra/change-probe/requirements.txt --no-deps --disable-pip
shellcheck -x scripts/*.sh
docker run --rm -v "$PWD:/repo:ro" trufflesecurity/trufflehog:3.99.8 git file:///repo --results=verified,unknown
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
