#!/usr/bin/env pwsh
# Find who changed each Intune file (Intune audit events) and commit drift per author group,
# then push the drift branch. Shared by azure-pipelines.yml and .github/workflows/astral-backup.yml.
#
# Env: REPO_ROOT (default: current dir), GRAPH_TOKEN, BACKUP_FOLDER, INTUNE_BACKUP_SUBDIR, REPORTS_SUBDIR,
#      BASELINE_BRANCH, DRIFT_BRANCH_INTUNE, BACKUP_START (yyyy.MM.dd:HH.mm.ss), USER_NAME, USER_EMAIL,
#      BUILD_REASON (Schedule = service identity), BUILD_REQUESTEDFOR, BUILD_REQUESTEDFOREMAIL.
# Outputs: CHANGE_DETECTED, ROLLING_PR_SYNC_REQUIRED, COMMIT_DATE, MODIFICATION_AUTHOR, COMMIT_SHA.

$ErrorActionPreference = 'Stop'          # same as the Azure DevOps PowerShell@2 default
$PSNativeCommandArgumentPassing = 'Standard'

# Same rule as common.scm_platform() / ci.sh: ASTRAL_SCM overrides detection; Gitea sets GITHUB_ACTIONS too.
function Test-ActionsRunner {
  if ($env:ASTRAL_SCM -in @('github', 'gitea')) { return $true }
  if ($env:ASTRAL_SCM -eq 'ado') { return $false }
  return $env:GITHUB_ACTIONS -eq 'true'
}

function Set-CiOutput([string] $Name, [string] $Value) {
  $Value = $Value -replace "[`r`n]", ' '
  if ((Test-ActionsRunner) -and $env:GITHUB_OUTPUT) {
    Add-Content -LiteralPath $env:GITHUB_OUTPUT -Value "$Name=$Value"
  } else {
    Write-Host "##vso[task.setVariable variable=$Name]$Value"
    Write-Host "##vso[task.setVariable variable=$Name;isOutput=true]$Value"
  }
}

$root = if ($env:REPO_ROOT) { $env:REPO_ROOT } else { (Get-Location).Path }
Set-Location $root

$serviceCommitUserName = "$($env:USER_NAME)"
if ([string]::IsNullOrWhiteSpace($serviceCommitUserName)) { $serviceCommitUserName = "unknown" }
$serviceCommitUserEmail = "$($env:USER_EMAIL)"
if ([string]::IsNullOrWhiteSpace($serviceCommitUserEmail)) { $serviceCommitUserEmail = "unknown@unknown.com" }

$fallbackCommitUserName = $serviceCommitUserName
$fallbackCommitUserEmail = $serviceCommitUserEmail

$buildReason = "$($env:BUILD_REASON)"
$requestedForName = "$($env:BUILD_REQUESTEDFOR)"
$requestedForEmail = "$($env:BUILD_REQUESTEDFOREMAIL)"
if (
  $buildReason -ne "Schedule" -and
  -not [string]::IsNullOrWhiteSpace($requestedForEmail) -and
  $requestedForEmail -like "*@*"
) {
  $fallbackCommitUserEmail = $requestedForEmail
  if (-not [string]::IsNullOrWhiteSpace($requestedForName)) {
    $fallbackCommitUserName = $requestedForName
  } else {
    $fallbackCommitUserName = ($requestedForEmail -split "@")[0]
  }
  Write-Host "Fallback commit identity: manual requester '$fallbackCommitUserName <$fallbackCommitUserEmail>'"
} else {
  Write-Host "Fallback commit identity: service '$fallbackCommitUserName <$fallbackCommitUserEmail>'"
}

git config user.name $fallbackCommitUserName
git config user.email $fallbackCommitUserEmail
git config core.longpaths true
git config core.quotepath off
git config core.eol lf
git config core.autocrlf false

