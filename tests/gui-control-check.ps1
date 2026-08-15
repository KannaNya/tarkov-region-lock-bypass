$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$controlPath = Join-Path $root 'scripts\control.ps1'
$guiPath = Join-Path $root 'src\Tarkov-CisGui.ps1'

function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$control = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($controlPath))
$gui = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($guiPath))

Assert-Test ($control -match 'Global\\Tarkov-CisControl') 'Control operations are not protected by the global mutex.'
Assert-Test ($control -notmatch '(?m)&\s*\$connectorPath\s+-Action\s+Connect') 'Start still invokes the blocking connector directly.'
Assert-Test ($control -match '(?s)''Start''\s*\{.*?\$keeperPath\s+-Action\s+Install') 'Start does not delegate connection ownership to the installed keeper.'
Assert-Test ($gui -match '\\bSID-\[A-Za-z0-9-\]\+\\b') 'GUI does not use the locale-independent SoftEther SID check.'
Assert-Test ($gui -match '(?s)elseif\s*\(\$hasLease\).*?\$vpnValue\.Text') 'GUI does not distinguish a stale adapter lease from an established VPN session.'
Assert-Test ($gui -match '(?s)if\s*\(\$Action\s+-eq\s+''Start''\).*?\$existingTask.*?return') 'GUI does not suppress repeated Start operations.'

Write-Host 'OK GUI and control flow checks'
