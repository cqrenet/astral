#requires -Version 5.1
<#
.SYNOPSIS
    One-stop provisioning script for the ASTRAL change probe.

.DESCRIPTION
    This script handles the entire probe deployment in one pass:
      1. Creates (or updates) a dedicated Entra app registration with Graph permissions.
      2. Grants admin consent.
      3. Provisions Azure resources (Resource Group, Storage Account, Function App).
      4. Configures Function App settings.
      5. Optionally deploys the function code if the Azure Functions Core Tools (func) are installed.

    Any parameter omitted on the command line is prompted for interactively.

.PARAMETER AppDisplayName
    Display name for the Entra app registration. Default: "ASTRAL Change Probe".

.PARAMETER ResourceGroup
    Azure resource group name. Default: "rg-astral-probe".

.PARAMETER Location
    Azure region. Default: "westeurope".

.PARAMETER SubscriptionId
    Azure subscription ID. If omitted, the current default subscription is used.

.PARAMETER AdoOrganization
    Azure DevOps organization name (e.g. "contoso").

.PARAMETER AdoProject
    Azure DevOps project name.

.PARAMETER AdoPipelineId
    Azure DevOps pipeline ID (numeric).

.PARAMETER AdoToken
    Azure DevOps Personal Access Token with Build (Read & Execute) scope.

.PARAMETER AdoBranch
    Git branch the pipeline should run against. Default: "main".

.PARAMETER QuietWindowMinutes
    Debouncer quiet window. Default: 15.

.PARAMETER CooldownMinutes
    Debouncer cooldown. Default: 30.

.EXAMPLE
    .\provision-change-probe.ps1

.EXAMPLE
    .\provision-change-probe.ps1 -AdoOrganization "cqre" -AdoProject "ASTRAL" -AdoPipelineId "42"
