#requires -Version 5.1
<#
.SYNOPSIS
    Removes ASTRAL change probe and/or MCP server resources from Azure and Entra ID.

.DESCRIPTION
    Reads deployment state from .astral-deploy.json (written by provision-change-probe.ps1)
    and removes all identified resources. Can also discover resources by Azure tags when
    the config file is missing or partial.

    Resources removed:
    - Azure: resource group(s) containing the Function App, Storage Account, ACR,
             Container Apps Environment, and Container App
    - Entra ID: change probe app registration, MCP auth app registration (if used)

    Deprovisioning is confirmed interactively before any deletion occurs.

.PARAMETER ConfigPath
    Path to .astral-deploy.json. Defaults to the repo root (one level above deploy/).

.PARAMETER DiscoverByTags
    Query Azure for resources tagged astral=true as a fallback or supplement to the
    config file. Useful when the config is missing or from a different machine.

.PARAMETER SubscriptionId
    Azure subscription ID to search. Uses the current az default if omitted.

.PARAMETER SkipEntraCleanup
    Skip deletion of Entra app registrations. Use when the app was created externally
    (e.g. auto-created by ADO) and should be managed separately.

.PARAMETER WhatIf
    Show what would be deleted without actually deleting anything.

.EXAMPLE
    # Standard cleanup using saved config
    .\deploy\deprovision-change-probe.ps1

.EXAMPLE
    # Discover leftover resources by tag when config is missing
    .\deploy\deprovision-change-probe.ps1 -DiscoverByTags

.EXAMPLE
    # Preview what would be removed
    .\deploy\deprovision-change-probe.ps1 -WhatIf
