param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class TarkovCisGuiNative {
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int command);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $hostPath = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $escapedScript = $PSCommandPath.Replace('"', '\"')
    $escapedConfig = $ConfigPath.Replace('"', '\"')
    $arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}"' -f $escapedScript, $escapedConfig
    try {
        Start-Process -FilePath $hostPath -ArgumentList $arguments -Verb RunAs | Out-Null
    } catch {
        [Windows.Forms.MessageBox]::Show(
            '需要管理员权限才能安装后台任务和临时路由。',
            'Tarkov CIS 分流',
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
    }
    exit
}

[Windows.Forms.Application]::EnableVisualStyles()

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$configFile = if ([IO.Path]::IsPathRooted($ConfigPath)) {
    [IO.Path]::GetFullPath($ConfigPath)
} else {
    [IO.Path]::GetFullPath((Join-Path $projectRoot $ConfigPath))
}
$configExample = Join-Path $projectRoot 'config.example.json'
$controllerPath = Join-Path $projectRoot 'scripts\control.ps1'
$taskName = 'Tarkov-CIS-RouteKeeper'
$vpnAlias = 'VPN - VPN Client'
$vpnCmdPath = 'C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe'
$vpnAccountName = 'Tarkov-CIS-PlayOnly'
$keeperLogPath = Join-Path $projectRoot 'src\Tarkov-CisRouteKeeper.log'

function Ensure-Configuration {
    if (-not (Test-Path -LiteralPath $configFile)) {
        Copy-Item -LiteralPath $configExample -Destination $configFile
    }
}

Ensure-Configuration

$form = [Windows.Forms.Form]::new()
$form.Text = 'Tarkov CIS 分流'
$form.ClientSize = [Drawing.Size]::new(720, 540)
$form.StartPosition = [Windows.Forms.FormStartPosition]::CenterScreen
$form.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.Font = [Drawing.Font]::new('Microsoft YaHei UI', 9)
$form.BackColor = [Drawing.SystemColors]::Window

$titleLabel = [Windows.Forms.Label]::new()
$titleLabel.Text = 'Tarkov CIS 分流'
$titleLabel.Location = [Drawing.Point]::new(24, 18)
$titleLabel.Size = [Drawing.Size]::new(650, 32)
$titleLabel.Font = [Drawing.Font]::new('Microsoft YaHei UI', 16, [Drawing.FontStyle]::Bold)

$subtitleLabel = [Windows.Forms.Label]::new()
$subtitleLabel.Text = '只让区域鉴权目标走 VPN Gate，普通网络和 Raid 保持本地直连。'
$subtitleLabel.Location = [Drawing.Point]::new(26, 55)
$subtitleLabel.Size = [Drawing.Size]::new(650, 24)
$subtitleLabel.ForeColor = [Drawing.SystemColors]::GrayText

$separator = [Windows.Forms.Label]::new()
$separator.Location = [Drawing.Point]::new(24, 84)
$separator.Size = [Drawing.Size]::new(672, 2)
$separator.BorderStyle = [Windows.Forms.BorderStyle]::Fixed3D

$statusGroup = [Windows.Forms.GroupBox]::new()
$statusGroup.Text = '当前状态'
$statusGroup.Location = [Drawing.Point]::new(24, 98)
$statusGroup.Size = [Drawing.Size]::new(672, 112)

$taskCaption = [Windows.Forms.Label]::new()
$taskCaption.Text = '后台任务'
$taskCaption.Location = [Drawing.Point]::new(18, 28)
$taskCaption.Size = [Drawing.Size]::new(90, 22)

$taskValue = [Windows.Forms.Label]::new()
$taskValue.Text = '检查中'
$taskValue.Location = [Drawing.Point]::new(112, 28)
$taskValue.Size = [Drawing.Size]::new(190, 22)
$taskValue.Font = [Drawing.Font]::new('Microsoft YaHei UI', 9, [Drawing.FontStyle]::Bold)

$vpnCaption = [Windows.Forms.Label]::new()
$vpnCaption.Text = 'VPN 接口'
$vpnCaption.Location = [Drawing.Point]::new(336, 28)
$vpnCaption.Size = [Drawing.Size]::new(90, 22)

