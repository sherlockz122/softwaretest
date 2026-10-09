[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
$wrapper = Join-Path $PSScriptRoot 'Invoke-ProjectCommand.ps1'
$names = @('TEMP','TMP','PIP_CACHE_DIR','UV_CACHE_DIR','npm_config_cache','HF_HOME','TORCH_HOME','PATH')
$snapshot = @{}
foreach ($name in $names) {
    foreach ($scope in @('Process','User','Machine')) {
        $snapshot["$scope/$name"] = [Environment]::GetEnvironmentVariable($name, $scope)
    }
}
function Assert-Restored {
    foreach ($name in $names) {
        foreach ($scope in @('Process','User','Machine')) {
            if ([Environment]::GetEnvironmentVariable($name, $scope) -cne $snapshot["$scope/$name"]) {
                throw "Environment changed: $scope/$name"
            }
        }
    }
}
foreach ($script in Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1') {
    $tokens = $null; $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "Syntax errors: $($script.Name)" }
}
Write-Output 'PASS PowerShell syntax'
$probe = 'import os,json,tempfile; print(json.dumps({"env":{k:os.environ[k] for k in ["TEMP","TMP","PIP_CACHE_DIR","UV_CACHE_DIR","npm_config_cache","HF_HOME","TORCH_HOME"]},"temp_dir":tempfile.gettempdir()}))'
$result = (& $wrapper -FilePath $python -ArgumentList @('-c', $probe)) | ConvertFrom-Json
$runtimePrefix = (Join-Path $projectRoot 'runtime') + [IO.Path]::DirectorySeparatorChar
foreach ($property in $result.env.PSObject.Properties) {
    $fullPath = [IO.Path]::GetFullPath($property.Value)
    if (-not $fullPath.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Child cache/temp escaped runtime: $($property.Name)"
    }
}
if ([IO.Path]::GetFullPath($result.temp_dir) -ne (Join-Path $projectRoot 'runtime/temp')) { throw 'Python tempfile directory mismatch' }
Assert-Restored
Write-Output 'PASS child cache/temp paths and environment restore on success'
$rejected = $false
try { & $wrapper -FilePath $python -ArgumentList @('-c','import sys; sys.exit(7)') }
catch { if ($_.Exception.Message -notmatch 'exit code 7') { throw }; $rejected = $true }
if (-not $rejected) { throw 'Nonzero command was not reported' }
Assert-Restored
Write-Output 'PASS nonzero exit and environment restore'
$rejected = $false
$output = @()
try { $output = @(& $wrapper -FilePath $python -StrictStorage -ReserveGiB 1000000 -ArgumentList @('-c','print("MUST_NOT_RUN")')) }
catch { if ($_.Exception.Message -notmatch 'Storage guard rejected command') { throw }; $rejected = $true }
if (-not $rejected -or $output.Count) { throw 'Low capacity did not block child execution' }
Assert-Restored
Write-Output 'PASS insufficient capacity rejects before command; environment unchanged'
$rejected = $false
$output = @()
try { $output = @(& $wrapper -FilePath $python -ExpectedGrowthGiB 1000000 -ArgumentList @('-c','print("MUST_NOT_RUN")')) }
catch { if ($_.Exception.Message -notmatch 'Project disk cannot accommodate expected growth') { throw }; $rejected = $true }
if (-not $rejected -or $output.Count) { throw 'Expected growth must block execution even outside strict mode' }
Assert-Restored
Write-Output 'PASS known insufficient growth blocks before command without strict mode; no drive fallback'
$output = @(& $wrapper -FilePath $python -ReserveGiB 1000000 -ArgumentList @('-c','print("ADVISORY_ALLOWED")') -WarningAction SilentlyContinue)
if ($output -notcontains 'ADVISORY_ALLOWED') { throw 'Advisory capacity should not block development' }
Assert-Restored
Write-Output 'PASS advisory capacity allows command; environment restored'
$storage = (& (Join-Path $PSScriptRoot 'Measure-ProjectStorage.ps1')) | ConvertFrom-Json
if ($storage.project_root -ne $projectRoot -or $storage.project_logical_bytes -le 0 -or $storage.venv_logical_bytes -le 0) { throw 'Storage report invalid' }
Write-Output 'PASS read-only storage report'
