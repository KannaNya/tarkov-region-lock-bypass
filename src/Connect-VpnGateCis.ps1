param(
    [ValidateSet('Connect', 'Status', 'Disconnect', 'Candidates', 'RememberCurrent')]
    [string]$Action = 'Connect',
    [string]$AccountName = 'Tarkov-CIS-PlayOnly',
    [string]$VpnCmdPath = 'C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe',
    [string]$InterfaceAlias = 'VPN - VPN Client',
    [string]$NicName = 'VPN',
    [int]$ConnectTimeoutSeconds = 25,
    [int]$MaxCandidatesPerCountry = 5,
    [int]$MaxCandidatesTotal = 12,
    [int]$TcpProbeTimeoutMilliseconds = 2500,
    [int]$DiscoveryTimeoutSeconds = 15,
    [int]$FailureCooldownMinutes = 15,
    [int]$KnownGoodLifetimeHours = 48,
    [string]$RelayCountry,
    [string]$StatePath
)

$ErrorActionPreference = 'Stop'
if (-not $StatePath) { $StatePath = Join-Path $PSScriptRoot 'Connect-VpnGateCis.state.json' }
if (-not (Test-Path -LiteralPath $VpnCmdPath)) { throw "SoftEther vpncmd not found: $VpnCmdPath" }

$countryPriority = [ordered]@{
    RU = 100
    UA = 95
    KZ = 90
    BY = 85
    AM = 80
    AZ = 75
    GE = 70
    MD = 65
    KG = 60
    TJ = 55
    TM = 50
    UZ = 45
}

function Invoke-VpnCmd {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = @(& $VpnCmdPath /CLIENT localhost /CMD @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "vpncmd failed with exit code ${exitCode}: $($Arguments[0])"
    }
    $output
}

function Test-SoftEtherSession {
    & $VpnCmdPath /CLIENT localhost /CMD AccountStatusGet $AccountName 2>$null | Out-Null
    $LASTEXITCODE -eq 0
}

function Get-VpnLease {
    $adapter = Get-NetAdapter -InterfaceAlias $InterfaceAlias -ErrorAction SilentlyContinue
    if (-not $adapter -or $adapter.Status -ne 'Up') { return $null }
    $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue
    $ip = $ipConfig.IPv4Address.IPAddress | Where-Object { $_ -and $_ -notlike '169.254.*' -and $_ -ne '0.0.0.0' } | Select-Object -First 1
    $gateway = $ipConfig.IPv4DefaultGateway.NextHop | Select-Object -First 1
    if (-not $ip -or -not $gateway) { return $null }
    [pscustomobject]@{
        InterfaceIndex = [int]$adapter.ifIndex
        InterfaceAlias = $adapter.InterfaceAlias
        IPv4 = [string]$ip
        Gateway = [string]$gateway
    }
}

function Get-ConnectedVpnInfo {
    if (-not (Test-SoftEtherSession)) { return $null }
    Get-VpnLease
}

function Get-ConfiguredAccountEndpoint {
    $output = Invoke-VpnCmd -Arguments @('AccountGet', $AccountName)
    $hostName = $null
    $port = $null
    foreach ($line in $output) {
        $text = [string]$line
        if ($text -notmatch '^(?<Label>[^|]+)\|(?<Value>.+)$') { continue }
        $label = $Matches['Label'].Trim()
        $value = $Matches['Value'].Trim()
        if (-not $hostName -and ($label -match '(?i)VPN Server.*Host|主机名' -or $value -match '^(?:\d{1,3}\.){3}\d{1,3}$|\.opengw\.net$')) {
            $hostName = $value
            continue
        }
        if ($hostName -and -not $port -and ($label -match '(?i)VPN Server.*Port|端口号') -and $value -match '^\d+$') {
            $port = [int]$value
        }
    }
    if (-not $hostName -or -not $port) { throw 'Could not read the configured VPN endpoint from SoftEther.' }
    [pscustomobject]@{
        HostName = $hostName
        IP = $hostName
        Port = $port
        Endpoint = ('{0}:{1}' -f $hostName, $port)
    }
}

function Disconnect-VpnAccount {
    & $VpnCmdPath /CLIENT localhost /CMD AccountDisconnect $AccountName 2>$null | Out-Null
    Start-Sleep -Milliseconds 500
}

