param(
    [ValidateSet('Install', 'Run', 'Status', 'Stop', 'Uninstall')]
    [string]$Action = 'Status',
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json'),
    [int]$RefreshSeconds = 30,
    [switch]$Legacy
)

$ErrorActionPreference = 'Stop'
# Existing scheduled-task filename now delegates future starts to Python.
# The already-running legacy process is not stopped by a source-only change.
$pythonPidPath = Join-Path $env:LOCALAPPDATA 'TarkovCIS\keeper.pid.json'
if (-not $Legacy -and ($Action -in @('Run', 'Install') -or
    (Test-Path -LiteralPath $pythonPidPath))) {
    if ($Action -eq 'Run' -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'Tarkov-CisRouteKeeper.state.json'))) {
        throw 'Legacy route ownership remains. Use python-task.ps1 -Action Install for controlled cleanup/migration before starting Python.'
    }
    & (Join-Path $PSScriptRoot '..\scripts\python-task.ps1') -Action $Action -ConfigPath $ConfigPath
    exit $LASTEXITCODE
}
$scriptRoot = (Resolve-Path $PSScriptRoot).Path
$statePath = Join-Path $scriptRoot 'Tarkov-CisRouteKeeper.state.json'
$pidPath = Join-Path $scriptRoot 'Tarkov-CisRouteKeeper.pid'
$logPath = Join-Path $scriptRoot 'Tarkov-CisRouteKeeper.log'
$logMaxBytes = 2MB
$defaultTaskName = 'Tarkov-CIS-RouteKeeper'
$connectorPath = Join-Path $scriptRoot 'Connect-VpnGateCis.ps1'
$vpnCmdPath = 'C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe'
$vpnAccountName = 'Tarkov-CIS-PlayOnly'
$defaultHosts = @(
    'gw-pvp.escapefromtarkov.ru',
    'gw-pvp.escapefromtarkov.com',
    'gw-pvp-season.escapefromtarkov.ru',
    'lobby.escapefromtarkov.ru'
)

function Read-Config {
    $config = [ordered]@{
        TaskName = $defaultTaskName
        VpnInterfaceAlias = 'VPN - VPN Client'
        RefreshSeconds = 30
        FailedCycleRetrySeconds = 10
        DisconnectedPollSeconds = 5
        GameLogRoots = @()
        TargetHosts = $defaultHosts
        RaidTargets = @()
    }
    if (Test-Path -LiteralPath $ConfigPath) {
        $raw = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
        foreach ($property in $raw.PSObject.Properties) { $config[$property.Name] = $property.Value }
    }
    if ($RefreshSeconds -eq 30 -and $config.RefreshSeconds) { $RefreshSeconds = [int]$config.RefreshSeconds }
    $config
}

$config = Read-Config
$taskName = [string]$config.TaskName
$vpnAlias = [string]$config.VpnInterfaceAlias
$effectiveRefreshSeconds = if ($PSBoundParameters.ContainsKey('RefreshSeconds')) { $RefreshSeconds } else { [int]$config.RefreshSeconds }
$effectiveFailedCycleRetrySeconds = [math]::Max(5, [int]$config.FailedCycleRetrySeconds)
$effectiveDisconnectedPollSeconds = [math]::Max(1, [int]$config.DisconnectedPollSeconds)

function Write-Log {
    param([AllowEmptyString()][string]$Message)

    # Keep one rotated copy so a permanently running task cannot grow the log
    # without bound.  The connector output is already bounded by its candidate
    # limits, so a single 2 MiB rollover is sufficient here.
    if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        $currentLog = Get-Item -LiteralPath $logPath -ErrorAction SilentlyContinue
        if ($currentLog -and $currentLog.Length -ge $logMaxBytes) {
            $rotatedLog = "$logPath.1"
            Remove-Item -LiteralPath $rotatedLog -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $logPath -Destination $rotatedLog -Force
        }
    }

    $timestamp = Get-Date -Format o
    $lines = @(([string]$Message) -split '\r?\n') | ForEach-Object {
        '{0} {1}' -f $timestamp, $_
    }
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value $lines
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This action requires an elevated PowerShell window.'
    }
}

