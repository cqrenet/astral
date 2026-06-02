# M365 Baseline Expansion Roadmap

This document tracks the repository from its current implemented state rather than from the original proposal.

## Current State

The repository already operates as a two-workload baseline system:

- Intune drift backup via IntuneCD
- Entra drift backup for Named Locations, Authentication Strengths, Conditional Access, App Registrations, and Enterprise Applications

The surrounding control loop is also implemented:

- hourly backup pipeline plus midnight Prague full run
- rolling PR per workload
- deterministic reviewer summary with optional Azure OpenAI narrative
- optional per-file change-ticket threads
- reviewer `/reject` and `/accept` decision sync every 20 minutes
- auto-remediation for rejected drift snapshots
- post-merge restore queue for partial-accept / partial-reject review flows
- selective historical restore from branch, tag, or commit
- output validation and drift-noise filtering before commit

## What Is Stable Today

Stable and part of the normal operating model:

- Intune export and reporting
- Entra Named Locations export
- Entra Authentication Strengths export
- Entra Conditional Access export with reference-name enrichment
- object inventory and assignment reporting for both workloads
- Entra app inventory reporting
- drift-branch commit and rolling PR update workflow

Implemented, but intentionally constrained:

- App Registrations export is full-run only
- Enterprise Applications export is full-run only
- light runs preserve the previous committed snapshot for those two Entra categories

Not yet implemented:

- Directory role templates and active directory roles
- Exchange Online, Teams, SharePoint, Purview, and Azure governance modules

## Current Gaps And Stabilization Backlog

1. Fix App Registrations light-run stability.
   The current pipeline still disables hourly App Registrations export because some runs produce resolver-only churn. Re-enabling hourly export requires a deterministic light-run result.

2. Keep Enterprise Applications scoped as a heavy module unless runtime proves otherwise.
   Enterprise Applications are already exported, but only on full runs. The current design assumes this category should remain bounded to the daily/full path unless runtime and diff quality support widening scope.

3. Add the next identity-baseline module only after Phase 1 is fully stable.
   Directory roles are the next logical addition, but they should follow the same pattern: deterministic export, report generation, validation, reviewer-noise filtering, and tests.

## Design Rules For New Modules

Every expansion module should follow the conventions already used by Intune and Entra:

1. Store raw JSON under `tenant-state/<workload-or-module>/`.
2. Store human-review reports under `tenant-state/reports/<workload-or-module>/`.
3. Keep one object per file with deterministic naming and stable key ordering where possible.
4. Validate expected outputs before drift commit.
5. Filter known non-config churn before PR creation.
6. Update permissions, README, and tests in the same change.
7. Start as daily/full-run scope unless there is evidence it is safe and cheap to run hourly.

## Roadmap By Phase

### Phase 1: Identity Baseline

Completed:

- Entra Named Locations
- Entra Authentication Strengths
- Entra Conditional Access
- App Registrations exporter
- Enterprise Applications exporter

Remaining:

- stabilize App Registrations for light runs
- decide whether Enterprise Applications should remain full-run only
- add Directory Roles / Directory Role Templates

### Phase 2: Service Policy Baseline

Candidate modules:

- Exchange Online transport and mail flow rules
- Defender for Office policy configuration
- Teams policy families
- SharePoint and OneDrive tenant-level sharing controls

### Phase 3: Governance Baseline

Candidate modules:

- Purview DLP policies
- Purview retention labels and policies
- Azure policy assignments and initiatives
- Azure RBAC role assignments

## Proposed Future Repository Shape

Keep the existing lowercase workload structure and extend it consistently:

```text
tenant-state/
  intune/
  entra/
  exchange/
  teams/
  sharepoint/
  purview/
  azure-governance/
  reports/
    intune/
    entra/
    exchange/
    teams/
    sharepoint/
    purview/
    azure-governance/
```

## Recommended Execution Order

1. Finish Phase 1 stabilization.
2. Add Directory Roles as the next identity module.
3. Add one Phase 2 or Phase 3 module at a time.
4. Require several stable daily cycles before widening scope or adding the next module.
5. Promote a module from full-run only to hourly only after unchanged tenants produce clean, low-noise diffs.

## Acceptance Criteria

- No regression in current Intune or Entra backup success rate.
- Unchanged environments produce deterministic outputs across repeated runs.
- Reviewer PRs stay focused on configuration-effective drift, not enrichment noise.
- New modules document exact permissions and expected outputs.
- Restore and review workflows remain coherent as scope expands.
