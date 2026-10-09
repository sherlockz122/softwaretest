[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $FilePath,
    [string[]] $ArgumentList = @(),
    [ValidateRange(6, 1000000)][double] $ReserveGiB = 6,
    [ValidateRange(0, 1000000)][double] $ExpectedGrowthGiB = 0
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($projectRoot))
$freeGiB = $drive.AvailableFreeSpace / 1GB
if ($freeGiB -lt ($ReserveGiB + $ExpectedGrowthGiB)) {
    throw ('Storage guard rejected command: {0:N2} GiB free; need {1:N2} GiB (reserve plus expected growth).' -f $freeGiB, ($ReserveGiB + $ExpectedGrowthGiB))
}
if (-not [IO.Path]::IsPathRooted($FilePath) -or -not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw 'FilePath must be an existing absolute executable path.'
}
$runtimeRoot = Join-Path $projectRoot 'runtime'
# Reject junctions/symlinks instead of silently writing to another volume.
foreach ($path in @($projectRoot, $runtimeRoot)) {
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Project/runtime directory must not be a reparse point.'
    }
}
$settings = @{
    TEMP = 'temp'; TMP = 'temp'; PIP_CACHE_DIR = 'cache/pip'
    UV_CACHE_DIR = 'cache/uv'; npm_config_cache = 'cache/npm'
    HF_HOME = 'cache/huggingface'; TORCH_HOME = 'cache/torch'
}
$previous = @{}
$commandExitCode = 0
try {
    foreach ($name in $settings.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    foreach ($name in $settings.Keys) {
        $target = Join-Path $runtimeRoot $settings[$name]
        # Check every existing ancestor below runtime, including cache subdirectories.
        $ancestor = $target
        while ($ancestor.StartsWith($runtimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Cache/temp directory must not be a reparse point.'
            }
            $ancestor = Split-Path -Parent $ancestor
        }
        $null = New-Item -ItemType Directory -Path $target -Force
        [Environment]::SetEnvironmentVariable($name, $target, 'Process')
    }
    & $FilePath @ArgumentList
    $commandExitCode = $LASTEXITCODE
} finally {
    foreach ($name in $previous.Keys) {
        if ($null -eq $previous[$name]) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
        }
    }
}
if ($commandExitCode -ne 0) { throw "Project command failed with exit code $commandExitCode." }
