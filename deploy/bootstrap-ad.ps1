#requires -Version 5.1
<#
.SYNOPSIS
    Onboards on-premises Active Directory to ASTRAL: service account, runner VM and a verification run.

.DESCRIPTION
    The counterpart of bootstrap-tenant.ps1 for the optional Active Directory workload (backup only).
    Three steps, run on different machines. Every step is safe to run again.

      DomainSetup   Domain admin, on any machine with the ActiveDirectory module.
                    Checks the KDS root key, creates the group managed service account (gMSA) the runner
                    service runs as, and allows the runner computer to retrieve its password. The account gets
                    no group membership: Authenticated Users can already read AD objects and all GPOs.
                    Optional -GrantEventLogAccess adds it to the built-in group Event Log Readers, which the
                    backup needs to read directory-change events for author attribution. That group can read the
                    whole Security log of the domain controllers: see docs/security/README.md.

      RunnerSetup   Local administrator, on the runner VM (domain-joined, not a domain controller; Server Core
                    is fine). Installs RSAT-AD-PowerShell and GPMC, installs the gMSA, checks Git and Python,
                    and prints the runner registration commands for -Platform.

      GiteaRunner   Gitea only. Local administrator, on the runner VM. act_runner has no Windows service mode, so
                    this registers act_runner with the label ad:host and starts it at boot from a scheduled task
                    that runs as the gMSA. Download act_runner.exe first (gitea.com/gitea/act_runner) into
                    -RunnerDir. The registration token comes from the repository (Settings, Actions, Runners).
                    The task runs under the right "Log on as a batch job": grant it to the gMSA if the task
                    fails with a logon error.

      Verify        Local administrator, on the runner VM, with a clone of this repository (-RepoPath).
                    Runs the exporter as the gMSA in a scheduled task, prints what was exported, and removes
                    the task. With -CheckEvents it also reads directory-change events from all domain
                    controllers, so you can see whether author attribution will work.
                    The account needs the right "Log on as a batch job" for this step.

    The runner is not registered by this script: that needs a token from your forge.
    See docs/active-directory-setup.md. Use -WhatIf to see what would change.

.PARAMETER Step
    DomainSetup, RunnerSetup, GiteaRunner or Verify.

.PARAMETER AccountName
    Name of the gMSA. Default: gmsa-astral-ad.

.PARAMETER RunnerComputer
    DomainSetup: computer name of the runner VM (allowed to retrieve the gMSA password).

.PARAMETER GrantEventLogAccess
    DomainSetup: also add the gMSA to Event Log Readers (author attribution).

.PARAMETER Platform
    RunnerSetup and Verify: ado, github or gitea. Prints the matching runner registration commands.

.PARAMETER GiteaUrl
    GiteaRunner: base URL of your Gitea instance, for example https://gitea.contoso.com.

.PARAMETER GiteaToken
    GiteaRunner: runner registration token (SecureString). Asked for when omitted.

.PARAMETER RunnerDir
    GiteaRunner: folder with act_runner.exe. Default: C:\act_runner.

.PARAMETER RepoPath
    Verify: path of a clone of this repository (needs scripts\export_ad_baseline.ps1).

.PARAMETER OutDir
    Verify: folder for the test export and logs. Default: C:\astral-verify.

.PARAMETER Server
    Verify: domain controller to query. Default: the runner's own domain.

.PARAMETER CheckEvents
    Verify: also test reading directory-change events (needs -GrantEventLogAccess in DomainSetup).

.EXAMPLE
    # 1. Domain admin:
    .\bootstrap-ad.ps1 -Step DomainSetup -RunnerComputer RUNNER01 -GrantEventLogAccess

.EXAMPLE
    # 2. On RUNNER01, as local administrator:
    .\bootstrap-ad.ps1 -Step RunnerSetup -Platform github

.EXAMPLE
    # 3. On RUNNER01, with a clone of the repository:
    .\bootstrap-ad.ps1 -Step Verify -RepoPath C:\astral -CheckEvents
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param (
    [Parameter(Mandatory = $true)]
    [ValidateSet('DomainSetup', 'RunnerSetup', 'GiteaRunner', 'Verify')]
    [string]$Step,

    [string]$AccountName = 'gmsa-astral-ad',

    [string]$RunnerComputer = '',

    [switch]$GrantEventLogAccess,

    [ValidateSet('', 'ado', 'github', 'gitea')]
    [string]$Platform = '',

    [string]$GiteaUrl = '',

    [securestring]$GiteaToken,

    [string]$RunnerDir = 'C:\act_runner',

    [string]$RepoPath = '',

    [string]$OutDir = 'C:\astral-verify',

    [string]$Server = '',

    [switch]$CheckEvents
)

