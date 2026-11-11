#requires -Version 5.1
<#
.SYNOPSIS
    Bootstraps an Azure AD app registration for ASTRAL with required Microsoft Graph permissions.

.DESCRIPTION
    Two usage modes:

    Mode A — automatic service connection (recommended):
        Create the ADO service connection using the "App registration (automatic)" option first.
        ADO creates the app registration and federated credential for you. Then run this script
        with -ExistingAppId to assign the required Graph permissions and grant admin consent to
        that auto-created app.

        .\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ExistingAppId "<app-id-from-ado>"

    Mode B — manual service connection:
        Run this script first. It creates the app registration, assigns Graph permissions, grants
        admin consent, and optionally creates the federated credential. Then create the ADO service
        connection manually using the Issuer and Subject Identifier from the ADO draft.

        .\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ServiceConnectionName "sc-astral-backup"

.PARAMETER TenantName
    The Microsoft 365 tenant domain, e.g. contoso.onmicrosoft.com.

.PARAMETER ExistingAppId
    App ID of an existing app registration (e.g. one auto-created by ADO).
    When provided, the script skips app registration and federated credential creation
    and only assigns Graph permissions and grants admin consent.
    ServiceConnectionName is not required in this mode.

.PARAMETER ServiceConnectionName
    The intended Azure DevOps service connection name. Used as the display name suffix
    and federated credential subject when creating a new app registration (Mode B).
    Not required when -ExistingAppId is provided.

.PARAMETER AppDisplayName
    Display name for a newly created app registration. Default: "ASTRAL Backup Service".
    Ignored when -ExistingAppId is provided.

.PARAMETER AdoOrganizationUrl
    Optional Azure DevOps organization URL, e.g. https://dev.azure.com/contoso.
    Used only in Mode B to print a REST API helper command.

.PARAMETER AddRestorePermissions
    If specified, also adds write permissions for the restore pipeline.

.EXAMPLE
    # Mode A: assign permissions to an ADO auto-created app
    .\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ExistingAppId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

.EXAMPLE
    # Mode B: create everything from scratch
    .\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ServiceConnectionName "sc-astral-backup"

.EXAMPLE
    # Mode B with restore permissions
    .\bootstrap-tenant.ps1 -TenantName "contoso.onmicrosoft.com" -ServiceConnectionName "sc-astral-backup" -AddRestorePermissions
#>
[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string]$TenantName,

    [Parameter(Mandatory = $false)]
    [string]$ExistingAppId = "",

    [Parameter(Mandatory = $false)]
    [string]$ServiceConnectionName = "",

    [string]$AppDisplayName = "ASTRAL Backup Service",

    [string]$AdoOrganizationUrl = "",

    [switch]$AddRestorePermissions
)

$ErrorActionPreference = "Stop"

# Validate parameter combinations
$modeAutomatic = -not [string]::IsNullOrWhiteSpace($ExistingAppId)
$modeManual    = -not $modeAutomatic

if ($modeManual -and [string]::IsNullOrWhiteSpace($ServiceConnectionName)) {
    throw "ServiceConnectionName is required when ExistingAppId is not provided. " +
          "Either supply -ExistingAppId (automatic service connection mode) or " +
          "-ServiceConnectionName (manual service connection mode)."
}

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

if ($modeAutomatic) {
    Write-Host ""
    Write-Host "Mode: automatic service connection — targeting existing app registration $ExistingAppId" -ForegroundColor Cyan
} else {
    Write-Host ""
    Write-Host "Mode: manual service connection — creating or updating app registration" -ForegroundColor Cyan
}

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

# ---------------------------------------------------------------------------
# Resolve or create the app registration
# ---------------------------------------------------------------------------
if ($modeAutomatic) {
    # Target the existing app created by ADO
    $app = Get-MgApplication -Filter "appId eq '$ExistingAppId'" | Select-Object -First 1
    if (-not $app) {
        throw "No app registration found with App ID '$ExistingAppId'. " +
              "Ensure you are authenticated to the correct tenant and the App ID is correct."
    }
    Write-Host "Found app registration: $($app.DisplayName) ($($app.AppId))" -ForegroundColor Green
    Update-MgApplication -ApplicationId $app.Id -RequiredResourceAccess $requiredResourceAccess
    Write-Host "Graph permissions assigned." -ForegroundColor Green
}
else {
    # Create or update by display name
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

# ---------------------------------------------------------------------------
# Federated credential (Mode B only — Mode A uses the one ADO created)
# ---------------------------------------------------------------------------
if ($modeManual) {
    $federatedCredentialName = "AstralAzureDevOps-$ServiceConnectionName"
    $existingFedCred = Get-MgApplicationFederatedIdentityCredential -ApplicationId $app.Id | Where-Object { $_.Name -eq $federatedCredentialName }
    if ($existingFedCred) {
        Write-Host "Federated credential '$federatedCredentialName' already exists — skipping." -ForegroundColor Yellow
        Write-Host "The existing credential will be used when you complete the ADO service connection draft." -ForegroundColor Yellow
    }
    else {
        Write-Host ""
        Write-Host "No federated credential found for '$federatedCredentialName'." -ForegroundColor Yellow
        Write-Host "You have two options:" -ForegroundColor Cyan
        Write-Host "  1. Create the ADO service connection draft first, then re-run this script to let ADO generate the correct Issuer/Subject."
        Write-Host "  2. Create the federated credential manually in Entra after creating the ADO service connection draft."
        Write-Host ""
        Write-Host "Skipping federated credential creation. Complete the ADO service connection setup and grant admin consent before running pipelines." -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Bootstrap complete ===" -ForegroundColor Green
Write-Host "Tenant:      $($tenant.DisplayName) ($($tenant.Id))"
Write-Host "App Name:    $($app.DisplayName)"
Write-Host "App ID:      $($app.AppId)"
Write-Host ""

if ($modeAutomatic) {
    Write-Host "Next step:" -ForegroundColor Cyan
    Write-Host "  Graph permissions and admin consent are now set."
    Write-Host "  Your service connection is ready — proceed to Step 6 of the onboarding runbook (import pipelines)."
}
else {
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "  1. Create the ADO service connection (manual) using:"
    Write-Host "     - App ID:    $($app.AppId)"
    Write-Host "     - Tenant ID: $($tenant.Id)"
    Write-Host "  2. Save as draft, copy the Issuer and Subject Identifier from ADO."
    Write-Host "  3. In Entra, add a federated credential to this app using those values."
    Write-Host "  4. Return to ADO, click 'Finish setup', then 'Verify and save'."
}

Disconnect-MgGraph | Out-Null
