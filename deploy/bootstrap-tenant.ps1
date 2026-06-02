#requires -Version 5.1
<#
.SYNOPSIS
    Bootstraps an Azure AD app registration for ASTRAL with required Microsoft Graph permissions.

.DESCRIPTION
    Creates a single-tenant app registration, assigns read (and optional write) Graph application permissions,
    grants admin consent, and configures a workload federated credential for Azure DevOps.

.PARAMETER TenantName
    The Microsoft 365 tenant domain, e.g. contoso.onmicrosoft.com.

.PARAMETER ServiceConnectionName
    The intended Azure DevOps service connection name (used for the federated credential subject).

.PARAMETER AppDisplayName
    Optional display name for the app registration. Default: "ASTRAL Backup Service".

.PARAMETER AdoOrganizationUrl
    Optional Azure DevOps organization URL, e.g. https://dev.azure.com/contoso.
    If provided, the script prints a one-liner to create the service connection via REST API.

.PARAMETER AddRestorePermissions
    If specified, also adds write permissions for the restore pipeline.

.EXAMPLE
    .\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ServiceConnectionName "sc-astral-backup"
#>
[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string]$TenantName,

    [Parameter(Mandatory = $true)]
    [string]$ServiceConnectionName,

    [string]$AppDisplayName = "ASTRAL Backup Service",

    [string]$AdoOrganizationUrl = "",

    [switch]$AddRestorePermissions
)

$ErrorActionPreference = "Stop"

function Test-ModuleInstalled {
    param ([string]$Name)
    $mod = Get-Module -ListAvailable -Name $Name | Select-Object -First 1
    if (-not $mod) {
        Write-Host "Installing module: $Name" -ForegroundColor Cyan
        Install-Module $Name -Scope CurrentUser -Force -AllowClobber
    }
}

Test-ModuleInstalled "Microsoft.Graph.Applications"
Test-ModuleInstalled "Microsoft.Graph.Identity.SignIns"

Import-Module Microsoft.Graph.Applications
Import-Module Microsoft.Graph.Identity.SignIns

Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Cyan
Connect-MgGraph -Scopes "Application.ReadWrite.All","AppRoleAssignment.ReadWrite.All","Directory.Read.All" -NoWelcome

$tenant = Get-MgOrganization | Select-Object -First 1
if (-not $tenant) {
    throw "Unable to read tenant details. Ensure you are authenticated to the correct tenant."
}

Write-Host "Tenant: $($tenant.DisplayName) ($($tenant.Id))" -ForegroundColor Green

# Required read permissions
$readPermissions = @(
    "Device.Read.All",
    "DeviceManagementApps.Read.All",
    "DeviceManagementConfiguration.Read.All",
    "DeviceManagementManagedDevices.Read.All",
    "DeviceManagementRBAC.Read.All",
    "DeviceManagementScripts.Read.All",
    "DeviceManagementServiceConfig.Read.All",
    "Group.Read.All",
    "Policy.Read.All",
    "Policy.Read.ConditionalAccess",
    "Policy.Read.DeviceConfiguration",
    "User.Read.All",
    "Application.Read.All"
)

$optionalReadPermissions = @(
    "RoleManagement.Read.Directory",
    "Directory.Read.All",
    "AuditLog.Read.All"
)

$restorePermissions = @(
    "DeviceManagementApps.ReadWrite.All",
    "DeviceManagementConfiguration.ReadWrite.All",
    "DeviceManagementManagedDevices.ReadWrite.All",
    "DeviceManagementRBAC.ReadWrite.All",
    "DeviceManagementScripts.ReadWrite.All",
    "DeviceManagementServiceConfig.ReadWrite.All",
    "Policy.Read.All",
    "Policy.ReadWrite.ConditionalAccess"
)

$allPermissions = $readPermissions + $optionalReadPermissions
if ($AddRestorePermissions) {
    $allPermissions += $restorePermissions
}

# Get Microsoft Graph SP to map permissions to AppRoles
$graphSp = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"
if (-not $graphSp) {
    throw "Microsoft Graph service principal not found in tenant."
}

$requiredResourceAccess = @()
$appRoles = @()
foreach ($permName in ($allPermissions | Select-Object -Unique)) {
    $appRole = $graphSp.AppRoles | Where-Object { $_.Value -eq $permName } | Select-Object -First 1
    if (-not $appRole) {
        Write-Warning "Permission '$permName' not found in Microsoft Graph. Skipping."
        continue
    }
    $appRoles += $appRole
}

if ($appRoles.Count -eq 0) {
    throw "No valid Graph permissions resolved. Cannot continue."
}

