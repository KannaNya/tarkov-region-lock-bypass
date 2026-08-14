param(
    [ValidateSet('Install', 'Run', 'Status', 'Stop', 'Uninstall')]
    [string]$Action = 'Status',
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json'),
    [int]$RefreshSeconds = 30
)

$ErrorActionPreference = 'Stop'
$scriptRoot = (Resolve-Path $PSScriptRoot).Path
$statePath = Join-Path $scriptRoot 'Tarkov-CisRouteKeeper.state.json'
$pidPath = Join-Path $scriptRoot 'Tarkov-CisRouteKeeper.pid'
$logPath = Join-Path $scriptRoot 'Tarkov-CisRouteKeeper.log'
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
        ReconnectSeconds = 60
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
$effectiveReconnectSeconds = [math]::Max(30, [int]$config.ReconnectSeconds)

function Write-Log {
    param([string]$Message)
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value ('{0} {1}' -f (Get-Date -Format o), $Message)
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
    $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex
    $ip = $ipConfig.IPv4Address.IPAddress | Select-Object -First 1
    $gateway = $ipConfig.IPv4DefaultGateway.NextHop | Select-Object -First 1
    if (-not $ip -or -not $gateway) { return $null }
    [pscustomobject]@{ InterfaceIndex = [int]$adapter.ifIndex; Alias = $adapter.InterfaceAlias; IPv4 = $ip; Gateway = $gateway }
}

function Test-SoftEtherSession {
    if (-not (Test-Path -LiteralPath $vpnCmdPath)) { return $false }
    & $vpnCmdPath /CLIENT localhost /CMD AccountStatusGet $vpnAccountName 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
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
    @($result | Sort-Object IPAddress -Unique)
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
            Set-NetRoute -RouteMetric ([int]$route.RouteMetric) -PolicyStore ActiveStore
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
}

function Sync-Routes {
    param($Vpn, $Targets)
    $desired = @($Targets.IPAddress | Sort-Object -Unique)
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

    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $Vpn.InterfaceIndex -ErrorAction SilentlyContinue |
        Set-NetRoute -RouteMetric 9000 -PolicyStore ActiveStore
    Write-State -Vpn $Vpn -IPs $desired -Hosts (Get-ObservedHosts) -OriginalDefaults $originalDefaults
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $vpn = Get-VpnInfo
    $sessionConnected = Test-SoftEtherSession
    [pscustomobject]@{ Task = $taskName; TaskState = if ($task) { $task.State } else { 'NotInstalled' }; VpnConnected = [bool]($vpn -and $sessionConnected); VpnInterface = $vpn.Alias; VpnIPv4 = $vpn.IPv4; VpnGateway = $vpn.Gateway } | Format-List
    Write-Host 'Observed hosts:'
    Get-ObservedHosts | ForEach-Object { [pscustomobject]@{ Host = $_ } } | Format-Table -AutoSize
    Write-Host 'Selected routes:'
    $targets = Resolve-Targets
    @(foreach ($target in $targets) {
        $route = Find-NetRoute -RemoteIPAddress $target.IPAddress | Where-Object NextHop | Select-Object -First 1
        [pscustomobject]@{ Host = $target.Host; IPAddress = $target.IPAddress; Interface = $route.InterfaceAlias; IfIndex = $route.InterfaceIndex; NextHop = $route.NextHop }
    }) | Format-Table -AutoSize
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
$lastConnectAttempt = [datetime]::MinValue
try {
    while ($true) {
        try {
            $vpn = Get-VpnInfo
            if ($vpn -and -not (Test-SoftEtherSession)) {
                Write-Log 'SoftEther session is no longer established; entering relay failover.'
                $vpn = $null
            }
            if (-not $vpn) {
                if (Test-Path -LiteralPath $statePath) { Remove-StateRoutes; Write-Log 'VPN disconnected; managed routes removed.' }
                if ((Get-Date) - $lastConnectAttempt -gt [TimeSpan]::FromSeconds($effectiveReconnectSeconds) -and (Test-Path -LiteralPath $connectorPath)) {
                    $lastConnectAttempt = Get-Date
                    Write-Log 'VPN unavailable; refreshing VPN Gate and trying the next CIS relay candidate.'
                    & $connectorPath -Action Connect -InterfaceAlias $vpnAlias 2>&1 | Out-String | ForEach-Object { Write-Log $_.Trim() }
                }
            } else {
                Sync-Routes -Vpn $vpn -Targets (Resolve-Targets)
            }
        } catch { Write-Log "Sync error: $($_.Exception.Message)" }
        Start-Sleep -Seconds $effectiveRefreshSeconds
    }
} finally {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex(); $mutex.Dispose()
}
