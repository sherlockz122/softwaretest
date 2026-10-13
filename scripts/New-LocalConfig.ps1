#requires -Version 7.2
[CmdletBinding()]
param([string] $BootstrapUsername = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$target = Join-Path $projectRoot '.env'
$lines = Get-Content -LiteralPath (Join-Path $projectRoot '.env.example')
$secrets = @('DG_MYSQL_PASSWORD','DG_MYSQL_ROOT_PASSWORD','DG_REDIS_PASSWORD','DG_SIGNING_KEY')
if ($BootstrapUsername) {
    if ($BootstrapUsername -cnotmatch '^[A-Za-z0-9_.-]{1,64}$') { throw 'Invalid explicit bootstrap username.' }
    $BootstrapUsername = $BootstrapUsername.ToLowerInvariant()
    $secrets += 'DG_BOOTSTRAP_PASSWORD'
}
$output = foreach ($line in $lines) {
    $name = ($line -split '=', 2)[0]
    if ($name -eq 'DG_BOOTSTRAP_USERNAME' -and $BootstrapUsername) {
        $name + '=' + $BootstrapUsername
    } elseif ($name -in $secrets) {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        $name + '=' + [Convert]::ToHexString($bytes)
    } else { $line }
}
# CreateNew refuses overwrite, including concurrent creation.
$stream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
try {
    $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
    try { foreach ($line in $output) { $writer.WriteLine($line) } }
    finally { $writer.Dispose() }
} finally { $stream.Dispose() }
Write-Output 'Created local .env with independent random secrets; values were not printed.'
