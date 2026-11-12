# Security Policy

## Supported versions

Security fixes are released for the latest minor version of ASTRAL (`v1.x`). Update with the
`deploy/update-from-upstream.yml` pipeline and set **Upstream ref** to the latest release tag.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private reporting: open the **Security** tab of this repository and choose
**Report a vulnerability**. Include the affected version or commit, what an attacker can do, and steps to
reproduce. Do not include real tenant data; redact tenant IDs, domains and secrets.

Target response times (best effort, not a contract): acknowledgement within 5 business days, an assessment
within 15 business days, and a fix or mitigation plan for confirmed issues after that. Reporters are credited in the
release notes unless they ask not to be.

## Scope

In scope: the scripts, pipelines, workflows and infrastructure code in this repository, including the restore
path (which writes to a tenant), the MCP server, the change probe and the provisioning scripts.

Out of scope: vulnerabilities in third-party components (IntuneCD, Microsoft Graph, Azure services, GitHub or
Azure DevOps) - report those to their owners - and findings that need an attacker who already controls your tenant,
your repository administration or your runners.

## Security model

The full security overview, including permissions, network exposure, secrets, known limitations and the controls
mapped to tests, is in [docs/security/](docs/security/README.md). In short:

- Backups use read-only Graph permissions. Restore is a separate pipeline with write permissions and defaults to
  dry run. Restore refs must be in the approved baseline branch history.
- Reviewer `/reject` and `/accept` decisions count only from `PR_DECISION_ALLOWED_REVIEWERS`.
- Tenant exports can contain administrative data and secrets embedded in policies. Treat the repository that
  holds `tenant-state/` as confidential: private repository, restricted collaborators, no public forks.
- ASTRAL's scripts take secrets from the environment or platform secret stores. IntuneCD is the exception: it
  accepts the Graph token only as a command-line argument.
- The optional MCP server and PR comment webhook are the only inbound endpoints. Both require authentication.
- Dependencies of the deployable components are hash-locked (`infra/*/requirements.txt`) and the MCP image is
  pinned by digest.
