# ASTRAL for on-premises Active Directory

ASTRAL can keep a Git snapshot of your on-premises Active Directory configuration, in the same way as for Intune and Entra. Changes appear in a rolling pull request on the branch `drift/ad`.

This workload is **backup only**. There is no restore pipeline for Active Directory. A `/reject` decision in the pull request does not change the directory.

The workload is **off by default**. It needs a runner inside your network (see below).

## How it works

```text
 Forge (Azure DevOps / GitHub / Gitea)        Your network
 +------------------------------+            +--------------------------------+
 | "AD backup" workflow         | <--------- | Runner VM (Windows Server,     |
 |   queues the job             |  HTTPS 443 | domain member, not a DC)       |
 | Rolling PR, summary, review  |  outbound  |  - service runs as a gMSA      |
 |   run on the normal runners  |  only      |  - RSAT AD + GPMC, Python, Git |
 +------------------------------+            |        | LDAP / ADWS / SMB     |
                                             |        v                       |
                                             |  Domain controller (read only) |
                                             +--------------------------------+
```

Only the export job runs on the AD runner. The runner contacts the forge. You open no inbound port.

| Platform | Backup file | Runner selection |
| --- | --- | --- |
| Azure DevOps | `azure-pipelines-ad.yml` | Agent pool `AD_AGENT_POOL_NAME` (default `astral-ad`) |
| GitHub | `.github/workflows/astral-ad-backup.yml` | Labels `["self-hosted","windows","ad"]`. Change with the variable `ASTRAL_AD_RUNS_ON`. |
| Gitea | `.gitea/workflows/astral-ad-backup.yml` | Label `ad` (`ad:host`) |

The reviewer-decision sync (`azure-pipelines-review-sync.yml`, `astral-review-sync.yml`) also handles `ad` once the workload is enabled.

## What is exported

Folder `tenant-state/ad/`:

| Folder or file | Content |
| --- | --- |
| `Domain/Domain.json` | Domain and forest names, functional levels, role holders (FSMO), domain controllers, global catalogs, UPN suffixes, GPOs linked to the domain root |
| `Domain/DefaultPasswordPolicy.json` | Default domain password and lockout policy |
| `Group Policy Objects/<name>__<guid>.xml` | `Get-GPOReport` XML for each GPO: settings, links, security filtering, WMI filter |
| `Organizational Units/<name>__<guid>.json` | OU properties, block inheritance, ordered GPO links (enabled, enforced) |
| `Privileged Groups/<name>__<sid>.json` | Direct members of Domain Admins, Enterprise Admins, Schema Admins, Group Policy Creator Owners, Key Admins, Enterprise Key Admins and the built-in Administrators, Account/Server/Print/Backup Operators and Replicator groups. Add more with `AD_EXTRA_PRIVILEGED_GROUPS_CSV`. |
| `Fine-Grained Password Policies/` | One file for each policy, with the principals it applies to |
| `Trusts/Trusts.json` | Direction, type, SID filtering, selective authentication |
| `Sites/` | Sites, subnets, site links |

Not exported: user and computer objects, password hashes, DNS zones, AD CS templates, object ACLs, GPO backup files (`Backup-GPO`). These are not planned for this version.

### Noise and secrets

`scripts/backup_ad.py` runs after the export and before the commit:

- It writes every JSON file in a fixed format (sorted keys), so the PowerShell version does not change the diff.
- It removes `ReadTime` from GPO reports. This value changes with every run.
- It replaces every Group Policy Preferences `cpassword` value with `[REDACTED]` and prints a warning. A `cpassword` is reversible, so any domain user can decrypt it. Remove such passwords from the GPOs.

## Set up

### 1. The runner VM

- Windows Server 2022 or later. Server Core works. 2 vCPU, 4 GB RAM and 40 GB disk are enough.
- Join the VM to the domain. Put it in its own OU. It must **not** be a domain controller and must not run other workloads.
- Install Git for Windows and a normal Python 3 (not the Microsoft Store stub). Both must be on `PATH` for the service account. Gitea also uses the `bash` that comes with Git for Windows.
- Allow outbound HTTPS to your forge only. Allow the VM to reach a domain controller (LDAP, ADWS 9389, SMB, RPC).

### 2. The service account

Use a group managed service account (gMSA). It needs no group membership: read access to AD and GPOs is part of the default `Authenticated Users` permissions.

```powershell
# On a machine with the ActiveDirectory module, as a domain admin (needs a KDS root key):
.\deploy\setup-ad-runner.ps1 -Step CreateAccount -RunnerComputer RUNNER01

# On the runner VM, as local administrator:
.\deploy\setup-ad-runner.ps1 -Step PrepareRunner
```

`PrepareRunner` installs `RSAT-AD-PowerShell` and `GPMC`, installs the gMSA and checks Git and Python. If it warns that the `GroupPolicy` module is missing, GPMC does not work on that installation. Use Server with Desktop Experience.

Deny the gMSA interactive and Remote Desktop logon with Group Policy.

### 3. Register the runner (the service runs as the gMSA)

Azure DevOps (agent pool `astral-ad`):

```powershell
.\config.cmd --unattended --url https://dev.azure.com/ORG --auth pat --token <PAT> `
  --pool astral-ad --agent RUNNER01 --runAsService --windowsLogonAccount 'CORP\gmsa-astral-ad$'
```

GitHub (private repositories only: a pull request from a fork must never run on this host):

```powershell
.\config.cmd --url https://github.com/ORG/REPO --token <REG_TOKEN> --labels ad `
  --runasservice --windowslogonaccount 'CORP\gmsa-astral-ad$'