$vpnValue = [Windows.Forms.Label]::new()
$vpnValue.Text = '检查中'
$vpnValue.Location = [Drawing.Point]::new(430, 28)
$vpnValue.Size = [Drawing.Size]::new(210, 22)
$vpnValue.Font = [Drawing.Font]::new('Microsoft YaHei UI', 9, [Drawing.FontStyle]::Bold)

$ipCaption = [Windows.Forms.Label]::new()
$ipCaption.Text = 'VPN 地址'
$ipCaption.Location = [Drawing.Point]::new(18, 66)
$ipCaption.Size = [Drawing.Size]::new(90, 22)

$ipValue = [Windows.Forms.Label]::new()
$ipValue.Text = '—'
$ipValue.Location = [Drawing.Point]::new(112, 66)
$ipValue.Size = [Drawing.Size]::new(220, 22)

$routeCaption = [Windows.Forms.Label]::new()
$routeCaption.Text = '普通出口'
$routeCaption.Location = [Drawing.Point]::new(336, 66)
$routeCaption.Size = [Drawing.Size]::new(90, 22)

$routeValue = [Windows.Forms.Label]::new()
$routeValue.Text = '检查中'
$routeValue.Location = [Drawing.Point]::new(430, 66)
$routeValue.Size = [Drawing.Size]::new(210, 22)

$statusGroup.Controls.AddRange(@(
    $taskCaption, $taskValue, $vpnCaption, $vpnValue,
    $ipCaption, $ipValue, $routeCaption, $routeValue
))

$logPathLabel = [Windows.Forms.Label]::new()
$logPathLabel.Text = 'EFT 日志目录（用于发现 lobby/WSN 主机）'
$logPathLabel.Location = [Drawing.Point]::new(24, 224)
$logPathLabel.Size = [Drawing.Size]::new(430, 22)

$logPathText = [Windows.Forms.TextBox]::new()
$logPathText.Location = [Drawing.Point]::new(24, 248)
$logPathText.Size = [Drawing.Size]::new(570, 28)

$browseButton = [Windows.Forms.Button]::new()
$browseButton.Text = '选择目录'
$browseButton.Location = [Drawing.Point]::new(604, 246)
$browseButton.Size = [Drawing.Size]::new(92, 30)
$browseButton.FlatStyle = [Windows.Forms.FlatStyle]::System

$startButton = [Windows.Forms.Button]::new()
$startButton.Text = '启动并常驻'
$startButton.Location = [Drawing.Point]::new(24, 292)
$startButton.Size = [Drawing.Size]::new(142, 36)
$startButton.FlatStyle = [Windows.Forms.FlatStyle]::System

$statusButton = [Windows.Forms.Button]::new()
$statusButton.Text = '查看详细状态'
$statusButton.Location = [Drawing.Point]::new(176, 292)
$statusButton.Size = [Drawing.Size]::new(142, 36)
$statusButton.FlatStyle = [Windows.Forms.FlatStyle]::System

$stopButton = [Windows.Forms.Button]::new()
$stopButton.Text = '停止分流'
$stopButton.Location = [Drawing.Point]::new(328, 292)
$stopButton.Size = [Drawing.Size]::new(142, 36)
$stopButton.FlatStyle = [Windows.Forms.FlatStyle]::System

$uninstallButton = [Windows.Forms.Button]::new()
$uninstallButton.Text = '卸载后台任务'
$uninstallButton.Location = [Drawing.Point]::new(480, 292)
$uninstallButton.Size = [Drawing.Size]::new(142, 36)
$uninstallButton.FlatStyle = [Windows.Forms.FlatStyle]::System

$outputLabel = [Windows.Forms.Label]::new()
$outputLabel.Text = '运行信息'
$outputLabel.Location = [Drawing.Point]::new(24, 344)
$outputLabel.Size = [Drawing.Size]::new(120, 22)

$outputBox = [Windows.Forms.RichTextBox]::new()
$outputBox.Location = [Drawing.Point]::new(24, 368)
$outputBox.Size = [Drawing.Size]::new(672, 132)
$outputBox.ReadOnly = $true
$outputBox.BackColor = [Drawing.SystemColors]::Window
$outputBox.BorderStyle = [Windows.Forms.BorderStyle]::FixedSingle
$outputBox.Font = [Drawing.Font]::new('Consolas', 9)
$outputBox.Text = "准备就绪。选择日志目录后，点击启动并常驻。`r`n"