$resourceAccess = @()
foreach ($ar in $appRoles) {
    $resourceAccess += @{
        id   = $ar.Id
        type = "Role"
    }
}

$requiredResourceAccess = @(
    @{
        resourceAppId  = $graphSp.AppId
        resourceAccess = $resourceAccess
    }
)

# Create or update app registration
$existingApp = Get-MgApplication -Filter "displayName eq '$AppDisplayName'" | Select-Object -First 1
if ($existingApp) {
    Write-Host "Found existing app registration: $($existingApp.AppId)" -ForegroundColor Yellow
    $app = $existingApp
    Update-MgApplication -ApplicationId $app.Id -RequiredResourceAccess $requiredResourceAccess
    Write-Host "Updated required resource access." -ForegroundColor Green
}
else {
    Write-Host "Creating app registration: $AppDisplayName" -ForegroundColor Cyan
    $app = New-MgApplication -DisplayName $AppDisplayName -SignInAudience "AzureADMyOrg" -RequiredResourceAccess $requiredResourceAccess
    Write-Host "Created app registration. AppId: $($app.AppId)" -ForegroundColor Green
}

# Ensure service principal exists
$sp = Get-MgServicePrincipal -Filter "appId eq '$($app.AppId)'" | Select-Object -First 1
if (-not $sp) {
    Write-Host "Creating service principal..." -ForegroundColor Cyan
    $sp = New-MgServicePrincipal -AppId $app.AppId
}

# Grant admin consent
Write-Host "Granting admin consent..." -ForegroundColor Cyan
foreach ($ar in $appRoles) {
    $existingAssignment = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id | Where-Object { $_.AppRoleId -eq $ar.Id }
    if (-not $existingAssignment) {
        New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id -PrincipalId $sp.Id -ResourceId $graphSp.Id -AppRoleId $ar.Id | Out-Null
    }
}
Write-Host "Admin consent granted." -ForegroundColor Green

# Federated credential for Azure DevOps
$federatedCredentialName = "AstralAzureDevOps-$ServiceConnectionName"
$existingFedCred = Get-MgApplicationFederatedIdentityCredential -ApplicationId $app.Id | Where-Object { $_.Name -eq $federatedCredentialName }
if (-not $existingFedCred) {
    Write-Host "Creating federated credential for Azure DevOps..." -ForegroundColor Cyan
    # Subject identifier for Azure DevOps workload identity federation
    # Format: sc://<ado-org>/<project>/<service-connection-name>
    # We require the user to fill in org/project manually or via parameters.
    $adoOrg = Read-Host "Enter your Azure DevOps organization name (e.g. 'contoso')"
    $adoProject = Read-Host "Enter your Azure DevOps project name (e.g. 'ASTRAL')"
    $subject = "sc://$adoOrg/$adoProject/$ServiceConnectionName"

    $params = @{
        Name      = $federatedCredentialName
        Issuer    = "https://vstoken.dev.azure.com"
        Subject   = $subject
        Audiences = @("api://AzureADTokenExchange")
    }
    New-MgApplicationFederatedIdentityCredential -ApplicationId $app.Id -BodyParameter $params | Out-Null
    Write-Host "Federated credential created. Subject: $subject" -ForegroundColor Green
}
else {
    Write-Host "Federated credential already exists." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Bootstrap complete ===" -ForegroundColor Green
Write-Host "Tenant Name:        $TenantName"
Write-Host "Tenant ID:          $($tenant.Id)"
Write-Host "App Display Name:   $AppDisplayName"
Write-Host "App ID:             $($app.AppId)"
Write-Host "Service Connection: $ServiceConnectionName"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. In Azure DevOps, create a Workload Identity Federation service connection."
Write-Host "   - Tenant ID: $($tenant.Id)"
Write-Host "   - App ID:    $($app.AppId)"
Write-Host "   - Name:      $ServiceConnectionName"
Write-Host ""

if ($AdoOrganizationUrl) {
    $project = if ($AdoOrganizationUrl -match "/([^/]+)$") { $matches[1] } else { "YOUR_PROJECT" }
    $pat = Read-Host "Enter an Azure DevOps PAT with 'ServiceConnections: Read & manage' scope (input is hidden)" -AsSecureString
    $patPlain = [System.Net.NetworkCredential]::new("", $pat).Password
    Write-Host ""
    Write-Host "You can create the service connection via REST API using:"
    Write-Host "  curl -u :$patPlain -X POST -H 'Content-Type: application/json' "
    Write-Host "    -d '{ ... }' "
    Write-Host "    '$AdoOrganizationUrl/_apis/serviceendpoint/endpoints?api-version=7.1'"
}

Disconnect-MgGraph | Out-Null
