$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$keeperPath = Join-Path $root 'src\Tarkov-CisRouteKeeper.ps1'
$source = Get-Content -LiteralPath $keeperPath -Raw

function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

# Load only the pure/helper function definitions from the keeper.  The script's
# action dispatcher is intentionally not executed, so this test cannot stop a
# task, change routes, or initiate a VPN connection.
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($keeperPath, [ref]$tokens, [ref]$parseErrors)
Assert-Test (-not $parseErrors) 'Route keeper source contains PowerShell parse errors.'
$functionNames = @(
    'Write-Log',
    'Get-VpnInfo',
    'Get-StreamRecordType',
    'Get-StreamRecordLines',
    'Write-ConnectorStream',
    'Get-KeeperLoopSleepSeconds',
    'Resolve-Targets'
)
foreach ($functionName in $functionNames) {
    $functionAst = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
    }, $true) | Select-Object -First 1
    Assert-Test ($null -ne $functionAst) "Function $functionName was not found."
    Invoke-Expression $functionAst.Extent.Text
}

Assert-Test ($source -match '(?m)\*>&1') 'Keeper does not redirect all connector streams.'
Assert-Test ($source -match '\$logMaxBytes') 'Keeper does not configure a bounded log.'
Assert-Test ($source -match 'HostNames') 'Keeper does not preserve shared-IP hostname relationships.'
Assert-Test ($source -match 'FailedCycleRetrySeconds') 'Keeper does not define a short retry after an exhausted relay batch.'

# Resolve-Targets must collapse route entries by IP while retaining every host
# that points at the shared address.
function Get-ObservedHosts { @('gw-a.example', 'lobby.example', 'wsn.example') }
function Resolve-DnsName {
    param([string]$Name, [string]$Type, [switch]$DnsOnly, [string]$ErrorAction)
    switch ($Name) {
        'gw-a.example' { [pscustomobject]@{ Type = 'A'; IPAddress = '203.0.113.10' } }
        'lobby.example' { [pscustomobject]@{ Type = 'A'; IPAddress = '203.0.113.10' } }
        'wsn.example' { [pscustomobject]@{ Type = 'A'; IPAddress = '203.0.113.11' } }
    }
}
$config = [ordered]@{ TargetHosts = @(); GameLogRoots = @() }
$defaultHosts = @()
$effectiveRefreshSeconds = 30
$effectiveDisconnectedPollSeconds = 5
$logPath = Join-Path ([IO.Path]::GetTempPath()) ('route-keeper-test-{0}.log' -f [guid]::NewGuid().ToString('N'))
$logMaxBytes = 1024 * 1024
$targets = @(Resolve-Targets)
$sharedTarget = $targets | Where-Object IPAddress -eq '203.0.113.10'
Assert-Test (@($targets).Count -eq 2) 'Resolve-Targets did not keep one route entry per IP.'
Assert-Test ($null -ne $sharedTarget) 'Shared-IP target was not returned.'
Assert-Test (@($sharedTarget.HostNames).Count -eq 2) 'Shared-IP target lost one of its hostnames.'
Assert-Test (@($sharedTarget.HostNames) -contains 'gw-a.example') 'Shared-IP mapping lost gw-a.example.'
Assert-Test (@($sharedTarget.HostNames) -contains 'lobby.example') 'Shared-IP mapping lost lobby.example.'

# A stale APIPA lease must not be reported as a usable VPN address.
$vpnAlias = 'VPN test'
$script:testVpnMode = 'valid'
function Get-NetAdapter {
    param([string]$InterfaceAlias, [string]$ErrorAction)
    [pscustomobject]@{ Status = 'Up'; ifIndex = 42; InterfaceAlias = $InterfaceAlias }
}
function Get-NetIPConfiguration {
    param([int]$InterfaceIndex)
    if ($script:testVpnMode -eq 'apipa') {
        return [pscustomobject]@{
            IPv4Address = @([pscustomobject]@{ IPAddress = '169.254.10.20' })
            IPv4DefaultGateway = [pscustomobject]@{ NextHop = '169.254.10.1' }
        }
    }
    [pscustomobject]@{
        IPv4Address = @(
            [pscustomobject]@{ IPAddress = '169.254.10.20' },
            [pscustomobject]@{ IPAddress = '10.255.0.23' }
        )
        IPv4DefaultGateway = [pscustomobject]@{ NextHop = '10.255.0.1' }
    }
}
$vpn = Get-VpnInfo
Assert-Test ($vpn.IPv4 -eq '10.255.0.23') 'Get-VpnInfo did not skip the stale APIPA address.'
$script:testVpnMode = 'apipa'
Assert-Test ($null -eq (Get-VpnInfo)) 'Get-VpnInfo accepted an APIPA-only lease.'
Assert-Test ((Get-KeeperLoopSleepSeconds -VpnAvailable $true) -eq 30) 'Connected keeper polling interval changed unexpectedly.'
Assert-Test ((Get-KeeperLoopSleepSeconds -VpnAvailable $false) -eq 5) 'Disconnected keeper does not poll at the fast interval.'

# Every redirected stream type should be rendered with a recognizable label.
$streamCases = @(
    [pscustomobject]@{ Record = 'plain output'; Expected = 'output' },
    [pscustomobject]@{ Record = [System.Management.Automation.WarningRecord]::new('warning'); Expected = 'warning' },
    [pscustomobject]@{ Record = [System.Management.Automation.VerboseRecord]::new('verbose'); Expected = 'verbose' },
    [pscustomobject]@{ Record = [System.Management.Automation.DebugRecord]::new('debug'); Expected = 'debug' },
    [pscustomobject]@{ Record = [System.Management.Automation.InformationRecord]::new('information', 'test'); Expected = 'information' },
    [pscustomobject]@{ Record = [System.Management.Automation.ProgressRecord]::new(1, 'activity', 'progress'); Expected = 'progress' }
)
foreach ($case in $streamCases) {
    $rendered = Get-StreamRecordLines -Record $case.Record
    Assert-Test ($rendered.Stream -eq $case.Expected) "Stream type $($case.Expected) was not recognized."
}

# Rotation and line-level timestamps are tested using a private temporary log.
try {
    $logMaxBytes = 64
    Set-Content -LiteralPath $logPath -Value ('x' * 128) -Encoding UTF8
    Write-Log "first line`nsecond line"
    $rotatedPath = "$logPath.1"
    Assert-Test (Test-Path -LiteralPath $rotatedPath -PathType Leaf) 'Log rotation did not create the .1 file.'
    $writtenLines = @(Get-Content -LiteralPath $logPath)
    Assert-Test (@($writtenLines).Count -ge 2) 'Multi-line log output was not written line by line.'
    Assert-Test ($writtenLines[0] -match '^\d{4}-\d{2}-\d{2}T[^ ]+ connector|^\d{4}-\d{2}-\d{2}T[^ ]+ first line') 'Log line does not start with an ISO timestamp.'
    Assert-Test ($writtenLines[1] -match '^\d{4}-\d{2}-\d{2}T[^ ]+ second line') 'Second log line did not receive its own timestamp.'
    Write-Host 'OK route keeper helper, stream, grouping, APIPA, and log tests'
} finally {
    foreach ($path in @($logPath, "$logPath.1")) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}
