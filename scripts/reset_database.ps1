[CmdletBinding()]
param(
    [string]$HostName = 'localhost',
    [int]$Port = 5432,
    [string]$AdminUser = 'postgres',
    [string]$PsqlPath = 'C:\Program Files\PostgreSQL\17\bin\psql.exe',
    [switch]$ConfirmReset
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmReset) {
    throw 'Pass -ConfirmReset to clear operational and ingestion data.'
}
if (-not (Test-Path -LiteralPath $PsqlPath)) {
    throw "psql was not found at $PsqlPath"
}

$adminPassword = $env:OPUS_PG_ADMIN_PASSWORD
if ([string]::IsNullOrWhiteSpace($adminPassword)) {
    throw 'Set OPUS_PG_ADMIN_PASSWORD in the current process before resetting data.'
}

$resetPath = Join-Path (Split-Path -Parent $PSScriptRoot) `
    'db\reset_operational_data.sql'
if (-not (Test-Path -LiteralPath $resetPath)) {
    throw "Reset script not found: $resetPath"
}

$previousPgPassword = $env:PGPASSWORD
$env:PGPASSWORD = $adminPassword
try {
    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $resetPath

    if ($LASTEXITCODE -ne 0) {
        throw "Operational data reset failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:PGPASSWORD = $previousPgPassword
    $adminPassword = $null
}