```

Gitea: `act_runner` has no Windows service mode. Run it in host mode, labelled `ad:host`, from a scheduled task or a service wrapper that runs as the gMSA.

Use a pool or label only for this workload. Limit who can edit pipeline files for it.

### 4. Author attribution (optional, recommended)

Without this step every commit carries the service identity. With it, ASTRAL names the administrator behind each change, as it does for Entra: one commit per set of authors, and `get_recent_drift` shows who changed what.

ASTRAL reads the directory-change events 5136 (attribute changed), 5137 (object created), 5139 (object moved) and 5141 (object deleted) from the Security log of every domain controller. It matches each event to the exported file, using the object GUID or distinguished name. For a GPO it uses the GUID in the policy container name. Editing a GPO changes its `versionNumber`, and that is logged. It does **not** read attribute values.

The events exist only if you turn on auditing. This changes the domain controllers, so do it with your normal change process:

1. **Audit policy.** In the Default Domain Controllers Policy, turn on *Audit Directory Service Changes* (Success): Advanced Audit Policy, DS Access.
2. **Audit entries (SACL).** On the domain root, add an audit entry: principal *Everyone*, type *Success*, applies to *this object and all descendant objects*, permissions *Write all properties*, *Create all child objects*, *Delete all child objects* and *Delete*. To limit log volume, apply it only to the descendant object types that ASTRAL exports: `organizationalUnit`, `groupPolicyContainer`, `group`, `trustedDomain` and `msDS-PasswordSettings`. For sites and subnets, add the same entry on `CN=Sites,CN=Configuration`.
3. **Read access.** Add the gMSA to the built-in group *Event Log Readers* (`deploy/setup-ad-runner.ps1 -Step GrantEventLogAccess`, domain admin), then restart the runner service. Allow the firewall rule *Remote Event Log Management (RPC)* from the runner to the domain controllers.
4. **Retention.** Make the Security log large enough to keep the events between two backups, with a margin.

How it behaves:

- The window starts at the last merged AD backup commit (the time in its subject) and ends now. On the first run it covers the last `AD_AUDIT_LOOKBACK_HOURS` (default 48). **Merge the rolling PR with a merge type that keeps the commit messages** (rebase or merge commit, not squash), otherwise every run looks back the full 48 hours.
- Changes by machine accounts (`DC01$`) and by `SYSTEM` are ignored: they are replication and housekeeping.
- A file with no matching event is committed under the service identity, and the job prints a warning with the count. If no event is found at all, the job warns that auditing may be off.
- A domain controller that cannot be read is named in a warning. The backup does not fail because of attribution.
- The author e-mail is `<sAMAccountName>@<dns domain>`. This is not always the real UPN.
- The 5136 event of a group-membership change names the person, but not the member that was added. The member list in the export shows that.

### 5. Turn it on

| Platform | Setting |
| --- | --- |
| Azure DevOps | In the variable group: `ENABLE_WORKLOAD_AD=true`. Create a pipeline from `azure-pipelines-ad.yml`. |
| GitHub / Gitea | Repository variable `ENABLE_WORKLOAD_AD=true` |

Optional variables (all platforms): `AD_SERVER` (domain controller to query), `AD_INCLUDE_GPOS`, `AD_INCLUDE_OUS`, `AD_INCLUDE_PRIVILEGED_GROUPS`, `AD_INCLUDE_PASSWORD_POLICIES`, `AD_INCLUDE_TRUSTS`, `AD_INCLUDE_SITES` (all default `true`), `AD_EXTRA_PRIVILEGED_GROUPS_CSV`, `DRIFT_BRANCH_AD` (default `drift/ad`), `AD_ATTRIBUTE_AUTHORS` (default `true`), `AD_AUDIT_LOOKBACK_HOURS` (default 48) and `AD_AUDIT_SERVERS` (domain controllers to read events from; default all in `Domain.json`).

Start the workflow once by hand. The first run commits the whole domain as one change set. Merge that pull request to make it the baseline.

## AI assistant (MCP) tools

The MCP server reads `tenant-state/ad/` once the workload is exported. The generic tools (`list_policies`, `get_policy`, `get_policy_history`, `search_policies`, `get_recent_drift`) accept the workload `ad`. These tools are specific to AD:

| Tool | Answers |
| --- | --- |
| `list_group_policy_objects(name)` | Which GPOs exist? |
| `get_group_policy_object(name_or_id)` | What does this GPO configure, and who is it scoped to? |
| `get_ad_organizational_units(name)` | Which GPOs are linked to this OU, in which order, enforced or disabled? Which OUs block inheritance? |
| `get_ad_privileged_groups(group_name)` | Who is in Domain Admins, Enterprise Admins, ...? |
| `get_ad_domain()` | Functional levels, FSMO holders, password policy, trusts, sites |

The prompt `ad_security_briefing` combines them. Redeploy the MCP server to get the new tools (`deploy/provision.ps1 -DeployMcpOnly`).

## Limits of this version

- No restore. `/reject` is recorded in the pull request, but nothing is written to the directory.
- Author attribution needs the auditing from step 4. Without it, every commit uses the service identity. A change made on a controller that is down during the backup is not attributed.
- No event-driven trigger. The change probe reads Microsoft 365 audit logs and cannot see on-premises changes. The backup runs daily (02:30 UTC) and on demand.
- Privileged group members are direct members only. Nested groups appear as group objects.
- The report dispatcher does not know the `ad` workload yet. The PR review summary covers the files.
