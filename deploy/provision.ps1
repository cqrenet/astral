#requires -Version 5.1
<#
.SYNOPSIS
    One-stop provisioning script for ASTRAL (change probe + optional MCP server).

.DESCRIPTION
    Provisions everything ASTRAL needs in one pass:

      Entra ID
        - One app registration ("ASTRAL") covering both:
            * Graph application permissions for the change probe (service account)
            * OAuth delegated scope + PKCE for the MCP server (user auth)
        - Admin consent granted automatically
        - Client secret created for the probe
        - Redirect URIs registered for Claude Desktop and Cursor

      Azure
        - Resource Group
        - Storage Account + Table + Queue (for the change probe)
        - Function App on Linux Consumption plan (change probe)
        - Optionally: Azure Container Registry + Container App (MCP server)

    Any parameter omitted is prompted for interactively.
    Defaults from a previous run are saved to .astral-deploy.json at the repo root
    and restored on subsequent runs.

.PARAMETER AppDisplayName
    Display name for the Entra app registration. Default: "ASTRAL".

.PARAMETER ResourceGroup
    Azure resource group name. Default: "rg-astral".

.PARAMETER Location
    Azure region. Default: "swedencentral".

.PARAMETER SubscriptionId
    Azure subscription ID. If omitted, you are prompted to choose.

.PARAMETER AdoOrganization
    Azure DevOps organization name (e.g. "contoso").

.PARAMETER AdoProject
    Azure DevOps project name.

.PARAMETER AdoPipelineId
    Numeric pipeline ID of azure-pipelines.yml. Find it as definitionId=XX in the
    pipeline URL.

.PARAMETER AdoToken
    Azure DevOps PAT with Build (Read & Execute) scope.

.PARAMETER AdoBranch
    Git branch the pipeline should target. Default: "main".

.PARAMETER QuietWindowMinutes
    Change probe debouncer quiet window. Default: 15.

.PARAMETER CooldownMinutes
    Change probe debouncer cooldown. Default: 30.

.PARAMETER DeployMcpServer
    Deploy the MCP server to Azure Container Apps without prompting.

.PARAMETER SkipMcpServer
    Skip the MCP server deployment without prompting.

.PARAMETER DeployMcpOnly
    Skip the change probe and deploy only the MCP server.

.PARAMETER McpContainerAppName
    Name for the Container App. Default prompted interactively.

.PARAMETER McpAcrName
    Azure Container Registry name (globally unique, lowercase). Default prompted.

.EXAMPLE
    .\provision.ps1

.EXAMPLE
    .\provision.ps1 -ResourceGroup "rg-astral-prod" -Location "swedencentral" -DeployMcpServer
