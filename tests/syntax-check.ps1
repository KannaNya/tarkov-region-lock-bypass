$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$scripts = Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.ps1' |
    Where-Object FullName -notmatch '\\outputs\\'
foreach ($script in $scripts) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors)
    if ($errors) {
        throw "Syntax errors in $($script.FullName): $($errors | Out-String)"
    }
    Write-Host "OK $($script.FullName)"
}