$statusStrip = [Windows.Forms.StatusStrip]::new()
$operationStatus = [Windows.Forms.ToolStripStatusLabel]::new()
$operationStatus.Text = '就绪'
$operationStatus.Spring = $true
$operationStatus.TextAlign = [Drawing.ContentAlignment]::MiddleLeft
[void]$statusStrip.Items.Add($operationStatus)

$form.Controls.AddRange(@(
    $titleLabel, $subtitleLabel, $separator, $statusGroup,
    $logPathLabel, $logPathText, $browseButton,
    $startButton, $statusButton, $stopButton, $uninstallButton,
    $outputLabel, $outputBox, $statusStrip
))

$script:activeProcess = $null
$script:stdoutPath = $null
$script:stderrPath = $null

function Get-SoftEtherSessionState {
    # AccountStatusGet is the source of truth for the VPN session.  The
    # adapter can retain its DHCP lease briefly after a relay disconnects, so
    # an interface/IP check alone must never be shown as "已连接".
    if (-not (Test-Path -LiteralPath $vpnCmdPath -PathType Leaf)) {
        return [pscustomobject]@{ State = 'Unavailable'; Detail = '找不到 SoftEther vpncmd' }
    }

    $token = [Guid]::NewGuid().ToString('N')
    $outPath = Join-Path ([IO.Path]::GetTempPath()) "tarkov-cis-gui-status-$token.out.log"
    $errPath = Join-Path ([IO.Path]::GetTempPath()) "tarkov-cis-gui-status-$token.err.log"
    $process = $null
    try {
        # Keep the account name unquoted here.  Start-Process passes embedded
        # quotes through to vpncmd, where they become part of the account
        # name instead of shell quoting.
        $arguments = '/CLIENT localhost /CMD AccountStatusGet {0}' -f $vpnAccountName
        $process = Start-Process -FilePath $vpnCmdPath -ArgumentList $arguments `
            -WindowStyle Hidden -RedirectStandardOutput $outPath `
            -RedirectStandardError $errPath -PassThru
        if (-not $process.WaitForExit(1500)) {
            try { $process.Kill() } catch {}
            return [pscustomobject]@{ State = 'Unknown'; Detail = 'AccountStatusGet 超时' }
        }

        $process.Refresh()
        $exitCode = [int]$process.ExitCode
        $output = if (Test-Path -LiteralPath $outPath) {
            # vpncmd writes UTF-8 without a BOM.  Reading with the platform
            # default encoding breaks Chinese AccountStatusGet labels under
            # Windows PowerShell 5.1, so decode the bytes explicitly.
            [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($outPath))
        } else { '' }
        if ($exitCode -ne 0) {
            return [pscustomobject]@{ State = 'NotConnected'; Detail = 'AccountStatusGet 未返回已建立会话' }
        }

        $statusMatch = [regex]::Match($output, '(?im)^\s*(?:会话状态|Session\s+Status)\s*\|\s*(?<Value>.+?)\s*$')
        $hasEstablishedSession = $output -match '(?im)\bSID-[A-Za-z0-9-]+\b'
        if ($hasEstablishedSession) {
            $detail = if ($statusMatch.Success) { $statusMatch.Groups['Value'].Value.Trim() } else { 'SoftEther SID 会话已建立' }
            return [pscustomobject]@{ State = 'Connected'; Detail = $detail }
        }
        if (-not $statusMatch.Success) {
            return [pscustomobject]@{ State = 'Unknown'; Detail = 'AccountStatusGet 未返回可识别的会话状态' }
        }

        $statusValue = $statusMatch.Groups['Value'].Value.Trim()
        if ($statusValue -match '(?i)连接完成|会话建立|connection\s+(?:completed|established)|session\s+established|\bconnected\b') {
            return [pscustomobject]@{ State = 'Connected'; Detail = $statusValue }
        }
        if ($statusValue -match '(?i)连接中|正在连接|connecting') {
            return [pscustomobject]@{ State = 'Connecting'; Detail = $statusValue }
        }
        if ($statusValue -match '(?i)未连接|断开|disconnected|not\s+connected|offline') {
            return [pscustomobject]@{ State = 'NotConnected'; Detail = $statusValue }
        }
        [pscustomobject]@{ State = 'Unknown'; Detail = $statusValue }
    } catch {
        [pscustomobject]@{ State = 'Unknown'; Detail = $_.Exception.Message }
    } finally {
        if ($process) { $process.Dispose() }
        if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $errPath) { Remove-Item -LiteralPath $errPath -Force -ErrorAction SilentlyContinue }
    }
}