function Get-VpnInfo {
    $adapter = Get-NetAdapter -InterfaceAlias $vpnAlias -ErrorAction SilentlyContinue
    if (-not $adapter -or $adapter.Status -ne 'Up') { return $null }
    # SoftEther removes and recreates its IP interface while changing relays.
    # The adapter can therefore disappear between these two queries.  That is
    # a normal disconnected sample, not a keeper-wide failure.
    try {
        $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $ipConfig) { return $null }
    # An APIPA lease can remain briefly after SoftEther disconnects.  Treating
    # it as a usable VPN address creates a false "connected" state and leaves
    # route reconciliation running against a dead adapter.
    $ip = $ipConfig.IPv4Address |
        Where-Object { $_.IPAddress -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -ExpandProperty IPAddress -First 1
    $gateway = $ipConfig.IPv4DefaultGateway.NextHop | Select-Object -First 1
    if (-not $ip -or -not $gateway) { return $null }
    [pscustomobject]@{ InterfaceIndex = [int]$adapter.ifIndex; Alias = $adapter.InterfaceAlias; IPv4 = $ip; Gateway = $gateway }
}

function Test-SoftEtherSession {
    if (-not (Test-Path -LiteralPath $vpnCmdPath)) { return $false }
    $output = @(& $vpnCmdPath /CSV /CLIENT localhost /CMD AccountStatusGet $vpnAccountName 2>$null)
    if ($LASTEXITCODE -ne 0) { return $false }

    # AccountStatusGet returns exit code 0 even when the account exists but is
    # not connected.  An SID is emitted only for an established session and is
    # locale-independent, unlike the human-readable status text.
    $text = ($output | ForEach-Object { [string]$_ }) -join "`n"
    return ($text -match '(?im)\bSID-[A-Za-z0-9-]+\b')
}

function Get-StreamRecordType {
    param($Record)
    if ($Record -is [System.Management.Automation.ErrorRecord]) { return 'error' }
    if ($Record -is [System.Management.Automation.WarningRecord]) { return 'warning' }
    if ($Record -is [System.Management.Automation.VerboseRecord]) { return 'verbose' }
    if ($Record -is [System.Management.Automation.DebugRecord]) { return 'debug' }
    if ($Record -is [System.Management.Automation.InformationRecord]) { return 'information' }
    if ($Record -is [System.Management.Automation.ProgressRecord]) { return 'progress' }
    'output'
}

function Get-StreamRecordLines {
    param($Record)
    $streamType = Get-StreamRecordType -Record $Record
    if ($Record -is [System.Management.Automation.ErrorRecord]) {
        $text = $Record.ToString()
    } elseif ($Record -is [System.Management.Automation.WarningRecord] -or
        $Record -is [System.Management.Automation.VerboseRecord] -or
        $Record -is [System.Management.Automation.DebugRecord]) {
        $text = [string]$Record.Message
    } elseif ($Record -is [System.Management.Automation.InformationRecord]) {
        $text = [string]$Record.MessageData
    } elseif ($Record -is [System.Management.Automation.ProgressRecord]) {
        $text = '{0}: {1}' -f $Record.Activity, $Record.StatusDescription
    } elseif ($Record -is [string]) {
        $text = $Record
    } else {
        # Connector success output can contain formatted PSCustomObjects (for
        # example its candidate table), so preserve its rendered content.
        $text = $Record | Out-String -Width 4096
    }
    [pscustomobject]@{ Stream = $streamType; Lines = @(([string]$text) -split '\r?\n') }
}

function Write-ConnectorStream {
    param($Record)
    $rendered = Get-StreamRecordLines -Record $Record
    foreach ($line in @($rendered.Lines)) {
        Write-Log ("connector[{0}] {1}" -f $rendered.Stream, $line)
    }
}

function Get-KeeperLoopSleepSeconds {
    param([bool]$VpnAvailable)
    if ($VpnAvailable) { return [int]$effectiveRefreshSeconds }
    [int][math]::Min($effectiveRefreshSeconds, $effectiveDisconnectedPollSeconds)
}

function Get-ObservedHosts {
    $observed = @()
    $pattern = '(?:https?|wss?)://(?:lobby\.escapefromtarkov\.ru|wsn-pvp-season-[A-Za-z0-9-]+\.escapefromtarkov\.com)'
    foreach ($root in @($config.GameLogRoots)) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $files = Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.log' -ErrorAction SilentlyContinue |
            Where-Object LastWriteTime -gt (Get-Date).AddDays(-7) |
            Sort-Object LastWriteTime -Descending | Select-Object -First 50
        foreach ($file in $files) {
            foreach ($line in @(Select-String -LiteralPath $file.FullName -Pattern $pattern -AllMatches -ErrorAction SilentlyContinue)) {
                foreach ($match in $line.Matches) {
                    $hostMatch = [regex]::Match($match.Value, '://(?<Host>[^/:?\s]+)')
                    if ($hostMatch.Success) { $observed += $hostMatch.Groups['Host'].Value }
                }
            }
        }
    }
    @(@($config.TargetHosts) + $defaultHosts + $observed | Sort-Object -Unique)
}

