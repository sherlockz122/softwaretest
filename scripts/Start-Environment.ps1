[CmdletBinding()]
param(
    [ValidateSet('services','full','worker')][string] $Mode = 'services',
    [string] $DockerPath = '',
    [switch] $Build
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    throw 'Create and review .env before starting: scripts/New-LocalConfig.ps1 (no default secrets).'
}
if (-not $DockerPath) {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $command) { throw 'Docker executable unavailable; supply -DockerPath with its absolute path.' }
    $DockerPath = $command.Source
}
$wrapper = Join-Path $PSScriptRoot 'Invoke-ProjectCommand.ps1'
Push-Location -LiteralPath $projectRoot
try {
    & $wrapper -FilePath $DockerPath -ArgumentList @('compose','config','--quiet')
    $arguments = @('compose')
    if ($Mode -ne 'services') { $arguments += @('--profile', $Mode) }
    $arguments += @('up','-d','--wait','--wait-timeout','180')
    if ($Build) { $arguments += '--build' }
    if ($Mode -eq 'services') { $arguments += @('mysql','redis') }
    if ($Mode -eq 'worker') { $arguments += 'worker' }
    $growth = if ($Build) { 1.5 } elseif ($Mode -eq 'services') { 1.5 } else { 0.5 }
    & $wrapper -FilePath $DockerPath -ExpectedGrowthGiB $growth -ArgumentList $arguments
} finally { Pop-Location }
