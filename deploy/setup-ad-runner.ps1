<#
.SYNOPSIS
    Prepare the domain-joined runner for the ASTRAL Active Directory backup.

.DESCRIPTION
    Two steps, run on different machines:

      -Step CreateAccount   Run once as a domain admin on a machine with the ActiveDirectory module.
                            Creates the group managed service account (gMSA) the runner service runs as,
                            and allows the runner computer to retrieve its password. The account gets no
                            group memberships: Authenticated Users can already read AD and all GPOs.

      -Step GrantEventLogAccess
                            Optional, run once as a domain admin. Adds the gMSA to the built-in group Event Log
                            Readers, so the backup can read directory-change events (author attribution) from the
                            domain controllers. The group also allows reading the whole Security log: see
                            docs/security/README.md.

      -Step PrepareRunner   Run on the runner VM (local admin; Server Core is fine). Installs RSAT-AD-PowerShell
                            and GPMC, installs the gMSA on the machine, and checks Python and Git.

    Neither step registers the runner: that needs a token from your forge. See docs/active-directory-setup.md.
    Use -WhatIf to see what would change.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][ValidateSet('CreateAccount', 'GrantEventLogAccess', 'PrepareRunner')][string]$Step,
    [string]$AccountName = 'gmsa-astral-ad',
    # CreateAccount: computer name of the runner VM (allowed to retrieve the gMSA password).
    [string]$RunnerComputer = ''
)

$ErrorActionPreference = 'Stop'

if ($Step -eq 'CreateAccount') {
    if (-not $RunnerComputer) { throw 'CreateAccount needs -RunnerComputer (the runner VM computer name).' }
    Import-Module ActiveDirectory
    if (-not (Get-KdsRootKey)) {
        throw 'The domain has no KDS root key, so no gMSA can be created. Create one first (Add-KdsRootKey; in production wait 10 hours for replication).'
    }
    $computer = Get-ADComputer -Identity $RunnerComputer
    $dnsHost = '{0}.{1}' -f $AccountName, (Get-ADDomain).DNSRoot
    if ($PSCmdlet.ShouldProcess($AccountName, 'Create gMSA')) {
        New-ADServiceAccount -Name $AccountName -DNSHostName $dnsHost `
            -PrincipalsAllowedToRetrieveManagedPassword $computer `
            -KerberosEncryptionType AES256 `
            -Description 'ASTRAL Active Directory backup runner (read-only)'
        Write-Host "Created $AccountName. Next: run this script with -Step PrepareRunner on $RunnerComputer."
        Write-Host 'Also deny this account interactive and Remote Desktop logon (Group Policy: Deny log on locally / through Remote Desktop Services).'
    }
    return
}

if ($Step -eq 'GrantEventLogAccess') {
    Import-Module ActiveDirectory
    # S-1-5-32-573 = Event Log Readers (built-in, same on every domain controller).
    $group = Get-ADGroup -Identity 'S-1-5-32-573'
    $account = Get-ADServiceAccount -Identity $AccountName
    if ($PSCmdlet.ShouldProcess($group.Name, "Add $AccountName")) {
        Add-ADGroupMember -Identity $group -Members $account
        Write-Host "$AccountName is now in '$($group.Name)'. Restart the runner service so it gets a new token."
    }
    return
}

# PrepareRunner
if ($PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Install RSAT-AD-PowerShell and GPMC')) {
    Install-WindowsFeature -Name RSAT-AD-PowerShell, GPMC | Out-Null
    Import-Module ActiveDirectory
    Install-ADServiceAccount -Identity $AccountName
    if (-not (Test-ADServiceAccount -Identity $AccountName)) {
        throw "Test-ADServiceAccount failed for $AccountName. Check that this computer may retrieve the password (-Step CreateAccount -RunnerComputer $env:COMPUTERNAME)."
    }
    Write-Host "gMSA $AccountName is installed and working."
}
foreach ($tool in 'git', 'python') {
    if (Get-Command $tool -ErrorAction SilentlyContinue) { Write-Host "Found $tool" }
    else { Write-Warning "$tool is not on PATH. Install it (Python from python.org or winget, not the Microsoft Store stub) before registering the runner." }
}
if (Get-Module -ListAvailable -Name GroupPolicy) { Write-Host 'GroupPolicy module available.' }
else { Write-Warning 'GroupPolicy module missing: GPMC is not usable on this installation. Use Server with Desktop Experience.' }
