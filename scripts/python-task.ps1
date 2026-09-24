param(
    [ValidateSet('Install', 'Run', 'Status', 'Stop', 'Uninstall')]
    [string]$Action = 'Status',
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$currentWrapperPath = (Resolve-Path -LiteralPath $PSCommandPath).Path
$defaultTaskName = 'Tarkov-CIS-RouteKeeper'
$controlMutex = $null
$controlMutexHeld = $false
$script:LastPythonExitCode = 0

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This action requires administrator privileges.'
    }
}

function Resolve-ConfigPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path)
    }
    [IO.Path]::GetFullPath((Join-Path $projectRoot $Path))
}

function Get-TaskName {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $defaultTaskName }
    try {
        $configured = (Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json).TaskName
        if ($configured -and -not [string]::IsNullOrWhiteSpace([string]$configured)) {
            return [string]$configured
        }
    } catch {
        # The Python application performs the authoritative configuration
        # validation.  Keep the stable default task name so Status can still
        # identify an already-installed task and the real error remains visible.
    }
    $defaultTaskName
}

function ConvertTo-SafeWindowsArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    # Windows paths cannot contain a double quote. Reject it explicitly rather
    # than constructing an ambiguous Task Scheduler command line.
    if ($Value.IndexOf([char]0) -ge 0 -or $Value.Contains('"')) {
        throw 'A scheduled-task argument contains an unsupported character.'
    }
    '"{0}"' -f $Value
}

function Join-SafeWindowsArguments {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    (@($Arguments | ForEach-Object { ConvertTo-SafeWindowsArgument -Value $_ }) -join ' ')
}

function Get-PythonRuntime {
    $executableCandidates = @(
        (Join-Path $projectRoot 'TarkovCIS.exe'),
        (Join-Path $projectRoot 'dist\TarkovCIS\TarkovCIS.exe')
    )
    # Source worktrees must not silently run an older frozen build.
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'Tarkov-CIS-Python.py'))) {
    foreach ($candidate in $executableCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [pscustomobject]@{
                Kind = 'Executable'
                FilePath = (Resolve-Path -LiteralPath $candidate).Path
                PrefixArguments = [string[]]@()
            }
        }
    }
    }

    $sourceEntry = Join-Path $projectRoot 'Tarkov-CIS-Python.py'
    if (Test-Path -LiteralPath $sourceEntry -PathType Leaf) {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            & $launcher.Source -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]@{
                    Kind = 'PythonSource'
                    FilePath = $launcher.Source
                    PrefixArguments = [string[]]@('-3', $sourceEntry)
                }
            }
        }

        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($python) {
            & $python.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]@{
                    Kind = 'PythonSource'
                    FilePath = $python.Source
                    PrefixArguments = [string[]]@($sourceEntry)
                }
            }
        }
    }

    throw 'Neither TarkovCIS.exe nor a usable Python 3.11+ source runtime was found.'
}

function Invoke-PythonCommand {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)][ValidateSet('run', 'status', 'cleanup')][string]$Command,
        [Parameter(Mandatory = $true)][string]$ResolvedConfigPath,
        [switch]$AllowFailure
    )

    [string[]]$arguments = @($Runtime.PrefixArguments) + @($Command, '--config', $ResolvedConfigPath)
    & $Runtime.FilePath @arguments
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    $script:LastPythonExitCode = $exitCode
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "The Python $Command command failed with exit code $exitCode."
    }
}

function Get-TaskPowerShell {
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf) {
        return (Resolve-Path -LiteralPath $windowsPowerShell).Path
    }
    $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($pwsh) { return $pwsh.Source }
    (Get-Command powershell.exe -ErrorAction Stop).Source
}

function Wait-TaskNotRunning {
    param([Parameter(Mandatory = $true)][string]$TaskName, [int]$TimeoutSeconds = 20)

    $deadline = (Get-Date).AddSeconds([math]::Max(1, $TimeoutSeconds))
    do {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task -or [string]$task.State -ne 'Running') { return $true }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    $false
}

function Get-PythonWrapperPath {
    param([Parameter(Mandatory = $true)]$Task)

    foreach ($action in @($Task.Actions)) {
        $arguments = [string]$action.Arguments
        $fileMatch = [regex]::Match($arguments, '(?i)(?:^|\s)"?-File"?\s+(?:"([^"]+)"|(\S+))')
        if (-not $fileMatch.Success) { continue }
        $candidate = if ($fileMatch.Groups[1].Success) { $fileMatch.Groups[1].Value } else { $fileMatch.Groups[2].Value }
        $actionMatch = [regex]::Match($arguments, '(?i)(?:^|\s)"?-Action"?\s+(?:"Run"|Run)(?:\s|$)')
        if ([IO.Path]::GetFileName($candidate) -ieq 'python-task.ps1' -and $actionMatch.Success) {
            return $candidate
        }
    }
    $null
}

