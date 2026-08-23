param(
    [ValidateSet('Connect', 'Status', 'Disconnect', 'Candidates', 'RememberCurrent')]
    [string]$Action = 'Connect',
    [string]$AccountName = 'Tarkov-CIS-PlayOnly',
    [string]$VpnCmdPath = 'C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe',
    [string]$InterfaceAlias = 'VPN - VPN Client',
    [string]$NicName = 'VPN',
    [int]$ConnectTimeoutSeconds = 18,
    [int]$FailoverTimeoutSeconds = 180,
    [int]$DisconnectWaitSeconds = 15,
    [int]$ResourceBusyRetryCount = 2,
    [int]$MaxCandidatesPerCountry = 10,
    [int]$MaxCandidatesTotal = 20,
    [int]$TcpProbeTimeoutMilliseconds = 1500,
    [int]$DiscoveryTimeoutSeconds = 15,
    [int]$FailureCooldownMinutes = 15,
    [int]$CoolingFallbackMinutes = 2,
    [int]$CoolingFallbackCandidates = 3,
    [int]$KnownGoodLifetimeHours = 48,
    [string]$NativeCatalogPath,
    [int]$NativeCatalogMaxAgeHours = 24,
    [string]$RelayCountry,
    [string]$StatePath,
    [switch]$TestMode
)

$ErrorActionPreference = 'Stop'
if (-not $StatePath) { $StatePath = Join-Path $PSScriptRoot 'Connect-VpnGateCis.state.json' }
if (-not $TestMode -and -not (Test-Path -LiteralPath $VpnCmdPath)) { throw "SoftEther vpncmd not found: $VpnCmdPath" }
$nativeCatalogReaderPath = Join-Path $PSScriptRoot 'VpnGateNativeCatalog.ps1'
if (-not (Test-Path -LiteralPath $nativeCatalogReaderPath)) { throw "VPN Gate native catalog reader was not found: $nativeCatalogReaderPath" }
. $nativeCatalogReaderPath
if (-not $NativeCatalogPath) { $NativeCatalogPath = Join-Path (Split-Path -Parent $VpnCmdPath) 'VPNGate.dat' }

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
        $exception = [InvalidOperationException]::new("vpncmd failed with exit code ${exitCode}: $($Arguments[0])")
        $exception.Data['ExitCode'] = [int]$exitCode
        $exception.Data['VpnCommand'] = [string]$Arguments[0]
        throw $exception
    }
    $output
}

function Test-SoftEtherSession {
    $output = @(& $VpnCmdPath /CSV /CLIENT localhost /CMD AccountStatusGet $AccountName 2>$null)
    if ($LASTEXITCODE -ne 0) { return $false }
    Test-EstablishedSoftEtherSessionOutput -Output $output
}

function Test-EstablishedSoftEtherSessionOutput {
    param([object[]]$Output)
    $text = (@($Output) | ForEach-Object { [string]$_ }) -join "`n"
    # SID is locale-independent and is emitted for an established SoftEther
    # session. Human-readable status text changes with the client language.
    $text -match '(?im)\bSID-[A-Za-z0-9-]+\b'
}

