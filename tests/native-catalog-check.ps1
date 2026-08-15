$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $root 'src\VpnGateNativeCatalog.ps1')

function Write-TestUInt32 {
    param([IO.MemoryStream]$Stream, [uint32]$Value)
    [byte[]]$bytes = @(
        [byte](($Value -shr 24) -band 0xff),
        [byte](($Value -shr 16) -band 0xff),
        [byte](($Value -shr 8) -band 0xff),
        [byte]($Value -band 0xff)
    )
    $Stream.Write($bytes, 0, $bytes.Length)
}

function Write-TestUInt64 {
    param([IO.MemoryStream]$Stream, [uint64]$Value)
    [byte[]]$bytes = [BitConverter]::GetBytes($Value)
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($bytes) }
    $Stream.Write($bytes, 0, $bytes.Length)
}

function Write-TestBytes {
    param([IO.MemoryStream]$Stream, [byte[]]$Bytes)
    if ($Bytes.Length -gt 0) { $Stream.Write($Bytes, 0, $Bytes.Length) }
}

function New-TestPack {
    param([object[]]$Elements)
    $stream = [IO.MemoryStream]::new()
    try {
        Write-TestUInt32 $stream ([uint32]$Elements.Count)
        foreach ($element in $Elements) {
            [byte[]]$nameBytes = [Text.Encoding]::ASCII.GetBytes([string]$element.Name)
            Write-TestUInt32 $stream ([uint32]($nameBytes.Length + 1))
            Write-TestBytes $stream $nameBytes
            Write-TestUInt32 $stream ([uint32]$element.Type)
            [object[]]$values = @($element.Values)
            Write-TestUInt32 $stream ([uint32]$values.Count)
            foreach ($value in $values) {
                switch ([int]$element.Type) {
                    0 { Write-TestUInt32 $stream ([uint32]$value) }
                    1 {
                        [byte[]]$data = $value
                        Write-TestUInt32 $stream ([uint32]$data.Length)
                        Write-TestBytes $stream $data
                    }
                    2 {
                        [byte[]]$text = [Text.Encoding]::UTF8.GetBytes([string]$value)
                        Write-TestUInt32 $stream ([uint32]$text.Length)
                        Write-TestBytes $stream $text
                    }
                    3 {
                        [byte[]]$text = [Text.Encoding]::UTF8.GetBytes(([string]$value) + [char]0)
                        Write-TestUInt32 $stream ([uint32]$text.Length)
                        Write-TestBytes $stream $text
                    }
                    4 { Write-TestUInt64 $stream ([uint64]$value) }
                    default { throw "Unsupported test PACK type: $($element.Type)" }
                }
            }
        }
        return ,$stream.ToArray()
    } finally {
        $stream.Dispose()
    }
}

function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$innerElements = @(
    [pscustomobject]@{ Name = 'CountryShort'; Type = 2; Values = [object[]]@('RU', 'JP') },
    [pscustomobject]@{ Name = 'CountryFull'; Type = 2; Values = [object[]]@('Russian Federation', 'Japan') },
    [pscustomobject]@{ Name = 'Fqdn'; Type = 2; Values = [object[]]@('vpn-test-ru.opengw.net', 'vpn-test-jp.opengw.net') },
    [pscustomobject]@{ Name = 'IP'; Type = 2; Values = [object[]]@('192.0.2.10', '192.0.2.20') },
    [pscustomobject]@{ Name = 'SslPorts'; Type = 2; Values = [object[]]@('443 992', '') },
    [pscustomobject]@{ Name = 'PingToJapan'; Type = 0; Values = [object[]]@([uint32]88, [uint32]12) },
    [pscustomobject]@{ Name = 'SpeedToJapan'; Type = 4; Values = [object[]]@([uint64]52000000, [uint64]100000000) },
    [pscustomobject]@{ Name = 'Score'; Type = 4; Values = [object[]]@([uint64]900001, [uint64]900002) },
    [pscustomobject]@{ Name = 'NumClients'; Type = 0; Values = [object[]]@([uint32]3, [uint32]4) }
)
[byte[]]$inner = New-TestPack -Elements $innerElements
[byte[]]$testSignature = [byte[]]::new(128)
$outerElements = @(
    [pscustomobject]@{ Name = 'data'; Type = 1; Values = [object[]]@(,$inner) },
    [pscustomobject]@{ Name = 'data_size'; Type = 0; Values = [object[]]@([uint32]$inner.Length) },
    [pscustomobject]@{ Name = 'sign'; Type = 1; Values = [object[]]@(,$testSignature) },
    [pscustomobject]@{ Name = 'soap_url'; Type = 2; Values = [object[]]@('https://example.invalid/') },
    [pscustomobject]@{ Name = 'timestamp'; Type = 4; Values = [object[]]@([uint64]1) },
    [pscustomobject]@{ Name = 'compressed'; Type = 0; Values = [object[]]@([uint32]0) }
)
[byte[]]$outer = New-TestPack -Elements $outerElements