$untrackedFile = git ls-files --others --exclude-standard --full-name
$trackedFile = git ls-files --modified --full-name
$generatedSplitMarkdownPattern = '^' + [Regex]::Escape("$($env:BACKUP_FOLDER)") + '/.*\.md$'
$generatedReportPattern = '^' + [Regex]::Escape("$($env:BACKUP_FOLDER)/$($env:REPORTS_SUBDIR)/")
$workloadConfigPattern = '^' + [Regex]::Escape("$($env:BACKUP_FOLDER)/$($env:INTUNE_BACKUP_SUBDIR)/")
Write-Host "DEBUG: BACKUP_FOLDER=$($env:BACKUP_FOLDER), INTUNE_BACKUP_SUBDIR=$($env:INTUNE_BACKUP_SUBDIR)"
Write-Host "DEBUG: workloadConfigPattern = $workloadConfigPattern"
Write-Host "DEBUG: untracked count = $($untrackedFile.Count), tracked count = $($trackedFile.Count)"
$allFiles = @()
if ($untrackedFile) { $allFiles += $untrackedFile }
if ($trackedFile) { $allFiles += $trackedFile }
$changedFile = $allFiles | ? {
  $_ -and
  $_ -match $workloadConfigPattern -and
  $_ -notmatch $generatedSplitMarkdownPattern -and
  $_ -notmatch $generatedReportPattern -and
  $_ -notlike "*/Assignment Report/*"
}
Write-Host "DEBUG: changed count = $($changedFile.Count)"
if ($changedFile.Count -gt 0) {
  $changedFile | Select-Object -First 5 | ForEach-Object { Write-Host "DEBUG: changed file: $_" }
}