#>
[CmdletBinding()]
param (
    [string]$ConfigPath = "",
    [switch]$DiscoverByTags,
    [string]$SubscriptionId = "",
    [switch]$SkipEntraCleanup,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Invoke-AzCli {
    param ([string[]]$ArgumentList)
    $argsCopy = @() + $ArgumentList
    if ($SubscriptionId) { $argsCopy += @("--subscription", $SubscriptionId) }
    $env:PYTHONWARNINGS = "ignore"
    $output = & az @argsCopy 2>&1
    $env:PYTHONWARNINGS = ""
    if ($LASTEXITCODE -ne 0) {
        $outputString = ($output | ForEach-Object { if ($_ -is [string]) { $_ } else { $_.ToString() } }) -join "`n"
        throw "az command failed: az $($argsCopy -join ' ')`n$outputString"
    }
    return $output
}

function Write-Resource {
    param ([string]$Label, [string]$Value, [string]$Color = "White")
    if ($Value -and $Value -ne "N/A" -and $Value -ne "") {
        Write-Host "  $Label $Value" -ForegroundColor $Color
    }
}

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

if (-not $ConfigPath) {
    $ConfigPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".astral-deploy.json"
}

$config = $null
if (Test-Path $ConfigPath) {
    try {
        $config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        Write-Host "Loaded deployment config from $ConfigPath" -ForegroundColor Green
    } catch {
        Write-Warning "Could not parse $ConfigPath: $_"
    }
} else {
    Write-Warning "Config file not found at $ConfigPath."
    if (-not $DiscoverByTags) {
        Write-Host "Run with -DiscoverByTags to find resources by Azure tag instead." -ForegroundColor Cyan
        exit 1
    }
}

if ($SubscriptionId -eq "" -and $config -and $config.SubscriptionId) {
    $SubscriptionId = $config.SubscriptionId
}

# ---------------------------------------------------------------------------
# Build resource inventory from config
# ---------------------------------------------------------------------------

$resourceGroups    = @()
$entraApps         = @()   # list of @{DisplayName; AppId}
$tagDiscoveredRGs  = @()

if ($config) {
    if ($config.ResourceGroup)    { $resourceGroups += $config.ResourceGroup }
    if ($config.McpResourceGroup -and $config.McpResourceGroup -ne $config.ResourceGroup) {
        $resourceGroups += $config.McpResourceGroup
    }
    if (-not $SkipEntraCleanup) {
        if ($config.ProbeAppDisplayName -and $config.ProbeAppId -and $config.ProbeAppId -ne "N/A") {
            $entraApps += @{ DisplayName = $config.ProbeAppDisplayName; AppId = $config.ProbeAppId }
        }
        if ($config.McpAuthAppId -and $config.McpAuthAppId -ne "") {
            $entraApps += @{ DisplayName = "ASTRAL MCP Server"; AppId = $config.McpAuthAppId }
        }
    }
}

# ---------------------------------------------------------------------------
# Tag discovery (supplement or fallback)
# ---------------------------------------------------------------------------

if ($DiscoverByTags) {
    Write-Host "`nQuerying Azure for resources tagged astral=true..." -ForegroundColor Cyan
    try {
        $taggedJson = Invoke-AzCli -ArgumentList @(
            "resource", "list",
            "--tag", "astral=true",
            "--query", "[].resourceGroup",
            "--output", "json"
        )
        $taggedRGs = ($taggedJson -join "") | ConvertFrom-Json | Select-Object -Unique
        foreach ($rg in $taggedRGs) {
            if ($rg -and $resourceGroups -notcontains $rg) {
                $tagDiscoveredRGs += $rg
                Write-Host "  Found tagged resource group: $rg" -ForegroundColor Yellow
            }
        }
    } catch {
        Write-Warning "Tag discovery failed: $_"
    }
}

$allResourceGroups = ($resourceGroups + $tagDiscoveredRGs) | Select-Object -Unique | Where-Object { $_ }

if ($allResourceGroups.Count -eq 0 -and $entraApps.Count -eq 0) {
    Write-Host "`nNothing to remove — no resource groups or Entra apps identified." -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------------------
# Show inventory
# ---------------------------------------------------------------------------

Write-Host "`n=== Resources identified for removal ===" -ForegroundColor Yellow

if ($allResourceGroups.Count -gt 0) {
    Write-Host "`nAzure resource groups (all resources inside will be deleted):" -ForegroundColor Cyan
    foreach ($rg in $allResourceGroups) {
        $source = if ($tagDiscoveredRGs -contains $rg -and $resourceGroups -notcontains $rg) { " (tag-discovered)" } else { "" }
        Write-Host "  $rg$source" -ForegroundColor White

        # Show contents
        try {
            $resourcesJson = Invoke-AzCli -ArgumentList @(
                "resource", "list",
                "--resource-group", $rg,
                "--query", "[].{name:name, type:type}",
                "--output", "json"
            )
            $resources = ($resourcesJson -join "") | ConvertFrom-Json
            foreach ($r in $resources) {
                $shortType = $r.type -replace "^.*/", ""
                Write-Host "    - $($r.name) ($shortType)" -ForegroundColor DarkGray
            }
        } catch {
            Write-Host "    (could not list contents — may not exist)" -ForegroundColor DarkGray
        }
    }
}

if ($entraApps.Count -gt 0) {
    Write-Host "`nEntra ID app registrations:" -ForegroundColor Cyan
    foreach ($app in $entraApps) {
        Write-Host "  $($app.DisplayName) ($($app.AppId))" -ForegroundColor White
    }
}

if ($config) {
    Write-Host "`nDeployment details from config:" -ForegroundColor DarkGray
    Write-Resource "ADO org/project:" "$($config.AdoOrganization)/$($config.AdoProject)" "DarkGray"
    Write-Resource "Function App:   " $config.FunctionAppName "DarkGray"
    Write-Resource "Storage Account:" $config.StorageName "DarkGray"
    Write-Resource "MCP Container:  " $config.McpContainerAppName "DarkGray"
    Write-Resource "MCP ACR:        " $config.McpAcrName "DarkGray"
}

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

if ($WhatIf) {
    Write-Host "`n[WhatIf] No changes made." -ForegroundColor Cyan
    exit 0
}

Write-Host ""
Write-Host "WARNING: This will permanently delete all resources listed above." -ForegroundColor Red
$confirm = Read-Host "Type 'yes' to confirm deletion"
if ($confirm -ne "yes") {
    Write-Host "Aborted." -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------------------
# Delete Azure resource groups
# ---------------------------------------------------------------------------

foreach ($rg in $allResourceGroups) {
    Write-Host "`nDeleting resource group '$rg'..." -ForegroundColor Cyan
    try {
        Invoke-AzCli -ArgumentList @(
            "group", "delete",
            "--name", $rg,
            "--yes",
            "--no-wait"
        )
        Write-Host "  Deletion of '$rg' initiated (--no-wait). Check Azure Portal for completion." -ForegroundColor Green
    } catch {
        Write-Warning "  Could not delete resource group '$rg': $_"
    }
}

# ---------------------------------------------------------------------------
# Delete Entra app registrations
# ---------------------------------------------------------------------------

if ($entraApps.Count -gt 0 -and -not $SkipEntraCleanup) {
    Write-Host "`nRemoving Entra ID app registrations..." -ForegroundColor Cyan

    try {
        Import-Module Microsoft.Graph.Applications -ErrorAction Stop
    } catch {
        Write-Warning "Microsoft.Graph.Applications module not available. Skipping Entra cleanup."
        Write-Host "To remove manually: delete app registrations from Entra ID → App registrations:" -ForegroundColor Yellow
        foreach ($app in $entraApps) {
            Write-Host "  $($app.DisplayName) ($($app.AppId))"
        }
        exit 0
    }

    Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Cyan
    Connect-MgGraph -Scopes "Application.ReadWrite.All" -NoWelcome

    foreach ($app in $entraApps) {
        Write-Host "  Removing '$($app.DisplayName)' ($($app.AppId))..." -ForegroundColor Cyan
        try {
            $mgApp = Get-MgApplication -Filter "appId eq '$($app.AppId)'" | Select-Object -First 1
            if ($mgApp) {
                Remove-MgApplication -ApplicationId $mgApp.Id
                Write-Host "  Removed." -ForegroundColor Green
            } else {
                Write-Host "  Not found — may have already been deleted." -ForegroundColor Yellow
            }
        } catch {
            Write-Warning "  Could not remove '$($app.DisplayName)': $_"
        }
    }

    Disconnect-MgGraph | Out-Null
}

# ---------------------------------------------------------------------------
# Clean up local config
# ---------------------------------------------------------------------------

if (Test-Path $ConfigPath) {
    $removeConfig = Read-Host "`nRemove local config file $ConfigPath? [Y/n]"
    if ($removeConfig -eq "" -or $removeConfig -match "^[Yy]") {
        Remove-Item $ConfigPath
        Write-Host "Config file removed." -ForegroundColor Green
    }
}

Write-Host "`n=== Deprovision complete ===" -ForegroundColor Green
Write-Host "Azure resource group deletions run asynchronously — verify completion in the Azure Portal."