$ErrorActionPreference = 'Stop'

function Write-Ok([string]$Text) { Write-Host $Text -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host $Text -ForegroundColor Cyan }
function Write-Skip([string]$Text) { Write-Host $Text -ForegroundColor Yellow }

function Get-LogonName {
    # DOMAIN\account$ : the form scheduled tasks and the runner configuration need.
    $netbios = (Get-ADDomain).NetBIOSName
    return ('{0}\{1}$' -f $netbios, $AccountName)
}

function Write-RegistrationHelp([string]$Name) {
    Write-Host ''
    Write-Info "Register the runner (the service must run as $Name):"
    switch ($Platform) {
        'ado' {
            Write-Host '  .\config.cmd --unattended --url https://dev.azure.com/<ORG> --auth pat --token <PAT> `'
            Write-Host "    --pool astral-ad --agent $env:COMPUTERNAME --runAsService --windowsLogonAccount '$Name'"
            Write-Host '  Pool name = the AD_AGENT_POOL_NAME variable. Set ENABLE_WORKLOAD_AD=true in the variable group.'
        }
        'github' {
            Write-Host '  .\config.cmd --url https://github.com/<ORG>/<REPO> --token <REGISTRATION_TOKEN> --labels ad `'
            Write-Host "    --runasservice --windowslogonaccount '$Name'"
            Write-Host '  Private repositories only. Set the repository variable ENABLE_WORKLOAD_AD=true.'
        }
        'gitea' {
            Write-Host '  act_runner has no Windows service mode. Register it with the label ad:host and start it from'
            Write-Host "  a scheduled task or service wrapper that runs as $Name."
            Write-Host '  Gitea also needs the bash that comes with Git for Windows. Set the repository variable ENABLE_WORKLOAD_AD=true.'
        }
        default {
            Write-Host '  Pass -Platform ado, github or gitea to print the registration commands.'
        }
    }
}

