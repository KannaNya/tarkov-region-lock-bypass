param([string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.json'))
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot '..\src\Tarkov-CisRouteKeeper.ps1') -Action Status -ConfigPath $ConfigPath