function Test-TcpQuick {
    param([string]$IPAddress, [int]$Port, [int]$TimeoutMs = 2500)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($IPAddress, $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-TcpPortFromOpenVpnConfig {
    param([string]$EncodedConfig)
    if (-not $EncodedConfig) { return $null }
    try {
        $configText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedConfig))
    } catch {
        return $null
    }

    $globalTcp = $configText -match '(?im)^\s*proto\s+tcp(?:-client)?\s*(?:[#;].*)?$'
    foreach ($match in [regex]::Matches($configText, '(?im)^\s*remote\s+\S+\s+(\d+)(?:\s+(\S+))?')) {
        $remoteProtocol = [string]$match.Groups[2].Value
        if ($globalTcp -or $remoteProtocol -match '^tcp(?:-client)?$') {
            return [int]$match.Groups[1].Value
        }
    }
    $null
}

function Get-CisServers {
    # VPN Gate's API contains an OpenVPN profile rather than a dedicated
    # SoftEther port field. Its TCP profile uses the same listener displayed
    # in the official SSL-VPN column. UDP-only profiles must never be passed to
    # SoftEther as if their UDP port were a TCP listener.
    $cacheBust = [guid]::NewGuid().ToString('N')
    $apiUri = "https://www.vpngate.net/api/iphone/?t=$cacheBust"
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec $DiscoveryTimeoutSeconds -Uri $apiUri -Headers @{
        'User-Agent' = 'Tarkov-CIS-RouteKeeper/1.1'
        'Cache-Control' = 'no-cache, no-store'
        'Pragma' = 'no-cache'
    }
    $lines = $response.Content -split "`r?`n"
    $headerLine = '#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64'
    $headerIndex = [Array]::IndexOf($lines, $headerLine)
    if ($headerIndex -lt 0) { throw 'VPN Gate CSV header was not found.' }
    $header = $headerLine.TrimStart('#') -split ','
    $rows = $lines[($headerIndex + 1)..($lines.Length - 1)] |
        Where-Object { $_ -and $_ -notmatch '^\*' } |
        ConvertFrom-Csv -Header $header

    $servers = @($rows | Where-Object CountryShort -in @($countryPriority.Keys) | ForEach-Object {
        $port = Get-TcpPortFromOpenVpnConfig -EncodedConfig $_.OpenVPN_ConfigData_Base64
        if ($port) {
            [pscustomobject]@{
                HostName = [string]$_.HostName
                IP = [string]$_.IP
                Port = [int]$port
                Endpoint = ('{0}:{1}' -f $_.IP, $port)
                CountryLong = [string]$_.CountryLong
                CountryShort = [string]$_.CountryShort
                Priority = [int]$countryPriority[[string]$_.CountryShort]
                Score = [int64]$_.Score
                Ping = [int]$_.Ping
                SpeedMbps = [math]::Round([int64]$_.Speed / 1000000, 1)
                Sessions = [int]$_.NumVpnSessions
                Source = 'Live'
                SourcePriority = 1
            }
        }
    })

    # A volunteer may publish duplicate rows. Keep the strongest row for each
    # concrete SoftEther endpoint so a failed endpoint is not retried under a
    # different hostname in the same cycle.
    @($servers | Group-Object Endpoint | ForEach-Object {
        $_.Group | Sort-Object Score -Descending | Select-Object -First 1
    })
}

function New-FailoverState {
    [ordered]@{
        Version = 2
        UpdatedAt = (Get-Date).ToString('o')
        Current = $null
        Failures = @()
        KnownGood = @()
    }
}

function Read-FailoverState {
    $state = New-FailoverState
    if (-not (Test-Path -LiteralPath $StatePath)) { return $state }
    try {
        $saved = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
        if ($saved.Current) { $state.Current = $saved.Current }
        if ($saved.Failures) { $state.Failures = @($saved.Failures) }
        if ($saved.KnownGood) { $state.KnownGood = @($saved.KnownGood) }
    } catch {
        # A corrupt local cache must not prevent discovery of a fresh relay.
    }
    $state
}