function Get-VpnAdapterLease {
    $adapter = Get-NetAdapter -Name $vpnAlias -ErrorAction SilentlyContinue
    if (-not $adapter -or $adapter.Status -ne 'Up') { return $null }
    $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue
    $ip = @($ipConfig.IPv4Address | ForEach-Object { $_.IPAddress } | Where-Object {
        $_ -and $_ -notlike '169.254.*' -and $_ -ne '0.0.0.0'
    } | Select-Object -First 1)
    $gateway = @($ipConfig.IPv4DefaultGateway | ForEach-Object { $_.NextHop } | Select-Object -First 1)
    if (-not $ip) { return $null }
    [pscustomobject]@{
        Adapter = $adapter
        IPv4 = [string]$ip[0]
        Gateway = if ($gateway) { [string]$gateway[0] } else { $null }
    }
}

function Get-ReconnectState {
    param(
        [string]$TaskState,
        [string]$SessionState,
        [bool]$HasLease
    )

    if ($TaskState -ne 'Running') { return '后台未运行' }
    if ($SessionState -eq 'Connected' -and $HasLease) { return '守护正常' }
    if ($SessionState -eq 'Connecting') { return '正在建立会话' }

    $recentFailover = $false
    if (Test-Path -LiteralPath $keeperLogPath -PathType Leaf) {
        $recentLines = @(Get-Content -Tail 30 -LiteralPath $keeperLogPath -ErrorAction SilentlyContinue)
        foreach ($line in $recentLines) {
            if ($line -notmatch '(?i)VPN unavailable|failover|refreshing VPN Gate|session is no longer established') { continue }
            $timestampMatch = [regex]::Match([string]$line, '^(?<Timestamp>\d{4}-\d{2}-\d{2}T[^ ]+)')
            if (-not $timestampMatch.Success) { $recentFailover = $true; break }
            try {
                if ([datetimeoffset]::Now - [datetimeoffset]::Parse($timestampMatch.Groups['Timestamp'].Value) -lt [TimeSpan]::FromMinutes(5)) {
                    $recentFailover = $true
                    break
                }
            } catch { $recentFailover = $true; break }
        }
    }
    if ($recentFailover) { return '正在重连' }
    if ($HasLease -and $SessionState -ne 'Connected') { return '正在确认会话' }
    '等待后台重连'
}

function Write-OutputText {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return }
    $outputBox.AppendText($Text.TrimEnd() + "`r`n")
    $outputBox.SelectionStart = $outputBox.TextLength
    $outputBox.ScrollToCaret()
}

function Set-ControlsBusy {
    param([bool]$Busy, [string]$Message)
    $startButton.Enabled = -not $Busy
    $statusButton.Enabled = -not $Busy
    $stopButton.Enabled = -not $Busy
    $uninstallButton.Enabled = -not $Busy
    $browseButton.Enabled = -not $Busy
    $operationStatus.Text = $Message
    $form.UseWaitCursor = $Busy
}

function Read-ConfigurationIntoUi {
    try {
        $config = Get-Content -Raw -LiteralPath $configFile | ConvertFrom-Json
        if ($config.GameLogRoots -and $config.GameLogRoots.Count -gt 0) {
            $logPathText.Text = [string]$config.GameLogRoots[0]
        }
    } catch {
        Write-OutputText "读取配置失败：$($_.Exception.Message)"
    }
}