function Resolve-Targets {
    $result = @()
    foreach ($hostName in Get-ObservedHosts) {
        try {
            $records = Resolve-DnsName -Name $hostName -Type A -DnsOnly -ErrorAction Stop | Where-Object Type -eq 'A'
            foreach ($record in $records) {
                $result += [pscustomobject]@{ Host = $hostName; IPAddress = $record.IPAddress }
            }
        } catch { Write-Log "DNS resolution failed for ${hostName}: $($_.Exception.Message)" }
    }
    # Routing is still one /32 per IP, but keep every hostname that resolved
    # to that IP.  Cloudflare/shared backend addresses commonly serve lobby,
    # WSN and launcher hosts at once; dropping the relation made Status output
    # look as if those hosts had never been observed.
    foreach ($group in @($result | Group-Object IPAddress | Sort-Object Name)) {
        $hosts = [string[]]@($group.Group | ForEach-Object Host | Sort-Object -Unique)
        [pscustomobject]@{
            Host = $hosts[0]
            HostNames = $hosts
            IPAddress = [string]$group.Name
        }
    }
}

function Read-State {
    if (Test-Path -LiteralPath $statePath) {
        try { return Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json } catch { Write-Log 'State file could not be read.' }
    }
    $null
}

function Write-State {
    param($Vpn, [string[]]$IPs, [string[]]$Hosts, $OriginalDefaults)
    [ordered]@{
        UpdatedAt = (Get-Date).ToString('o')
        VpnInterfaceIndex = $Vpn.InterfaceIndex
        VpnInterfaceAlias = $Vpn.Alias
        VpnGateway = $Vpn.Gateway
        ManagedIPs = @($IPs)
        TargetHosts = @($Hosts)
        OriginalDefaults = @($OriginalDefaults)
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Remove-StateRoutes {
    $saved = Read-State
    if (-not $saved) { return }
    foreach ($ip in @($saved.ManagedIPs)) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ip/32" -InterfaceIndex ([int]$saved.VpnInterfaceIndex) -ErrorAction SilentlyContinue |
            Where-Object NextHop -eq $saved.VpnGateway |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    foreach ($route in @($saved.OriginalDefaults)) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex ([int]$saved.VpnInterfaceIndex) -ErrorAction SilentlyContinue |
            Where-Object NextHop -eq $route.NextHop |
            Set-NetRoute -RouteMetric ([int]$route.RouteMetric) -PolicyStore ActiveStore -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
}

function Sync-Routes {
    param($Vpn, $Targets)
    $desired = @($Targets.IPAddress | Sort-Object -Unique)
    if (-not $desired) { throw 'No DNS targets resolved; previous owned routes retained.' }
    $saved = Read-State
    $oldIPs = if ($saved) { @($saved.ManagedIPs) } else { @() }
    $originalDefaults = if ($saved -and $saved.OriginalDefaults) {
        @($saved.OriginalDefaults)
    } else {
        @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $Vpn.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object NextHop, RouteMetric)
    }
    $identityChanged = $saved -and (([int]$saved.VpnInterfaceIndex -ne $Vpn.InterfaceIndex) -or ([string]$saved.VpnGateway -ne [string]$Vpn.Gateway))

    if ($identityChanged) {
        foreach ($ip in $oldIPs) {
            Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ip/32" -InterfaceIndex ([int]$saved.VpnInterfaceIndex) -ErrorAction SilentlyContinue |
                Where-Object NextHop -eq $saved.VpnGateway |
                Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
        }
        Write-Log 'Previous VPN interface/gateway routes were removed after identity change.'
    }

    foreach ($ip in @($oldIPs | Where-Object { $_ -notin $desired })) {
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ip/32" -InterfaceIndex $Vpn.InterfaceIndex -ErrorAction SilentlyContinue |
            Where-Object NextHop -eq $Vpn.Gateway |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }

    foreach ($ip in $desired) {
        $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ip/32" -InterfaceIndex $Vpn.InterfaceIndex -ErrorAction SilentlyContinue |
            Where-Object NextHop -eq $Vpn.Gateway
        if (-not $route) {
            New-NetRoute -AddressFamily IPv4 -DestinationPrefix "$ip/32" -InterfaceIndex $Vpn.InterfaceIndex -NextHop $Vpn.Gateway -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
            Write-Log "Added $ip/32 via $($Vpn.Gateway)."
        }
    }

    Write-State -Vpn $Vpn -IPs $desired -Hosts (Get-ObservedHosts) -OriginalDefaults $originalDefaults
    Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $Vpn.InterfaceIndex -ErrorAction SilentlyContinue |
        Set-NetRoute -RouteMetric 9000 -PolicyStore ActiveStore
    Write-State -Vpn $Vpn -IPs $desired -Hosts (Get-ObservedHosts) -OriginalDefaults $originalDefaults
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $vpn = Get-VpnInfo
    $sessionConnected = Test-SoftEtherSession
    $vpnConnected = [bool]($vpn -and $sessionConnected)
    [pscustomobject]@{
        Task = $taskName
        TaskState = if ($task) { $task.State } else { 'NotInstalled' }
        VpnConnected = $vpnConnected
        VpnInterface = if ($vpn) { $vpn.Alias } else { $null }
        VpnIPv4 = if ($vpn) { $vpn.IPv4 } else { $null }
        VpnGateway = if ($vpn) { $vpn.Gateway } else { $null }
    } | Format-List
    Write-Host 'Observed hosts:'
    Get-ObservedHosts | ForEach-Object { [pscustomobject]@{ Host = $_ } } | Format-Table -AutoSize
    Write-Host 'Selected routes:'
    $targets = Resolve-Targets
    @(foreach ($target in $targets) {
        $route = Find-NetRoute -RemoteIPAddress $target.IPAddress | Where-Object NextHop | Select-Object -First 1
        $viaVpn = $vpnConnected -and $route -and
            ([int]$route.InterfaceIndex -eq [int]$vpn.InterfaceIndex) -and
            ([string]$route.NextHop -eq [string]$vpn.Gateway)
        [pscustomobject]@{
            Hosts = ($target.HostNames -join ', ')
            IPAddress = $target.IPAddress
            RouteState = if (-not $route) { 'NoRoute' } elseif ($viaVpn) { 'VPN' } else { 'Other' }
            Interface = if ($route) { $route.InterfaceAlias } else { $null }
            IfIndex = if ($route) { $route.InterfaceIndex } else { $null }
            NextHop = if ($route) { $route.NextHop } else { $null }
        }
    }) | Format-Table IPAddress, RouteState, Interface, IfIndex, NextHop, Hosts -AutoSize -Wrap
}

if ($Action -eq 'Status') { Show-Status; exit 0 }

if ($Action -eq 'Install') {
    Assert-Administrator
    $shell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
    if (-not $shell) { $shell = (Get-Command powershell.exe -ErrorAction Stop).Source }
    $resolvedConfig = if (Test-Path -LiteralPath $ConfigPath) { (Resolve-Path $ConfigPath).Path } else { $ConfigPath }
    $args = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action Run -ConfigPath "{1}"' -f $PSCommandPath, $resolvedConfig
    $taskAction = New-ScheduledTaskAction -Execute $shell -Argument $args
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    # Register-ScheduledTask -Force updates a task definition but does not
    # replace an already-running process. Stop and wait first so an older
    # deployed keeper cannot retain the mutex and keep calling an obsolete
    # connector after an upgrade.
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $stopDeadline = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 250
            $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        } while ($existingTask -and $existingTask.State -eq 'Running' -and (Get-Date) -lt $stopDeadline)
        if ($existingTask -and $existingTask.State -eq 'Running') {
            throw 'The previous route-keeper task did not stop; refusing to start a second copy.'
        }
    }

    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'Keeps current Tarkov CIS backend host routes on the SoftEther adapter.' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 3
    Show-Status
    exit 0
}