function Write-FailoverState {
    param($State)
    $State.UpdatedAt = (Get-Date).ToString('o')
    $State.Failures = @($State.Failures | Where-Object {
        try { [datetimeoffset]::Parse([string]$_.FailedAt) -gt [datetimeoffset]::Now.AddDays(-2) } catch { $false }
    })
    $State.KnownGood = @($State.KnownGood | Where-Object {
        try { [datetimeoffset]::Parse([string]$_.VerifiedAt) -gt [datetimeoffset]::Now.AddHours(-$KnownGoodLifetimeHours) } catch { $false }
    })
    $State | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Set-EndpointFailure {
    param($State, $Server, [string]$Reason)
    $endpoint = [string]$Server.Endpoint
    $State.Failures = @($State.Failures | Where-Object { [string]$_.Endpoint -ne $endpoint })
    $State.Failures += [pscustomobject]@{
        Endpoint = $endpoint
        HostName = [string]$Server.HostName
        CountryShort = [string]$Server.CountryShort
        FailedAt = (Get-Date).ToString('o')
        Reason = $Reason
    }
    if ($State.Current -and [string]$State.Current.Endpoint -eq $endpoint) { $State.Current = $null }
    Write-FailoverState -State $State
}

function Set-EndpointSuccess {
    param($State, $Server)
    $endpoint = [string]$Server.Endpoint
    $State.Failures = @($State.Failures | Where-Object { [string]$_.Endpoint -ne $endpoint })
    $State.Current = [pscustomobject]@{
        Endpoint = $endpoint
        HostName = [string]$Server.HostName
        IP = [string]$Server.IP
        Port = [int]$Server.Port
        CountryShort = [string]$Server.CountryShort
        ConnectedAt = (Get-Date).ToString('o')
    }
    $State.KnownGood = @($State.KnownGood | Where-Object { [string]$_.Endpoint -ne $endpoint })
    $State.KnownGood += [pscustomobject]@{
        Endpoint = $endpoint
        HostName = [string]$Server.HostName
        IP = [string]$Server.IP
        Port = [int]$Server.Port
        CountryShort = [string]$Server.CountryShort
        VerifiedAt = (Get-Date).ToString('o')
    }
    Write-FailoverState -State $State
}

function Add-KnownGoodCandidates {
    param([object[]]$Servers, $State)
    $result = @($Servers)
    $present = @{}
    foreach ($server in $result) { $present[[string]$server.Endpoint] = $true }
    $cutoff = [datetimeoffset]::Now.AddHours(-$KnownGoodLifetimeHours)
    foreach ($known in @($State.KnownGood)) {
        try { $verifiedAt = [datetimeoffset]::Parse([string]$known.VerifiedAt) } catch { continue }
        $country = [string]$known.CountryShort
        if ($verifiedAt -le $cutoff -or -not $countryPriority.Contains($country) -or $present.ContainsKey([string]$known.Endpoint)) { continue }
        $result += [pscustomobject]@{
            HostName = [string]$known.HostName
            IP = [string]$known.IP
            Port = [int]$known.Port
            Endpoint = [string]$known.Endpoint
            CountryLong = $country
            CountryShort = $country
            Priority = [int]$countryPriority[$country]
            Score = [int64]0
            Ping = [int]9999
            SpeedMbps = [double]0
            Sessions = [int]0
            Source = 'RecentKnownGood'
            SourcePriority = 0
            VerifiedAt = $known.VerifiedAt
        }
        $present[[string]$known.Endpoint] = $true
    }
    $result
}

function Select-RelayCandidates {
    param([object[]]$Servers, $State)
    $cooling = @{}
    $cutoff = [datetimeoffset]::Now.AddMinutes(-$FailureCooldownMinutes)
    foreach ($failure in @($State.Failures)) {
        try {
            $failedAt = [datetimeoffset]::Parse([string]$failure.FailedAt)
            if ($failedAt -gt $cutoff) { $cooling[[string]$failure.Endpoint] = $failedAt.AddMinutes($FailureCooldownMinutes) }
        } catch {}
    }

    $eligible = @($Servers | Where-Object { -not $cooling.ContainsKey([string]$_.Endpoint) })
    $live = @($eligible | Where-Object Source -eq 'Live' | Group-Object CountryShort | ForEach-Object {
        $_.Group |
            Sort-Object @{Expression = { if ($_.Sessions -gt 0) { 1 } else { 0 } }; Descending = $true}, @{Expression = 'Score'; Descending = $true} |
            Select-Object -First $MaxCandidatesPerCountry
    })
    # Keep a small verified fallback pool even when a stale live snapshot has
    # enough higher-score entries to fill the per-country limit.
    $knownGood = @($eligible | Where-Object Source -eq 'RecentKnownGood' | Sort-Object VerifiedAt -Descending | Select-Object -First 3)
    $selected = @(@($live) + @($knownGood) | Sort-Object @{Expression = 'Priority'; Descending = $true}, @{Expression = 'SourcePriority'; Descending = $true}, @{Expression = { if ($_.Sessions -gt 0) { 1 } else { 0 } }; Descending = $true}, @{Expression = 'Score'; Descending = $true} | Select-Object -First $MaxCandidatesTotal)

    [pscustomobject]@{
        Candidates = $selected
        Cooling = $cooling
    }
}

function Ensure-VpnAccount {
    param($Server)
    $accountList = @(& $VpnCmdPath /CLIENT localhost /CMD AccountList 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'vpncmd failed with exit code while listing accounts.' }
    $exists = ($accountList | Out-String) -match [regex]::Escape($AccountName)
    if ($exists) {
        Invoke-VpnCmd -Arguments @('AccountSet', $AccountName, "/SERVER:$($Server.IP):$($Server.Port)", '/HUB:VPNGATE') | Out-Null
    } else {
        Invoke-VpnCmd -Arguments @('AccountCreate', $AccountName, "/SERVER:$($Server.IP):$($Server.Port)", '/HUB:VPNGATE', '/USERNAME:vpn', "/NICNAME:$NicName") | Out-Null
        Invoke-VpnCmd -Arguments @('AccountPasswordSet', $AccountName, '/PASSWORD:vpn', '/TYPE:standard') | Out-Null
    }

    # The connector, not SoftEther's internal retry loop, owns relay rotation.
    # NUM:0 prevents the client from retrying a dead endpoint forever.
    # SoftEther 4.44 enforces a minimum retry interval of five seconds even
    # when NUM is zero.
    Invoke-VpnCmd -Arguments @('AccountRetrySet', $AccountName, '/NUM:0', '/INTERVAL:5') | Out-Null
    Invoke-VpnCmd -Arguments @('AccountStatusHide', $AccountName) | Out-Null
}

function Protect-PhysicalDefaultRoute {
    param($Vpn)
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $Vpn.InterfaceIndex -ErrorAction SilentlyContinue |
        Set-NetRoute -RouteMetric 9000 -PolicyStore ActiveStore

    $rankedDefaults = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Where-Object State -eq 'Alive' |
        ForEach-Object {
            $route = $_
            $ipInterface = Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1
            [pscustomobject]@{
                Route = $route
                EffectiveMetric = [int]$route.RouteMetric + [int]$ipInterface.InterfaceMetric
            }
        } | Sort-Object EffectiveMetric)
    if (-not $rankedDefaults) { throw 'No live IPv4 default route remains.' }
    $selected = $rankedDefaults[0].Route
    if ([int]$selected.InterfaceIndex -eq [int]$Vpn.InterfaceIndex) {
        throw 'VPN remained the selected default route after metric adjustment.'
    }
    $selected
}