function Save-ConfigurationFromUi {
    Ensure-Configuration
    $config = Get-Content -Raw -LiteralPath $configFile | ConvertFrom-Json
    $selectedPath = $logPathText.Text.Trim()
    if ($selectedPath) {
        $config.GameLogRoots = @($selectedPath)
    }
    $json = $config | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText(
        $configFile,
        $json + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

function Refresh-StatusSummary {
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $taskState = if ($task) { [string]$task.State } else { 'NotInstalled' }
        if ($task) {
            $taskValue.Text = if ($taskState -eq 'Running') { '运行中' } else { $taskState }
            $taskValue.ForeColor = if ($taskState -eq 'Running') { [Drawing.Color]::DarkGreen } else { [Drawing.SystemColors]::ControlText }
        } else {
            $taskValue.Text = '未安装'
            $taskValue.ForeColor = [Drawing.SystemColors]::GrayText
        }

        $lease = Get-VpnAdapterLease
        $session = Get-SoftEtherSessionState
        $hasLease = [bool]$lease
        $sessionState = [string]$session.State
        if ($sessionState -eq 'Connected' -and $hasLease) {
            $vpnValue.Text = '已连接'
            $vpnValue.ForeColor = [Drawing.Color]::DarkGreen
            $ipValue.Text = $lease.IPv4
        } elseif ($sessionState -eq 'Connected') {
            $vpnValue.Text = '会话已连接，等待接口地址'
            $vpnValue.ForeColor = [Drawing.Color]::DarkOrange
            $ipValue.Text = '—'
        } elseif ($sessionState -eq 'Connecting') {
            $vpnValue.Text = '正在连接'
            $vpnValue.ForeColor = [Drawing.Color]::DarkOrange
            $ipValue.Text = if ($hasLease) { $lease.IPv4 } else { '—' }
        } elseif ($hasLease) {
            $vpnValue.Text = '适配器残留（会话未建立）'
            $vpnValue.ForeColor = [Drawing.Color]::DarkOrange
            $ipValue.Text = $lease.IPv4
        } else {
            $vpnValue.Text = if ($sessionState -eq 'Unavailable') { '无法检查会话' } else { '未连接' }
            $vpnValue.ForeColor = [Drawing.Color]::DarkRed
            $ipValue.Text = '—'
        }

        $defaultRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
            Where-Object State -eq 'Alive' |
            Sort-Object @{ Expression = {
                $interfaceMetric = (Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue).InterfaceMetric
                $_.RouteMetric + [int]$interfaceMetric
            } } |
            Select-Object -First 1
        $routeAlias = if ($defaultRoute) { [string]$defaultRoute.InterfaceAlias } else { '未知' }
        $routeValue.Text = $routeAlias

        $reconnectState = Get-ReconnectState -TaskState $taskState -SessionState $sessionState -HasLease $hasLease
        $statusSummary = '后台 {0} | VPN会话 {1} | 重连 {2} | 普通出口 {3}' -f `
            $(if ($taskState -eq 'Running') { '运行中' } elseif ($taskState -eq 'NotInstalled') { '未安装' } else { $taskState }), `
            $(switch ($sessionState) {
                'Connected' { if ($hasLease) { '已连接' } else { '已连接/待接口' }; break }
                'Connecting' { '连接中'; break }
                'NotConnected' { '未连接'; break }
                'Unavailable' { '不可用'; break }
                default { '未知' }
            }),
            $reconnectState,
            $routeAlias
        $operationStatus.ToolTipText = "${statusSummary}`r`n$($session.Detail)"
        if (-not $script:activeProcess -or $script:activeProcess.HasExited) {
            $operationStatus.Text = $statusSummary
        }
    } catch {
        $operationStatus.Text = "状态检查失败：$($_.Exception.Message)"
    }
}

function Start-ControlAction {
    param([ValidateSet('Start', 'Status', 'Stop', 'Uninstall')][string]$Action)
    if ($script:activeProcess -and -not $script:activeProcess.HasExited) { return }

    try {
        Save-ConfigurationFromUi
        if ($Action -eq 'Start') {
            $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($existingTask -and [string]$existingTask.State -eq 'Running') {
                $outputBox.Clear()
                Write-OutputText '后台任务已经在运行；忽略重复启动，后台会自动完成连接或重连。'
                $operationStatus.Text = '后台已运行；未重复启动连接器'
                Refresh-StatusSummary
                return
            }
        }
        $outputBox.Clear()
        Write-OutputText "开始执行：$Action"
        Set-ControlsBusy -Busy $true -Message '正在执行，请稍候...'

        $token = [Guid]::NewGuid().ToString('N')
        $script:stdoutPath = Join-Path ([IO.Path]::GetTempPath()) "tarkov-cis-gui-$token.out.log"
        $script:stderrPath = Join-Path ([IO.Path]::GetTempPath()) "tarkov-cis-gui-$token.err.log"
        $hostPath = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Action {1} -ConfigPath "{2}"' -f $controllerPath, $Action, $configFile
        $script:activeProcess = Start-Process -FilePath $hostPath -ArgumentList $arguments `
            -WindowStyle Hidden -RedirectStandardOutput $script:stdoutPath `
            -RedirectStandardError $script:stderrPath -PassThru
    } catch {
        Set-ControlsBusy -Busy $false -Message '执行失败'
        Write-OutputText "启动操作失败：$($_.Exception.Message)"
    }
}

$browseButton.Add_Click({
    $dialog = [Windows.Forms.FolderBrowserDialog]::new()
    $dialog.Description = '选择 EscapeFromTarkov 日志目录'
    $dialog.ShowNewFolderButton = $false
    if (Test-Path -LiteralPath $logPathText.Text) { $dialog.SelectedPath = $logPathText.Text }
    if ($dialog.ShowDialog($form) -eq [Windows.Forms.DialogResult]::OK) {
        $logPathText.Text = $dialog.SelectedPath
    }
    $dialog.Dispose()
})

$startButton.Add_Click({ Start-ControlAction -Action Start })
$statusButton.Add_Click({ Start-ControlAction -Action Status })
$stopButton.Add_Click({ Start-ControlAction -Action Stop })
$uninstallButton.Add_Click({
    $answer = [Windows.Forms.MessageBox]::Show(
        '确定卸载后台任务并断开 VPN 吗？本工具创建的临时路由也会被删除。',
        '确认卸载',
        [Windows.Forms.MessageBoxButtons]::YesNo,
        [Windows.Forms.MessageBoxIcon]::Question
    )
    if ($answer -eq [Windows.Forms.DialogResult]::Yes) {
        Start-ControlAction -Action Uninstall
    }
})

$actionTimer = [Windows.Forms.Timer]::new()
$actionTimer.Interval = 250
$actionTimer.Add_Tick({
    if (-not $script:activeProcess -or -not $script:activeProcess.HasExited) { return }

    $exitCode = $script:activeProcess.ExitCode
    $stdout = if (Test-Path -LiteralPath $script:stdoutPath) { Get-Content -Raw -LiteralPath $script:stdoutPath } else { '' }
    $stderr = if (Test-Path -LiteralPath $script:stderrPath) { Get-Content -Raw -LiteralPath $script:stderrPath } else { '' }
    Write-OutputText $stdout
    Write-OutputText $stderr
    if ($script:stdoutPath -and (Test-Path -LiteralPath $script:stdoutPath)) { [IO.File]::Delete($script:stdoutPath) }
    if ($script:stderrPath -and (Test-Path -LiteralPath $script:stderrPath)) { [IO.File]::Delete($script:stderrPath) }

    $script:activeProcess.Dispose()
    $script:activeProcess = $null
    $script:stdoutPath = $null
    $script:stderrPath = $null
    Set-ControlsBusy -Busy $false -Message $(if ($exitCode -eq 0) { '操作完成' } else { "操作失败（退出码 $exitCode）" })
    Refresh-StatusSummary
})

$statusTimer = [Windows.Forms.Timer]::new()
$statusTimer.Interval = 3000
$statusTimer.Add_Tick({ Refresh-StatusSummary })

$form.Add_Shown({
    [void][TarkovCisGuiNative]::ShowWindow($form.Handle, 5)
    [void][TarkovCisGuiNative]::SetForegroundWindow($form.Handle)
    Read-ConfigurationIntoUi
    Refresh-StatusSummary
    $statusTimer.Start()
    $actionTimer.Start()
    $form.Activate()
})

$form.Add_FormClosed({
    $statusTimer.Stop()
    $actionTimer.Stop()
    $statusTimer.Dispose()
    $actionTimer.Dispose()
})

$form.AcceptButton = $startButton
[void]$form.ShowDialog()
$form.Dispose()