if ($Action -eq 'Stop' -or $Action -eq 'Uninstall') {
    Assert-Administrator
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $pidPath) { Stop-Process -Id ([int](Get-Content -LiteralPath $pidPath)) -Force -ErrorAction SilentlyContinue }
    Remove-StateRoutes
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    if ($Action -eq 'Uninstall') { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
    Show-Status
    exit 0
}

Assert-Administrator
$created = $false
$mutex = [Threading.Mutex]::new($true, 'Global\Tarkov-CisRouteKeeper', [ref]$created)
if (-not $created) { exit 0 }
Set-Content -LiteralPath $pidPath -Value $PID -Encoding ascii
Write-Log "Keeper started as PID $PID."
$nextConnectAttempt = [datetime]::MinValue
try {
    while ($true) {
        $vpnAvailable = $false
        try {
            $vpn = Get-VpnInfo
            if ($vpn -and -not (Test-SoftEtherSession)) {
                Write-Log 'SoftEther session is no longer established; entering relay failover.'
                $vpn = $null
            }
            if (-not $vpn) {
                if (Test-Path -LiteralPath $statePath) { Remove-StateRoutes; Write-Log 'VPN disconnected; managed routes removed.' }
                if ((Get-Date) -ge $nextConnectAttempt -and (Test-Path -LiteralPath $connectorPath)) {
                    Write-Log 'VPN unavailable; refreshing VPN Gate and trying the next CIS relay candidate.'
                    # Redirect every PowerShell stream into the success stream
                    # and log each record/line with its stream type.  This keeps
                    # warnings, verbose diagnostics and connector errors visible
                    # in the long-running task log.
                    try {
                        & $connectorPath -Action Connect -InterfaceAlias $vpnAlias -DeferRouteProtection *>&1 |
                            ForEach-Object { Write-ConnectorStream -Record $_ }
                    } catch {
                        Write-Log "Connector cycle failed: $($_.Exception.Message)"
                    }

                    # Re-read both the SoftEther SID and the lease immediately.
                    # A successful connector must restore authorization routes
                    # now rather than waiting for the normal connected refresh.
                    $vpn = Get-VpnInfo
                    if ($vpn -and (Test-SoftEtherSession)) {
                        Sync-Routes -Vpn $vpn -Targets (Resolve-Targets)
                        $vpnAvailable = $true
                        $nextConnectAttempt = [datetime]::MinValue
                        Write-Log 'Relay failover completed; authorization routes were restored immediately.'
                    } else {
                        $nextConnectAttempt = (Get-Date).AddSeconds($effectiveFailedCycleRetrySeconds)
                        Write-Log "No relay connected; refreshing the candidate list again in $effectiveFailedCycleRetrySeconds second(s)."
                    }
                }
            } else {
                Sync-Routes -Vpn $vpn -Targets (Resolve-Targets)
                $vpnAvailable = $true
                $nextConnectAttempt = [datetime]::MinValue
            }
        } catch { Write-Log "Sync error: $($_.Exception.Message)" }
        Start-Sleep -Seconds (Get-KeeperLoopSleepSeconds -VpnAvailable $vpnAvailable)
    }
} finally {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex(); $mutex.Dispose()
}
