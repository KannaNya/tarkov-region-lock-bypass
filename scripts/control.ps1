param(
    [ValidateSet('Start', 'Status', 'Stop', 'Uninstall')]
    [string]$Action,
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$connectorPath = Join-Path $projectRoot 'src\Connect-VpnGateCis.ps1'
$keeperPath = Join-Path $projectRoot 'src\Tarkov-CisRouteKeeper.ps1'

switch ($Action) {
    'Start' {
        Write-Host '正在寻找可用的 CIS VPN Gate 节点...'
        $initialConnectError = $null
        try {
            & $connectorPath -Action Connect
        } catch {
            $initialConnectError = $_.Exception.Message
            Write-Warning "当前没有节点成功建立会话：$initialConnectError"
        }
        Write-Host '正在安装并启动后台分流任务...'
        & $keeperPath -Action Install -ConfigPath $ConfigPath
        if ($initialConnectError) {
            Write-Warning '后台任务已启动，将继续刷新节点并自动连接；无需反复点击启动。'
        }
    }
    'Status' {
        & $keeperPath -Action Status -ConfigPath $ConfigPath
        try {
            & $connectorPath -Action Status
        } catch {
            Write-Warning "SoftEther 账户当前未连接：$($_.Exception.Message)"
        }
    }
    'Stop' {
        & $keeperPath -Action Stop -ConfigPath $ConfigPath
        & $connectorPath -Action Disconnect
    }
    'Uninstall' {
        & $keeperPath -Action Uninstall -ConfigPath $ConfigPath
        & $connectorPath -Action Disconnect
    }
}
