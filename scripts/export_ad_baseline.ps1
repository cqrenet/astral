<#
.SYNOPSIS
    Export on-premises Active Directory configuration to files under -Root (read-only).

.DESCRIPTION
    Runs on a domain-joined runner with the RSAT modules ActiveDirectory and GroupPolicy
    (Install-WindowsFeature RSAT-AD-PowerShell, GPMC). Needs only read rights; it never writes to AD.

    Layout under -Root:
      Domain/Domain.json                         domain, forest, FSMO roles, domain-root GPO links
      Domain/DefaultPasswordPolicy.json
      Group Policy Objects/<name>__<guid>.xml    Get-GPOReport XML (settings, links, security filtering)
      Organizational Units/<name>__<guid>.json   OU properties and ordered GPO links
      Privileged Groups/<name>__<sid>.json       direct members of the privileged groups
      Fine-Grained Password Policies/<name>__<guid>.json
      Trusts/Trusts.json
      Sites/Sites.json, Sites/Subnets.json, Sites/SiteLinks.json

    Output is normalized (stable order, no volatile attributes). scripts/backup_ad.py then canonicalizes
    the JSON, drops report timestamps and redacts GPP cpassword values before anything is committed.

    Keep this file ASCII-only and Windows PowerShell 5.1 compatible: Server Core runs 5.1 by default.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    # Domain controller or domain to query. Empty = the runner's own domain.
    [string]$Server = '',
    [string]$IncludeGpos = 'true',
    [string]$IncludeOus = 'true',
    [string]$IncludePrivilegedGroups = 'true',
    [string]$IncludePasswordPolicies = 'true',
    [string]$IncludeTrusts = 'true',
    [string]$IncludeSites = 'true',
    # Extra groups to export next to the built-in privileged ones: names or SIDs, comma-separated.
    [string]$ExtraPrivilegedGroupsCsv = ''
)

$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Test-Flag([string]$Value) { return @('1', 'true', 'yes') -contains $Value.Trim().ToLowerInvariant() }

function Get-SafeName([string]$Name) {
    $clean = ($Name -replace '[\\/:*?"<>|\x00-\x1f]', '_').Trim().TrimEnd('.')
    if (-not $clean) { $clean = '_' }
    if ($clean.Length -gt 100) { $clean = $clean.Substring(0, 100) }
    return $clean
}

function Write-Utf8([string]$Path, [string]$Text) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, $utf8)
}

function Write-Json([string]$Path, $Object) {
    # -InputObject keeps one-element arrays as arrays.
    Write-Utf8 $Path (ConvertTo-Json -InputObject $Object -Depth 8)
}

function Get-Minutes($Span) { if ($null -eq $Span) { return $null } return [double]$Span.TotalMinutes }

function Get-SortedStrings($Values) { return @($Values | Where-Object { $_ } | ForEach-Object { [string]$_ } | Sort-Object) }

# AD cmdlets take -Server only when one was requested.
$ad = @{}
if ($Server) { $ad['Server'] = $Server }
$gp = @{}
if ($Server) { $gp['Server'] = $Server }

function ConvertFrom-GpLink([string]$GpLink) {
    # [LDAP://cn={GUID},cn=policies,cn=system,DC=x;OPTIONS] ...; options bit 1 = disabled, bit 2 = enforced.
    $links = @()
    if (-not $GpLink) { return $links }
    $order = 0
    foreach ($m in [regex]::Matches($GpLink, '\[LDAP://(?<dn>[^;\]]+);(?<opt>\d+)\]', 'IgnoreCase')) {
        $order++
        $opt = [int]$m.Groups['opt'].Value
        $guid = ''
        if ($m.Groups['dn'].Value -match '\{([0-9A-Fa-f-]{36})\}') { $guid = $Matches[1].ToLowerInvariant() }
        $links += [ordered]@{
            Order    = $order
            GpoId    = $guid
            Enabled  = (($opt -band 1) -eq 0)
            Enforced = (($opt -band 2) -ne 0)
        }
    }
    return $links
}

$domain = Get-ADDomain @ad
$forest = Get-ADForest @ad
$gp['Domain'] = $domain.DNSRoot
Write-Host "Exporting Active Directory domain $($domain.DNSRoot)"