function Test-TaskUsesThisWrapper {
    param([Parameter(Mandatory = $true)]$Task)

    $candidate = Get-PythonWrapperPath -Task $Task
    if ($candidate) {
        try {
            if ([IO.Path]::GetFullPath($candidate) -ieq $currentWrapperPath) {
                return $true
            }
        } catch {
            return $false
        }
    }
    # New installations run the Python entry point directly from Task
    # Scheduler.  The PowerShell file remains only the control/migration
    # surface, so do not classify a direct Python action as legacy.
    Test-TaskUsesDirectPythonRuntime -Task $Task
}

function Test-TaskUsesDirectPythonRuntime {
    param([Parameter(Mandatory = $true)]$Task)

    $sourceEntry = [regex]::Escape((Join-Path $projectRoot 'Tarkov-CIS-Python.py'))
    $sourceExe = [regex]::Escape((Join-Path $projectRoot 'TarkovCIS.exe'))
    $bundleExe = [regex]::Escape((Join-Path $projectRoot 'dist\TarkovCIS\TarkovCIS.exe'))
    foreach ($action in @($Task.Actions)) {
        $arguments = [string]$action.Arguments
        $isRun = $arguments -match '(?i)(?:^|\s)"?run"?(?:\s|$)'
        if (-not $isRun) { continue }
        if ($arguments -match $sourceEntry -or
            $arguments -match $sourceExe -or
            $arguments -match $bundleExe) {
            return $true
        }
    }
    $false
}

function Get-LegacyKeeperPath {
    param([Parameter(Mandatory = $true)]$Task)

    foreach ($action in @($Task.Actions)) {
        $arguments = [string]$action.Arguments
        $match = [regex]::Match($arguments, '(?i)(?:^|\s)-File\s+(?:"([^"]+)"|(\S+))')
        if (-not $match.Success) { continue }
        $candidate = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        if ([IO.Path]::GetFileName($candidate) -ieq 'Tarkov-CisRouteKeeper.ps1' -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $null
}

function Stop-LegacyKeeperSafely {
    param(
        [Parameter(Mandatory = $true)][string]$LegacyKeeperPath,
        [Parameter(Mandatory = $true)][string]$ResolvedConfigPath
    )

    # The compatibility filename may now host Python. Request its token-bound
    # cleanup before the legacy path is permitted to stop the scheduler task.
    $pythonPidPath = Join-Path $env:LOCALAPPDATA 'TarkovCIS\keeper.pid.json'
    if (Test-Path -LiteralPath $pythonPidPath) {
        $cleanupRuntime = Get-PythonRuntime
        Invoke-PythonCommand -Runtime $cleanupRuntime -Command cleanup -ResolvedConfigPath $ResolvedConfigPath
    }
    $shell = Get-TaskPowerShell
    [string[]]$arguments = @(
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy', 'Bypass',
        '-File', $LegacyKeeperPath,
        '-Action', 'Stop', '-Legacy',
        '-ConfigPath', $ResolvedConfigPath
    )
    & $shell @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The legacy keeper cleanup failed with exit code $LASTEXITCODE; refusing to replace it."
    }
}

function Stop-PythonTaskSafely {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)][string]$ResolvedConfigPath
    )

    Invoke-PythonCommand -Runtime $Runtime -Command cleanup -ResolvedConfigPath $ResolvedConfigPath -AllowFailure
    $stopExitCode = $script:LastPythonExitCode
    if ($stopExitCode -ne 0) {
        # A SoftEther disconnect can remove an owned route between the first
        # read and the delete.  Cleanup is idempotent, so retry once after the
        # adapter state has settled; never turn this into an infinite loop.
        Start-Sleep -Milliseconds 500
        Invoke-PythonCommand -Runtime $Runtime -Command cleanup -ResolvedConfigPath $ResolvedConfigPath -AllowFailure
        $stopExitCode = $script:LastPythonExitCode
    }
    if ($stopExitCode -ne 0) {
        throw 'The Python keeper did not finish its bounded cleanup; the scheduled task was not force-stopped.'
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not (Wait-TaskNotRunning -TaskName $TaskName)) {
        throw 'The scheduled task did not stop after the Python keeper completed cleanup.'
    }
}

$resolvedConfigPath = Resolve-ConfigPath -Path $ConfigPath
$taskName = Get-TaskName -Path $resolvedConfigPath

if ($Action -eq 'Run') {
    Assert-Administrator
    $runtime = Get-PythonRuntime
    Invoke-PythonCommand -Runtime $runtime -Command run -ResolvedConfigPath $resolvedConfigPath -AllowFailure
    $runExitCode = $script:LastPythonExitCode
    exit $runExitCode
}

if ($Action -eq 'Status') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    [pscustomobject]@{
        TaskName = $taskName
        TaskState = if ($task) { [string]$task.State } else { 'NotInstalled' }
    } | Format-List

    try {
        $runtime = Get-PythonRuntime
        Invoke-PythonCommand -Runtime $runtime -Command status -ResolvedConfigPath $resolvedConfigPath -AllowFailure
        $statusExitCode = $script:LastPythonExitCode
        exit $statusExitCode
    } catch {
        Write-Warning $_.Exception.Message
        exit 1
    }
}