if ($changedFile) {
  git show-ref --verify --quiet "refs/remotes/origin/$($env:DRIFT_BRANCH_INTUNE)"
  $hasRemoteDrift = $LASTEXITCODE -eq 0
  if ($hasRemoteDrift) {
    git diff --quiet "origin/$($env:DRIFT_BRANCH_INTUNE)" -- "$($env:BACKUP_FOLDER)/$($env:INTUNE_BACKUP_SUBDIR)"
    if ($LASTEXITCODE -eq 0) {
      "No Intune change detected (snapshot identical to existing drift branch)"
      Set-CiOutput "CHANGE_DETECTED" "0"
      Set-CiOutput "ROLLING_PR_SYNC_REQUIRED" "1"
      exit 0
    }
  }

  if (!(Get-Module "Microsoft.Graph.DeviceManagement.Administration" -ListAvailable)) {
    Install-Module Microsoft.Graph.DeviceManagement.Administration -AllowClobber -Force -AcceptLicense
  }

  Write-Host "Authenticating to Graph API"
  $secureToken = ConvertTo-SecureString -String "$($env:GRAPH_TOKEN)" -AsPlainText -Force
  Connect-MgGraph -AccessToken $secureToken -NoWelcome

  function _startProcess {
    [CmdletBinding()]
    param (
      [string] $filePath = '',
      [string] $argumentList = '',
      [string] $workingDirectory = (Get-Location),
      [switch] $dontWait,
      [switch] $outputErr2Std
    )

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo.UseShellExecute = $false
    $p.StartInfo.RedirectStandardOutput = $true
    $p.StartInfo.RedirectStandardError = $true
    $p.StartInfo.WorkingDirectory = $workingDirectory
    $p.StartInfo.FileName = $filePath
    $p.StartInfo.Arguments = $argumentList
    [void]$p.Start()
    # Drain both pipes while the process runs: waiting for exit first deadlocks once the child
    # writes more than a pipe buffer holds.
    $stdoutTask = $p.StandardOutput.ReadToEndAsync()
    $stderrTask = $p.StandardError.ReadToEndAsync()
    if (!$dontWait) { $p.WaitForExit() }

    $result = $stdoutTask.GetAwaiter().GetResult()
    if ($result) { $result }

    $err = $stderrTask.GetAwaiter().GetResult()
    if ($outputErr2Std) {
      $err
    } elseif ($err) {
      Write-Error $err
    }
  }

  function _getFirstNonEmptyString {
    [CmdletBinding()]
    param ([object] $value)

    if ($value -is [string]) {
      if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value.Trim()
      }
      return $null
    }

    if ($value -is [System.Collections.IEnumerable]) {
      foreach ($item in $value) {
        $resolved = _getFirstNonEmptyString $item
        if ($resolved) {
          return $resolved
        }
      }
    }

    if ($null -ne $value) {
      $stringValue = [string] $value
      if (-not [string]::IsNullOrWhiteSpace($stringValue)) {
        return $stringValue.Trim()
      }
    }

    return $null
  }

  function _getGraphPropertyValue {
    [CmdletBinding()]
    param (
      [object] $object,
      [string[]] $candidateNames
    )

    if ($null -eq $object) {
      return $null
    }

    foreach ($candidateName in $candidateNames) {
      $property = $object.PSObject.Properties | ? { $_.Name -eq $candidateName } | select -First 1
      if ($property) {
        $resolved = _getFirstNonEmptyString $property.Value
        if ($resolved) {
          return $resolved
        }
      }
    }

    $additionalProperty = $object.PSObject.Properties | ? { $_.Name -eq 'AdditionalProperties' } | select -First 1
    if ($additionalProperty -and $additionalProperty.Value -is [System.Collections.IDictionary]) {
      $additionalPropertyKeys = @($additionalProperty.Value.Keys)
      foreach ($candidateName in $candidateNames) {
        $candidateKeys = @($candidateName)
        if ($candidateName.Length -gt 0) {
          $candidateKeys += ($candidateName.Substring(0, 1).ToLowerInvariant() + $candidateName.Substring(1))
        }
        foreach ($candidateKey in ($candidateKeys | Select-Object -Unique)) {
          if ($additionalPropertyKeys -contains $candidateKey) {
            $resolved = _getFirstNonEmptyString $additionalProperty.Value[$candidateKey]
            if ($resolved) {
              return $resolved
            }
          }
        }
      }
    }

    return $null
  }

  function _getActorIdentity {
    [CmdletBinding()]
    param ([object] $actor)

    # Graph module updates can move actor fields between typed properties and AdditionalProperties.
    $userPrincipalName = _getGraphPropertyValue $actor @('UserPrincipalName', 'EmailAddress', 'Email')
    $userDisplayName = _getGraphPropertyValue $actor @('UserDisplayName', 'DisplayName')
    if ($userPrincipalName) {
      $actorName = $userDisplayName
      if (-not $actorName) {
        $actorName = ($userPrincipalName -split '@')[0]
      }
      return [PSCustomObject]@{
        Key   = "user:$userPrincipalName"
        Value = $userPrincipalName
        Name  = $actorName
      }
    }

    $applicationDisplayName = _getGraphPropertyValue $actor @('ApplicationDisplayName', 'AppDisplayName', 'ApplicationName')
    if ($applicationDisplayName) {
      return [PSCustomObject]@{
        Key   = "sp:$applicationDisplayName"
        Value = ($applicationDisplayName + " (SP)")
        Name  = $applicationDisplayName
      }
    }

    if ($userDisplayName) {
      return [PSCustomObject]@{
        Key   = "display:$userDisplayName"
        Value = $userDisplayName
        Name  = $userDisplayName
      }
    }

    return $null
  }

  function _getResourceId {
    [CmdletBinding()]
    param ([string] $filePath)

    $fileName = [System.IO.Path]::GetFileNameWithoutExtension($filePath)

    if ($filePath -like "*Device Configurations/mobileconfig/*") {
      $parentFolderPath = Split-Path (Split-Path $filePath -Parent) -Parent
      $fileName = Get-ChildItem $parentFolderPath -File | ? {
        (ConvertFrom-Json -InputObject (Get-Content $_.FullName -Raw)).payloadFileName -eq [System.IO.Path]::GetFileName($filePath)
      } | select -expand BaseName
      if (!$fileName) {
        Write-Warning "Unable to find 'parent' config file for $filePath"
        return
      }
    } elseif ($filePath -like "*/Managed Google Play/*") {
      return ($modificationEvent | ? { $_.Category -eq 'Enrollment' -and $_.ActivityType -eq "Patch AndroidForWorkSettings" }).Resources.ResourceId
    }

    $delimiter = "__"
    if ($fileName -like "*$delimiter*") {
      $resourceId = ($fileName -split $delimiter)[-1]
      $resourceId = $resourceId -replace "^_*"
    } else {
      $resourceId = $null
    }

    return $resourceId
  }

  $gitCommitDepth = 30
  git fetch --depth=$gitCommitDepth
  $commitList = _startProcess git "--no-pager log --no-show-signature -$gitCommitDepth --format=%s%%%%%%%cI" -outputErr2Std -dontWait
  $lastCommitDate = $commitList -split "`n" | ? { $_ } | % {
    $commitName, $commitDate = $_ -split "%%%"
    if ($commitName -match "^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2} -- ") { $commitDate }
  }

  if ($lastCommitDate) {
    $lastCommitDate = Get-Date @($lastCommitDate)[0]
  } else {
    Write-Warning "Unable to obtain date of the last backup config commit. ALL Intune audit events will be gathered."
  }

  $modificationData = New-Object System.Collections.ArrayList

  $filter = @("activityResult eq 'Success'", "ActivityOperationType ne 'Get'")

  if ($lastCommitDate) {
    $lastCommitDate = $lastCommitDate.ToUniversalTime()
    $filterDateTimeFrom = Get-Date -Date $lastCommitDate -Format "yyyy-MM-ddTHH:mm:ss"
    $filter += "ActivityDateTime ge $filterDateTimeFrom`Z"
  }

  $backupStart = [DateTime]::ParseExact("$($env:BACKUP_START)", "yyyy.MM.dd:HH.mm.ss", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]'AssumeUniversal, AdjustToUniversal')  # BACKUP_START is UTC
  $auditQueryEnd = $backupStart.AddMinutes(-10)
  $filterDateTimeTo = Get-Date -Date $auditQueryEnd -Format "yyyy-MM-ddTHH:mm:ss"
  $filter += "ActivityDateTime le $filterDateTimeTo`Z"

  $eventFilter = $filter -join " and "

  "`nGetting Intune event logs"
  "`t- from: '$lastCommitDate' (UTC) to: '$auditQueryEnd' (UTC)"
  "`t- filter: $eventFilter"
  $modificationEvent = Get-MgDeviceManagementAuditEvent -Filter $eventFilter -All

  $changedFileCount = @($changedFile).Count
  $showPerFileLog = $changedFileCount -le 100
  $unresolvedAuthorCount = 0
  $nonResourceFileCount = 0

  if ($showPerFileLog) {
    "`nProcessing changed files"
  } else {
    "`nProcessing changed files ($changedFileCount total, condensed logging enabled)"
  }
  foreach ($file in $changedFile) {
    $resourceId = _getResourceId $file

    if ($resourceId) {
      if ($showPerFileLog) {
        "`t- $resourceId ($file)"
      }
      $resourceModificationEvent = $modificationEvent | ? { $_.Resources.ResourceId -eq $resourceId }

      $modificationAuthor = @()
      $resourceModificationEvent.Actor | % {
        $actorIdentity = _getActorIdentity $_
        if ($actorIdentity) {
          $modificationAuthor += $actorIdentity
        }
      }
      $modificationAuthor = $modificationAuthor | Sort-Object Key -Unique
    } else {
      $nonResourceFileCount++
      $modificationAuthor = $null
    }

    if ($modificationAuthor) {
      if ($showPerFileLog) {
        "`t`t- changed by: $($modificationAuthor.Name -join ', ')"
      }
    } else {
      if ($resourceId) {
        $unresolvedAuthorCount++
      }
      if ($showPerFileLog) {
        "`t`t- unable to find out who made the change"
      }
      $modificationAuthor = @([PSCustomObject]@{
        Key   = "fallback:$fallbackCommitUserEmail"
        Value = $fallbackCommitUserEmail
        Name  = $fallbackCommitUserName
      })
    }

    $null = $modificationData.Add([PSCustomObject]@{
      resourceId              = $resourceId
      file                    = Join-Path $root $file
      modificationAuthorKey   = ($modificationAuthor.Key -join '&')
      modificationAuthorValue = $modificationAuthor.Value
      modificationAuthorName  = $modificationAuthor.Name
    })
  }

  if ($nonResourceFileCount -gt 0) {
    Write-Host "Skipped resourceId lookup for $nonResourceFileCount non-resource files."
  }
  if ($unresolvedAuthorCount -gt 0) {
    Write-Warning "Unable to resolve author from Intune audit logs for $unresolvedAuthorCount of $changedFileCount changed files. Fallback identity used."
  }

  "`nCommit changes"
  $script:nothingToCommit = $false
  $modificationData | Group-Object modificationAuthorKey | % {
    $modificationAuthorValue = @($_.Group | % { $_.modificationAuthorValue } | ? { $_ } | Select-Object -Unique)
    $modificationAuthorName = @($_.Group | % { $_.modificationAuthorName } | ? { $_ } | Select-Object -Unique)
    $modifiedFile = $_.Group.File

    $modifiedFile | % { "`t- Adding $_" }
    # Paths, author and message derive from tenant data (policy names, audit-log actors):
    # pass them via a pathspec file, env vars and a message file, never as argument text.
    $pathspecFile = New-TemporaryFile
    [System.IO.File]::WriteAllText($pathspecFile, ((@($modifiedFile) | % { [string]$_ }) -join "`0"))
    git add --pathspec-from-file="$pathspecFile" --pathspec-file-nul
    if ($LASTEXITCODE -ne 0) { throw "git add failed for author group $($modificationAuthorName -join ', ')" }
    Remove-Item $pathspecFile -ErrorAction SilentlyContinue

    "`t- Setting commit author(s): $($modificationAuthorName -join ', ')"
    $env:GIT_AUTHOR_NAME = $env:GIT_COMMITTER_NAME = ($modificationAuthorName -join ', ')
    $env:GIT_AUTHOR_EMAIL = $env:GIT_COMMITTER_EMAIL = ($modificationAuthorValue -join ', ')

    $DATEF = (Get-Date $backupStart -f "yyyy.MM.dd_HH.mm")
    $commitName = "$DATEF` -- $($modificationAuthorName -join ', ')"

    "`t- Creating commit '$commitName'"
    $messageFile = New-TemporaryFile
    [System.IO.File]::WriteAllText($messageFile, $commitName)
    git commit --quiet -F "$messageFile"
    Remove-Item $messageFile -ErrorAction SilentlyContinue

    $unpushedCommit = _startProcess git -ArgumentList "cherry -v origin/$($env:BASELINE_BRANCH)"
    if ([string]::IsNullOrEmpty($unpushedCommit)) {
      Write-Warning "Nothing to commit?! This shouldn't happen."
      Set-CiOutput "CHANGE_DETECTED" "0"
      Set-CiOutput "ROLLING_PR_SYNC_REQUIRED" "0"
      $script:nothingToCommit = $true
    } else {
      Set-CiOutput "COMMIT_DATE" "$DATEF"
      Set-CiOutput "MODIFICATION_AUTHOR" "$(($modificationData.modificationAuthorValue | select -Unique | Sort-Object) -join ', ')"
    }
  }

  if ($script:nothingToCommit) { exit 0 }   # the loop cannot leave the script itself; do not push or report a change

  "`nPush changes to drift branch"
  git push --force-with-lease origin "HEAD:$($env:DRIFT_BRANCH_INTUNE)"
  if ($LASTEXITCODE -ne 0) { throw "Failed to push backup commits to origin/$($env:DRIFT_BRANCH_INTUNE)" }
  $commitSha = (git rev-parse HEAD).Trim()
  Set-CiOutput "CHANGE_DETECTED" "1"
  Set-CiOutput "ROLLING_PR_SYNC_REQUIRED" "1"
  Set-CiOutput "COMMIT_SHA" "$commitSha"
} else {
  "No change detected"
  Set-CiOutput "CHANGE_DETECTED" "0"
  Set-CiOutput "ROLLING_PR_SYNC_REQUIRED" "0"
}
