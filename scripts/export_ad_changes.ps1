<#
.SYNOPSIS
    Read Active Directory change events from the Security log of domain controllers (read-only).

.DESCRIPTION
    Collects the directory service change events 5136 (attribute modified), 5137 (object created),
    5139 (object moved) and 5141 (object deleted) and writes them to -OutFile as JSON. scripts/backup_ad.py
    uses them to name the administrator behind each change.

    Needs, on every domain controller:
      - the subcategory "Audit Directory Service Changes" (Success), and
      - an audit entry (SACL) on the objects to watch, for example Everyone / Write all properties / Success
        on the domain root, with inheritance. Without a SACL no event is written.
      - the runner account in the built-in group "Event Log Readers", and the firewall rule
        "Remote Event Log Management (RPC)".
    A controller that cannot be read is reported in the output and does not stop the run.

    Attribute values are NOT exported: they can hold sensitive data, and only who/what/when is needed.

    Keep this file ASCII-only and Windows PowerShell 5.1 compatible.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutFile,
    # Domain controllers to read, comma-separated.
    [Parameter(Mandatory = $true)][string]$Servers,
    # UTC, ISO 8601 (for example 2026-05-01T02:30:00Z).
    [Parameter(Mandatory = $true)][string]$Since,
    [string]$Until = ''
)

$ErrorActionPreference = 'Stop'
$start = [DateTime]::Parse($Since, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
$filter = @{ LogName = 'Security'; Id = 5136, 5137, 5139, 5141; StartTime = $start }
if ($Until) {
    $filter['EndTime'] = [DateTime]::Parse($Until, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
}

$events = New-Object System.Collections.ArrayList
$readable = @()
$failed = @()
foreach ($server in @($Servers.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
    try {
        $found = @(Get-WinEvent -ComputerName $server -FilterHashtable $filter -ErrorAction Stop)
    }
    catch {
        if ($_.FullyQualifiedErrorId -like 'NoMatching*' -or $_.Exception.Message -like 'No events were found*') { $readable += $server; continue }
        $failed += [ordered]@{ Server = $server; Error = $_.Exception.Message }
        Write-Warning "Cannot read the Security log of ${server}: $($_.Exception.Message)"
        continue
    }
    $readable += $server
    foreach ($event in $found) {
        $data = @{}
        foreach ($node in ([xml]$event.ToXml()).Event.EventData.Data) { $data[[string]$node.Name] = [string]$node.'#text' }
        [void]$events.Add([ordered]@{
                TimeUtc        = $event.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
                EventId        = $event.Id
                Server         = $server
                SubjectName    = $data['SubjectUserName']
                SubjectDomain  = $data['SubjectDomainName']
                SubjectSid     = $data['SubjectUserSid']
                ObjectDn       = $data['ObjectDN']
                ObjectGuid     = $data['ObjectGUID']
                ObjectClass    = $data['ObjectClass']
                Attribute      = $data['AttributeLDAPDisplayName']
                Operation      = $data['OperationType']
                OldObjectDn    = $data['OldObjectDN']
                NewObjectDn    = $data['NewObjectDN']
            })
    }
}

$result = [ordered]@{ ServersRead = @($readable); ServersFailed = @($failed); Events = @($events) }
$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
[System.IO.File]::WriteAllText($OutFile, (ConvertTo-Json -InputObject $result -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Read $($readable.Count) of $($readable.Count + $failed.Count) domain controllers, $($events.Count) change events"