# --- Domain and forest -------------------------------------------------------------------------
$rootObject = Get-ADObject -Identity $domain.DistinguishedName -Properties gPLink, gPOptions @ad
Write-Json (Join-Path $Root 'Domain/Domain.json') ([ordered]@{
        DnsRoot                = $domain.DNSRoot
        NetBiosName            = $domain.NetBIOSName
        DistinguishedName      = $domain.DistinguishedName
        DomainSid              = $domain.DomainSID.Value
        DomainMode             = [string]$domain.DomainMode
        ForestName             = $forest.Name
        ForestMode             = [string]$forest.ForestMode
        ForestRootDomain       = $forest.RootDomain
        PdcEmulator            = $domain.PDCEmulator
        RidMaster              = $domain.RIDMaster
        InfrastructureMaster   = $domain.InfrastructureMaster
        SchemaMaster           = $forest.SchemaMaster
        DomainNamingMaster     = $forest.DomainNamingMaster
        DomainControllers      = Get-SortedStrings $domain.ReplicaDirectoryServers
        ReadOnlyDomainControllers = Get-SortedStrings $domain.ReadOnlyReplicaDirectoryServers
        GlobalCatalogs         = Get-SortedStrings $forest.GlobalCatalogs
        UpnSuffixes            = Get-SortedStrings $forest.UPNSuffixes
        Domains                = Get-SortedStrings $forest.Domains
        BlockInheritance       = (([int]$rootObject.gPOptions -band 1) -ne 0)
        GpoLinks               = @(ConvertFrom-GpLink $rootObject.gPLink)
    })

# --- Group Policy objects ----------------------------------------------------------------------
if (Test-Flag $IncludeGpos) {
    Import-Module GroupPolicy
    $count = 0
    foreach ($gpo in (Get-GPO -All @gp | Sort-Object Id)) {
        $xml = Get-GPOReport -Guid $gpo.Id -ReportType Xml @gp
        $name = '{0}__{1}.xml' -f (Get-SafeName $gpo.DisplayName), $gpo.Id.ToString().ToLowerInvariant()
        Write-Utf8 (Join-Path $Root "Group Policy Objects/$name") $xml
        $count++
    }
    Write-Host "  GPOs: $count"
}

# --- Organizational units ----------------------------------------------------------------------
if (Test-Flag $IncludeOus) {
    $count = 0
    $props = 'gPLink', 'gPOptions', 'ManagedBy', 'Description', 'ProtectedFromAccidentalDeletion'
    foreach ($ou in (Get-ADOrganizationalUnit -Filter * -Properties $props @ad | Sort-Object DistinguishedName)) {
        $name = '{0}__{1}.json' -f (Get-SafeName $ou.Name), $ou.ObjectGUID.ToString().ToLowerInvariant()
        Write-Json (Join-Path $Root "Organizational Units/$name") ([ordered]@{
                Name                 = $ou.Name
                DistinguishedName    = $ou.DistinguishedName
                ObjectGuid           = $ou.ObjectGUID.ToString().ToLowerInvariant()
                Description          = $ou.Description
                ManagedBy            = $ou.ManagedBy
                ProtectedFromAccidentalDeletion = [bool]$ou.ProtectedFromAccidentalDeletion
                BlockInheritance     = (([int]$ou.gPOptions -band 1) -ne 0)
                GpoLinks             = @(ConvertFrom-GpLink $ou.gPLink)
            })
        $count++
    }
    Write-Host "  OUs: $count"
}