function Show-Status {
    $connected = Get-ConnectedVpnInfo
    $state = Read-FailoverState
    [pscustomobject]@{
        Account = $AccountName
        Connected = [bool]$connected
        Relay = if ($state.Current) { $state.Current.Endpoint } else { $null }
        Country = if ($state.Current) { $state.Current.CountryShort } else { $null }
        VpnInterface = if ($connected) { $connected.InterfaceAlias } else { $InterfaceAlias }
        VpnIPv4 = if ($connected) { $connected.IPv4 } else { $null }
        VpnGateway = if ($connected) { $connected.Gateway } else { $null }
        CoolingEndpoints = @($state.Failures).Count
        RecentKnownGood = @($state.KnownGood).Count
    } | Format-List

    if ($state.Failures) {
        Write-Host 'Recently failed relays:'
        $state.Failures | Sort-Object FailedAt -Descending | Select-Object Endpoint, CountryShort, FailedAt, Reason | Format-Table -AutoSize
    }
}

if ($Action -eq 'Status') {
    Show-Status
    exit 0
}

if ($Action -eq 'Disconnect') {
    Disconnect-VpnAccount
    $state = Read-FailoverState
    $state.Current = $null
    Write-FailoverState -State $state
    Write-Host "Disconnected $AccountName."
    exit 0
}

$state = Read-FailoverState
$existingConnection = Get-ConnectedVpnInfo
if ($Action -eq 'RememberCurrent') {
    if (-not $existingConnection) { throw 'No verified SoftEther session is connected.' }
    if (-not $RelayCountry -or -not $countryPriority.Contains($RelayCountry)) { throw 'RelayCountry must be a configured CIS country code such as RU or UA.' }
    $endpoint = Get-ConfiguredAccountEndpoint
    $remembered = [pscustomobject]@{
        HostName = $endpoint.HostName
        IP = $endpoint.IP
        Port = $endpoint.Port
        Endpoint = $endpoint.Endpoint
        CountryShort = $RelayCountry
    }
    Set-EndpointSuccess -State $state -Server $remembered
    $selectedDefault = Protect-PhysicalDefaultRoute -Vpn $existingConnection
    Write-Host "Remembered verified $RelayCountry relay $($endpoint.Endpoint); default remains $($selectedDefault.InterfaceAlias)."
    exit 0
}
if ($Action -eq 'Connect' -and $existingConnection) {
    $selectedDefault = Protect-PhysicalDefaultRoute -Vpn $existingConnection
    Write-Host "CIS relay is already connected; default remains $($selectedDefault.InterfaceAlias)."
    exit 0
}