# ===========================================================================
# DomainSetup
# ===========================================================================
if ($Step -eq 'DomainSetup') {
    if ([string]::IsNullOrWhiteSpace($RunnerComputer)) {
        throw 'DomainSetup needs -RunnerComputer (the computer name of the runner VM).'
    }
    Import-Module ActiveDirectory
    $domain = Get-ADDomain
    Write-Info "Domain: $($domain.DNSRoot)"

    if (-not (Get-KdsRootKey)) {
        throw 'The domain has no KDS root key, so no gMSA can be created. Create one (Add-KdsRootKey); in production wait 10 hours for replication, in a lab use -EffectiveTime (Get-Date).AddHours(-10).'
    }
    $computer = Get-ADComputer -Identity $RunnerComputer
    Write-Ok "Runner computer found: $($computer.DistinguishedName)"

    $account = $null
    try { $account = Get-ADServiceAccount -Identity $AccountName -Properties PrincipalsAllowedToRetrieveManagedPassword } catch { $account = $null }

    if (-not $account) {
        $dnsHost = '{0}.{1}' -f $AccountName, $domain.DNSRoot
        if ($PSCmdlet.ShouldProcess($AccountName, 'Create gMSA')) {
            New-ADServiceAccount -Name $AccountName -DNSHostName $dnsHost `
                -PrincipalsAllowedToRetrieveManagedPassword $computer `
                -KerberosEncryptionType AES256 `
                -Description 'ASTRAL Active Directory backup runner (read-only)'
            Write-Ok "Created gMSA $AccountName."
        }
    }
    else {
        $allowed = @($account.PrincipalsAllowedToRetrieveManagedPassword)
        if ($allowed -contains $computer.DistinguishedName) {
            Write-Skip "gMSA $AccountName already exists and $RunnerComputer may retrieve its password."
        }
        elseif ($PSCmdlet.ShouldProcess($AccountName, "Allow $RunnerComputer to retrieve the password")) {
            Set-ADServiceAccount -Identity $AccountName -PrincipalsAllowedToRetrieveManagedPassword ($allowed + $computer.DistinguishedName)
            Write-Ok "Allowed $RunnerComputer to retrieve the password of $AccountName."
        }
    }

    if ($GrantEventLogAccess) {
        # S-1-5-32-573 = Event Log Readers (built-in, the same on every domain controller).
        $group = Get-ADGroup -Identity 'S-1-5-32-573'
        $sid = (Get-ADServiceAccount -Identity $AccountName).SID
        $isMember = @(Get-ADGroupMember -Identity $group | Where-Object { $_.SID -eq $sid }).Count -gt 0
        if ($isMember) { Write-Skip "$AccountName is already in '$($group.Name)'." }
        elseif ($PSCmdlet.ShouldProcess($group.Name, "Add $AccountName")) {
            Add-ADGroupMember -Identity $group -Members (Get-ADServiceAccount -Identity $AccountName)
            Write-Ok "Added $AccountName to '$($group.Name)'."
        }
    }

    Write-Host ''
    Write-Host '=== Domain setup complete ===' -ForegroundColor Green
    Write-Host "Domain:        $($domain.DNSRoot)"
    Write-Host "Account:       $($domain.NetBIOSName)\$AccountName`$"
    Write-Host "Runner:        $RunnerComputer"
    Write-Host "Event access:  $(if ($GrantEventLogAccess) { 'Event Log Readers' } else { 'not granted (no author attribution)' })"
    Write-Host ''
    Write-Info 'Next steps:'
    Write-Host "  1. On ${RunnerComputer}: .\bootstrap-ad.ps1 -Step RunnerSetup -Platform <ado|github|gitea>"
    Write-Host '  2. Deny the account interactive and Remote Desktop logon (Group Policy: Deny log on locally / through Remote Desktop Services).'
    Write-Host '  3. Author attribution only: turn on Audit Directory Service Changes and the audit entries (docs/active-directory-setup.md, step 4).'
    return
}

# ===========================================================================
# RunnerSetup
# ===========================================================================
if ($Step -eq 'RunnerSetup') {
    if ($PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Install RSAT-AD-PowerShell and GPMC, install the gMSA')) {
        Install-WindowsFeature -Name RSAT-AD-PowerShell, GPMC | Out-Null
        Import-Module ActiveDirectory
        Install-ADServiceAccount -Identity $AccountName
        if (-not (Test-ADServiceAccount -Identity $AccountName)) {
            throw "Test-ADServiceAccount failed for $AccountName. Check that this computer may retrieve the password: run -Step DomainSetup -RunnerComputer $env:COMPUTERNAME."
        }
        Write-Ok "gMSA $AccountName is installed and working."
    }
    Import-Module ActiveDirectory -ErrorAction SilentlyContinue

    $problems = 0
    foreach ($tool in 'git', 'python') {
        if (Get-Command $tool -ErrorAction SilentlyContinue) { Write-Ok "Found $tool" }
        else { Write-Warning "$tool is not on PATH. Install it (Python from python.org or winget, not the Microsoft Store stub) before registering the runner."; $problems++ }
    }
    if (Get-Module -ListAvailable -Name GroupPolicy) { Write-Ok 'GroupPolicy module available.' }
    else { Write-Warning 'GroupPolicy module missing: GPMC is not usable on this installation. Use Server with Desktop Experience.'; $problems++ }

    $dc = (Get-ADDomain).PDCEmulator
    if (Test-NetConnection -ComputerName $dc -Port 9389 -WarningAction SilentlyContinue -InformationLevel Quiet) { Write-Ok "Active Directory Web Services reachable on $dc (port 9389)." }
    else { Write-Warning "Cannot reach $dc on port 9389 (ADWS). Check the firewall between the runner and the domain controllers."; $problems++ }

    Write-Host ''
    Write-Host '=== Runner setup complete ===' -ForegroundColor Green
    Write-Host "Runner:   $env:COMPUTERNAME"
    Write-Host "Account:  $(Get-LogonName)"
    Write-Host "Problems: $problems"
    Write-RegistrationHelp (Get-LogonName)
    Write-Host ''
    Write-Info 'Next step: .\bootstrap-ad.ps1 -Step Verify -RepoPath <clone of this repository> [-CheckEvents]'
    return
}

# ===========================================================================
# GiteaRunner
# ===========================================================================
if ($Step -eq 'GiteaRunner') {
    if ([string]::IsNullOrWhiteSpace($GiteaUrl)) { throw 'GiteaRunner needs -GiteaUrl, for example https://gitea.contoso.com.' }
    $exe = Join-Path $RunnerDir 'act_runner.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw "Not found: $exe. Download act_runner for Windows from gitea.com/gitea/act_runner into $RunnerDir." }
    Import-Module ActiveDirectory
    $logonName = Get-LogonName
    if (-not $GiteaToken) { $GiteaToken = Read-Host -AsSecureString -Prompt 'Runner registration token' }
    $token = (New-Object System.Net.NetworkCredential('', $GiteaToken)).Password

    if (-not (Test-Path -LiteralPath (Join-Path $RunnerDir '.runner'))) {
        if ($PSCmdlet.ShouldProcess($RunnerDir, 'Register act_runner with the label ad:host')) {
            Push-Location $RunnerDir
            try {
                & $exe register --no-interactive --instance $GiteaUrl --token $token --name $env:COMPUTERNAME --labels 'ad:host'
                if ($LASTEXITCODE -ne 0) { throw "act_runner register failed with exit code $LASTEXITCODE." }
            }
            finally { Pop-Location }
            Write-Ok 'Runner registered (label ad:host).'
        }
    }
    else { Write-Skip "$RunnerDir\.runner exists: already registered. Delete it to register again." }

    if ($PSCmdlet.ShouldProcess($logonName, 'Run act_runner at startup as the gMSA')) {
        & icacls $RunnerDir /grant ('{0}:(OI)(CI)M' -f $logonName) | Out-Null
        $taskName = 'astral-act-runner'
        $action = New-ScheduledTaskAction -Execute $exe -Argument 'daemon' -WorkingDirectory $RunnerDir
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId $logonName -LogonType Password -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Start-ScheduledTask -TaskName $taskName
        Start-Sleep -Seconds 5
        Write-Ok "Scheduled task '$taskName' started ($logonName)."
        Write-Host "  State: $((Get-ScheduledTask -TaskName $taskName).State)"
    }
    Write-Host ''
    Write-Host '=== Gitea runner ready ===' -ForegroundColor Green
    Write-Host "In Gitea (Settings, Actions, Runners) the runner $env:COMPUTERNAME should show as Idle with the label ad."
    Write-Host 'Also set the repository variable ENABLE_WORKLOAD_AD=true and make sure Git for Windows (bash) and Python are on PATH for the gMSA.'
    return
}

# ===========================================================================
# Verify
# ===========================================================================
if ([string]::IsNullOrWhiteSpace($RepoPath)) { throw 'Verify needs -RepoPath (a clone of this repository).' }
$exporter = Join-Path $RepoPath 'scripts\export_ad_baseline.ps1'
$changes = Join-Path $RepoPath 'scripts\export_ad_changes.ps1'
if (-not (Test-Path -LiteralPath $exporter)) { throw "Not found: $exporter" }
Import-Module ActiveDirectory

$logonName = Get-LogonName
$taskName = 'astral-ad-verify'
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
if ($PSCmdlet.ShouldProcess($OutDir, "Run the export as $logonName")) {
    & icacls $OutDir /grant ('{0}:(OI)(CI)M' -f $logonName) | Out-Null
}

function Invoke-AsGmsa([string]$Script, [string]$ScriptArguments, [string]$Log) {
    # Runs one script as the gMSA in a scheduled task, waits for it and returns the exit code (0 = success).
    $command = "& '$Script' $ScriptArguments *> '$Log'; exit `$LASTEXITCODE"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -Command "{0}"' -f $command)
    $principal = New-ScheduledTaskPrincipal -UserId $logonName -LogonType Password -RunLevel Limited
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal | Out-Null
    try {
        Start-ScheduledTask -TaskName $taskName
        $deadline = (Get-Date).AddMinutes(15)
        do { Start-Sleep -Seconds 3 } while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running' -and (Get-Date) -lt $deadline)
        return [int](Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
    }
    finally {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    }
}

$exportRoot = Join-Path $OutDir 'ad'
$exportLog = Join-Path $OutDir 'export.log'
Remove-Item -LiteralPath $exportRoot -Recurse -Force -ErrorAction SilentlyContinue
$serverArg = ''
if ($Server) { $serverArg = " -Server '$Server'" }
Write-Info "Running the export as $logonName (this can take a few minutes)..."
$code = Invoke-AsGmsa $exporter "-Root '$exportRoot'$serverArg" $exportLog

$failed = $false
if ($code -ne 0) {
    $failed = $true
    Write-Warning "The export task ended with result $code."
    if ($code -eq -2147023570 -or $code -eq 1326) { Write-Warning 'Logon failure: the gMSA is not installed on this computer (RunnerSetup) or may not retrieve its password (DomainSetup).' }
    if ($code -eq -2147023511 -or $code -eq 1385) { Write-Warning "Logon type not allowed: grant $logonName the right 'Log on as a batch job' (Local Security Policy or Group Policy) for this test."}
}
if (Test-Path -LiteralPath $exportLog) {
    Write-Host '--- export.log ---'
    Get-Content -LiteralPath $exportLog -Tail 25
}

Write-Host ''
Write-Host '=== Export result ===' -ForegroundColor Green
if (Test-Path -LiteralPath $exportRoot) {
    Get-ChildItem -LiteralPath $exportRoot -Directory | ForEach-Object {
        Write-Host ('  {0,-34} {1,5} files' -f $_.Name, @(Get-ChildItem -LiteralPath $_.FullName -Recurse -File).Count)
    }
    if (-not (Test-Path -LiteralPath (Join-Path $exportRoot 'Domain\Domain.json'))) { $failed = $true; Write-Warning 'Domain\Domain.json is missing.' }
    if (@(Get-ChildItem -LiteralPath (Join-Path $exportRoot 'Group Policy Objects') -Filter *.xml -ErrorAction SilentlyContinue).Count -eq 0) { $failed = $true; Write-Warning 'No GPO reports were exported.' }
}
else {
    $failed = $true
    Write-Warning "Nothing was exported to $exportRoot."
}

if ($CheckEvents -and (Test-Path -LiteralPath $changes)) {
    $dcNames = @(Get-ADDomainController -Filter * | ForEach-Object { $_.HostName })
    $eventsFile = Join-Path $OutDir 'events.json'
    $eventsLog = Join-Path $OutDir 'events.log'
    Remove-Item -LiteralPath $eventsFile -Force -ErrorAction SilentlyContinue
    $since = (Get-Date).ToUniversalTime().AddHours(-24).ToString('yyyy-MM-ddTHH:mm:ssZ')
    Write-Info "Reading directory-change events (last 24 hours) from: $($dcNames -join ', ')"
    $eventCode = Invoke-AsGmsa $changes "-OutFile '$eventsFile' -Servers '$($dcNames -join ',')' -Since '$since'" $eventsLog
    Write-Host ''
    Write-Host '=== Event access result ===' -ForegroundColor Green
    if ($eventCode -eq 0 -and (Test-Path -LiteralPath $eventsFile)) {
        $result = Get-Content -LiteralPath $eventsFile -Raw | ConvertFrom-Json
        Write-Host ("  Domain controllers read:   {0}" -f @($result.ServersRead).Count)
        foreach ($f in @($result.ServersFailed)) { Write-Warning ("  Cannot read {0}: {1}" -f $f.Server, $f.Error) }
        Write-Host ("  Change events (24 hours):  {0}" -f @($result.Events).Count)
        if (@($result.ServersFailed).Count -gt 0) { Write-Skip '  Unreadable controllers: check Event Log Readers (DomainSetup -GrantEventLogAccess, then restart the runner service) and the firewall rule "Remote Event Log Management (RPC)".' }
        if (@($result.Events).Count -eq 0 -and @($result.ServersRead).Count -gt 0) { Write-Skip '  No events: either nothing changed, or auditing is off. Change a test OU, run Verify again; if still 0, turn on Audit Directory Service Changes and the audit entries (docs/active-directory-setup.md, step 4).' }
    }
    else {
        Write-Warning "The event task ended with result $eventCode."
        if (Test-Path -LiteralPath $eventsLog) { Get-Content -LiteralPath $eventsLog -Tail 15 }
    }
}

Write-Host ''
if ($failed) {
    Write-Host '=== Verify FAILED ===' -ForegroundColor Red
    Write-Host "Logs: $OutDir. Send export.log and the lines above if you need help."
    exit 1
}
Write-Host '=== Verify passed ===' -ForegroundColor Green
Write-Host "The export ran as $logonName. Files: $exportRoot"
Write-RegistrationHelp $logonName
Write-Host ''
Write-Info 'Next steps: register the runner, set ENABLE_WORKLOAD_AD=true, start the AD backup workflow once, and merge the first PR (docs/active-directory-setup.md).'
