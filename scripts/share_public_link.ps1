#Requires -Version 5.1
<#
.SYNOPSIS
    Starts the dashboard and publishes it on a public HTTPS link.

.DESCRIPTION
    Brings up the database, applies migrations, starts the application, then
    opens a Cloudflare quick tunnel and prints the public URL.

    The link is served from this machine, so it works only while this machine
    is on and connected, and the URL changes each time it starts. Use it for
    demos and test rounds, not as permanent hosting.

.PARAMETER Stop
    Stops the stack and removes the public link.

.EXAMPLE
    .\scripts\share_public_link.ps1
.EXAMPLE
    .\scripts\share_public_link.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

# Docker writes ordinary progress to stderr. Under $ErrorActionPreference =
# 'Stop', Windows PowerShell turns that into a terminating NativeCommandError
# even on success, so native calls are run with stderr merged and judged by
# their exit code instead.
function Invoke-Compose {
    param(
        [Parameter(Mandatory)][string[]]$ComposeArgs,
        [switch]$IgnoreExitCode
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & docker @ComposeArgs 2>&1 | Out-String
        if (-not $IgnoreExitCode -and $LASTEXITCODE -ne 0) {
            throw "docker $($ComposeArgs -join ' ') failed with exit code $LASTEXITCODE`n$output"
        }
        return $output
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

Push-Location $projectRoot
try {
    $composeArgs = @('compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.share.yml')

    if ($Stop) {
        Invoke-Compose -ComposeArgs ($composeArgs + 'down') | Out-Null
        Write-Host 'Stack stopped and the public link removed.'
        return
    }

    if (-not (Test-Path '.env')) {
        throw "No .env file found. Copy .env.example to .env and fill it in first."
    }

    $envText = Get-Content '.env' -Raw
    # Horizontal whitespace only: \s would match the newline and let the next
    # line satisfy the check, so a blank value would slip through.
    if ($envText -notmatch '(?m)^[^\S\r\n]*OPUS_APP_ACCESS_PASSWORD[^\S\r\n]*=[^\S\r\n]*\S') {
        throw @'
OPUS_APP_ACCESS_PASSWORD is not set in .env.

A public link without it would let anyone who has the URL read live
operational data and trigger extraction. Set it, then run this again.
'@
    }

    Invoke-Compose -ComposeArgs ($composeArgs + @('pull', '--quiet')) | Out-Null
    Invoke-Compose -ComposeArgs ($composeArgs + @('up', '-d')) | Out-Null

    Write-Host 'Waiting for the public link...'
    $publicUrl = $null
    foreach ($attempt in 1..30) {
        Start-Sleep -Seconds 4
        $log = Invoke-Compose -ComposeArgs ($composeArgs + @('logs', 'share')) -IgnoreExitCode
        $found = [regex]::Match($log, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($found.Success) {
            $publicUrl = $found.Value
            break
        }
    }

    if (-not $publicUrl) {
        Write-Warning 'The link did not appear in time. Inspect it with:'
        Write-Warning '  docker compose -f docker-compose.yml -f docker-compose.share.yml logs share'
        return
    }

    Write-Host ''
    Write-Host "Dashboard is live at: $publicUrl"
    Write-Host 'Share the URL and the access password with your testers.'
    Write-Host ''
    Write-Host 'The link stays up only while this machine is on and this stack is'
    Write-Host 'running, and the URL changes on every restart.'
    Write-Host ''
    Write-Host 'Stop it with: .\scripts\share_public_link.ps1 -Stop'
}
finally {
    Pop-Location
}