# If a relay that previously succeeded is now offline, quarantine it before
# refreshing the list. This is what makes failover move to the next endpoint
# instead of selecting the same high-score dead relay again.
if ($state.Current -and -not $existingConnection) {
    Set-EndpointFailure -State $state -Server $state.Current -Reason 'Previous VPN session is no longer established.'
}

$liveServers = @()
try {
    $liveServers = @(Get-CisServers)
} catch {
    Write-Warning "VPN Gate live list refresh failed; trying only recently verified local relays: $($_.Exception.Message)"
}
$servers = @(Add-KnownGoodCandidates -Servers $liveServers -State $state)
if (-not $servers) { throw 'No live or recently verified TCP-capable CIS SoftEther relay is available.' }
$selection = Select-RelayCandidates -Servers $servers -State $state
$candidates = @($selection.Candidates)

if ($Action -eq 'Candidates') {
    $candidates | Format-Table CountryShort, Source, HostName, IP, Port, Ping, SpeedMbps, Sessions, Score -AutoSize
    if (-not $candidates) { Write-Host 'All currently listed TCP-capable CIS relays are cooling down after recent failures.' }
    exit 0
}

if (-not $candidates) {
    $nextRetry = @($selection.Cooling.Values | Sort-Object | Select-Object -First 1)
    if ($nextRetry) { throw "All currently listed CIS relays recently failed; next retry after $($nextRetry[0].ToString('o'))." }
    throw 'No eligible CIS relay is available.'
}

Write-Host 'Fresh TCP-capable CIS VPN Gate candidates:'
$candidates | Format-Table CountryShort, Source, HostName, IP, Port, Ping, SpeedMbps, Sessions, Score -AutoSize

$attempted = 0
foreach ($server in $candidates) {
    $attempted++
    Write-Host "Trying $($server.CountryShort) $($server.Endpoint)..."
    if (-not (Test-TcpQuick -IPAddress $server.IP -Port $server.Port -TimeoutMs $TcpProbeTimeoutMilliseconds)) {
        Set-EndpointFailure -State $state -Server $server -Reason 'Published TCP endpoint did not accept a connection.'
        Write-Warning "Skipped unreachable relay $($server.Endpoint)."
        continue
    }

    Disconnect-VpnAccount
    try {
        Ensure-VpnAccount -Server $server
        Invoke-VpnCmd -Arguments @('AccountConnect', $AccountName) | Out-Null
    } catch {
        Set-EndpointFailure -State $state -Server $server -Reason $_.Exception.Message
        Write-Warning "SoftEther could not start $($server.Endpoint): $($_.Exception.Message)"
        continue
    }

    $vpn = $null
    $deadline = (Get-Date).AddSeconds($ConnectTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $vpn = Get-ConnectedVpnInfo
        if ($vpn) { break }
    } while ((Get-Date) -lt $deadline)

    if (-not $vpn) {
        Disconnect-VpnAccount
        Set-EndpointFailure -State $state -Server $server -Reason 'SoftEther handshake or IPv4 lease timed out.'
        Write-Warning "Relay $($server.Endpoint) failed the VPN session check; moving to the next candidate."
        continue
    }

    try {
        $selectedDefault = Protect-PhysicalDefaultRoute -Vpn $vpn
    } catch {
        Disconnect-VpnAccount
        Set-EndpointFailure -State $state -Server $server -Reason $_.Exception.Message
        throw
    }

    Set-EndpointSuccess -State $state -Server $server
    Write-Host "Connected CIS relay $($server.CountryShort) $($server.Endpoint)."
    Write-Host "VPN interface $($vpn.InterfaceIndex) $($vpn.InterfaceAlias), IPv4 $($vpn.IPv4), gateway $($vpn.Gateway)."
    Write-Host "Default route remains $($selectedDefault.InterfaceAlias) via $($selectedDefault.NextHop)."
    exit 0
}

Disconnect-VpnAccount
throw "Tried $attempted fresh CIS relay candidate(s); none established a verified SoftEther session and IPv4 lease. Failed endpoints are cooling down before reuse."