#>
[CmdletBinding()]
param (
    [string]$AppDisplayName = "ASTRAL Change Probe",
    [string]$ResourceGroup = "rg-astral-probe",
    [string]$Location = "westeurope",
    [string]$SubscriptionId = "",
    [string]$AdoOrganization = "",
    [string]$AdoProject = "",
    [string]$AdoPipelineId = "",
    [string]$AdoToken = "",
    [string]$AdoBranch = "main",
    [int]$QuietWindowMinutes = 15,
    [int]$CooldownMinutes = 30,
    [switch]$DeployMcpServer,
    [switch]$SkipMcpServer,
    [switch]$DeployMcpOnly,
    [string]$McpContainerAppName = "",
    [string]$McpAcrName = "",
    [string]$McpResourceGroup = "",
    [string]$McpLocation = "",
    [string]$McpImageName = "astral-mcp",
    [string]$McpApiKey = "",
    [switch]$EnableMcpEntraIdAuth
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Get-OrPrompt {
    param ([string]$Value, [string]$Prompt, [switch]$Sensitive)
    if ($Value) { return $Value }
    if ($Sensitive) {
        return Read-Host -Prompt $Prompt -AsSecureString | ForEach-Object { [PSCredential]::New("x", $_).GetNetworkCredential().Password }
    }
    return Read-Host -Prompt $Prompt
}

function Test-Command {
    param ([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-AzCli {
    param (
        [string[]]$ArgumentList,
        [switch]$NoRetry
    )
    # Clone the array so recursive calls don't double-append --subscription.
    $argsCopy = @() + $ArgumentList
    if ($SubscriptionId) {
        $argsCopy += @("--subscription", $SubscriptionId)
    }
    # Suppress Python SyntaxWarnings that leak from the Azure CLI into stderr/stdout.
    $env:PYTHONWARNINGS = "ignore"
    $output = & az @argsCopy 2>&1
    $env:PYTHONWARNINGS = ""
    if ($LASTEXITCODE -ne 0) {
        $outputStrings = @()
        $hasSubNotFound = $false
        foreach ($line in $output) {
            $str = if ($line -is [string]) { $line } else { $line.ToString() }
            $outputStrings += $str
            if ($str -match "SubscriptionNotFound") { $hasSubNotFound = $true }
        }
        $outputString = $outputStrings -join "`n"
        if ((-not $NoRetry) -and $hasSubNotFound) {
            Write-Host "`nARM returned SubscriptionNotFound. Clearing token cache and re-authenticating..." -ForegroundColor Yellow
            $subTenantId = Get-SubscriptionTenantId -SubId $SubscriptionId
            $promptTenant = if ($subTenantId) { $subTenantId } else { $tenantId }
            & az account clear | Out-Null
            & az login --tenant $promptTenant | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "az login --tenant $promptTenant failed." }
            # Explicitly set subscription and give token cache time to settle.
            & az account set --subscription $SubscriptionId | Out-Null
            Start-Sleep -Seconds 2
            Invoke-AzCli -ArgumentList $ArgumentList -NoRetry
            return
        }
        $redactedArgs = $argsCopy | ForEach-Object {
            if ($_ -match '^[A-Za-z][A-Za-z0-9_]*=') { $_ -replace '=.*$', '=***' } else { $_ }
        }
        throw "az command failed: az $($redactedArgs -join ' ')`n$outputString"
    }
    return $output
}

function Test-ModuleInstalled {
    param ([string]$Name)
    $mod = Get-Module -ListAvailable -Name $Name | Select-Object -First 1
    if (-not $mod) {
        Write-Host "Installing module: $Name" -ForegroundColor Cyan
        Install-Module $Name -Scope CurrentUser -Force -AllowClobber
    }
}

function Wait-ProviderRegistration {
    param ([string]$Namespace)
    $state = ""
    $attempts = 0
    while ($state -ne "Registered" -and $attempts -lt 30) {
        $state = Invoke-AzCli -ArgumentList @("provider", "show", "--namespace", $Namespace, "--query", "registrationState", "--output", "tsv")
        if ($state -eq "Registered") { break }
        Start-Sleep -Seconds 10
        $attempts++
    }
    if ($state -ne "Registered") {
        throw "Timed out waiting for $Namespace provider to register."
    }
}

# ---------------------------------------------------------------------------
# Saved deployment defaults
# ---------------------------------------------------------------------------

$configPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".astral-deploy.json"
$savedConfig = $null
if (Test-Path $configPath) {
    try {
        $savedConfig = Get-Content $configPath -Raw | ConvertFrom-Json
        Write-Host "Loaded saved deployment defaults from $configPath" -ForegroundColor Green
    } catch {
        Write-Warning "Could not parse $configPath. Starting with empty defaults."
    }
}

if ($savedConfig) {
    if (-not $PSBoundParameters.ContainsKey('AdoOrganization'))     { $AdoOrganization     = $savedConfig.AdoOrganization }
    if (-not $PSBoundParameters.ContainsKey('AdoProject'))          { $AdoProject          = $savedConfig.AdoProject }
    if (-not $PSBoundParameters.ContainsKey('AdoPipelineId'))       { $AdoPipelineId       = $savedConfig.AdoPipelineId }
    if (-not $PSBoundParameters.ContainsKey('AdoBranch'))           { $AdoBranch           = $savedConfig.AdoBranch }
    if (-not $PSBoundParameters.ContainsKey('ResourceGroup'))       { $ResourceGroup       = $savedConfig.ResourceGroup }
    if (-not $PSBoundParameters.ContainsKey('Location'))            { $Location            = $savedConfig.Location }
    if (-not $PSBoundParameters.ContainsKey('McpContainerAppName')) { $McpContainerAppName = $savedConfig.McpContainerAppName }
    if (-not $PSBoundParameters.ContainsKey('McpAcrName'))          { $McpAcrName          = $savedConfig.McpAcrName }
    if (-not $PSBoundParameters.ContainsKey('McpResourceGroup'))    { $McpResourceGroup    = $savedConfig.McpResourceGroup }
    if (-not $PSBoundParameters.ContainsKey('McpLocation'))         { $McpLocation         = $savedConfig.McpLocation }
    if (-not $PSBoundParameters.ContainsKey('EnableMcpEntraIdAuth')) {
        if ($savedConfig.EnableMcpEntraIdAuth -eq $true) { $EnableMcpEntraIdAuth = $true }
    }
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

Write-Host "=== ASTRAL Change Probe Provisioning ===" -ForegroundColor Green

if (-not (Test-Command "az")) {
    throw "Azure CLI (az) is not installed or not in PATH. Install from https://aka.ms/installazurecli"
}

Write-Host "Checking Microsoft Graph modules..." -ForegroundColor Cyan
Test-ModuleInstalled "Microsoft.Graph.Applications"
Import-Module Microsoft.Graph.Applications

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

Write-Host "`n--- Azure DevOps Settings ---" -ForegroundColor Cyan
$AdoOrganization = Get-OrPrompt -Value $AdoOrganization -Prompt "Azure DevOps Organization (e.g. 'cqre')"
$AdoProject      = Get-OrPrompt -Value $AdoProject      -Prompt "Azure DevOps Project"
$AdoPipelineId   = Get-OrPrompt -Value $AdoPipelineId   -Prompt "Azure DevOps Pipeline ID (numeric)"
$AdoToken        = Get-OrPrompt -Value $AdoToken        -Prompt "Azure DevOps PAT (Build Read & Execute)" -Sensitive

# ---------------------------------------------------------------------------
# MCP Server deployment decision
# ---------------------------------------------------------------------------

$mcpDeploy = $false
if ($SkipMcpServer) {
    $mcpDeploy = $false
    Write-Host "`nSkipping MCP server deployment (-SkipMcpServer specified)." -ForegroundColor Yellow
} elseif ($DeployMcpServer -or $DeployMcpOnly) {
    $mcpDeploy = $true
    Write-Host "`nMCP server deployment enabled." -ForegroundColor Green
} else {
    $mcpAnswer = Read-Host -Prompt "`nDeploy ASTRAL MCP Server to Azure Container Apps? [Y/n]"
    $mcpDeploy = ($mcpAnswer -eq "" -or $mcpAnswer -match "^[Yy]")
}

if ($mcpDeploy) {
    if (-not $McpResourceGroup) { $McpResourceGroup = $ResourceGroup }
    $McpContainerAppName = Get-OrPrompt -Value $McpContainerAppName -Prompt "MCP Container App name (e.g. 'ca-astral-mcp')"
    $McpAcrName = Get-OrPrompt -Value $McpAcrName -Prompt "Azure Container Registry name (globally unique, lowercase, e.g. 'acrastral123')"

    # Authentication choice
    $mcpEnableEntraIdAuth = $false
    if ($EnableMcpEntraIdAuth) {
        $mcpEnableEntraIdAuth = $true
        Write-Host "Microsoft Entra ID authentication enabled (-EnableMcpEntraIdAuth specified)." -ForegroundColor Green
    } else {
        $authAnswer = Read-Host -Prompt "`nUse Microsoft Entra ID authentication for the MCP endpoint? [Y/n]"
        $mcpEnableEntraIdAuth = ($authAnswer -eq "" -or $authAnswer -match "^[Yy]")
    }

    if (-not $mcpEnableEntraIdAuth) {
        if (-not $McpApiKey) {
            $McpApiKey = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
        }
    }
}

# ---------------------------------------------------------------------------
# Region selection
# ---------------------------------------------------------------------------

$locationExplicit = $PSBoundParameters.ContainsKey('Location')
$mcpLocationExplicit = $PSBoundParameters.ContainsKey('McpLocation')

if (-not $locationExplicit -and -not $mcpLocationExplicit) {
    $regionAnswer = Read-Host -Prompt "`nAzure region for resources [$Location]"
    if ($regionAnswer) { $Location = $regionAnswer }
} elseif (-not $locationExplicit -and $mcpLocationExplicit -and -not $DeployMcpOnly) {
    $Location = $McpLocation
}
if ($mcpDeploy -and -not $mcpLocationExplicit) { $McpLocation = $Location }
if ($DeployMcpOnly -and $McpLocation)  { $Location = $McpLocation }

# ---------------------------------------------------------------------------
# Graph authentication & App registration
# ---------------------------------------------------------------------------

$probeAppId     = "N/A"
$probeAppSecret = ""
$tenantId       = ""
$mcpAuthAppId   = ""
$mcpAuthSecret  = ""

$needGraph = (-not $DeployMcpOnly) -or ($mcpDeploy -and $mcpEnableEntraIdAuth)

if ($needGraph) {
    Write-Host "`nConnecting to Microsoft Graph..." -ForegroundColor Cyan
    Connect-MgGraph -Scopes "Application.ReadWrite.All","AppRoleAssignment.ReadWrite.All","Directory.Read.All" -NoWelcome

    $tenant = Get-MgOrganization | Select-Object -First 1
    $tenantId = $tenant.Id
    Write-Host "Tenant: $($tenant.DisplayName) ($tenantId)" -ForegroundColor Green

    # ---------------------------------------------------------------------------
    # MCP Entra ID authentication app (if requested)
    # ---------------------------------------------------------------------------
    if ($mcpDeploy -and $mcpEnableEntraIdAuth) {
        $mcpAuthAppName = "ASTRAL MCP Server"
        $existingMcpAuthApp = Get-MgApplication -Filter "displayName eq '$mcpAuthAppName'" | Select-Object -First 1
        if ($existingMcpAuthApp) {
            Write-Host "Found existing MCP auth app: $($existingMcpAuthApp.AppId)" -ForegroundColor Yellow
            $mcpAuthApp = $existingMcpAuthApp
        } else {
            Write-Host "Creating MCP authentication app registration: $mcpAuthAppName" -ForegroundColor Cyan
            $mcpAuthApp = New-MgApplication -DisplayName $mcpAuthAppName -SignInAudience "AzureADMyOrg"
            Write-Host "Created MCP auth app. AppId: $($mcpAuthApp.AppId)" -ForegroundColor Green
        }

        # Rotate client secret
        $mcpAuthSecretDesc = "MCPAuthSecret"
        $mcpAuthAppWithCreds = Get-MgApplication -ApplicationId $mcpAuthApp.Id -Property "id,passwordCredentials"
        $existingMcpAuthSecrets = $mcpAuthAppWithCreds.PasswordCredentials | Where-Object { $_.DisplayName -eq $mcpAuthSecretDesc }
        foreach ($cred in $existingMcpAuthSecrets) {
            Write-Host "Removing old MCP auth secret ($($cred.KeyId))..." -ForegroundColor Yellow
            Remove-MgApplicationPassword -ApplicationId $mcpAuthApp.Id -BodyParameter @{ "keyId" = $cred.KeyId }
        }

        Write-Host "Creating new MCP auth client secret (valid 1 year)..." -ForegroundColor Cyan
        $mcpAuthPasswordCred = @{
            displayName = $mcpAuthSecretDesc
            endDateTime = (Get-Date).AddYears(1).ToString("o")
        }
        $mcpAuthSecretObj = Add-MgApplicationPassword -ApplicationId $mcpAuthApp.Id -BodyParameter $mcpAuthPasswordCred
        $mcpAuthAppId  = $mcpAuthApp.AppId
        $mcpAuthSecret = $mcpAuthSecretObj.SecretText
    }

    # ---------------------------------------------------------------------------
    # Change Probe App registration (only if deploying change probe)
    # ---------------------------------------------------------------------------
    if (-not $DeployMcpOnly) {
        $requiredPermissions = @(
            "AuditLog.Read.All",
            "DeviceManagementApps.Read.All",
            "DeviceManagementConfiguration.Read.All",
            "DeviceManagementManagedDevices.Read.All",
            "DeviceManagementScripts.Read.All",
            "DeviceManagementServiceConfig.Read.All"
        )

        $graphSp = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"
        if (-not $graphSp) { throw "Microsoft Graph service principal not found." }

        $appRoles = @()
        foreach ($permName in $requiredPermissions) {
            $appRole = $graphSp.AppRoles | Where-Object { $_.Value -eq $permName } | Select-Object -First 1
            if (-not $appRole) {
                Write-Warning "Permission '$permName' not found. Skipping."
                continue
            }
            $appRoles += $appRole
        }

        $resourceAccess = @()
        foreach ($ar in $appRoles) {
            $resourceAccess += @{ id = $ar.Id; type = "Role" }
        }

        $requiredResourceAccess = @(
            @{
                resourceAppId  = $graphSp.AppId
                resourceAccess = $resourceAccess
            }
        )

        $existingApp = Get-MgApplication -Filter "displayName eq '$AppDisplayName'" | Select-Object -First 1
        if ($existingApp) {
            Write-Host "Found existing app registration: $($existingApp.AppId)" -ForegroundColor Yellow
            $app = $existingApp
            Update-MgApplication -ApplicationId $app.Id -RequiredResourceAccess $requiredResourceAccess
            Write-Host "Updated required resource access." -ForegroundColor Green
        } else {
            Write-Host "Creating app registration: $AppDisplayName" -ForegroundColor Cyan
            $app = New-MgApplication -DisplayName $AppDisplayName -SignInAudience "AzureADMyOrg" -RequiredResourceAccess $requiredResourceAccess
            Write-Host "Created app registration. AppId: $($app.AppId)" -ForegroundColor Green
        }

        $sp = Get-MgServicePrincipal -Filter "appId eq '$($app.AppId)'" | Select-Object -First 1
        if (-not $sp) {
            Write-Host "Creating service principal..." -ForegroundColor Cyan
            $sp = New-MgServicePrincipal -AppId $app.AppId
        }

        Write-Host "Granting admin consent..." -ForegroundColor Cyan
        foreach ($ar in $appRoles) {
            $existingAssignment = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id | Where-Object { $_.AppRoleId -eq $ar.Id }
            if (-not $existingAssignment) {
                New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id -PrincipalId $sp.Id -ResourceId $graphSp.Id -AppRoleId $ar.Id | Out-Null
            }
        }
        Write-Host "Admin consent granted." -ForegroundColor Green

        # Client secret
        $secretDescription = "ChangeProbeSecret"
        $appWithCreds = Get-MgApplication -ApplicationId $app.Id -Property "id,passwordCredentials"
        $existingSecrets = $appWithCreds.PasswordCredentials | Where-Object { $_.DisplayName -eq $secretDescription }
        foreach ($cred in $existingSecrets) {
            Write-Host "Removing old client secret ($($cred.KeyId))..." -ForegroundColor Yellow
            Remove-MgApplicationPassword -ApplicationId $app.Id -BodyParameter @{ "keyId" = $cred.KeyId }
        }

        Write-Host "Creating new client secret (valid 1 year)..." -ForegroundColor Cyan
        $passwordCred = @{
            displayName = $secretDescription
            endDateTime = (Get-Date).AddYears(1).ToString("o")
        }
        $secret = Add-MgApplicationPassword -ApplicationId $app.Id -BodyParameter $passwordCred
        $probeAppId     = $app.AppId
        $probeAppSecret = $secret.SecretText
    }
} else {
    Write-Host "`nSkipping Graph authentication (not required for this deployment)." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Azure authentication
# ---------------------------------------------------------------------------

Write-Host "`n--- Azure Resources ---" -ForegroundColor Cyan

function Ensure-AzLogin {
    param ([string]$TenantId)
    try {
        $null = Invoke-AzCli -ArgumentList @("account", "show", "--output", "none")
    } catch {
        if ($_ -match "az login") {
            $answer = Read-Host -Prompt "You are not logged in to Azure CLI. Run 'az login' now? [Y/n]"
            if ($answer -eq "" -or $answer -match "^[Yy]") {
                if ($TenantId) {
                    & az login --tenant $TenantId | Out-Host
                } else {
                    & az login | Out-Host
                }
                if ($LASTEXITCODE -ne 0) {
                    throw "az login failed. Please run 'az login' manually and retry."
                }
            } else {
                throw "Azure login required. Run 'az login' and retry."
            }
        } else {
            throw
        }
    }
}

Ensure-AzLogin -TenantId $tenantId

function Select-Subscription {
    param ([string]$CurrentId)
    # Run az directly and filter out stderr warning objects so only stdout strings reach ConvertFrom-Json.
    $lines = & az account list --output json 2>&1
    $stringLines = $lines | Where-Object { $_ -is [string] }
    if ($LASTEXITCODE -ne 0) {
        $errorLines = $lines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() }
        throw "az account list failed:`n$($errorLines -join "`n")"
    }
    $subs = ($stringLines -join "`n") | ConvertFrom-Json
    if ($subs.Count -eq 0) {
        throw "No Azure subscriptions found. Ensure your account has access to at least one subscription."
    }
    if ($subs.Count -eq 1) {
        $sub = $subs[0]
        Invoke-AzCli -ArgumentList @("account", "set", "--subscription", $sub.id)
        return $sub
    }
    Write-Host "`nAvailable subscriptions:" -ForegroundColor Cyan
    for ($i = 0; $i -lt $subs.Count; $i++) {
        $marker = if ($subs[$i].id -eq $CurrentId) { " (*)" } else { "" }
        Write-Host "  [$i] $($subs[$i].name) ($($subs[$i].id))$marker"
    }
    $selection = Read-Host -Prompt "Select subscription by number"
    if (-not [int]::TryParse($selection, [ref]$null)) {
        throw "Invalid selection. Aborting."
    }
    $chosen = $subs[[int]$selection]
    if (-not $chosen) {
        throw "Invalid selection. Aborting."
    }
    Invoke-AzCli -ArgumentList @("account", "set", "--subscription", $chosen.id)
    return $chosen
}

$azLines = & az account show --output json 2>&1
$azStringLines = $azLines | Where-Object { $_ -is [string] }
if ($LASTEXITCODE -ne 0) {
    $azErrorLines = $azLines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() }
    throw "az account show failed:`n$($azErrorLines -join "`n")"
}
$azAccount = ($azStringLines -join "`n") | ConvertFrom-Json
$currentSubId = $azAccount.id

function Get-SubscriptionTenantId {
    param ([string]$SubId)
    $lines = & az account list --output json 2>&1
    $stringLines = $lines | Where-Object { $_ -is [string] }
    $subs = ($stringLines -join "`n") | ConvertFrom-Json
    $sub = $subs | Where-Object { $_.id -eq $SubId } | Select-Object -First 1
    if ($sub) { return $sub.tenantId } else { return $null }
}

if ($SubscriptionId) {
    Invoke-AzCli -ArgumentList @("account", "set", "--subscription", $SubscriptionId)
    $subTenantId = Get-SubscriptionTenantId -SubId $SubscriptionId
    $azTenantLines = & az account show --query tenantId --output tsv 2>&1 | Where-Object { $_ -is [string] }
    $azTenantId = ($azTenantLines -join "").Trim()
    if ($subTenantId -and $azTenantId -ne $subTenantId) {
        Write-Host "`nSubscription '$SubscriptionId' belongs to tenant '$subTenantId' but current az context is '$azTenantId'." -ForegroundColor Yellow
        Write-Host "Re-authenticating to the subscription's tenant..." -ForegroundColor Yellow
        & az account clear | Out-Null
        & az login --tenant $subTenantId | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "az login --tenant $subTenantId failed." }
        Invoke-AzCli -ArgumentList @("account", "set", "--subscription", $SubscriptionId)
    }
    Write-Host "Using specified subscription: $SubscriptionId" -ForegroundColor Green
} else {
    $chosenSub = Select-Subscription -CurrentId $currentSubId
    $SubscriptionId = $chosenSub.id
    $subTenantId = $chosenSub.tenantId
    $azTenantLines = & az account show --query tenantId --output tsv 2>&1 | Where-Object { $_ -is [string] }
    $azTenantId = ($azTenantLines -join "").Trim()
    if ($subTenantId -and $azTenantId -ne $subTenantId) {
        Write-Host "`nSubscription '$SubscriptionId' belongs to tenant '$subTenantId' but current az context is '$azTenantId'." -ForegroundColor Yellow
        Write-Host "Re-authenticating to the subscription's tenant..." -ForegroundColor Yellow
        & az account clear | Out-Null
        & az login --tenant $subTenantId | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "az login --tenant $subTenantId failed." }
        $chosenSub = Select-Subscription -CurrentId $SubscriptionId
        $SubscriptionId = $chosenSub.id
    }
    Write-Host "Using subscription: $SubscriptionId" -ForegroundColor Green
}

# Validate the subscription is accessible for ARM operations (catches tenant mismatches).
try {
    $null = Invoke-AzCli -ArgumentList @("group", "list", "--output", "none")
} catch {
    if ($_ -match "SubscriptionNotFound") {
        Write-Host "`nThe selected subscription is listed but ARM operations fail with 'SubscriptionNotFound'." -ForegroundColor Yellow
        Write-Host "This usually means the subscription belongs to a different Entra tenant." -ForegroundColor Yellow
        $subTenantId = Get-SubscriptionTenantId -SubId $SubscriptionId
        $promptTenant = if ($subTenantId) { $subTenantId } else { $tenantId }
        $answer = Read-Host -Prompt "Run 'az login --tenant $promptTenant' now and retry? [Y/n]"
        if ($answer -eq "" -or $answer -match "^[Yy]") {
            & az account clear | Out-Null
            & az login --tenant $promptTenant | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "az login --tenant failed. Please run it manually and retry."
            }
            $chosenSub = Select-Subscription -CurrentId $SubscriptionId
            $SubscriptionId = $chosenSub.id
            Write-Host "Using subscription: $SubscriptionId" -ForegroundColor Green
            # Validate again
            $null = Invoke-AzCli -ArgumentList @("group", "list", "--output", "none")
        } else {
            throw "Subscription validation failed. Run 'az login --tenant $promptTenant' and retry."
        }
    } else {
        throw
    }
}

# ---------------------------------------------------------------------------
# Resource Group
# ---------------------------------------------------------------------------

Write-Host "Ensuring resource group '$ResourceGroup'..." -ForegroundColor Cyan
Invoke-AzCli -ArgumentList @("group", "create", "--name", $ResourceGroup, "--location", $Location, "--output", "none")

# Quick diagnostic: confirm ARM can read back the RG in this subscription.
try {
    $diag = Invoke-AzCli -ArgumentList @("group", "show", "--name", $ResourceGroup, "--query", "id", "--output", "tsv")
    Write-Host "ARM context OK (RG id: $diag)" -ForegroundColor Green
} catch {
    Write-Host "WARNING: ARM diagnostic failed: $_" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Change Probe Infrastructure (skipped for MCP-only deployment)
# ---------------------------------------------------------------------------

if (-not $DeployMcpOnly) {
# ---------------------------------------------------------------------------
# Storage Account
# ---------------------------------------------------------------------------

$randomSuffix = [System.Guid]::NewGuid().ToString("n").Substring(0, 8)
$StorageName = "stastralprobe$randomSuffix"
$FunctionAppName = "func-astral-probe-$randomSuffix"

Write-Host "Creating storage account '$StorageName'..." -ForegroundColor Cyan

# Ensure Microsoft.Storage provider is registered (required for new subscriptions).
$storageProv = Invoke-AzCli -ArgumentList @("provider", "show", "--namespace", "Microsoft.Storage", "--query", "registrationState", "--output", "tsv")
if ($storageProv -ne "Registered") {
    Write-Host "Registering Microsoft.Storage provider..." -ForegroundColor Yellow
    Invoke-AzCli -ArgumentList @("provider", "register", "--namespace", "Microsoft.Storage")
    Wait-ProviderRegistration -Namespace "Microsoft.Storage"
    Write-Host "Microsoft.Storage registered." -ForegroundColor Green
}

Invoke-AzCli -ArgumentList @(
    "storage", "account", "create",
    "--name", $StorageName,
    "--resource-group", $ResourceGroup,
    "--location", $Location,
    "--sku", "Standard_LRS",
    "--kind", "StorageV2",
    "--output", "none"
)

$storageConnection = Invoke-AzCli -ArgumentList @(
    "storage", "account", "show-connection-string",
    "--name", $StorageName,
    "--resource-group", $ResourceGroup,
    "--query", "connectionString",
    "--output", "tsv"
)

# ---------------------------------------------------------------------------
# Table and Queue
# ---------------------------------------------------------------------------

Write-Host "Creating Table and Queue..." -ForegroundColor Cyan
Invoke-AzCli -ArgumentList @("storage", "table", "create", "--name", "ProbeState", "--connection-string", $storageConnection, "--output", "none")
Invoke-AzCli -ArgumentList @("storage", "queue", "create", "--name", "backup-trigger-queue", "--connection-string", $storageConnection, "--output", "none")

# ---------------------------------------------------------------------------
# Function App
# ---------------------------------------------------------------------------

# Ensure Microsoft.Web provider is registered (required for Function Apps).
$webProv = Invoke-AzCli -ArgumentList @("provider", "show", "--namespace", "Microsoft.Web", "--query", "registrationState", "--output", "tsv")
if ($webProv -ne "Registered") {
    Write-Host "Registering Microsoft.Web provider..." -ForegroundColor Yellow
    Invoke-AzCli -ArgumentList @("provider", "register", "--namespace", "Microsoft.Web")
    Wait-ProviderRegistration -Namespace "Microsoft.Web"
    Write-Host "Microsoft.Web registered." -ForegroundColor Green
}

Write-Host "Creating Function App '$FunctionAppName'..." -ForegroundColor Cyan
Invoke-AzCli -ArgumentList @(
    "functionapp", "create",
    "--name", $FunctionAppName,
    "--resource-group", $ResourceGroup,
    "--storage-account", $StorageName,
    "--consumption-plan-location", $Location,
    "--os-type", "Linux",
    "--runtime", "python",
    "--runtime-version", "3.11",
    "--functions-version", "4",
    "--output", "none"
)

# ---------------------------------------------------------------------------
# App Settings
# ---------------------------------------------------------------------------

Write-Host "Configuring Function App settings..." -ForegroundColor Cyan
Invoke-AzCli -ArgumentList @(
    "functionapp", "config", "appsettings", "set",
    "--name", $FunctionAppName,
    "--resource-group", $ResourceGroup,
    "--settings",
    "AzureWebJobsStorage=$storageConnection",
    "FUNCTIONS_EXTENSION_VERSION=~4",
    "FUNCTIONS_WORKER_RUNTIME=python",
    "WEBSITE_RUN_FROM_PACKAGE=1",
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
    "PROBE_APP_ID=$probeAppId",
    "PROBE_APP_SECRET=$probeAppSecret",
    "TENANT_ID=$tenantId",
    "GRAPH_TOKEN=",
    "ADO_ORGANIZATION=$AdoOrganization",
    "ADO_PROJECT=$AdoProject",
    "ADO_PIPELINE_ID=$AdoPipelineId",
    "ADO_TOKEN=$AdoToken",
    "ADO_BRANCH=$AdoBranch",
    "PROBE_QUIET_WINDOW_MINUTES=$QuietWindowMinutes",
    "PROBE_COOLDOWN_MINUTES=$CooldownMinutes",
    "REPO_ROOT=/home/site/wwwroot",
    "--output", "none"
)
}

# ---------------------------------------------------------------------------
# MCP Server (Azure Container Apps)

# ---------------------------------------------------------------------------

if ($mcpDeploy) {
    Write-Host "`n=== ASTRAL MCP Server Provisioning ===" -ForegroundColor Green

    # Ensure Container Apps provider is registered.
    $caProv = Invoke-AzCli -ArgumentList @("provider", "show", "--namespace", "Microsoft.App", "--query", "registrationState", "--output", "tsv")
    if ($caProv -ne "Registered") {
        Write-Host "Registering Microsoft.App provider..." -ForegroundColor Yellow
        Invoke-AzCli -ArgumentList @("provider", "register", "--namespace", "Microsoft.App")
        Wait-ProviderRegistration -Namespace "Microsoft.App"
        Write-Host "Microsoft.App registered." -ForegroundColor Green
    }

    # Ensure Microsoft.OperationalInsights provider (required for Container Apps Environment logging).
    $logProv = Invoke-AzCli -ArgumentList @("provider", "show", "--namespace", "Microsoft.OperationalInsights", "--query", "registrationState", "--output", "tsv")
    if ($logProv -ne "Registered") {
        Write-Host "Registering Microsoft.OperationalInsights provider..." -ForegroundColor Yellow
        Invoke-AzCli -ArgumentList @("provider", "register", "--namespace", "Microsoft.OperationalInsights")
        Wait-ProviderRegistration -Namespace "Microsoft.OperationalInsights"
        Write-Host "Microsoft.OperationalInsights registered." -ForegroundColor Green
    }

    # Ensure Microsoft.ContainerRegistry provider (required for ACR).
    $acrProv = Invoke-AzCli -ArgumentList @("provider", "show", "--namespace", "Microsoft.ContainerRegistry", "--query", "registrationState", "--output", "tsv")
    if ($acrProv -ne "Registered") {
        Write-Host "Registering Microsoft.ContainerRegistry provider..." -ForegroundColor Yellow
        Invoke-AzCli -ArgumentList @("provider", "register", "--namespace", "Microsoft.ContainerRegistry")
        Wait-ProviderRegistration -Namespace "Microsoft.ContainerRegistry"
        Write-Host "Microsoft.ContainerRegistry registered." -ForegroundColor Green
    }

    # Create or reuse Azure Container Registry.
    $acrExists = $false
    try {
        $null = Invoke-AzCli -ArgumentList @("acr", "show", "--name", $McpAcrName, "--resource-group", $McpResourceGroup, "--output", "none") -NoRetry
        $acrExists = $true
    } catch {
        $acrExists = $false
    }
    if (-not $acrExists) {
        Write-Host "Creating Azure Container Registry '$McpAcrName'..." -ForegroundColor Cyan
        Invoke-AzCli -ArgumentList @(
            "acr", "create",
            "--name", $McpAcrName,
            "--resource-group", $McpResourceGroup,
            "--location", $McpLocation,
            "--sku", "Basic",
            "--admin-enabled", "true",
            "--output", "none"
        )
    } else {
        Write-Host "Using existing Azure Container Registry '$McpAcrName'." -ForegroundColor Green
    }

    # Build container image via ACR Task (no local Docker required).
    # We create a minimal build context in a temp directory so we don't upload
    # the entire repo (which can be hundreds of MB and cause timeouts).
    Write-Host "Building MCP server image in Azure Container Registry..." -ForegroundColor Cyan
    Write-Host "This may take 2-5 minutes. Build logs will stream below..." -ForegroundColor Yellow
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $buildTemp = Join-Path ([System.IO.Path]::GetTempPath()) "astral-mcp-build-$([System.Guid]::NewGuid().ToString("n").Substring(0, 8))"
    try {
        # Prepare minimal build context.
        $mcpDir = Join-Path $buildTemp "infra" "mcp-server"
        $scriptsDir = Join-Path $buildTemp "scripts"
        New-Item -ItemType Directory -Path $mcpDir -Force | Out-Null
        New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null

        Copy-Item (Join-Path $repoRoot "infra" "mcp-server" "Dockerfile")      $mcpDir -Force
        Copy-Item (Join-Path $repoRoot "infra" "mcp-server" "requirements.txt") $mcpDir -Force
        Copy-Item (Join-Path $repoRoot "infra" "mcp-server" "mcp_server.py")    $mcpDir -Force
        Copy-Item (Join-Path $repoRoot "scripts" "common.py")                   $scriptsDir -Force
        Copy-Item (Join-Path $repoRoot "scripts" "astral_mcp_tools.py")         $scriptsDir -Force

        Push-Location $buildTemp
        try {
            # Stream build output live so the user sees progress.
            & az acr build `
                --registry $McpAcrName `
                --image "$McpImageName`:latest" `
                --file "infra/mcp-server/Dockerfile" `
                --resource-group $McpResourceGroup `
                .
            if ($LASTEXITCODE -ne 0) {
                throw "az acr build failed. Check the output above for details."
            }
            Write-Host "Image build complete." -ForegroundColor Green
        } finally {
            Pop-Location
        }
    } finally {
        if (Test-Path $buildTemp) {
            Remove-Item -Recurse -Force $buildTemp
        }
    }

    # Create Container Apps Environment.
    $caEnvName = "$McpContainerAppName-env"
    Write-Host "Creating Container Apps Environment '$caEnvName'..." -ForegroundColor Cyan
    Invoke-AzCli -ArgumentList @(
        "containerapp", "env", "create",
        "--name", $caEnvName,
        "--resource-group", $McpResourceGroup,
        "--location", $McpLocation,
        "--output", "none"
    )

    # Create Container App.
    Write-Host "Creating Container App '$McpContainerAppName'..." -ForegroundColor Cyan
    $acrLoginServer = Invoke-AzCli -ArgumentList @(
        "acr", "show",
        "--name", $McpAcrName,
        "--query", "loginServer",
        "--output", "tsv"
    )

    # Retrieve ACR admin credentials so the Container App can pull the image.
    $acrUsername = $McpAcrName
    $acrPassword = Invoke-AzCli -ArgumentList @(
        "acr", "credential", "show",
        "--name", $McpAcrName,
        "--query", "passwords[0].value",
        "--output", "tsv"
    )

    Invoke-AzCli -ArgumentList @(
        "containerapp", "create",
        "--name", $McpContainerAppName,
        "--resource-group", $McpResourceGroup,
        "--environment", $caEnvName,
        "--image", "$acrLoginServer/$McpImageName`:latest",
        "--target-port", "8080",
        "--ingress", "external",
        "--transport", "auto",
        "--min-replicas", "1",
        "--max-replicas", "3",
        "--cpu", "0.5",
        "--memory", "1Gi",
        "--registry-server", $acrLoginServer,
        "--registry-username", $acrUsername,
        "--registry-password", $acrPassword,
        "--output", "none"
    )

    # Configure environment variables.
    Write-Host "Configuring MCP Container App settings..." -ForegroundColor Cyan
    Invoke-AzCli -ArgumentList @(
        "containerapp", "update",
        "--name", $McpContainerAppName,
        "--resource-group", $McpResourceGroup,
        "--set-env-vars",
        "ADO_ORGANIZATION=$AdoOrganization",
        "ADO_PROJECT=$AdoProject",
        "ADO_BRANCH=$AdoBranch",
        "ADO_TOKEN=$AdoToken",
        "--output", "none"
    )

    # Retrieve FQDN for summary.
    $mcpFqdn = Invoke-AzCli -ArgumentList @(
        "containerapp", "show",
        "--name", $McpContainerAppName,
        "--resource-group", $McpResourceGroup,
        "--query", "properties.configuration.ingress.fqdn",
        "--output", "tsv"
    )

    Write-Host "MCP Server deployed. Endpoint: https://$mcpFqdn" -ForegroundColor Green

    # Configure Entra ID authentication on the Container App.
    if ($mcpEnableEntraIdAuth) {
        Write-Host "Configuring Microsoft Entra ID authentication..." -ForegroundColor Cyan

        Invoke-AzCli -ArgumentList @(
            "containerapp", "auth", "microsoft", "update",
            "--name", $McpContainerAppName,
            "--resource-group", $McpResourceGroup,
            "--client-id", $mcpAuthAppId,
            "--client-secret", $mcpAuthSecret,
            "--issuer", "https://login.microsoftonline.com/$tenantId/v2.0",
            "--yes",
            "--output", "none"
        )

        Invoke-AzCli -ArgumentList @(
            "containerapp", "auth", "update",
            "--name", $McpContainerAppName,
            "--resource-group", $McpResourceGroup,
            "--enabled", "true",
            "--unauthenticated-client-action", "Return401",
            "--output", "none"
        )

        Write-Host "Entra ID authentication enabled." -ForegroundColor Green
        Write-Host "MCP Auth App ID: $mcpAuthAppId" -ForegroundColor Green
        Write-Host "Restarting Container App to apply authentication settings..." -ForegroundColor Cyan
        [string]$activeRevision = Invoke-AzCli -ArgumentList @(
            "containerapp", "revision", "list",
            "--name", $McpContainerAppName,
            "--resource-group", $McpResourceGroup,
            "--query", "[?active==true].name | [0]",
            "--output", "tsv"
        )
        $activeRevision = $activeRevision.Trim()
        if (-not $activeRevision) {
            Write-Warning "Could not determine active revision for restart. You may need to restart manually via: az containerapp revision restart -n $McpContainerAppName -g $McpResourceGroup --revision <revision-name>"
        } else {
            Invoke-AzCli -ArgumentList @(
                "containerapp", "revision", "restart",
                "--name", $McpContainerAppName,
                "--resource-group", $McpResourceGroup,
                "--revision", $activeRevision,
                "--output", "none"
            )
            Write-Host "Container App restarted." -ForegroundColor Green
        }
    }
}

# ---------------------------------------------------------------------------
# Optional: code deployment (skipped for MCP-only deployment)
# ---------------------------------------------------------------------------

if (-not $DeployMcpOnly) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $probePath = Join-Path $repoRoot "infra" "change-probe"
    if (Test-Path $probePath) {
        $deployNow = Read-Host -Prompt "`nDeploy function code now? [Y/n]"
        if ($deployNow -eq "" -or $deployNow -match "^[Yy]") {
            Write-Host "Deploying function code via Azure CLI..." -ForegroundColor Cyan
            $zipName = "astral-probe-$([System.Guid]::NewGuid().ToString('n').Substring(0,8)).zip"
            $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) $zipName
            $exclude = @("__pycache__",".venv",".env",".DS_Store",".gitignore","*.zip")
            $items = Get-ChildItem -Path $probePath | Where-Object {
                $name = $_.Name
                $excluded = $false
                foreach ($pattern in $exclude) {
                    if ($name -like $pattern) { $excluded = $true; break }
                }
                -not $excluded
            }
            if ($items) {
                # Stage files in a clean temp directory to avoid path/permission issues
                $stagePath = Join-Path ([System.IO.Path]::GetTempPath()) "astral-probe-stage-$([System.Guid]::NewGuid().ToString('n').Substring(0,8))"
                New-Item -ItemType Directory -Path $stagePath | Out-Null
                try {
                    foreach ($item in $items) {
                        $dest = Join-Path $stagePath $item.Name
                        if ($item.PSIsContainer) {
                            Copy-Item -Path $item.FullName -Destination $dest -Recurse -Force
                        } else {
                            Copy-Item -Path $item.FullName -Destination $dest -Force
                        }
                    }
                    $localSettings = Join-Path $stagePath "local.settings.json"
                    if (Test-Path $localSettings) { Remove-Item $localSettings }

                    if (Test-Command "zip") {
                        Push-Location $stagePath
                        try {
                            & zip -r $zipPath . | Out-Null
                        } finally {
                            Pop-Location
                        }
                    } else {
                        Compress-Archive -Path "$stagePath/*" -DestinationPath $zipPath -Force
                    }
                } finally {
                    Remove-Item $stagePath -Recurse -Force -ErrorAction SilentlyContinue
                }

                # Get storage account name from Function App settings
                $storageConnection = Invoke-AzCli -ArgumentList @(
                    "functionapp", "config", "appsettings", "list",
                    "--name", $FunctionAppName,
                    "--resource-group", $ResourceGroup,
                    "--query", "[?name=='AzureWebJobsStorage'].value",
                    "--output", "tsv"
                )
                if (-not $storageConnection) {
                    throw "Could not retrieve AzureWebJobsStorage for $FunctionAppName"
                }
                $storageName = ($storageConnection -split ';' | Where-Object { $_ -match '^AccountName=' }) -replace '^AccountName=', ''
                if (-not $storageName) {
                    throw "Could not parse storage account name from connection string"
                }

                $storageKey = Invoke-AzCli -ArgumentList @(
                    "storage", "account", "keys", "list",
                    "--account-name", $storageName,
                    "--query", "[0].value",
                    "--output", "tsv"
                )

                $container = "function-releases"
                $blobName = "deploy-$([System.Guid]::NewGuid().ToString('n')).zip"
                $expiry = (Get-Date).AddYears(10).ToString("yyyy-MM-ddTHH:mm:ssZ")

                $null = Invoke-AzCli -ArgumentList @(
                    "storage", "container", "create",
                    "--name", $container,
                    "--account-name", $storageName,
                    "--account-key", $storageKey,
                    "--output", "none"
                ) -NoRetry

                $null = Invoke-AzCli -ArgumentList @(
                    "storage", "blob", "upload",
                    "--container-name", $container,
                    "--file", $zipPath,
                    "--name", $blobName,
                    "--account-name", $storageName,
                    "--account-key", $storageKey,
                    "--output", "none",
                    "--overwrite"
                ) -NoRetry

                $sasToken = Invoke-AzCli -ArgumentList @(
                    "storage", "blob", "generate-sas",
                    "--container-name", $container,
                    "--name", $blobName,
                    "--account-name", $storageName,
                    "--account-key", $storageKey,
                    "--permissions", "r",
                    "--expiry", $expiry,
                    "--output", "tsv"
                ) -NoRetry

                $sasUrl = "https://$storageName.blob.core.windows.net/$container/$blobName`?$sasToken"

                $null = Invoke-AzCli -ArgumentList @(
                    "functionapp", "config", "appsettings", "set",
                    "--name", $FunctionAppName,
                    "--resource-group", $ResourceGroup,
                    "--settings",
                    "WEBSITE_RUN_FROM_PACKAGE=$sasUrl",
                    "--output", "none"
                )

                $null = Invoke-AzCli -ArgumentList @(
                    "functionapp", "restart",
                    "--name", $FunctionAppName,
                    "--resource-group", $ResourceGroup,
                    "--output", "none"
                )

                Remove-Item $zipPath -ErrorAction SilentlyContinue
                Write-Host "Function code deployed." -ForegroundColor Green
            } else {
                Write-Warning "No files found to deploy in $probePath"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Save deployment defaults
# ---------------------------------------------------------------------------

$configToSave = [PSCustomObject]@{
    AdoOrganization      = $AdoOrganization
    AdoProject           = $AdoProject
    AdoPipelineId        = $AdoPipelineId
    AdoBranch            = $AdoBranch
    ResourceGroup        = $ResourceGroup
    Location             = $Location
    McpContainerAppName  = $McpContainerAppName
    McpAcrName           = $McpAcrName
    McpResourceGroup     = $McpResourceGroup
    McpLocation          = $McpLocation
    EnableMcpEntraIdAuth = [bool]$mcpEnableEntraIdAuth
}

try {
    $configToSave | ConvertTo-Json | Set-Content $configPath
    Write-Host "Deployment defaults saved to $configPath" -ForegroundColor Green
} catch {
    Write-Warning "Could not save deployment defaults: $_"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host "`n=== Provisioning Complete ===" -ForegroundColor Green
Write-Host "Subscription:     $SubscriptionId"
Write-Host "Resource Group:   $ResourceGroup"
if (-not $DeployMcpOnly) {
    Write-Host "Storage Account:  $StorageName"
    Write-Host "Function App:     $FunctionAppName"
    Write-Host "App Registration: $probeAppId"
}
if ($mcpDeploy) {
    Write-Host "MCP Container App: $McpContainerAppName"
    Write-Host "MCP Endpoint:      https://$mcpFqdn"
    Write-Host "MCP ACR:           $McpAcrName.azurecr.io"
    if ($mcpEnableEntraIdAuth) {
        Write-Host "MCP Auth:          Microsoft Entra ID (App ID: $mcpAuthAppId)"
    } else {
        Write-Host "MCP API Key:       $McpApiKey"
    }
}
Write-Host "`nNext steps:"
if (-not $DeployMcpOnly) {
    Write-Host "  - Verify the timer trigger in the Azure Portal or with:"
    Write-Host "    az functionapp function show --name $FunctionAppName --resource-group $ResourceGroup --function-name probe_timer"
    Write-Host "  - To redeploy code later:"
    Write-Host "    cd infra/change-probe && zip -r deploy.zip . -x '__pycache__/*' '.venv/*' '.env' '.DS_Store' && az functionapp deployment source config-zip --name $FunctionAppName --resource-group $ResourceGroup --src deploy.zip"
}
if ($mcpDeploy) {
    Write-Host "  - MCP Server is deployed. Configure your MCP client with:"
    Write-Host "    URL: https://$mcpFqdn/sse"
    if ($mcpEnableEntraIdAuth) {
        Write-Host "    Auth: Microsoft Entra ID (app registration: ASTRAL MCP Server)"
    } else {
        Write-Host "    Header: x-api-key: $McpApiKey"
    }
    Write-Host "  - To redeploy MCP image later:"
    Write-Host "    az acr build --registry $McpAcrName --image $McpImageName`:latest --file infra/mcp-server/Dockerfile ."
}
