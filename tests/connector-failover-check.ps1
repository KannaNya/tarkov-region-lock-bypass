param([switch]$HoldMutex, [string]$ReadyPath)

$ErrorActionPreference = 'Stop'
if ($HoldMutex) {
    [bool]$createdNew = $false
    $mutex = [Threading.Mutex]::new($false, 'Global\TarkovCisVpnGateConnector', [ref]$createdNew)
    $mutexHeld = $false
    try {
        try { $mutexHeld = $mutex.WaitOne([TimeSpan]::FromSeconds(5)) } catch [Threading.AbandonedMutexException] { $mutexHeld = $true }
        if (-not $mutexHeld) { throw 'Mutex holder could not acquire the connector mutex.' }
        if ($ReadyPath) { [IO.File]::WriteAllText($ReadyPath, 'ready') }
        Start-Sleep -Seconds 10
    } finally {
        if ($mutexHeld) { try { $mutex.ReleaseMutex() } catch {} }
        $mutex.Dispose()
    }
    return
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$scriptPath = Join-Path $root 'src\Connect-VpnGateCis.ps1'
$statePath = Join-Path ([IO.Path]::GetTempPath()) ('tarkov-cis-atomic-{0}.json' -f [guid]::NewGuid().ToString('N'))
. $scriptPath -TestMode -VpnCmdPath $env:SystemRoot -StatePath $statePath

function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$state = New-FailoverState
$servers = @(
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-a.opengw.net'; IP='192.0.2.10'; Port=443; Endpoint='192.0.2.10:443'; Priority=100; Score=900; Ping=30; Sessions=2; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-a.opengw.net'; IP='192.0.2.10'; Port=992; Endpoint='192.0.2.10:992'; Priority=100; Score=899; Ping=31; Sessions=2; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-b.opengw.net'; IP='192.0.2.11'; Port=443; Endpoint='192.0.2.11:443'; Priority=100; Score=800; Ping=40; Sessions=1; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-c.opengw.net'; IP='192.0.2.12'; Port=443; Endpoint='192.0.2.12:443'; Priority=100; Score=700; Ping=50; Sessions=0; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-d.opengw.net'; IP='192.0.2.13'; Port=443; Endpoint='192.0.2.13:443'; Priority=100; Score=600; Ping=60; Sessions=0; Source='NativeCatalog'; SourcePriority=3 }
)
$selection = Select-RelayCandidates -Servers $servers -State $state -PerCountryLimit 5 -TotalLimit 5 -CooldownMinutes 15
$chosen = @($selection.Candidates)
Assert-Test ($chosen.Count -eq 5) "Expected five candidates, got $($chosen.Count)."
Assert-Test ($chosen[0].IP -eq '192.0.2.10' -and $chosen[1].IP -eq '192.0.2.11' -and $chosen[2].IP -eq '192.0.2.12') 'Independent relay/IPs were not prioritized first.'
Assert-Test ($chosen[3].IP -eq '192.0.2.13' -and $chosen[4].IP -eq '192.0.2.10' -and $chosen[4].Port -eq 992) 'Alternate port was not retained as fallback.'
$candidateText = Format-RelayCandidateTable -Candidates $chosen
Assert-Test ($candidateText -match '192\.0\.2\.10' -and $candidateText -match '192\.0\.2\.13') 'Candidate table was not rendered as a complete text record.'

# A high-volume RU listing must not starve a healthy UA relay.  The first
# candidate still follows the configured RU priority, but the next independent
# candidate must come from another CIS country before RU is filled further.
$diverseServers = @(
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-primary.opengw.net'; IP='192.0.2.20'; Port=443; Endpoint='192.0.2.20:443'; Priority=100; Score=900; Ping=30; Sessions=2; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-secondary.opengw.net'; IP='192.0.2.21'; Port=443; Endpoint='192.0.2.21:443'; Priority=100; Score=890; Ping=31; Sessions=2; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='UA'; HostName='ua-healthy.opengw.net'; IP='192.0.2.22'; Port=5555; Endpoint='192.0.2.22:5555'; Priority=95; Score=1200; Ping=15; Sessions=34; Source='NativeCatalog'; SourcePriority=3 }
)
$diverseSelection = Select-RelayCandidates -Servers $diverseServers -State (New-FailoverState) -PerCountryLimit 5 -TotalLimit 3 -CooldownMinutes 15
$diverseChosen = @($diverseSelection.Candidates)
Assert-Test ($diverseChosen.Count -eq 3) 'Country-diverse fixture did not produce three candidates.'
Assert-Test ($diverseChosen[0].CountryShort -eq 'RU' -and $diverseChosen[1].CountryShort -eq 'UA' -and $diverseChosen[2].CountryShort -eq 'RU') 'A healthy UA relay was starved behind the RU candidate batch.'