Assert-Administrator
try {
    [bool]$createdNew = $false
    try {
        $controlMutex = [Threading.Mutex]::new($false, 'Global\TarkovCisPythonTaskControl', [ref]$createdNew)
    } catch {
        [bool]$createdNew = $false
        $controlMutex = [Threading.Mutex]::new($false, 'Local\TarkovCisPythonTaskControl', [ref]$createdNew)
    }
    try {
        $controlMutexHeld = $controlMutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $controlMutexHeld = $true
    }
    if (-not $controlMutexHeld) {
        throw 'Another Tarkov CIS task-control operation is already running.'
    }

    if ($Action -eq 'Install') {
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing -and [string]$existing.State -eq 'Running' -and
            (Test-TaskUsesDirectPythonRuntime -Task $existing)) {
            Write-Host "$taskName is already running; the duplicate Install request was ignored."
            exit 0
        }

        $existingIsDirectPython = if ($existing) {
            Test-TaskUsesDirectPythonRuntime -Task $existing
        } else {
            $false
        }
        if ($existing -and -not $existingIsDirectPython) {
            $pythonWrapper = Get-PythonWrapperPath -Task $existing
            $legacyKeeper = Get-LegacyKeeperPath -Task $existing
            if ($pythonWrapper) {
                Write-Host "Migrating the Python keeper after shared PID/token cleanup."
                $migrationRuntime = Get-PythonRuntime
                Stop-PythonTaskSafely -TaskName $taskName -Runtime $migrationRuntime -ResolvedConfigPath $resolvedConfigPath
            } elseif ($legacyKeeper) {
                Write-Host "Migrating the legacy PowerShell keeper after its owned-route cleanup."
                Stop-LegacyKeeperSafely -LegacyKeeperPath $legacyKeeper -ResolvedConfigPath $resolvedConfigPath
                if (-not (Wait-TaskNotRunning -TaskName $taskName)) {
                    throw 'The legacy route-keeper task did not stop; refusing to replace it.'
                }
            } else {
                throw "A different scheduled task already uses the name $taskName; refusing to overwrite it."
            }
        } elseif ($existing -and [string]$existing.State -ne 'Running') {
            # A stopped direct-Python task can still own persisted /32 routes.
            # Reuse the same token-bound cleanup before replacing it.
            Write-Host "Cleaning the stopped Python keeper before reinstalling it."
            $migrationRuntime = Get-PythonRuntime
            Stop-PythonTaskSafely -TaskName $taskName -Runtime $migrationRuntime -ResolvedConfigPath $resolvedConfigPath
        }

        $runtime = Get-PythonRuntime
        # The scheduled task must execute Python directly.  This keeps the
        # long-running Keeper independent from PowerShell; this script is only
        # used for install/migration/control and legacy cleanup.
        [string[]]$taskArgumentArray = @($runtime.PrefixArguments) + @(
            'run',
            '--config', $resolvedConfigPath
        )
        $taskAction = New-ScheduledTaskAction `
            -Execute $runtime.FilePath `
            -Argument (Join-SafeWindowsArguments -Arguments $taskArgumentArray) `
            -WorkingDirectory $projectRoot
        $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew `
            -StartWhenAvailable

        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $taskAction `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Runs the Python Tarkov CIS authorization split-route keeper.' `
            -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName
        Write-Host "Installed and started $taskName using $($runtime.Kind)."
        exit 0
    }

    if ($Action -eq 'Stop' -or $Action -eq 'Uninstall') {
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing -and -not (Test-TaskUsesThisWrapper -Task $existing)) {
            $pythonWrapper = Get-PythonWrapperPath -Task $existing
            $directPython = Test-TaskUsesDirectPythonRuntime -Task $existing
            $legacyKeeper = Get-LegacyKeeperPath -Task $existing
            if ($pythonWrapper -or $directPython) {
                Write-Host "Stopping the Python keeper through shared PID/token cleanup."
            } elseif ($legacyKeeper) {
                Write-Host "Stopping the legacy PowerShell keeper through its own cleanup path."
                Stop-LegacyKeeperSafely -LegacyKeeperPath $legacyKeeper -ResolvedConfigPath $resolvedConfigPath
                if (-not (Wait-TaskNotRunning -TaskName $taskName)) {
                    throw 'The legacy route-keeper task did not stop after cleanup.'
                }
            } else {
                throw "A different scheduled task already uses the name $taskName; refusing to stop it."
            }
        }
        $runtime = Get-PythonRuntime
        Stop-PythonTaskSafely -TaskName $taskName -Runtime $runtime -ResolvedConfigPath $resolvedConfigPath
        if ($Action -eq 'Uninstall') {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "Uninstalled $taskName."
        } else {
            Write-Host "Stopped $taskName."
        }
        exit 0
    }
} finally {
    if ($controlMutex -and $controlMutexHeld) {
        try { $controlMutex.ReleaseMutex() } catch {}
    }
    if ($controlMutex) { $controlMutex.Dispose() }
}
