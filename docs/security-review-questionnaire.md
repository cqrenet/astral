<img src="./assets/astral-logo.svg" alt="ASTRAL logo" width="700" />

# ASTRAL Security Review Questionnaire

Prepared: 2026-04-20

This appendix is a shorter, copy/paste-friendly companion to the full ASTRAL security review package.

| Question | Answer |
| --- | --- |
| What is the system? | ASTRAL is an Azure DevOps pipeline workflow that exports Microsoft Intune and selected Entra ID configuration, stores approved baseline snapshots in Git, and raises configuration drift for review through rolling pull requests. |
| What deployment modes are supported? | The same repository can be operated in progressive modes: backup-only, review package, or full package with restore/remediation. AI is optional in all modes. |
| Is it a public-facing application? | No. It is an administrative pipeline workflow with no public UI or inbound application endpoint created by this repository. |
| Does it require inbound network access from the internet? | No. The implemented workflow is outbound-only over HTTPS. |
| What production systems does it access? | Microsoft Graph for Intune and Entra configuration and audit logs; Azure DevOps APIs for pull request and pipeline operations; Azure Storage (Table and Queue) for probe state and trigger messages. |
| Does it make production changes? | Backup and review pipelines are read-oriented against Microsoft Graph. The restore pipeline is write-capable and can apply approved baseline configuration back to the tenant when explicitly enabled and authorized. |
| What data is processed? | Administrative configuration data such as Intune policies, device configuration, enrollment profiles, apps, scripts, conditional access, named locations, authentication strengths, app registrations, and enterprise application metadata. |
| Does it process end-user business content? | It is not designed for business content. However, exported admin-authored scripts or custom payloads can contain sensitive operational data if the tenant already stores it there. |
| Where is data stored? | In the Azure DevOps Git repository, Azure DevOps pull requests/threads, build logs, and optional build artifacts such as markdown, HTML, and PDF documentation. |
| How does it authenticate to Microsoft Graph? | By obtaining a Microsoft Graph token at runtime through an Azure DevOps Azure service connection using workload identity / federated credential flow. |
| How does it authenticate to Azure DevOps APIs? | With `System.AccessToken` scoped to the pipeline identity. |
| Are long-lived secrets stored in the repository? | The pipeline logic does not require repository-stored application secrets. The change probe app secret is stored in Azure Function App settings, not in the repository. Runtime tokens are acquired during pipeline execution, but exported tenant content should still be treated as potentially sensitive and reviewed for embedded secrets in admin-authored scripts or custom payloads. |
| How are secrets handled in the pipeline? | The Graph access token is set as a secret pipeline variable. The implementation logs token claims and granted roles for diagnostics, but not the token value. |
| What minimum permissions are required? | Read-only Microsoft Graph application permissions for backup/review, and additional write permissions only for restore. Exact permissions are listed in the full package. |
| Is there separation between read and write access? | The code supports a safe separation model. For production, create separate read-only and write-enabled service principals/connections so backup and restore use different identities. |
| What change-control mechanism exists? | Drift is committed to dedicated workload branches and reviewed through rolling pull requests into `main`. New rolling PRs can be created as drafts until the automated summary is inserted, and optional per-file change-ticket threads and reviewer `/reject` commands are supported. |
| Can reviewers block or scope changes? | Yes. Reviewers can approve the rolling PR, reject it, or reject individual file-level drift items through PR threads when that feature is enabled. |
| Is rollback supported? | Yes. The restore pipeline supports full restore, selective restore by file path, historical restore by Git ref, and dry-run mode. |
| What external network destinations are required? | Microsoft Graph, Azure DevOps APIs, Azure Storage (Table and Queue), optional Azure OpenAI, Python package registry for `IntuneCD`, npm registry for `md-to-pdf`, and optionally OS package repositories when browser dependencies are installed for HTML/PDF generation. |
| Does the system send data to AI services? | Only if Azure OpenAI summary generation is explicitly configured. It is optional for the platform overall. |
| What AI service is intended? | A customer-controlled Azure OpenAI deployment configured through the Azure OpenAI endpoint and deployment variables, rather than an unrelated public AI service. |
| What data is sent to Azure OpenAI when enabled? | A reduced change-review payload containing changed paths, semantic summaries, deterministic summary text, and fingerprints derived from the repo diff. This is intended to support review summarization, not raw tenant-wide export ingestion. |
| Why is AI included? | The AI summary is meant to translate technical Intune and Entra drift into manager-readable language so PMs, management, and other non-specialist reviewers can understand impact and review intent without parsing raw policy JSON. |
| Recommended deployment posture? | Start with backup-only or review-package mode, enable Azure OpenAI only on a customer-controlled deployment when approved, keep auto-remediation disabled by default, and use separate read-only and write-enabled service principals if restore is enabled. |
| What customer-specific controls still need to be defined? | Agent type and hardening, repo/build/artifact retention, exact access groups, branch policies, and whether restore or Azure OpenAI are enabled in the target deployment. |
