[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Get-LogicalBytes([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return [long]0 }
    $bytes = [long]0
    $directories = [Collections.Generic.Stack[string]]::new()
    $directories.Push($Path)
    while ($directories.Count -gt 0) {
        foreach ($item in Get-ChildItem -LiteralPath $directories.Pop() -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($item.PSIsContainer) { $directories.Push($item.FullName) }
            else { $bytes += $item.Length }
        }
    }
    return $bytes
}

$drives = foreach ($drive in [IO.DriveInfo]::GetDrives()) {
    if ($drive.IsReady -and $drive.DriveType -eq 'Fixed') {
        [pscustomobject]@{ drive = $drive.Name; free_gib = [math]::Round($drive.AvailableFreeSpace / 1GB, 3) }
    }
}
[pscustomobject]@{
    measured_at_utc = [DateTime]::UtcNow.ToString('o')
    project_root = $projectRoot
    drives = @($drives)
    project_logical_bytes = Get-LogicalBytes $projectRoot
    venv_logical_bytes = Get-LogicalBytes (Join-Path $projectRoot '.venv')
    runtime_logical_bytes = Get-LogicalBytes (Join-Path $projectRoot 'runtime')
    note = 'File lengths, excluding reparse points; not allocated disk usage. Docker/WSL outside this project are excluded.'
} | ConvertTo-Json -Depth 4