# --- Privileged groups -------------------------------------------------------------------------
if (Test-Flag $IncludePrivilegedGroups) {
    $domainSid = $domain.DomainSID.Value
    $rootSid = (Get-ADDomain -Identity $forest.RootDomain).DomainSID.Value
    # SIDs, so the export does not depend on the language of the domain.
    $sids = @(
        "$domainSid-512", "$rootSid-519", "$rootSid-518", "$domainSid-520", "$domainSid-526", "$rootSid-527",
        'S-1-5-32-544', 'S-1-5-32-548', 'S-1-5-32-549', 'S-1-5-32-550', 'S-1-5-32-551', 'S-1-5-32-552'
    )
    if ($ExtraPrivilegedGroupsCsv.Trim()) {
        $sids += @($ExtraPrivilegedGroupsCsv.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    $count = 0
    foreach ($id in $sids) {
        try { $group = Get-ADGroup -Identity $id -Properties Description @ad }
        catch { Write-Warning "Privileged group $id not found: $($_.Exception.Message)"; continue }
        $members = @()
        foreach ($member in @(Get-ADGroupMember -Identity $group @ad)) {
            $members += [ordered]@{
                Name              = $member.Name
                SamAccountName    = $member.SamAccountName
                ObjectClass       = $member.objectClass
                Sid               = $member.SID.Value
                DistinguishedName = $member.distinguishedName
            }
        }
        $name = '{0}__{1}.json' -f (Get-SafeName $group.Name), $group.SID.Value
        Write-Json (Join-Path $Root "Privileged Groups/$name") ([ordered]@{
                Name              = $group.Name
                Sid               = $group.SID.Value
                ObjectGuid        = $group.ObjectGUID.ToString().ToLowerInvariant()
                DistinguishedName = $group.DistinguishedName
                GroupScope        = [string]$group.GroupScope
                Description       = $group.Description
                Members           = @($members | Sort-Object { $_.Sid })
            })
        $count++
    }
    Write-Host "  Privileged groups: $count"
}

# --- Password policies -------------------------------------------------------------------------
if (Test-Flag $IncludePasswordPolicies) {
    $policy = Get-ADDefaultDomainPasswordPolicy @ad
    Write-Json (Join-Path $Root 'Domain/DefaultPasswordPolicy.json') ([ordered]@{
            ComplexityEnabled           = [bool]$policy.ComplexityEnabled
            LockoutDurationMinutes      = Get-Minutes $policy.LockoutDuration
            LockoutObservationWindowMinutes = Get-Minutes $policy.LockoutObservationWindow
            LockoutThreshold            = $policy.LockoutThreshold
            MaxPasswordAgeMinutes       = Get-Minutes $policy.MaxPasswordAge
            MinPasswordAgeMinutes       = Get-Minutes $policy.MinPasswordAge
            MinPasswordLength           = $policy.MinPasswordLength
            PasswordHistoryCount        = $policy.PasswordHistoryCount
            ReversibleEncryptionEnabled = [bool]$policy.ReversibleEncryptionEnabled
        })
    $count = 0
    foreach ($p in (Get-ADFineGrainedPasswordPolicy -Filter * @ad | Sort-Object Name)) {
        $name = '{0}__{1}.json' -f (Get-SafeName $p.Name), $p.ObjectGUID.ToString().ToLowerInvariant()
        Write-Json (Join-Path $Root "Fine-Grained Password Policies/$name") ([ordered]@{
                Name                        = $p.Name
                DistinguishedName           = $p.DistinguishedName
                ObjectGuid                  = $p.ObjectGUID.ToString().ToLowerInvariant()
                Precedence                  = $p.Precedence
                AppliesTo                   = Get-SortedStrings $p.AppliesTo
                ComplexityEnabled           = [bool]$p.ComplexityEnabled
                LockoutDurationMinutes      = Get-Minutes $p.LockoutDuration
                LockoutObservationWindowMinutes = Get-Minutes $p.LockoutObservationWindow
                LockoutThreshold            = $p.LockoutThreshold
                MaxPasswordAgeMinutes       = Get-Minutes $p.MaxPasswordAge
                MinPasswordAgeMinutes       = Get-Minutes $p.MinPasswordAge
                MinPasswordLength           = $p.MinPasswordLength
                PasswordHistoryCount        = $p.PasswordHistoryCount
                ReversibleEncryptionEnabled = [bool]$p.ReversibleEncryptionEnabled
            })
        $count++
    }
    Write-Host "  Fine-grained password policies: $count"
}

# --- Trusts ------------------------------------------------------------------------------------
if (Test-Flag $IncludeTrusts) {
    $trusts = @(Get-ADTrust -Filter * @ad | Sort-Object Name | ForEach-Object {
            [ordered]@{
                Name                    = $_.Name
                Direction               = [string]$_.Direction
                TrustType               = [string]$_.TrustType
                ForestTransitive        = [bool]$_.ForestTransitive
                SelectiveAuthentication = [bool]$_.SelectiveAuthentication
                SidFilteringQuarantined = [bool]$_.SIDFilteringQuarantined
                SidFilteringForestAware = [bool]$_.SIDFilteringForestAware
                DisallowTransivity      = [bool]$_.DisallowTransivity
                UsesRc4Encryption       = [bool]$_.UsesRC4Encryption
            }
        })
    Write-Json (Join-Path $Root 'Trusts/Trusts.json') $trusts
    Write-Host "  Trusts: $($trusts.Count)"
}

# --- Sites -------------------------------------------------------------------------------------
if (Test-Flag $IncludeSites) {
    $sites = @(Get-ADReplicationSite -Filter * -Properties Description @ad | Sort-Object Name | ForEach-Object {
            [ordered]@{ Name = $_.Name; Description = $_.Description }
        })
    Write-Json (Join-Path $Root 'Sites/Sites.json') $sites
    $subnets = @(Get-ADReplicationSubnet -Filter * -Properties Location @ad | Sort-Object Name | ForEach-Object {
            [ordered]@{ Name = $_.Name; Site = $_.Site; Location = $_.Location }
        })
    Write-Json (Join-Path $Root 'Sites/Subnets.json') $subnets
    $links = @(Get-ADReplicationSiteLink -Filter * @ad | Sort-Object Name | ForEach-Object {
            [ordered]@{
                Name                         = $_.Name
                Cost                         = $_.Cost
                ReplicationFrequencyInMinutes = $_.ReplicationFrequencyInMinutes
                SitesIncluded                = Get-SortedStrings $_.SitesIncluded
            }
        })
    Write-Json (Join-Path $Root 'Sites/SiteLinks.json') $links
    Write-Host "  Sites: $($sites.Count), subnets: $($subnets.Count), site links: $($links.Count)"
}
