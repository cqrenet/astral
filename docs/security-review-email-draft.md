# Security Review Email Draft

## Subject

Security review package for ASTRAL

## Email Body

Hello,

As discussed, I am sending the security review package for ASTRAL.

ASTRAL stands for Admin Security Through Review, Automation & Least-privilege.

Attached are:

- `security-review-package.pdf` - product security overview, architecture, deployment modes, permissions, data flows, and key security considerations
- `security-review-questionnaire.pdf` - short-form questionnaire answers for easier circulation within your security review process

A few points to highlight up front:

- the platform supports multiple deployment modes, from backup-only through full review and remediation workflows
- AI-assisted review summaries are optional and can be enabled or disabled independently of the backup and restore functions
- when AI is enabled, the intended model is a customer-controlled Azure OpenAI deployment rather than an unrelated public AI service
- the AI summary feature is advisory and is intended to help non-technical reviewers such as PMs or management understand technical Intune and Entra changes in plain language

The source repository is private because it contains operational implementation details and tenant-specific configuration material. If your review requires deeper technical evidence, we can provide a controlled walkthrough of the implementation, configuration, and pipeline behavior.

If useful, I can also provide:

- a live architecture walkthrough
- a permission-by-permission review of the Microsoft Graph access model
- a demonstration of deployment modes and AI-assisted review summaries

Please let me know if your team would like any additional material in a different format.

Best regards,

[Your Name]
