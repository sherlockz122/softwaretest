[CmdletBinding()]
param([string] $DockerPath = 'D:\Develop\DockerDesktop\resources\bin\docker.exe', [switch] $Rebuild, [switch] $Browser, [string] $NodePath = '', [string] $EvidenceDirectory = '')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runId = [Guid]::NewGuid().ToString('N')
$project = 'dg-fresh-' + $runId.Substring(0,12)
$work = Join-Path $root ('runtime/fresh/' + $runId)
$null = New-Item -ItemType Directory -Path (Join-Path $work 'scripts') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'New-LocalConfig.ps1') -Destination (Join-Path $work 'scripts/New-LocalConfig.ps1')
Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination (Join-Path $work '.env.example')
& (Join-Path $work 'scripts/New-LocalConfig.ps1') -BootstrapUsername 'fresh-admin'
function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}
$ports = @()
while ($ports.Count -lt 4) { $port = Get-FreePort; if ($port -notin $ports) { $ports += $port } }
$config = Join-Path $work '.env'
$contents = [IO.File]::ReadAllText($config).Replace('DG_MYSQL_PORT=13306', "DG_MYSQL_PORT=$($ports[0])").Replace('DG_REDIS_PORT=16379', "DG_REDIS_PORT=$($ports[1])")
[IO.File]::WriteAllText($config,$contents,[Text.UTF8Encoding]::new($false))
if ($Browser) {
    Add-Content -LiteralPath $config -Value 'DG_LOGIN_RATE_LIMIT=1000'
    if (-not $NodePath) { $NodePath = (Get-Command node -ErrorAction Stop).Source }
    if (-not $EvidenceDirectory) { $EvidenceDirectory = Join-Path $root ('runtime/acceptance/' + $runId) }
    $EvidenceDirectory = [IO.Path]::GetFullPath($EvidenceDirectory)
    $null = New-Item -ItemType Directory -Path $EvidenceDirectory -Force
}
$override = Join-Path $work 'compose.override.yaml'
@"
services:
  api:
    environment:
      DG_FRONTEND_ORIGIN: http://127.0.0.1:$($ports[3])
    ports: !override ["127.0.0.1:$($ports[2]):8000"]
  web:
    ports: !override ["127.0.0.1:$($ports[3]):8080"]
"@ | Set-Content -LiteralPath $override -Encoding utf8
$base = @('compose','--project-name',$project,'--env-file',$config,'-f',(Join-Path $root 'compose.yaml'),'-f',$override)
function Invoke-FreshCompose([string[]] $Arguments) {
    & $DockerPath @base @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Disposable environment action failed; inspect its local runtime record.' }
}
Push-Location $root
$acceptanceFailed = $false
try {
    Invoke-FreshCompose -Arguments @('config','--quiet')
    if ($Rebuild) { Invoke-FreshCompose -Arguments @('--profile','full','build','--no-cache','api','web') }
    Invoke-FreshCompose -Arguments @('up','-d','--wait','--wait-timeout','180','mysql','redis')
    Invoke-FreshCompose -Arguments @('--profile','maintenance','run','--rm','--no-deps','migrate')
    Invoke-FreshCompose -Arguments @('--profile','maintenance','run','--rm','--no-deps','bootstrap')
    Invoke-FreshCompose -Arguments @('--profile','maintenance','run','--rm','--no-deps','bootstrap')
    Invoke-FreshCompose -Arguments @('--profile','full','up','-d','--wait','--wait-timeout','180')
    $projectPython = if ($IsWindows) { Join-Path $root '.venv/Scripts/python.exe' } else { Join-Path $root '.venv/bin/python' }
    & (Join-Path $PSScriptRoot 'Invoke-ProjectCommand.ps1') -FilePath $projectPython -ArgumentList @('-m','tests.fresh_probe','--config',$config,'--base-url',"http://127.0.0.1:$($ports[3])")
    if ($Browser) {
        $browserEnv = @{ DG_E2E_ENV_FILE=$config; DG_E2E_BASE_URL="http://127.0.0.1:$($ports[3])"; DG_E2E_PROJECT=$project; DG_DOCKER_EXE=$DockerPath; DG_PYTHON_EXE=$projectPython; DG_EVIDENCE_DIR=$EvidenceDirectory }
        $previousBrowserEnv = @{}
        foreach ($name in $browserEnv.Keys) { $previousBrowserEnv[$name]=[Environment]::GetEnvironmentVariable($name,'Process'); [Environment]::SetEnvironmentVariable($name,$browserEnv[$name],'Process') }
        Push-Location (Join-Path $root 'apps/web')
        try { & (Join-Path $PSScriptRoot 'Invoke-ProjectCommand.ps1') -FilePath $NodePath -ArgumentList @('node_modules/@playwright/test/cli.js','test') }
        finally { Pop-Location; foreach ($name in $browserEnv.Keys) { [Environment]::SetEnvironmentVariable($name,$previousBrowserEnv[$name],'Process') } }
    }
    'PASS isolated project, independent random secrets, new volumes and idempotent seed' | Set-Content (Join-Path $work 'result.txt')
} catch {
    $acceptanceFailed = $true
    Write-Error 'Isolated acceptance failed; inspect its local test results.' -ErrorAction Continue
} finally {
    # Only this uniquely named acceptance project; never the wang development volumes.
    if ($project -notmatch '^dg-fresh-[0-9a-f]{12}$') { throw 'Disposable cleanup scope invalid' }
    try { Invoke-FreshCompose -Arguments @('--profile','full','--profile','maintenance','down','--volumes','--remove-orphans') }
    finally { Pop-Location }
}
if ($acceptanceFailed) { exit 1 }
