[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

Push-Location $projectRoot
try {
    $gitRoot = (& git rev-parse --show-toplevel).Trim()
    $branch = (& git branch --show-current).Trim()
    $commit = (& git rev-parse --short HEAD).Trim()
    $status = @(& git status --short)
    $portOwner = Get-NetTCPConnection `
        -LocalPort 8091 `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
    $portCommand = if ($portOwner) {
        (
            Get-CimInstance Win32_Process -Filter "ProcessId=$portOwner"
        ).CommandLine
    }
    else {
        ''
    }

    [pscustomobject]@{
        ProjectRoot = $projectRoot
        GitRoot = $gitRoot
        Branch = $branch
        Commit = $commit
        DirtyFiles = $status.Count
        Python = if (Test-Path -LiteralPath $pythonPath) {
            $pythonPath
        }
        else {
            'MISSING: run python -m venv .venv and install requirements.txt'
        }
        Port8091Process = if ($portOwner) { $portOwner } else { 'available' }
        Port8091Command = if ($portCommand) { $portCommand } else { 'available' }
    } | Format-List

    if (
        [IO.Path]::GetFullPath($gitRoot).TrimEnd('\') -ne
        [IO.Path]::GetFullPath($projectRoot).TrimEnd('\')
    ) {
        throw 'PyCharm is not running from the canonical Git worktree.'
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "The project interpreter does not exist at $pythonPath"
    }
    if (
        $portCommand -and
        $portCommand.IndexOf(
            [IO.Path]::GetFullPath($projectRoot),
            [StringComparison]::OrdinalIgnoreCase
        ) -lt 0
    ) {
        throw "Port 8091 is running from another checkout: $portCommand"
    }
}
finally {
    Pop-Location
}
