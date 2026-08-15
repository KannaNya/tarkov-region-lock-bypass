param(
    [ValidateSet('Start', 'Status', 'Stop', 'Uninstall')]
    [string]$Action,
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$connectorPath = Join-Path $projectRoot 'src\Connect-VpnGateCis.ps1'
$keeperPath = Join-Path $projectRoot 'src\Tarkov-CisRouteKeeper.ps1'
$taskName = 'Tarkov-CIS-RouteKeeper'
if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
    try {
        $configuredTaskName = (Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json).TaskName
        if ($configuredTaskName) { $taskName = [string]$configuredTaskName }
    } catch {}
}
$controlMutex = $null
$controlMutexHeld = $false

try {
    # GUI and command-line invocations share this short-lived lock.  Start no
    # longer performs a synchronous relay connection, but Install still stops
    # and replaces the scheduled task, so two clicks must not do that at once.
    $controlMutexCreated = $false
    $controlMutex = [Threading.Mutex]::new($false, 'Global\Tarkov-CisControl', [ref]$controlMutexCreated)
    try {
        $controlMutexHeld = $controlMutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        # The previous controller exited unexpectedly, but .NET grants this
        # invocation ownership so it can safely release the mutex in finally.
        $controlMutexHeld = $true
    }
    if (-not $controlMutexHeld) {
        Write-Warning '已有一个控制操作正在执行；忽略本次重复操作。'
        $controlMutex.Dispose()
        $controlMutex = $null
        exit 2
    }
} catch {
    if ($controlMutex) { $controlMutex.Dispose() }
    throw
}

try {
    switch ($Action) {
        'Start' {
            # Install starts the keeper, and the keeper owns all relay
            # discovery/connection attempts.  Keeping Connect out of this
            # foreground action prevents the GUI from blocking for the full
            # candidate timeout and prevents a second connector from racing
            # the newly started scheduled task.
            $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($existingTask -and [string]$existingTask.State -eq 'Running') {
                Write-Host '后台分流任务已经在运行；不重复启动连接器。'
                break
            }
            Write-Host '正在安装并启动后台分流任务；节点连接由后台任务负责。'
            & $keeperPath -Action Install -ConfigPath $ConfigPath
            Write-Host '后台任务已启动；首次连接和后续节点切换将在后台进行。'
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
} finally {
    if ($controlMutex -and $controlMutexHeld) {
        try { $controlMutex.ReleaseMutex() } catch {}
    }
    if ($controlMutex) { $controlMutex.Dispose() }
}
