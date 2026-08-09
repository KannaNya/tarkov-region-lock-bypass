param(
    [ValidateSet('Connect', 'Status', 'Disconnect')]
    [string]$Action = 'Connect',
    [string]$AccountName = 'Tarkov-CIS-PlayOnly',
    [string]$VpnCmdPath = 'C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe',
    [string]$InterfaceAlias = 'VPN - VPN Client',
    [string]$NicName = 'VPN',
    [int]$ConnectTimeoutSeconds = 15,
    [int]$MaxCandidatesPerCountry = 5
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $VpnCmdPath)) { throw "SoftEther vpncmd not found: $VpnCmdPath" }

function Invoke-VpnCmd {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $VpnCmdPath /CLIENT localhost /CMD @Arguments
    if ($LASTEXITCODE -ne 0) { throw "vpncmd failed: $($Arguments -join ' ')" }
}

function Disconnect-VpnAccount {
    & $VpnCmdPath /CLIENT localhost /CMD AccountDisconnect $AccountName 2>$null | Out-Null
}

function Get-VpnIPv4 {
    Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $InterfaceAlias -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '0.0.0.0' } |
        Select-Object -First 1
}

function Test-TcpQuick {
    param([string]$IPAddress, [int]$Port, [int]$TimeoutMs = 2500)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($IPAddress, $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $task.IsCompletedSuccessfully -and $client.Connected
    } catch { return $false } finally { $client.Dispose() }
}

function Get-CisServers {
    # Add a cache-buster: the public list is a live snapshot and intermediary
    # caches can otherwise return an older country inventory.
    $apiUri = 'https://www.vpngate.net/api/iphone/?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $lines = (Invoke-WebRequest -UseBasicParsing -Uri $apiUri -Headers @{
        'User-Agent' = 'Tarkov-CIS-RouteKeeper/1.0'
        'Cache-Control' = 'no-cache'
    }).Content -split "`r?`n"
    $headerLine = '#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64'
    $headerIndex = [Array]::IndexOf($lines, $headerLine)
    if ($headerIndex -lt 0) { throw 'VPN Gate CSV header was not found.' }
    $header = $headerLine.TrimStart('#') -split ','
    $rows = $lines[($headerIndex + 1)..($lines.Length - 1)] |
        Where-Object { $_ -and $_ -notmatch '^\*' } | ConvertFrom-Csv -Header $header
    # UA is included because it is present in VPN Gate's live CIS-compatible
    # list (and is a requested fallback in the user's deployment).
    $priority = @{ RU = 100; UA = 95; KZ = 90; BY = 85; AM = 80; AZ = 75; GE = 70; MD = 65; KG = 60; TJ = 55; TM = 50; UZ = 45 }
    $codes = @($priority.Keys)
    $candidates = @($rows | Where-Object CountryShort -in $codes | ForEach-Object {
        $port = 443
        try {
            $ovpn = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_.OpenVPN_ConfigData_Base64))
            if ($ovpn -match '(?m)^remote\s+\S+\s+(\d+)') { $port = [int]$Matches[1] }
        } catch {}
        [pscustomobject]@{
            HostName = $_.HostName; IP = $_.IP; Port = $port
            CountryLong = $_.CountryLong; CountryShort = $_.CountryShort
            Priority = $priority[[string]$_.CountryShort]
            Score = [int64]$_.Score; Ping = [int]$_.Ping
            SpeedMbps = [math]::Round([int64]$_.Speed / 1000000, 1)
            Sessions = [int]$_.NumVpnSessions
        }
    })
    @($candidates | Group-Object CountryShort | ForEach-Object {
        $_.Group | Sort-Object Score -Descending | Select-Object -First $MaxCandidatesPerCountry
    } | Sort-Object Priority, Score -Descending)
}

if ($Action -eq 'Status') {
    try { Invoke-VpnCmd AccountStatusGet $AccountName } catch { Write-Host "Account is offline or not configured: $AccountName" }
    Get-NetAdapter -Name $InterfaceAlias -ErrorAction SilentlyContinue |
        Format-Table ifIndex, Name, Status, MacAddress, LinkSpeed -AutoSize
    exit 0
}

if ($Action -eq 'Disconnect') {
    Disconnect-VpnAccount
    Write-Host "Disconnected $AccountName."
    exit 0
}

$servers = Get-CisServers
if (-not $servers) { throw 'VPN Gate currently lists no CIS relay.' }
$servers | Format-Table CountryShort, IP, Port, Ping, SpeedMbps, Sessions, Score -AutoSize
$reachable = @($servers | Where-Object { Test-TcpQuick -IPAddress $_.IP -Port $_.Port })
if (-not $reachable) {
    Disconnect-VpnAccount
    throw 'CIS relays are listed, but none currently accept their published endpoint.'
}

$existingText = (& $VpnCmdPath /CLIENT localhost /CMD AccountList | Out-String)
$exists = $existingText -match [regex]::Escape($AccountName)
foreach ($server in $reachable) {
    Disconnect-VpnAccount
    if ($exists) {
        Invoke-VpnCmd AccountSet $AccountName "/SERVER:$($server.IP):$($server.Port)" '/HUB:VPNGATE'
    } else {
        Invoke-VpnCmd AccountCreate $AccountName "/SERVER:$($server.IP):$($server.Port)" '/HUB:VPNGATE' "/USERNAME:vpn" "/NICNAME:$NicName"
        Invoke-VpnCmd AccountPasswordSet $AccountName '/PASSWORD:vpn' '/TYPE:standard'
        $exists = $true
    }
    Invoke-VpnCmd AccountConnect $AccountName
    $deadline = (Get-Date).AddSeconds($ConnectTimeoutSeconds)
    do { Start-Sleep -Milliseconds 500; $vpnIP = Get-VpnIPv4; if ($vpnIP) { break } } while ((Get-Date) -lt $deadline)
    if ($vpnIP) {
        $adapter = Get-NetAdapter -Name $InterfaceAlias
        Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue |
            Set-NetRoute -RouteMetric 9000 -PolicyStore ActiveStore
        $selected = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
            Where-Object State -eq 'Alive' |
            Sort-Object @{Expression={ $_.RouteMetric + (Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex).InterfaceMetric }} |
            Select-Object -First 1
        if ($selected.InterfaceIndex -eq $adapter.ifIndex) {
            Disconnect-VpnAccount
            throw 'VPN remained the selected default route; disconnected for safety.'
        }
        Write-Host "Connected CIS relay $($server.CountryShort) $($server.IP):$($server.Port); default remains $($selected.InterfaceAlias)."
        exit 0
    }
}

Disconnect-VpnAccount
throw 'Reachable CIS relays were tried, but none produced a usable VPN IPv4 lease.'