function Get-VpnLease {
    $adapter = Get-NetAdapter -InterfaceAlias $InterfaceAlias -ErrorAction SilentlyContinue
    if (-not $adapter -or $adapter.Status -ne 'Up') { return $null }
    try {
        $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $ipConfig) { return $null }
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

function Test-VpnAdapterReleased {
    $adapter = Get-NetAdapter -InterfaceAlias $InterfaceAlias -ErrorAction SilentlyContinue
    if (-not $adapter) { return $true }
    try {
        $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex -ErrorAction Stop
    } catch {
        return $true
    }
    if (-not $ipConfig) { return $true }
    $hasAddress = @($ipConfig.IPv4Address | Where-Object {
        $_.IPAddress -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '0.0.0.0'
    }).Count -gt 0
    $hasGateway = @($ipConfig.IPv4DefaultGateway | Where-Object NextHop).Count -gt 0
    -not $hasAddress -and -not $hasGateway
}

function Wait-SoftEtherOffline {
    param([int]$TimeoutSeconds = $DisconnectWaitSeconds)
    $deadline = (Get-Date).AddSeconds([math]::Max(1, $TimeoutSeconds))
    do {
        $sessionOnline = Test-SoftEtherSession
        $adapterReleased = Test-VpnAdapterReleased
        if (-not $sessionOnline -and $adapterReleased) { return $true }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    $false
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
    param([int]$TimeoutSeconds = $DisconnectWaitSeconds)
    & $VpnCmdPath /CLIENT localhost /CMD AccountDisconnect $AccountName 2>$null | Out-Null
    $disconnectExitCode = $LASTEXITCODE
    if (-not (Wait-SoftEtherOffline -TimeoutSeconds $TimeoutSeconds)) {
        throw "SoftEther account '$AccountName' did not fully release its session and VPN adapter within $TimeoutSeconds second(s)."
    }
    if ($disconnectExitCode -ne 0 -and (Test-SoftEtherSession)) {
        throw "vpncmd AccountDisconnect failed with exit code $disconnectExitCode."
    }
}

function Test-SoftEtherResourceBusyError {
    param([Parameter(Mandatory = $true)]$ErrorRecord)
    $exception = $ErrorRecord.Exception
    if ($exception -and $exception.Data -and $exception.Data.Contains('ExitCode')) {
        try { if ([int]$exception.Data['ExitCode'] -eq 43) { return $true } } catch {}
    }
    [string]$message = if ($exception) { $exception.Message } else { [string]$ErrorRecord }
    $message -match '(?i)(?:exit\s*code|error\s*code|返回码|错误码)\s*[:=]?\s*43(?:\D|$)'
}

function Enter-ConnectorMutex {
    param([int]$TimeoutSeconds = 0)
    $mutex = $null
    $mutexName = 'Global\TarkovCisVpnGateConnector'
    try {
        [bool]$createdNew = $false
        $mutex = New-Object System.Threading.Mutex($false, $mutexName, [ref]$createdNew)
    } catch {
        # Some locked-down Windows sessions do not allow creating a Global
        # kernel object. Local still serializes the GUI and scheduled task
        # when they run in the same interactive account.
        $mutexName = 'Local\TarkovCisVpnGateConnector'
        [bool]$createdNew = $false
        $mutex = New-Object System.Threading.Mutex($false, $mutexName, [ref]$createdNew)
    }

    try {
        $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds([math]::Max(0, $TimeoutSeconds)))
    } catch [Threading.AbandonedMutexException] {
        $acquired = $true
    }
    if (-not $acquired) {
        $mutex.Dispose()
        throw "Another Tarkov CIS VPN operation is already running (mutex: $mutexName)."
    }
    $mutex
}

function Exit-ConnectorMutex {
    param($Mutex)
    if (-not $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch {}
    $Mutex.Dispose()
}

function Get-FailoverSecondsRemaining {
    if (-not $script:FailoverDeadline) { return [double]::PositiveInfinity }
    ([datetime]$script:FailoverDeadline - (Get-Date)).TotalSeconds
}

function Assert-FailoverTimeRemaining {
    $remaining = Get-FailoverSecondsRemaining
    if ($remaining -le 0) {
        throw "CIS relay failover exceeded the $FailoverTimeoutSeconds-second total deadline."
    }
    $remaining
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

function Get-CisServersFromHttpsApi {
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
                Source = 'HttpsApi'
                SourcePriority = 2
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

function Get-CisServersFromNativeCatalog {
    $catalog = Read-VpnGateNativeCatalog -Path $NativeCatalogPath -MaxAgeHours $NativeCatalogMaxAgeHours
    $servers = @($catalog.Rows | Where-Object CountryShort -in @($countryPriority.Keys) | ForEach-Object {
        $row = $_
        $parsedIp = $null
        $country = ([string]$row.CountryShort).ToUpperInvariant()
        if ([Net.IPAddress]::TryParse([string]$row.IP, [ref]$parsedIp) -and
            $parsedIp.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
            $ports = @([regex]::Matches([string]$row.SslPorts, '(?<!\d)(?<Port>\d{1,5})(?!\d)') | ForEach-Object {
                $port = [int]$_.Groups['Port'].Value
                if ($port -ge 1 -and $port -le 65535) { $port }
            } | Sort-Object -Unique)
            foreach ($port in $ports) {
                $hostName = if ($row.Fqdn) { [string]$row.Fqdn } else { [string]$row.IP }
                $ping = try { [int64]$row.PingToJapan } catch { [int64]9999 }
                if ($ping -lt 0 -or $ping -gt 60000) { $ping = 9999 }
                $speed = try { [int64]$row.SpeedToJapan } catch { [int64]0 }
                $score = try { [int64]$row.Score } catch { [int64]0 }
                $sessions = try { [int]$row.NumSessions } catch { [int]0 }
                [pscustomobject]@{
                    HostName = $hostName
                    IP = [string]$row.IP
                    Port = [int]$port
                    Endpoint = ('{0}:{1}' -f $row.IP, $port)
                    CountryLong = [string]$row.CountryLong
                    CountryShort = $country
                    Priority = [int]$countryPriority[$country]
                    Score = $score
                    Ping = [int]$ping
                    SpeedMbps = [math]::Round($speed / 1000000, 1)
                    Sessions = $sessions
                    Source = 'NativeCatalog'
                    SourcePriority = 3
                    CatalogTimestampUtc = $catalog.TimestampUtc
                }
            }
        }
    })

    @($servers | Group-Object Endpoint | ForEach-Object {
        $_.Group | Sort-Object Score -Descending | Select-Object -First 1
    })
}

function Get-CisServers {
    $servers = @()
    if (Test-Path -LiteralPath $NativeCatalogPath -PathType Leaf) {
        try {
            $servers += @(Get-CisServersFromNativeCatalog)
        } catch {
            Write-Warning "SoftEther native VPN Gate catalog could not be used: $($_.Exception.Message)"
        }
    }

    try {
        # Keep the official HTTPS/OpenVPN feed as a fresh secondary source.
        # It can recover when the local plugin cache is unavailable or stale,
        # but it does not expose every SoftEther SSL listener.
        $servers += @(Get-CisServersFromHttpsApi)
    } catch {
        if (-not $servers) { throw }
        Write-Warning "VPN Gate HTTPS fallback refresh failed; using the official plugin's local native catalog: $($_.Exception.Message)"
    }

    @($servers | Group-Object Endpoint | ForEach-Object {
        $_.Group |
            Sort-Object @{Expression = 'SourcePriority'; Descending = $true}, @{Expression = 'Score'; Descending = $true} |
            Select-Object -First 1
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
    $json = $State | ConvertTo-Json -Depth 6
    $tempPath = '{0}.{1}.tmp' -f $StatePath, ([guid]::NewGuid().ToString('N'))
    $backupPath = '{0}.{1}.bak' -f $StatePath, ([guid]::NewGuid().ToString('N'))
    try {
        # Write beside the destination, then replace it in one filesystem
        # operation. This prevents a reader from observing half-written JSON.
        [IO.File]::WriteAllText(
            $tempPath,
            $json + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
        if ([IO.File]::Exists($StatePath)) {
            [IO.File]::Replace($tempPath, $StatePath, $backupPath, $true)
        } else {
            [IO.File]::Move($tempPath, $StatePath)
        }
    } finally {
        if ([IO.File]::Exists($tempPath)) { [IO.File]::Delete($tempPath) }
        if ([IO.File]::Exists($backupPath)) { [IO.File]::Delete($backupPath) }
    }
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

function Get-RelayIdentity {
    param([Parameter(Mandatory = $true)]$Server)
    $ip = [string]$Server.IP
    if ($ip) {
        $parsed = $null
        if ([Net.IPAddress]::TryParse($ip, [ref]$parsed)) {
            return ('ip:{0}' -f $parsed.ToString())
        }
    }
    $hostName = [string]$Server.HostName
    if ($hostName) { return ('host:{0}' -f $hostName.Trim().ToLowerInvariant()) }
    'endpoint:{0}' -f ([string]$Server.Endpoint).Trim().ToLowerInvariant()
}

function Sort-RelayEndpoints {
    param([object[]]$Servers)
    @($Servers | Sort-Object @{Expression = 'SourcePriority'; Descending = $true}, @{Expression = { if ($_.Sessions -gt 0) { 1 } else { 0 } }; Descending = $true}, @{Expression = 'Score'; Descending = $true}, @{Expression = 'Ping'; Descending = $false})
}

function Select-DiverseCountryCandidates {
    param([object[]]$Servers, [int]$Limit)
    $ordered = @(Sort-RelayEndpoints -Servers $Servers)
    if (-not $ordered) { return @() }

    # First pass takes one endpoint per relay/IP. The second pass is made only
    # of alternate ports/hostnames for those relays, so a port fallback remains
    # available without crowding out independent volunteers.
    $primary = @()
    $fallback = @()
    $seenRelay = @{}
    foreach ($server in $ordered) {
        $relayKey = Get-RelayIdentity -Server $server
        if (-not $seenRelay.ContainsKey($relayKey)) {
            $seenRelay[$relayKey] = $true
            $primary += $server
        } else {
            $fallback += $server
        }
    }
    @($primary + $fallback | Select-Object -First $Limit)
}

function Select-CountryDiverseCandidates {
    param(
        [object[]]$Servers,
        [int]$PerCountryLimit
    )

    if ($PerCountryLimit -le 0) { return @() }
    $groups = @($Servers | Group-Object CountryShort | ForEach-Object {
        [pscustomobject]@{
            Country = [string]$_.Name
            Candidates = @(Select-DiverseCountryCandidates -Servers @($_.Group) -Limit $PerCountryLimit)
        }
    } | Sort-Object @{Expression = { [int]$countryPriority[[string]$_.Country] }; Descending = $true},
        @{Expression = 'Country'; Descending = $false})

    # Round-robin the best candidate from each CIS country before taking a
    # second candidate from any one country.  A large RU listing must not starve
    # a currently healthy UA/KZ/BY relay that is visible in the same snapshot.
    $result = @()
    for ($round = 0; $round -lt $PerCountryLimit; $round++) {
        foreach ($group in $groups) {
            $countryCandidates = @($group.Candidates)
            if ($round -lt $countryCandidates.Count) { $result += $countryCandidates[$round] }
        }
    }
    @($result)
}

function Select-RelayCandidates {
    param(
        [object[]]$Servers,
        $State,
        [int]$PerCountryLimit = $MaxCandidatesPerCountry,
        [int]$TotalLimit = $MaxCandidatesTotal,
        [int]$CooldownMinutes = $FailureCooldownMinutes
    )
    $cooling = @{}
    $cutoff = [datetimeoffset]::Now.AddMinutes(-$CooldownMinutes)
    foreach ($failure in @($State.Failures)) {
        try {
            $failedAt = [datetimeoffset]::Parse([string]$failure.FailedAt)
            if ($failedAt -gt $cutoff) { $cooling[[string]$failure.Endpoint] = $failedAt.AddMinutes($FailureCooldownMinutes) }
        } catch {}
    }

    $eligible = @($Servers | Where-Object { -not $cooling.ContainsKey([string]$_.Endpoint) })
    $live = @($eligible | Where-Object Source -ne 'RecentKnownGood')
    # Keep a small verified fallback pool per country even when a stale live
    # snapshot omits a country that still has a recently verified relay.
    $knownGood = @($eligible | Where-Object Source -eq 'RecentKnownGood' | Group-Object CountryShort | ForEach-Object {
        $_.Group | Sort-Object VerifiedAt -Descending | Select-Object -First 3
    })
    # Combine live and known-good entries before the country-diversity pass.
    # Otherwise a known-good UA relay can still be placed after every RU live
    # entry and never be reached within the failover deadline.
    $candidatePool = @($live) + @($knownGood)
    $ordered = @(Select-CountryDiverseCandidates -Servers $candidatePool -PerCountryLimit $PerCountryLimit)
    # $ordered is already country-diverse and score-ordered within each country.
    # Do not apply a global country-priority sort here or the diversity pass
    # would be undone before the total candidate limit is applied.
    $selected = @()
    $selectedRelay = @{}
    $fallbackEndpoints = @()
    foreach ($server in $ordered) {
        $relayKey = Get-RelayIdentity -Server $server
        if (-not $selectedRelay.ContainsKey($relayKey)) {
            $selectedRelay[$relayKey] = $true
            $selected += $server
        } else {
            $fallbackEndpoints += $server
        }
        if ($selected.Count -ge $TotalLimit) { break }
    }
    if ($selected.Count -lt $TotalLimit) {
        foreach ($server in $fallbackEndpoints) {
            $selected += $server
            if ($selected.Count -ge $TotalLimit) { break }
        }
    }

    [pscustomobject]@{
        Candidates = $selected
        Cooling = $cooling
    }
}

function Select-CoolingFallbackCandidates {
    param(
        [object[]]$Servers,
        $State,
        [int]$Limit = $CoolingFallbackCandidates,
        [int]$MinimumAgeMinutes = $CoolingFallbackMinutes
    )

    if ($Limit -le 0) { return @() }
    $now = [datetimeoffset]::Now
    $minimumAge = $now.AddMinutes(-[math]::Max(0, $MinimumAgeMinutes))
    $failures = @{}
    foreach ($failure in @($State.Failures)) {
        $endpoint = [string]$failure.Endpoint
        if (-not $endpoint) { continue }
        try {
            $failedAt = [datetimeoffset]::Parse([string]$failure.FailedAt)
            if (-not $failures.ContainsKey($endpoint) -or $failedAt -gt $failures[$endpoint]) {
                $failures[$endpoint] = $failedAt
            }
        } catch {}
    }

    # If the complete live pool is quarantined, retry only the oldest failures
    # after a short grace period.  This breaks the apparent 15-minute deadlock
    # without immediately hammering every endpoint on every keeper poll.
    $ranked = @($Servers | ForEach-Object {
        $endpoint = [string]$_.Endpoint
        if ($failures.ContainsKey($endpoint) -and $failures[$endpoint] -le $minimumAge) {
            [pscustomobject]@{
                Server = $_
                FailedAt = $failures[$endpoint]
            }
        }
    } | Sort-Object @{Expression = 'FailedAt'; Descending = $false},
        @{Expression = { [int]$_.Server.Priority }; Descending = $true},
        @{Expression = { [int]$_.Server.SourcePriority }; Descending = $true},
        @{Expression = { [int64]$_.Server.Score }; Descending = $true},
        @{Expression = { [int]$_.Server.Ping }; Descending = $false})

    $selected = @()
    $fallback = @()
    $seenRelay = @{}
    foreach ($entry in $ranked) {
        $relayKey = Get-RelayIdentity -Server $entry.Server
        if (-not $seenRelay.ContainsKey($relayKey)) {
            $seenRelay[$relayKey] = $true
            $selected += $entry.Server
        } else {
            $fallback += $entry.Server
        }
        if ($selected.Count -ge $Limit) { break }
    }
    if ($selected.Count -lt $Limit) {
        foreach ($server in $fallback) {
            $selected += $server
            if ($selected.Count -ge $Limit) { break }
        }
    }
    @($selected)
}

function Get-CoolingFallbackRetryAt {
    param(
        [object[]]$Servers,
        $State,
        [int]$MinimumAgeMinutes = $CoolingFallbackMinutes
    )

    $present = @{}
    foreach ($server in @($Servers)) {
        $endpoint = [string]$server.Endpoint
        if ($endpoint) { $present[$endpoint] = $true }
    }
    $next = $null
    foreach ($failure in @($State.Failures)) {
        if ($present.Count -gt 0 -and -not $present.ContainsKey([string]$failure.Endpoint)) { continue }
        try {
            $retryAt = [datetimeoffset]::Parse([string]$failure.FailedAt).AddMinutes([math]::Max(0, $MinimumAgeMinutes))
            if ($null -eq $next -or $retryAt -lt $next) { $next = $retryAt }
        } catch {}
    }
    $next
}

function Format-RelayCandidateTable {
    param([object[]]$Candidates)
    # Convert the complete formatting stream to text here. The background
    # keeper consumes records one by one so raw FormatStartData/FormatEntryData
    # objects cannot be rendered safely there in isolation.
    (($Candidates | Format-Table CountryShort, Source, HostName, IP, Port, Ping, SpeedMbps, Sessions, Score -AutoSize | Out-String -Width 240).TrimEnd())
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
        Set-NetRoute -RouteMetric 9000 -PolicyStore ActiveStore -ErrorAction SilentlyContinue

    $rankedDefaults = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Where-Object State -eq 'Alive' |
        ForEach-Object {
            $route = $_
            $ipInterface = $null
            try {
                $ipInterface = Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction Stop | Select-Object -First 1
            } catch {
                # A virtual interface may vanish while the route snapshot is
                # being enumerated. Skip that stale route and rank the live
                # physical defaults that remain.
            }
            if ($ipInterface) {
                [pscustomobject]@{
                    Route = $route
                    EffectiveMetric = [int]$route.RouteMetric + [int]$ipInterface.InterfaceMetric
                }
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
    $coolingCutoff = [datetimeoffset]::Now.AddMinutes(-$FailureCooldownMinutes)
    $coolingCount = @($state.Failures | Where-Object {
        try { [datetimeoffset]::Parse([string]$_.FailedAt) -gt $coolingCutoff } catch { $false }
    }).Count
    [pscustomobject]@{
        Account = $AccountName
        Connected = [bool]$connected
        Relay = if ($state.Current) { $state.Current.Endpoint } else { $null }
        Country = if ($state.Current) { $state.Current.CountryShort } else { $null }
        VpnInterface = if ($connected) { $connected.InterfaceAlias } else { $InterfaceAlias }
        VpnIPv4 = if ($connected) { $connected.IPv4 } else { $null }
        VpnGateway = if ($connected) { $connected.Gateway } else { $null }
        CoolingEndpoints = $coolingCount
        RecentKnownGood = @($state.KnownGood).Count
    } | Format-List

    if ($state.Failures) {
        Write-Host 'Recently failed relays:'
        $state.Failures | Sort-Object FailedAt -Descending | Select-Object Endpoint, CountryShort, FailedAt, Reason | Format-Table -AutoSize
    }
}

if ($TestMode) { return }

if ($Action -eq 'Status') {
    Show-Status
    return
}

$connectorMutex = $null
if ($Action -ne 'Candidates') {
    $connectorMutex = Enter-ConnectorMutex
}

try {
    if ($Action -eq 'Connect') {
        $script:FailoverDeadline = (Get-Date).AddSeconds([math]::Max(1, $FailoverTimeoutSeconds))
    }

    if ($Action -eq 'Disconnect') {
        Disconnect-VpnAccount
        $state = Read-FailoverState
        $state.Current = $null
        Write-FailoverState -State $state
        Write-Host "Disconnected $AccountName."
        return
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
        return
    }
    if ($Action -eq 'Connect' -and $existingConnection) {
        $selectedDefault = Protect-PhysicalDefaultRoute -Vpn $existingConnection
        Write-Host "CIS relay is already connected; default remains $($selectedDefault.InterfaceAlias)."
        return
    }

    # Candidates is deliberately read-only: an offline Current entry is not
    # quarantined while a user is merely inspecting the list.
    if ($Action -ne 'Candidates' -and $state.Current -and -not $existingConnection) {
        Set-EndpointFailure -State $state -Server $state.Current -Reason 'Previous VPN session is no longer established.'
    }

    $liveServers = @()
    try {
        $liveServers = @(Get-CisServers)
    } catch {
        Write-Warning "VPN Gate candidate discovery failed; trying only recently verified relays: $($_.Exception.Message)"
    }
    $servers = @(Add-KnownGoodCandidates -Servers $liveServers -State $state)
    if (-not $servers) { throw 'No live or recently verified TCP-capable CIS SoftEther relay is available.' }
    $selection = Select-RelayCandidates -Servers $servers -State $state
    $candidates = @($selection.Candidates)

    if ($Action -eq 'Candidates') {
        Write-Output (Format-RelayCandidateTable -Candidates $candidates)
        if (-not $candidates) { Write-Host 'All currently listed TCP-capable CIS relays are cooling down after recent failures.' }
        return
    }

    if (-not $candidates) {
        $candidates = @(Select-CoolingFallbackCandidates -Servers $servers -State $state)
        if ($candidates) {
            Write-Warning "All current CIS relays are in normal cooldown; retrying the oldest $($candidates.Count) relay candidate(s) after the controlled fallback grace period."
        }
    }

    if (-not $candidates) {
        $nextRetry = Get-CoolingFallbackRetryAt -Servers $servers -State $state
        if ($nextRetry) { throw "All currently listed CIS relays recently failed; controlled fallback retry after $($nextRetry.ToString('o'))." }
        throw 'No eligible CIS relay is available.'
    }

    Write-Host "TCP-capable CIS VPN Gate candidates (total deadline: $FailoverTimeoutSeconds second(s)):"
    Write-Output (Format-RelayCandidateTable -Candidates $candidates)

    $attempted = 0
    foreach ($server in $candidates) {
        [void](Assert-FailoverTimeRemaining)
        $attempted++
        Write-Host "Trying $($server.CountryShort) $($server.Endpoint)..."
        $remaining = Get-FailoverSecondsRemaining
        $probeTimeout = [int][math]::Max(1, [math]::Min($TcpProbeTimeoutMilliseconds, [math]::Ceiling($remaining * 1000)))
        if (-not (Test-TcpQuick -IPAddress $server.IP -Port $server.Port -TimeoutMs $probeTimeout)) {
            Set-EndpointFailure -State $state -Server $server -Reason 'Published TCP endpoint did not accept a connection.'
            Write-Warning "Skipped unreachable relay $($server.Endpoint)."
            continue
        }

        [void](Assert-FailoverTimeRemaining)
        Disconnect-VpnAccount
        $resourceBusyAttempts = 0
        $accountConnectStarted = $false
        while (-not $accountConnectStarted) {
            [void](Assert-FailoverTimeRemaining)
            try {
                Ensure-VpnAccount -Server $server
                Invoke-VpnCmd -Arguments @('AccountConnect', $AccountName) | Out-Null
                $accountConnectStarted = $true
            } catch {
                if (Test-SoftEtherResourceBusyError -ErrorRecord $_) {
                    if ($resourceBusyAttempts -ge $ResourceBusyRetryCount) {
                        throw "SoftEther reported local resource busy (exit code 43) for $($server.Endpoint) after $ResourceBusyRetryCount retry/retries; no relay was cooled down."
                    }
                    $resourceBusyAttempts++
                    Write-Warning "SoftEther local session resources are still busy (exit code 43); waiting before retry $resourceBusyAttempts/$ResourceBusyRetryCount for $($server.Endpoint)."
                    $waitSeconds = [int][math]::Max(1, [math]::Min($DisconnectWaitSeconds, [math]::Ceiling((Get-FailoverSecondsRemaining))))
                    if (-not (Wait-SoftEtherOffline -TimeoutSeconds $waitSeconds)) {
                        throw "SoftEther local session resources did not release after exit code 43; no relay was cooled down."
                    }
                    Start-Sleep -Milliseconds 500
                    continue
                }
                Set-EndpointFailure -State $state -Server $server -Reason $_.Exception.Message
                Write-Warning "SoftEther could not start $($server.Endpoint): $($_.Exception.Message)"
                break
            }
        }
        if (-not $accountConnectStarted) { continue }

        [void](Assert-FailoverTimeRemaining)
        $vpn = $null
        $remaining = Get-FailoverSecondsRemaining
        $deadline = (Get-Date).AddSeconds([math]::Min($ConnectTimeoutSeconds, $remaining))
        do {
            Start-Sleep -Milliseconds 500
            $vpn = Get-ConnectedVpnInfo
            if ($vpn) { break }
        } while ((Get-Date) -lt $deadline -and (Get-FailoverSecondsRemaining) -gt 0)

        if (-not $vpn) {
            $remaining = Get-FailoverSecondsRemaining
            $cleanupWait = [int][math]::Max(1, [math]::Min($DisconnectWaitSeconds, [math]::Ceiling([math]::Max(1, $remaining))))
            Disconnect-VpnAccount -TimeoutSeconds $cleanupWait
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
        return
    }

    # Do not leave a half-connected account behind after exhausting the list.
    Disconnect-VpnAccount
    throw "Tried $attempted CIS relay candidate(s); none established a verified SoftEther session and IPv4 lease before the $FailoverTimeoutSeconds-second deadline. Failed remote endpoints are cooling down; local resource-busy failures were not recorded."
} finally {
    $script:FailoverDeadline = $null
    Exit-ConnectorMutex -Mutex $connectorMutex
}
