param(
    [string]$PythonVersion = '3.13'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$buildVenv = Join-Path $projectRoot '.venv-build'
$entryPoint = Join-Path $projectRoot 'packaging\entry.py'
$requirementsPath = Join-Path $projectRoot 'requirements-build.txt'
$distRoot = Join-Path $projectRoot 'dist'
$bundleRoot = Join-Path $distRoot 'TarkovCIS'
$archivePath = Join-Path $distRoot 'TarkovCIS-Windows.zip'

function Invoke-CheckedNativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

foreach ($requiredPath in @($entryPoint, $requirementsPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required build input was not found: $requiredPath"
    }
}

$basePython = $null
[string[]]$basePythonPrefix = @()
$pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pythonLauncher) {
    & $pythonLauncher.Source "-$PythonVersion" -c 'import sys; raise SystemExit(0)'
    if ($LASTEXITCODE -eq 0) {
        $basePython = $pythonLauncher.Source
        $basePythonPrefix = @("-$PythonVersion")
    }
}
if (-not $basePython) {
    $activePython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $activePython) {
        throw 'Python was not found. Use the GitHub release build on machines without Python.'
    }
    $actualVersion = & $activePython.Source -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    if ($LASTEXITCODE -ne 0 -or [string]$actualVersion -ne $PythonVersion) {
        throw "Active python.exe is $actualVersion, but this build requested Python $PythonVersion."
    }
    $basePython = $activePython.Source
}

Invoke-CheckedNativeCommand -FilePath $basePython -Arguments @($basePythonPrefix + @('-m', 'venv', $buildVenv))
$buildPython = Join-Path $buildVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
    throw "Build virtual environment did not create Python: $buildPython"
}

Invoke-CheckedNativeCommand -FilePath $buildPython -Arguments @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip'
)
Invoke-CheckedNativeCommand -FilePath $buildPython -Arguments @(
    '-m', 'pip', 'install', '--disable-pip-version-check',
    '-e', $projectRoot,
    '-r', $requirementsPath
)
Invoke-CheckedNativeCommand -FilePath $buildPython -Arguments @(
    '-m', 'unittest', 'discover', '-s', (Join-Path $projectRoot 'tests_py'), '-v'
)
[string[]]$pyInstallerArguments = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    '--clean',
    '--onedir',
    '--name', 'TarkovCIS',
    '--paths', (Join-Path $projectRoot 'python'),
    '--hidden-import', 'tarkov_cis.gui',
    '--distpath', $distRoot,
    '--workpath', (Join-Path $projectRoot 'build\pyinstaller'),
    '--specpath', (Join-Path $projectRoot 'build'),
    $entryPoint
)
Invoke-CheckedNativeCommand -FilePath $buildPython -Arguments $pyInstallerArguments

$builtExecutable = Join-Path $bundleRoot 'TarkovCIS.exe'
if (-not (Test-Path -LiteralPath $builtExecutable -PathType Leaf)) {
    throw "PyInstaller did not produce the expected executable: $builtExecutable"
}

Copy-Item -LiteralPath (Join-Path $projectRoot 'config.example.json') -Destination $bundleRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $bundleRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $bundleRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'Tarkov-CIS-GUI.cmd') -Destination $bundleRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'Tarkov-CIS-GUI.vbs') -Destination $bundleRoot -Force
$bundleScripts = Join-Path $bundleRoot 'scripts'
if (-not (Test-Path -LiteralPath $bundleScripts -PathType Container)) {
    New-Item -ItemType Directory -Path $bundleScripts | Out-Null
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\python-task.ps1') -Destination $bundleScripts -Force

if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
    Remove-Item -LiteralPath $archivePath -Force
}
Compress-Archive -LiteralPath $bundleRoot -DestinationPath $archivePath -CompressionLevel Optimal
Write-Host "Built $archivePath"