$knownGoodDiverseServers = @(
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-live.opengw.net'; IP='192.0.2.30'; Port=443; Endpoint='192.0.2.30:443'; Priority=100; Score=900; Ping=30; Sessions=2; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='RU'; HostName='ru-live-2.opengw.net'; IP='192.0.2.31'; Port=443; Endpoint='192.0.2.31:443'; Priority=100; Score=890; Ping=31; Sessions=2; Source='NativeCatalog'; SourcePriority=3 },
    [pscustomobject]@{ CountryShort='UA'; HostName='ua-known-good.opengw.net'; IP='192.0.2.32'; Port=5555; Endpoint='192.0.2.32:5555'; Priority=95; Score=0; Ping=9999; Sessions=0; Source='RecentKnownGood'; SourcePriority=0; VerifiedAt=(Get-Date).ToString('o') }
)
$knownGoodSelection = Select-RelayCandidates -Servers $knownGoodDiverseServers -State (New-FailoverState) -PerCountryLimit 5 -TotalLimit 3 -CooldownMinutes 15
$knownGoodChosen = @($knownGoodSelection.Candidates)
Assert-Test ($knownGoodChosen.Count -eq 3) 'Known-good country fixture did not produce three candidates.'
Assert-Test ($knownGoodChosen[0].CountryShort -eq 'RU' -and $knownGoodChosen[1].CountryShort -eq 'UA') 'A recently verified UA relay was still placed behind all live RU entries.'

$state.Failures = @([pscustomobject]@{ Endpoint='192.0.2.10:443'; FailedAt=(Get-Date).ToString('o') })
$selection = Select-RelayCandidates -Servers $servers -State $state -PerCountryLimit 5 -TotalLimit 5 -CooldownMinutes 15
Assert-Test (-not (@($selection.Candidates).Endpoint -contains '192.0.2.10:443')) 'Cooling endpoint was selected.'
Assert-Test (@($selection.Candidates).Endpoint -contains '192.0.2.10:992') 'Alternate port should remain eligible.'

# When every currently listed endpoint is cooling, the connector must not
# deadlock for the full 15-minute quarantine.  After the short grace period it
# should rotate the oldest failed relays in a bounded fallback batch.
$oldFailure = (Get-Date).AddMinutes(-3).ToString('o')
$state.Failures = @(
    [pscustomobject]@{ Endpoint='192.0.2.10:443'; FailedAt=$oldFailure },
    [pscustomobject]@{ Endpoint='192.0.2.10:992'; FailedAt=$oldFailure },
    [pscustomobject]@{ Endpoint='192.0.2.11:443'; FailedAt=$oldFailure },
    [pscustomobject]@{ Endpoint='192.0.2.12:443'; FailedAt=$oldFailure },
    [pscustomobject]@{ Endpoint='192.0.2.13:443'; FailedAt=$oldFailure }
)
$selection = Select-RelayCandidates -Servers $servers -State $state -PerCountryLimit 5 -TotalLimit 5 -CooldownMinutes 15
Assert-Test (@($selection.Candidates).Count -eq 0) 'All-cooling fixture unexpectedly produced regular candidates.'
$fallback = @(Select-CoolingFallbackCandidates -Servers $servers -State $state -Limit 3 -MinimumAgeMinutes 2)
Assert-Test ($fallback.Count -eq 3) "Expected three bounded cooling fallback candidates, got $($fallback.Count)."
Assert-Test ($fallback[0].IP -eq '192.0.2.10' -and $fallback[1].IP -eq '192.0.2.11' -and $fallback[2].IP -eq '192.0.2.12') 'Cooling fallback did not rotate independent relays first.'
$futureFailure = (Get-Date).AddSeconds(-30).ToString('o')
$state.Failures = @([pscustomobject]@{ Endpoint='192.0.2.10:443'; FailedAt=$futureFailure })
$fallback = @(Select-CoolingFallbackCandidates -Servers $servers -State $state -Limit 3 -MinimumAgeMinutes 2)
Assert-Test ($fallback.Count -eq 0) 'Cooling fallback ignored its grace period.'
$retryAt = Get-CoolingFallbackRetryAt -Servers $servers -State $state -MinimumAgeMinutes 2
Assert-Test ($retryAt -gt (Get-Date)) 'Cooling fallback retry time was not in the future.'
$state.Failures += [pscustomobject]@{ Endpoint='198.51.100.200:443'; FailedAt=(Get-Date).AddMinutes(-10).ToString('o') }
$filteredRetryAt = Get-CoolingFallbackRetryAt -Servers $servers -State $state -MinimumAgeMinutes 2
Assert-Test ($filteredRetryAt -eq $retryAt) 'Cooling fallback retry time included an endpoint outside the current catalog.'

