[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Get-LogicalBytes([string] $Path) {
    $bytes = [long]0
    $unreadable = [Collections.Generic.List[string]]::new()
    $links = [Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ bytes = $bytes; complete = $true; unreadable = @(); links = @() }
    }
    $directories = [Collections.Generic.Stack[string]]::new()
    $directories.Push($Path)
    while ($directories.Count -gt 0) {
        $directory = $directories.Pop()
        try { $items = @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) }
        catch { $unreadable.Add($directory); continue }
        foreach ($item in $items) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                $links.Add($item.FullName)
                continue
            }
            if ($item.PSIsContainer) { $directories.Push($item.FullName) }
            else { $bytes += $item.Length }
        }
    }
    return [pscustomobject]@{
        bytes = $bytes; complete = ($unreadable.Count -eq 0)
        unreadable = @($unreadable.ToArray()); links = @($links.ToArray())
    }
}

$drives = foreach ($drive in [IO.DriveInfo]::GetDrives()) {
    if ($drive.IsReady -and $drive.DriveType -eq 'Fixed') {
        [pscustomobject]@{ drive = $drive.Name; free_gib = [math]::Round($drive.AvailableFreeSpace / 1GB, 3) }
    }
}
$project = Get-LogicalBytes $projectRoot
$venv = Get-LogicalBytes (Join-Path $projectRoot '.venv')
$runtime = Get-LogicalBytes (Join-Path $projectRoot 'runtime')
[pscustomobject]@{
    measured_at_utc = [DateTime]::UtcNow.ToString('o')
    project_root = $projectRoot
    drives = @($drives)
    project_logical_bytes = $project.bytes
    venv_logical_bytes = $venv.bytes
    runtime_logical_bytes = $runtime.bytes
    measurement_complete = $project.complete -and $venv.complete -and $runtime.complete
    unreadable_directories = @($project.unreadable + $venv.unreadable + $runtime.unreadable | Sort-Object -Unique)
    excluded_reparse_points = @($project.links + $venv.links + $runtime.links | Sort-Object -Unique)
    note = 'File lengths excluding reparse points; incomplete traversal is a lower bound, not full usage. Free drive space remains independently measured. Not allocated disk usage; external Docker/WSL excluded.'
} | ConvertTo-Json -Depth 5