#>
[CmdletBinding()]
param (
    [string]$AppDisplayName = "ASTRAL - Admin Security: Tenant Review, Automation & Lifecycle",
    [string]$ResourceGroup  = "rg-astral",
    [string]$Location       = "swedencentral",
    [string]$SubscriptionId = "",
    [string]$AdoOrganization = "",
    [string]$AdoProject      = "",
    [string]$AdoPipelineId   = "",
    [string]$AdoToken        = "",
    [string]$AdoBranch       = "main",
    [int]$QuietWindowMinutes = 15,
    [int]$CooldownMinutes    = 30,
    [switch]$DeployMcpServer,
    [switch]$SkipMcpServer,
    [switch]$DeployMcpOnly,
    [string]$McpContainerAppName = "",
    [string]$McpAcrName          = "",
    [string]$McpResourceGroup    = "",
    [string]$McpLocation         = "",
    [string]$McpImageName        = "astral-mcp"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Get-OrPrompt {
    param ([string]$Value, [string]$Prompt, [switch]$Sensitive)
    if ($Value) { return $Value }
    if ($Sensitive) {
        return Read-Host -Prompt $Prompt -AsSecureString |
            ForEach-Object { [PSCredential]::New("x", $_).GetNetworkCredential().Password }
    }
    return Read-Host -Prompt $Prompt
}

function Test-Command {
    param ([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-ModuleInstalled {
    param ([string]$Name)
    if (-not (Get-Module -ListAvailable -Name $Name | Select-Object -First 1)) {
        Write-Host "Installing module: $Name" -ForegroundColor Cyan
        Install-Module $Name -Scope CurrentUser -Force -AllowClobber
    }
}

function Wait-ProviderRegistration {
    param ([string]$Namespace)
    $attempts = 0
    while ($attempts -lt 30) {
        $state = Invoke-AzCli @("provider", "show", "--namespace", $Namespace, "--query", "registrationState", "--output", "tsv")
        if ($state -eq "Registered") { return }
        Start-Sleep -Seconds 10
        $attempts++
    }
    throw "Timed out waiting for $Namespace provider to register."
}

function Invoke-AzCli {
    param ([string[]]$ArgumentList, [switch]$NoRetry)
    $argsCopy = @() + $ArgumentList
    if ($SubscriptionId) { $argsCopy += @("--subscription", $SubscriptionId) }
    $env:PYTHONWARNINGS = "ignore"
    $output = & az @argsCopy 2>&1
    $env:PYTHONWARNINGS = ""
    if ($LASTEXITCODE -ne 0) {
        $lines = $output | ForEach-Object { if ($_ -is [string]) { $_ } else { $_.ToString() } }
        $text  = $lines -join "`n"
        if ((-not $NoRetry) -and $text -match "SubscriptionNotFound") {
            Write-Host "`nARM returned SubscriptionNotFound. Re-authenticating..." -ForegroundColor Yellow
            & az account clear | Out-Null
            & az login --tenant $tenantId | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "az login failed." }
            & az account set --subscription $SubscriptionId | Out-Null
            Start-Sleep -Seconds 2
            return Invoke-AzCli -ArgumentList $ArgumentList -NoRetry
        }
        $redacted = $argsCopy | ForEach-Object {
            if ($_ -match '^[A-Za-z][A-Za-z0-9_]*=') { $_ -replace '=.*$', '=***' } else { $_ }
        }
        throw "az $($redacted -join ' ') failed:`n$text"
    }
    return $output
}

# ---------------------------------------------------------------------------
# Saved deployment defaults
# ---------------------------------------------------------------------------

$configPath  = Join-Path (Split-Path -Parent $PSScriptRoot) ".astral-deploy.json"
$savedConfig = $null
if (Test-Path $configPath) {
    try {
        $savedConfig = Get-Content $configPath -Raw | ConvertFrom-Json
        Write-Host "Loaded saved defaults from $configPath" -ForegroundColor Green
    } catch {
        Write-Warning "Could not parse $configPath — starting fresh."
    }
}

if ($savedConfig) {
    foreach ($key in @('AdoOrganization','AdoProject','AdoPipelineId','AdoBranch',
                       'ResourceGroup','Location','McpContainerAppName','McpAcrName',
                       'McpResourceGroup','McpLocation')) {
        if (-not $PSBoundParameters.ContainsKey($key) -and $savedConfig.$key) {
            Set-Variable -Name $key -Value $savedConfig.$key
        }
    }
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

Write-Host "`n=== ASTRAL Provisioning ===" -ForegroundColor Green

if (-not (Test-Command "az")) {
    throw "Azure CLI not found. Install from https://aka.ms/installazurecli"
}

Test-ModuleInstalled "Microsoft.Graph.Applications"
Test-ModuleInstalled "Microsoft.Graph.Identity.SignIns"
Import-Module Microsoft.Graph.Applications
Import-Module Microsoft.Graph.Identity.SignIns

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

Write-Host "`n--- Azure DevOps ---" -ForegroundColor Cyan
$AdoOrganization = Get-OrPrompt $AdoOrganization "Azure DevOps Organization (e.g. 'contoso')"
$AdoProject      = Get-OrPrompt $AdoProject      "Azure DevOps Project"
$AdoPipelineId   = Get-OrPrompt $AdoPipelineId   "Pipeline ID of azure-pipelines.yml (definitionId=XX in URL)"
$AdoToken        = Get-OrPrompt $AdoToken        "Azure DevOps PAT (Build Read & Execute)" -Sensitive

# ---------------------------------------------------------------------------
# MCP server decision
# ---------------------------------------------------------------------------

$mcpDeploy = $false
if ($SkipMcpServer) {
    Write-Host "`nSkipping MCP server (-SkipMcpServer)." -ForegroundColor Yellow
} elseif ($DeployMcpServer -or $DeployMcpOnly) {
    $mcpDeploy = $true
    Write-Host "`nMCP server deployment enabled." -ForegroundColor Green
} else {
    $ans = Read-Host "`nDeploy MCP server to Azure Container Apps? [Y/n]"
    $mcpDeploy = ($ans -eq "" -or $ans -match "^[Yy]")
}

if ($mcpDeploy) {
    if (-not $McpResourceGroup) { $McpResourceGroup = $ResourceGroup }
    $McpContainerAppName = Get-OrPrompt $McpContainerAppName "MCP Container App name (e.g. 'ca-astral-mcp')"
    $McpAcrName          = Get-OrPrompt $McpAcrName          "Azure Container Registry name (globally unique lowercase, e.g. 'acrastral123')"
}

# Region
if (-not $PSBoundParameters.ContainsKey('Location')) {
    $regionAns = Read-Host "`nAzure region [$Location]"
    if ($regionAns) { $Location = $regionAns }
}
if ($mcpDeploy -and -not $PSBoundParameters.ContainsKey('McpLocation')) { $McpLocation = $Location }
if ($DeployMcpOnly -and $McpLocation) { $Location = $McpLocation }

# ---------------------------------------------------------------------------
# Entra ID — one app registration for everything
# ---------------------------------------------------------------------------

Write-Host "`n--- Entra ID ---" -ForegroundColor Cyan
Connect-MgGraph -Scopes "Application.ReadWrite.All","AppRoleAssignment.ReadWrite.All","Directory.Read.All" -NoWelcome

$tenant   = Get-MgOrganization | Select-Object -First 1
$tenantId = $tenant.Id
Write-Host "Tenant: $($tenant.DisplayName) ($tenantId)" -ForegroundColor Green

# Graph application permissions (for the change probe reading audit logs)
$graphSp = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"
if (-not $graphSp) { throw "Microsoft Graph service principal not found." }

$graphPermissions = @(
    "AuditLog.Read.All",
    "DeviceManagementApps.Read.All",
    "DeviceManagementConfiguration.Read.All",
    "DeviceManagementManagedDevices.Read.All",
    "DeviceManagementScripts.Read.All",
    "DeviceManagementServiceConfig.Read.All"
)

$appRoles = @()
foreach ($name in $graphPermissions) {
    $role = $graphSp.AppRoles | Where-Object { $_.Value -eq $name } | Select-Object -First 1
    if (-not $role) { Write-Warning "Graph permission '$name' not found — skipping."; continue }
    $appRoles += $role
}

$requiredResourceAccess = @(@{
    resourceAppId  = $graphSp.AppId
    resourceAccess = @($appRoles | ForEach-Object { @{ id = $_.Id; type = "Role" } })
})

# Create or update the app registration
$app = Get-MgApplication -Filter "displayName eq '$AppDisplayName'" | Select-Object -First 1
if ($app) {
    Write-Host "Found existing app '$AppDisplayName' ($($app.AppId)) — updating." -ForegroundColor Yellow
    Update-MgApplication -ApplicationId $app.Id -RequiredResourceAccess $requiredResourceAccess
} else {
    Write-Host "Creating app registration '$AppDisplayName'..." -ForegroundColor Cyan
    $app = New-MgApplication -DisplayName $AppDisplayName -SignInAudience "AzureADMyOrg" `
               -RequiredResourceAccess $requiredResourceAccess
    Write-Host "Created. App ID: $($app.AppId)" -ForegroundColor Green
}
$appId  = $app.AppId
$appOid = $app.Id

# Ensure service principal exists
$sp = Get-MgServicePrincipal -Filter "appId eq '$appId'" | Select-Object -First 1
if (-not $sp) {
    Write-Host "Creating service principal..." -ForegroundColor Cyan
    $sp = New-MgServicePrincipal -AppId $appId
}

# Admin consent for Graph application permissions
Write-Host "Granting admin consent..." -ForegroundColor Cyan
foreach ($role in $appRoles) {
    $exists = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id |
              Where-Object { $_.AppRoleId -eq $role.Id }
    if (-not $exists) {
        New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id `
            -PrincipalId $sp.Id -ResourceId $graphSp.Id -AppRoleId $role.Id | Out-Null
    }
}
Write-Host "Admin consent granted." -ForegroundColor Green

# Client secret (used by the change probe as CLIENT_SECRET)
$secretDesc  = "ASTRALSecret"
$appWithCreds = Get-MgApplication -ApplicationId $appOid -Property "id,passwordCredentials"
foreach ($cred in ($appWithCreds.PasswordCredentials | Where-Object { $_.DisplayName -eq $secretDesc })) {
    Write-Host "Removing old secret ($($cred.KeyId))..." -ForegroundColor Yellow
    Remove-MgApplicationPassword -ApplicationId $appOid -BodyParameter @{ keyId = $cred.KeyId }
}
Write-Host "Creating client secret (valid 1 year)..." -ForegroundColor Cyan
$secret       = Add-MgApplicationPassword -ApplicationId $appOid -BodyParameter @{
    displayName = $secretDesc
    endDateTime = (Get-Date).AddYears(1).ToString("o")
}
$clientSecret = $secret.SecretText
Write-Host "Client secret created." -ForegroundColor Green

# Application ID URI — required for api://<id>/user_impersonation scope
$appIdUri     = "api://$appId"
$currentUris  = (Get-MgApplication -ApplicationId $appOid).IdentifierUris
if ($currentUris -notcontains $appIdUri) {
    Write-Host "Setting Application ID URI to $appIdUri..." -ForegroundColor Cyan
    Update-MgApplication -ApplicationId $appOid `
        -IdentifierUris (@($appIdUri) + ($currentUris | Where-Object { $_ -ne $appIdUri }))
}

# Delegated scope — user_impersonation (used by Claude Desktop / MCP clients)
$appFull = Get-MgApplication -ApplicationId $appOid
$existingScopes = $appFull.Api.Oauth2PermissionScopes
if (-not ($existingScopes | Where-Object { $_.Value -eq "user_impersonation" })) {
    Write-Host "Adding user_impersonation scope..." -ForegroundColor Cyan
    $newScope = @{
        Id                      = [System.Guid]::NewGuid().ToString()
        Value                   = "user_impersonation"
        AdminConsentDisplayName = "Access ASTRAL"
        AdminConsentDescription = "Allows the application to query tenant state and drift history via ASTRAL."
        UserConsentDisplayName  = "Access ASTRAL"
        UserConsentDescription  = "Allows this app to query your tenant configuration via ASTRAL."
        IsEnabled               = $true
        Type                    = "User"
    }
    Update-MgApplication -ApplicationId $appOid -Api @{
        oauth2PermissionScopes = @($newScope) + @($existingScopes)
    }
    Write-Host "Scope added." -ForegroundColor Green
}

# Redirect URIs:
#   http://localhost                         — loopback, any port (RFC 8252 §8.3) — Cursor, etc.
#   https://claude.ai/api/mcp/auth_callback — Claude Desktop OAuth callback
Write-Host "Configuring redirect URIs..." -ForegroundColor Cyan
$claudeCallback  = "https://claude.ai/api/mcp/auth_callback"
$appForRedirects = Get-MgApplication -ApplicationId $appOid
$pubUris = @("http://localhost") + ($appForRedirects.PublicClient.RedirectUris |
               Where-Object { $_ -ne "http://localhost" })
$webUris = @($claudeCallback)   + ($appForRedirects.Web.RedirectUris |
               Where-Object { $_ -ne $claudeCallback })
Update-MgApplication -ApplicationId $appOid `
    -IsFallbackPublicClient:$true `
    -PublicClient @{ redirectUris = $pubUris } `
    -Web          @{ redirectUris = $webUris }
Write-Host "Redirect URIs configured." -ForegroundColor Green

# Generate MCP API key (static bearer token, simpler alternative for service callers)
$mcpApiKey = [System.Convert]::ToBase64String(
    [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).TrimEnd("=").Replace("+", "-").Replace("/", "_")

Disconnect-MgGraph | Out-Null

# ---------------------------------------------------------------------------
# Azure login + subscription
# ---------------------------------------------------------------------------

Write-Host "`n--- Azure Resources ---" -ForegroundColor Cyan

function Ensure-AzLogin {
    param ([string]$TenantId)
    try { $null = Invoke-AzCli @("account", "show", "--output", "none") } catch {
        if ($_ -match "az login") {
            $ans = Read-Host "Not logged in to Azure CLI. Run 'az login' now? [Y/n]"
            if ($ans -eq "" -or $ans -match "^[Yy]") {
                & az login $(if ($TenantId) { "--tenant", $TenantId }) | Out-Host
                if ($LASTEXITCODE -ne 0) { throw "az login failed." }
            } else { throw "Azure login required." }
        } else { throw }
    }
}

Ensure-AzLogin -TenantId $tenantId

function Select-Subscription {
    $lines = & az account list --output json 2>&1 | Where-Object { $_ -is [string] }
    $subs  = ($lines -join "`n") | ConvertFrom-Json
    if ($subs.Count -eq 0) { throw "No Azure subscriptions found." }
    if ($subs.Count -eq 1) {
        Invoke-AzCli @("account", "set", "--subscription", $subs[0].id)
        return $subs[0]
    }
    Write-Host "`nAvailable subscriptions:" -ForegroundColor Cyan
    for ($i = 0; $i -lt $subs.Count; $i++) {
        Write-Host "  [$i] $($subs[$i].name) ($($subs[$i].id))"
    }
    $sel = Read-Host "Select by number"
    $chosen = $subs[[int]$sel]
    if (-not $chosen) { throw "Invalid selection." }
    Invoke-AzCli @("account", "set", "--subscription", $chosen.id)
    return $chosen
}

if ($SubscriptionId) {
    Invoke-AzCli @("account", "set", "--subscription", $SubscriptionId)
} else {
    $chosen = Select-Subscription
    $SubscriptionId = $chosen.id
}
Write-Host "Subscription: $SubscriptionId" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Resource Group
# ---------------------------------------------------------------------------

Write-Host "Creating resource group '$ResourceGroup'..." -ForegroundColor Cyan
Invoke-AzCli @(
    "group", "create",
    "--name", $ResourceGroup,
    "--location", $Location,
    "--tags", "astral=true", "astral-ado-org=$AdoOrganization", "astral-ado-project=$AdoProject",
    "--output", "none"
)

# ---------------------------------------------------------------------------
# Change Probe (skipped for MCP-only)
# ---------------------------------------------------------------------------

$FunctionAppName = ""
$StorageName     = ""

if (-not $DeployMcpOnly) {
    $randomSuffix    = [System.Guid]::NewGuid().ToString("n").Substring(0, 8)
    $StorageName     = "stastral$randomSuffix"
    $FunctionAppName = "func-astral-$randomSuffix"

    # Storage
    $storageProv = Invoke-AzCli @("provider","show","--namespace","Microsoft.Storage","--query","registrationState","--output","tsv")
    if ($storageProv -ne "Registered") {
        Invoke-AzCli @("provider","register","--namespace","Microsoft.Storage")
        Wait-ProviderRegistration "Microsoft.Storage"
    }
    Write-Host "Creating storage account '$StorageName'..." -ForegroundColor Cyan
    Invoke-AzCli @(
        "storage", "account", "create",
        "--name", $StorageName,
        "--resource-group", $ResourceGroup,
        "--location", $Location,
        "--sku", "Standard_LRS",
        "--kind", "StorageV2",
        "--tags", "astral=true", "astral-ado-org=$AdoOrganization", "astral-ado-project=$AdoProject",
        "--output", "none"
    )
    $storageConnection = Invoke-AzCli @(
        "storage","account","show-connection-string",
        "--name", $StorageName,
        "--resource-group", $ResourceGroup,
        "--query", "connectionString",
        "--output", "tsv"
    )
    Write-Host "Creating Table and Queue..." -ForegroundColor Cyan
    Invoke-AzCli @("storage","table","create","--name","ProbeState","--connection-string",$storageConnection,"--output","none")
    Invoke-AzCli @("storage","queue","create","--name","backup-trigger-queue","--connection-string",$storageConnection,"--output","none")

    # Function App
    $webProv = Invoke-AzCli @("provider","show","--namespace","Microsoft.Web","--query","registrationState","--output","tsv")
    if ($webProv -ne "Registered") {
        Invoke-AzCli @("provider","register","--namespace","Microsoft.Web")
        Wait-ProviderRegistration "Microsoft.Web"
    }
    Write-Host "Creating Function App '$FunctionAppName'..." -ForegroundColor Cyan
    Invoke-AzCli @(
        "functionapp","create",
        "--name", $FunctionAppName,
        "--resource-group", $ResourceGroup,
        "--storage-account", $StorageName,
        "--consumption-plan-location", $Location,
        "--os-type", "Linux",
        "--runtime", "python",
        "--runtime-version", "3.11",
        "--functions-version", "4",
        "--tags", "astral=true", "astral-ado-org=$AdoOrganization", "astral-ado-project=$AdoProject",
        "--output", "none"
    )
    Write-Host "Configuring Function App settings..." -ForegroundColor Cyan
    Invoke-AzCli @(
        "functionapp","config","appsettings","set",
        "--name", $FunctionAppName,
        "--resource-group", $ResourceGroup,
        "--settings",
        "AzureWebJobsStorage=$storageConnection",
        "FUNCTIONS_EXTENSION_VERSION=~4",
        "FUNCTIONS_WORKER_RUNTIME=python",
        "WEBSITE_RUN_FROM_PACKAGE=1",
        "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
        "PROBE_APP_ID=$appId",
        "PROBE_APP_SECRET=$clientSecret",
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

$mcpFqdn = ""

if ($mcpDeploy) {
    Write-Host "`n=== MCP Server ===" -ForegroundColor Green

    foreach ($ns in @("Microsoft.App","Microsoft.OperationalInsights","Microsoft.ContainerRegistry")) {
        $state = Invoke-AzCli @("provider","show","--namespace",$ns,"--query","registrationState","--output","tsv")
        if ($state -ne "Registered") {
            Write-Host "Registering $ns..." -ForegroundColor Yellow
            Invoke-AzCli @("provider","register","--namespace",$ns)
            Wait-ProviderRegistration $ns
        }
    }

    # ACR
    $acrExists = $false
    try { $null = Invoke-AzCli @("acr","show","--name",$McpAcrName,"--resource-group",$McpResourceGroup,"--output","none") -NoRetry; $acrExists = $true } catch {}
    if (-not $acrExists) {
        Write-Host "Creating ACR '$McpAcrName'..." -ForegroundColor Cyan
        Invoke-AzCli @(
            "acr","create",
            "--name", $McpAcrName,
            "--resource-group", $McpResourceGroup,
            "--location", $McpLocation,
            "--sku", "Basic",
            "--admin-enabled", "true",
            "--output", "none"
        )
    }

    # Build image
    Write-Host "Building MCP server image (2-5 min)..." -ForegroundColor Cyan
    $repoRoot  = Split-Path -Parent $PSScriptRoot
    $buildTemp = Join-Path ([System.IO.Path]::GetTempPath()) "astral-mcp-build-$([System.Guid]::NewGuid().ToString('n').Substring(0,8))"
    try {
        $mcpDir     = Join-Path $buildTemp "infra" "mcp-server"
        $scriptsDir = Join-Path $buildTemp "scripts"
        New-Item -ItemType Directory -Path $mcpDir     -Force | Out-Null
        New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null
        Copy-Item (Join-Path $repoRoot "infra" "mcp-server" "Dockerfile")       $mcpDir -Force
        Copy-Item (Join-Path $repoRoot "infra" "mcp-server" "requirements.txt") $mcpDir -Force
        Copy-Item (Join-Path $repoRoot "infra" "mcp-server" "mcp_server.py")    $mcpDir -Force
        Copy-Item (Join-Path $repoRoot "scripts" "common.py")                   $scriptsDir -Force
        Copy-Item (Join-Path $repoRoot "scripts" "astral_mcp_tools.py")         $scriptsDir -Force
        Push-Location $buildTemp
        try {
            & az acr build --registry $McpAcrName --image "$McpImageName`:latest" `
                --file "infra/mcp-server/Dockerfile" --resource-group $McpResourceGroup .
            if ($LASTEXITCODE -ne 0) { throw "az acr build failed." }
            Write-Host "Image built." -ForegroundColor Green
        } finally { Pop-Location }
    } finally {
        if (Test-Path $buildTemp) { Remove-Item -Recurse -Force $buildTemp }
    }

    # Container Apps Environment
    $caEnvName = "$McpContainerAppName-env"
    Write-Host "Creating Container Apps Environment '$caEnvName'..." -ForegroundColor Cyan
    Invoke-AzCli @(
        "containerapp","env","create",
        "--name", $caEnvName,
        "--resource-group", $McpResourceGroup,
        "--location", $McpLocation,
        "--output", "none"
    )

    # Container App
    $acrLoginServer = Invoke-AzCli @("acr","show","--name",$McpAcrName,"--query","loginServer","--output","tsv")
    $acrPassword    = Invoke-AzCli @("acr","credential","show","--name",$McpAcrName,"--query","passwords[0].value","--output","tsv")
    Write-Host "Creating Container App '$McpContainerAppName'..." -ForegroundColor Cyan
    Invoke-AzCli @(
        "containerapp","create",
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
        "--registry-username", $McpAcrName,
        "--registry-password", $acrPassword,
        "--output", "none"
    )

    # Get FQDN — needed for MCP_ALLOWED_HOSTS
    $mcpFqdn = (Invoke-AzCli @(
        "containerapp","show",
        "--name", $McpContainerAppName,
        "--resource-group", $McpResourceGroup,
        "--query", "properties.configuration.ingress.fqdn",
        "--output", "tsv"
    )).Trim()

    # Configure env vars — including MCP_ALLOWED_HOSTS set to the actual FQDN
    Write-Host "Configuring Container App settings..." -ForegroundColor Cyan
    Invoke-AzCli @(
        "containerapp","update",
        "--name", $McpContainerAppName,
        "--resource-group", $McpResourceGroup,
        "--set-env-vars",
        "ADO_ORGANIZATION=$AdoOrganization",
        "ADO_PROJECT=$AdoProject",
        "ADO_BRANCH=$AdoBranch",
        "ADO_TOKEN=$AdoToken",
        "ENTRA_TENANT_ID=$tenantId",
        "MCP_CLIENT_ID=$appId",
        "MCP_API_KEY=$mcpApiKey",
        "MCP_ALLOWED_HOSTS=$mcpFqdn",
        "--output", "none"
    )
    Write-Host "MCP Server deployed: https://$mcpFqdn" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Optional: deploy function code
# ---------------------------------------------------------------------------

if (-not $DeployMcpOnly) {
    $repoRoot  = Split-Path -Parent $PSScriptRoot
    $probePath = Join-Path $repoRoot "infra" "change-probe"
    if (Test-Path $probePath) {
        $ans = Read-Host "`nDeploy function code now? [Y/n]"
        if ($ans -eq "" -or $ans -match "^[Yy]") {
            Write-Host "Packaging and deploying function code..." -ForegroundColor Cyan
            $exclude   = @("__pycache__",".venv",".env",".DS_Store",".gitignore","*.zip")
            $items     = Get-ChildItem $probePath | Where-Object {
                $n = $_.Name; -not ($exclude | Where-Object { $n -like $_ })
            }
            $stagePath = Join-Path ([System.IO.Path]::GetTempPath()) "astral-probe-stage-$([System.Guid]::NewGuid().ToString('n').Substring(0,8))"
            $zipPath   = "$stagePath.zip"
            New-Item -ItemType Directory -Path $stagePath | Out-Null
            try {
                foreach ($item in $items) {
                    $dest = Join-Path $stagePath $item.Name
                    if ($item.PSIsContainer) { Copy-Item $item.FullName $dest -Recurse -Force }
                    else                     { Copy-Item $item.FullName $dest -Force }
                }
                $ls = Join-Path $stagePath "local.settings.json"
                if (Test-Path $ls) { Remove-Item $ls }
                if (Test-Command "zip") {
                    Push-Location $stagePath; try { & zip -r $zipPath . | Out-Null } finally { Pop-Location }
                } else {
                    Compress-Archive -Path "$stagePath/*" -DestinationPath $zipPath -Force
                }
            } finally { Remove-Item $stagePath -Recurse -Force -ErrorAction SilentlyContinue }

            $conn     = Invoke-AzCli @("functionapp","config","appsettings","list","--name",$FunctionAppName,"--resource-group",$ResourceGroup,"--query","[?name=='AzureWebJobsStorage'].value","--output","tsv")
            $stName   = ($conn -split ';' | Where-Object { $_ -match '^AccountName=' }) -replace '^AccountName=', ''
            $stKey    = Invoke-AzCli @("storage","account","keys","list","--account-name",$stName,"--query","[0].value","--output","tsv")
            $container = "function-releases"
            $blobName  = "deploy-$([System.Guid]::NewGuid().ToString('n')).zip"
            $expiry    = (Get-Date).AddYears(10).ToString("yyyy-MM-ddTHH:mm:ssZ")
            $null = Invoke-AzCli @("storage","container","create","--name",$container,"--account-name",$stName,"--account-key",$stKey,"--output","none") -NoRetry
            $null = Invoke-AzCli @("storage","blob","upload","--container-name",$container,"--file",$zipPath,"--name",$blobName,"--account-name",$stName,"--account-key",$stKey,"--output","none","--overwrite") -NoRetry
            $sas = Invoke-AzCli @("storage","blob","generate-sas","--container-name",$container,"--name",$blobName,"--account-name",$stName,"--account-key",$stKey,"--permissions","r","--expiry",$expiry,"--output","tsv") -NoRetry
            $sasUrl = "https://$stName.blob.core.windows.net/$container/$blobName`?$sas"
            $null = Invoke-AzCli @("functionapp","config","appsettings","set","--name",$FunctionAppName,"--resource-group",$ResourceGroup,"--settings","WEBSITE_RUN_FROM_PACKAGE=$sasUrl","--output","none")
            $null = Invoke-AzCli @("functionapp","restart","--name",$FunctionAppName,"--resource-group",$ResourceGroup,"--output","none")
            Remove-Item $zipPath -ErrorAction SilentlyContinue
            Write-Host "Function code deployed." -ForegroundColor Green
        }
    }
}

# ---------------------------------------------------------------------------
# Save deployment defaults
# ---------------------------------------------------------------------------

try {
    [PSCustomObject]@{
        AdoOrganization     = $AdoOrganization
        AdoProject          = $AdoProject
        AdoPipelineId       = $AdoPipelineId
        AdoBranch           = $AdoBranch
        SubscriptionId      = $SubscriptionId
        TenantId            = $tenantId
        ResourceGroup       = $ResourceGroup
        Location            = $Location
        AppDisplayName      = $AppDisplayName
        AppId               = $appId
        FunctionAppName     = $FunctionAppName
        StorageName         = $StorageName
        McpContainerAppName = $McpContainerAppName
        McpAcrName          = $McpAcrName
        McpResourceGroup    = $McpResourceGroup
        McpLocation         = $McpLocation
        McpFqdn             = $mcpFqdn
    } | ConvertTo-Json | Set-Content $configPath
    Write-Host "Defaults saved to $configPath" -ForegroundColor Green
} catch {
    Write-Warning "Could not save defaults: $_"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host "`n=== Provisioning Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Subscription:     $SubscriptionId"
Write-Host "Resource Group:   $ResourceGroup"
Write-Host "App Registration: $AppDisplayName ($appId)"
Write-Host "  CLIENT_SECRET:  $clientSecret" -ForegroundColor Yellow
if (-not $DeployMcpOnly) {
    Write-Host "Function App:     $FunctionAppName"
}
if ($mcpDeploy) {
    Write-Host ""
    Write-Host "MCP Server:       https://$mcpFqdn/sse"
    Write-Host "MCP Client ID:    $appId   (same app — set as MCP_CLIENT_ID)"
    Write-Host "MCP API Key:      $mcpApiKey"
    Write-Host ""
    Write-Host "Claude Desktop config:" -ForegroundColor Cyan
    Write-Host "  URL:       https://$mcpFqdn/sse"
    Write-Host "  Client ID: $appId"
}
Write-Host ""
Write-Host "IMPORTANT: CLIENT_SECRET shown above only once. Save it now." -ForegroundColor Yellow
if (-not $DeployMcpOnly) {
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  Verify probe timer:"
    Write-Host "    az functionapp function show --name $FunctionAppName --resource-group $ResourceGroup --function-name probe_timer"
    Write-Host "  Redeploy probe code:"
    Write-Host "    Run .\deploy\provision.ps1 (it will prompt to redeploy code)"
}
if ($mcpDeploy) {
    Write-Host "  Redeploy MCP image:"
    Write-Host "    az acr build --registry $McpAcrName --image $McpImageName`:latest --file infra/mcp-server/Dockerfile ."
    Write-Host "    az containerapp update --name $McpContainerAppName --resource-group $McpResourceGroup --image $McpAcrName.azurecr.io/$McpImageName`:latest"
}