try {
    $state = New-FailoverState
    $state.Current = [pscustomobject]@{ Endpoint='192.0.2.99:443' }
    Write-FailoverState -State $state
    $first = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    Assert-Test ($first.Current.Endpoint -eq '192.0.2.99:443') 'Atomic state write did not produce readable JSON.'
    $state.Current = [pscustomobject]@{ Endpoint='192.0.2.100:443' }
    Write-FailoverState -State $state
    $second = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    Assert-Test ($second.Current.Endpoint -eq '192.0.2.100:443') 'Atomic replacement did not replace the prior state.'
    $temps = Get-ChildItem -LiteralPath ([IO.Path]::GetDirectoryName($statePath)) -Filter (([IO.Path]::GetFileName($statePath)) + '.*.tmp') -ErrorAction SilentlyContinue
    Assert-Test (@($temps).Count -eq 0) 'Atomic state temp file was left behind.'
    $backups = Get-ChildItem -LiteralPath ([IO.Path]::GetDirectoryName($statePath)) -Filter (([IO.Path]::GetFileName($statePath)) + '.*.bak') -ErrorAction SilentlyContinue
    Assert-Test (@($backups).Count -eq 0) 'Atomic state backup file was left behind.'
} finally {
    if (Test-Path -LiteralPath $statePath) { [IO.File]::Delete($statePath) }
}

$powershellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$readyPath = Join-Path ([IO.Path]::GetTempPath()) ('tarkov-cis-mutex-ready-{0}.txt' -f [guid]::NewGuid().ToString('N'))
$holder = Start-Process -FilePath $powershellPath -ArgumentList @('-NoProfile', '-NonInteractive', '-File', $PSCommandPath, '-HoldMutex', '-ReadyPath', $readyPath) -WindowStyle Hidden -PassThru
try {
    $readyDeadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path -LiteralPath $readyPath) -and -not $holder.HasExited -and (Get-Date) -lt $readyDeadline) {
        Start-Sleep -Milliseconds 100
    }
    Assert-Test (Test-Path -LiteralPath $readyPath) 'Mutex holder did not signal readiness.'
    $blocked = $false
    try { $other = Enter-ConnectorMutex -TimeoutSeconds 0 } catch { $blocked = $true }
    if ($other) { Exit-ConnectorMutex -Mutex $other }
    Assert-Test $blocked 'Connector mutex did not block a second process.'
} finally {
    if ($holder -and -not $holder.HasExited) {
        $holder.Kill()
        $holder.WaitForExit()
    }
    if (Test-Path -LiteralPath $readyPath) { Remove-Item -LiteralPath $readyPath -Force }
}

Assert-Test (Test-SoftEtherResourceBusyError -ErrorRecord ([System.Management.Automation.ErrorRecord]::new(
    [InvalidOperationException]::new('vpncmd failed with exit code 43: AccountConnect'),
    'busy',
    [System.Management.Automation.ErrorCategory]::InvalidOperation,
    $null))) 'Exit code 43 was not classified as local resource busy.'

Assert-Test (Test-EstablishedSoftEtherSessionOutput -Output @('Session Name,SID-VPN-12345-ABCDEF')) 'Established SID output was not recognized.'
Assert-Test (-not (Test-EstablishedSoftEtherSessionOutput -Output @('Session Status,Connecting'))) 'Connecting output was incorrectly treated as established.'

$script:FailoverDeadline = (Get-Date).AddMilliseconds(-10)
$deadlineTriggered = $false
try { [void](Assert-FailoverTimeRemaining) } catch { $deadlineTriggered = $_.Exception.Message -match 'total deadline' }
$script:FailoverDeadline = $null
Assert-Test $deadlineTriggered 'Expired whole-cycle failover deadline was not enforced.'

$script:offlinePoll = 0
function Test-SoftEtherSession {
    $script:offlinePoll++
    $script:offlinePoll -lt 3
}
function Test-VpnAdapterReleased { $script:offlinePoll -ge 3 }
Assert-Test (Wait-SoftEtherOffline -TimeoutSeconds 2) 'Disconnect wait did not observe session and adapter release.'
Assert-Test ($script:offlinePoll -ge 3) 'Disconnect wait returned before the local adapter was released.'

Write-Host 'OK connector failover mock checks'
