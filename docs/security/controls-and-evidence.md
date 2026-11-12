# ASTRAL security controls and evidence

This page maps each security control to the code that enforces it and the test that proves it. Run the tests with
`pip install pyyaml && python3 -m unittest discover -s tests -v`. Controls that depend on your platform
configuration are listed in [audit-preparation.md](./audit-preparation.md), not here.

## Components and identities

| Component | Runs as | Can read | Can write |
|---|---|---|---|
| Backup (Intune, Entra) | Backup identity (workload federated credential or managed identity) | Microsoft Graph read permissions (see README) | Only the drift branches and rolling PRs in the repository |
| Reports pipeline | Pipeline identity | The repository | `tenant-state/reports/` on `main` |
| Review sync | Pipeline token (`System.AccessToken` or forge token) | PRs and threads | PR threads, drift branch resets, restore queueing |
| Restore | Separate restore identity | Graph | **Graph write permissions** (`...ReadWrite.All`); dry run by default |
| Change probe | Function App with its own Entra app | Audit logs (`AuditLog.Read.All`) | Debouncer state in Table Storage, queue messages; queues the backup pipeline with an ADO token |
| MCP server | Container App | `tenant-state/` through a read-only repository token | Nothing |
| AI summary (optional) | Review job | Policy excerpts of the PR | Sends excerpts to your Azure OpenAI deployment |

## Trust boundaries

1. **Tenant to repository.** Exports can contain administrative data and secrets embedded in policies. Everything
   under `tenant-state/` inherits the confidentiality of the repository.
2. **Repository to tenant.** Only the restore pipeline crosses this boundary. It writes only content from a commit
   in the approved baseline history, after a human merged it.
3. **Reviewer to pipeline.** `/reject` and `/accept` comments are untrusted input until the author is on the
   allowlist.
4. **Operator to cloud.** `deploy/provision.ps1` creates the Entra app, Function App, registry and Container App.
5. **Upstream to deployment.** `deploy/update-from-upstream.yml` pulls code from this repository into a customer
   repository.
6. **AI client to MCP server.** Network clients query tenant state through the MCP endpoint.

## Controls

| Control | Enforced in | Evidence (tests) |
|---|---|---|
| Restore only from the approved baseline history | `scripts/restore_checkout_ref.sh` | `tests/test_restore_checkout_ref.py`: `test_commit_outside_the_approved_branch_is_refused`, `test_option_injection_and_malformed_refs_are_refused`, `test_unknown_ref_and_missing_configuration_fail` |
| Restore fails closed when refs cannot be refreshed | `scripts/restore_checkout_ref.sh` | `test_fetch_failure_is_fail_closed` |
| Selective restore rejects path traversal | `scripts/restore_prepare_scope.py` | `tests/test_restore_prepare_scope.py`: `test_leading_parent_traversal_is_rejected` |
| A failed IntuneCD restore is never reported as success | `scripts/run_intunecd_update.sh` | `tests/test_run_intunecd_update.py`: `test_failure_without_markers_preserves_the_failure` |
| Restore defaults to dry run and the mode is constrained | `azure-pipelines-restore.yml`, `.github/workflows/astral-restore.yml` | `tests/test_pipeline_policies.py`: `test_restore_mode_is_constrained_everywhere` |
| Only allowlisted reviewers can reject or accept | `scripts/common.py`, `scripts/apply_reviewer_rejections.py` | `tests/test_apply_reviewer_rejections.py`: `test_untrusted_author_is_ignored`; `test_pipeline_policies.py`: `test_steps_applying_reviewer_decisions_receive_the_allowlist` |
| Pipeline inputs cannot inject shell | all pipeline YAML | `test_pipeline_policies.py`: `test_actions_run_steps_never_embed_expressions`, `test_ado_scripts_only_template_typed_parameters` |
| Secrets reach steps through the environment | all pipeline YAML | `test_pipeline_policies.py`: `test_secrets_reach_the_steps_through_env_not_macros` |
| Third-party GitHub Actions are pinned to a commit | `.github/workflows/` | `test_pipeline_policies.py`: `test_github_actions_are_pinned_to_a_commit` |
| Upstream update refs are validated and never executed | `deploy/update-from-upstream.yml` | `test_pipeline_policies.py`: `test_malicious_refs_are_rejected_and_never_executed` |
| MCP requires authentication before serving | `infra/mcp-server/mcp_server.py` | `tests/test_mcp_server.py`: `test_sse_refuses_to_start_without_any_auth`, `test_wrong_missing_or_unconfigured_credentials_get_401` |
| MCP accepts only the exact Entra issuer, audience and scope | `infra/mcp-server/mcp_server.py` | `test_wrong_tenant_issuer_or_missing_scope_is_rejected`, `test_valid_delegated_token_is_accepted_with_both_audience_forms`, `test_unknown_signing_key_is_rejected`, `test_any_library_error_fails_closed` |
| MCP tool inputs are validated; the server only reads files and the repository | `scripts/astral_mcp_tools.py` | `tests/test_astral_mcp_tools.py`: `test_rejects_traversal_and_unknown_inputs` (the server has no write tools; verify by reviewing the tool list in `infra/mcp-server/mcp_server.py`) |
| Repository tokens never appear on a command line | change probe, trigger script, MCP clone, provisioning | `tests/test_change_probe_function.py` (probe and queue consumer), `tests/test_astral_mcp_tools.py`: `test_clone_token_is_passed_by_environment_not_argv` |
| Deployable dependencies are hash-locked | `infra/*/requirements.txt` | `tests/test_deploy_packages.py`: `test_requirements_lock_files_are_hash_pinned`; CI runs `pip-audit` on the locks |
| Function package is self-contained | `deploy/provision.ps1` staging | `tests/test_deploy_packages.py` |
| Container image uses a digest-pinned base | `infra/mcp-server/Dockerfile` | review the `FROM` line; Dependabot proposes updates |
| Registry pulls use a managed identity, not the admin user | `deploy/provision.ps1` | review; there is no automated test for the PowerShell Azure calls |

## Static analysis and dependency state

CI runs on every push and weekly: unit tests, `shellcheck`, `bandit` (high severity gate), `pip-audit` on both lock
files, and a secret scan with TruffleHog. Known and accepted findings:

- Bandit reports **B310** (`urllib` URL open) in the HTTP helpers. All calls use endpoints built from operator
  configuration (Graph, Azure DevOps, Azure OpenAI, the forge API); none take a URL from tenant data. They are below
  the CI gate and are accepted.
- The MCP server clones the repository into `/tmp` inside the container, which runs as an unprivileged user.