[byte[]]$header = [byte[]]::new(0xF0)
$stamp = [datetime]::UtcNow.ToString('yyyyMMdd_HHmmss.fff', [Globalization.CultureInfo]::InvariantCulture)
[byte[]]$headerText = [Text.Encoding]::ASCII.GetBytes("[VPNGate Data File]`r`n$stamp`r`n`r`n")
[Array]::Copy($headerText, 0, $header, 0, $headerText.Length)
[byte[]]$seed = 1..20
$sha1 = [Security.Cryptography.SHA1]::Create()
try { [byte[]]$key = $sha1.ComputeHash($seed) } finally { $sha1.Dispose() }
[byte[]]$encrypted = Invoke-VgRc4 -Data $outer -Key $key
[byte[]]$fixture = [byte[]]::new($header.Length + $seed.Length + $encrypted.Length)
[Array]::Copy($header, 0, $fixture, 0, $header.Length)
[Array]::Copy($seed, 0, $fixture, $header.Length, $seed.Length)
[Array]::Copy($encrypted, 0, $fixture, $header.Length + $seed.Length, $encrypted.Length)

$fixturePath = Join-Path ([IO.Path]::GetTempPath()) ('vpngate-catalog-test-{0}.dat' -f [guid]::NewGuid().ToString('N'))
try {
    [IO.File]::WriteAllBytes($fixturePath, $fixture)
    $catalog = Read-VpnGateNativeCatalog -Path $fixturePath -MaxAgeHours 1
    Assert-Test ($catalog.RowCount -eq 2) 'Synthetic native catalog row count was not preserved.'
    Assert-Test ($catalog.Rows[0].CountryShort -eq 'RU') 'Synthetic native catalog country was not parsed.'
    Assert-Test ($catalog.Rows[0].SslPorts -eq '443 992') 'Synthetic native catalog SSL ports were not parsed.'
    Assert-Test ([uint64]$catalog.Rows[0].SpeedToJapan -eq 52000000) 'Synthetic native catalog UInt64 was not parsed.'
    Assert-Test ([int]$catalog.Rows[0].NumSessions -eq 3) 'Synthetic native catalog client count was not parsed.'
    Write-Host 'OK synthetic SoftEther native catalog fixture'
} finally {
    if (Test-Path -LiteralPath $fixturePath) { Remove-Item -LiteralPath $fixturePath -Force }
}

$installedCatalog = 'C:\Program Files\SoftEther VPN Client\VPNGate.dat'
if (Test-Path -LiteralPath $installedCatalog -PathType Leaf) {
    $catalog = Read-VpnGateNativeCatalog -Path $installedCatalog -MaxAgeHours 0
    Assert-Test ($catalog.RowCount -gt 0) 'Installed SoftEther native catalog contained no rows.'
    Assert-Test (@($catalog.Rows | Where-Object SslPorts).Count -gt 0) 'Installed SoftEther native catalog contained no SSL listeners.'
    Write-Host "OK installed SoftEther native catalog ($($catalog.RowCount) rows)"
}
