[CmdletBinding()]
param(
    [string]$HostName = 'localhost',
    [int]$Port = 5432,
    [string]$AdminUser = 'postgres',
    [string]$PsqlPath = 'C:\Program Files\PostgreSQL\17\bin\psql.exe'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$bootstrapPath = Join-Path $projectRoot 'db\00_bootstrap.psql'
$schemaPath = Join-Path $projectRoot 'db\01_schema.sql'
$connectorPermissionsPath = Join-Path $projectRoot 'db\02_connector_permissions.sql'
$checklistDetailPath = Join-Path $projectRoot 'db\03_checklist_detail.sql'
$stageMappingPath = Join-Path $projectRoot 'db\04_repair_checklist_stage_mapping.sql'
$weeklyResetPath = Join-Path $projectRoot 'db\05_workflow_lifecycle_and_weekly_reset.sql'
$rawRecordSourceHashPath = Join-Path $projectRoot 'db\06_raw_record_source_hash.sql'
$transportRootBaselinePath = Join-Path $projectRoot 'db\07_transport_root_baseline.sql'
$operationalAnalyticsPath = Join-Path $projectRoot 'db\08_operational_analytics.sql'
$stockTransitDimensionsPath = Join-Path $projectRoot 'db\09_stock_transit_dimensions.sql'

if (-not (Test-Path -LiteralPath $PsqlPath)) {
    throw "psql was not found at $PsqlPath"
}

if (-not (Test-Path -LiteralPath $bootstrapPath)) {
    throw "Bootstrap script not found: $bootstrapPath"
}

if (-not (Test-Path -LiteralPath $schemaPath)) {
    throw "Schema script not found: $schemaPath"
}

if (-not (Test-Path -LiteralPath $connectorPermissionsPath)) {
    throw "Connector permissions script not found: $connectorPermissionsPath"
}

if (-not (Test-Path -LiteralPath $checklistDetailPath)) {
    throw "Checklist detail migration not found: $checklistDetailPath"
}

if (-not (Test-Path -LiteralPath $stageMappingPath)) {
    throw "Stage mapping migration not found: $stageMappingPath"
}

if (-not (Test-Path -LiteralPath $weeklyResetPath)) {
    throw "Weekly reset migration not found: $weeklyResetPath"
}

if (-not (Test-Path -LiteralPath $rawRecordSourceHashPath)) {
    throw "Raw record source hash migration not found: $rawRecordSourceHashPath"
}

if (-not (Test-Path -LiteralPath $transportRootBaselinePath)) {
    throw "Transport root baseline migration not found: $transportRootBaselinePath"
}

if (-not (Test-Path -LiteralPath $operationalAnalyticsPath)) {
    throw "Operational analytics migration not found: $operationalAnalyticsPath"
}

if (-not (Test-Path -LiteralPath $stockTransitDimensionsPath)) {
    throw "Stock and transit dimensions migration not found: $stockTransitDimensionsPath"
}

function Invoke-NumberedMigration {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $appliedResult = & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --tuples-only `
        --no-align `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --command "SELECT EXISTS (SELECT 1 FROM audit.schema_migrations WHERE version = '$Version');"

    if ($LASTEXITCODE -ne 0) {
        throw "Could not check migration $Version; psql exited with $LASTEXITCODE"
    }

    $applied = ($appliedResult | Select-Object -Last 1).Trim() -eq 't'
    if ($applied) {
        Write-Host "Skipped $Label migration $Version; already applied"
        return
    }

    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $Path

    if ($LASTEXITCODE -ne 0) {
        throw "$Label migration failed with exit code $LASTEXITCODE"
    }
}

$adminPassword = $env:OPUS_PG_ADMIN_PASSWORD
$appPassword = $env:OPUS_DB_APP_PASSWORD

if ([string]::IsNullOrWhiteSpace($adminPassword)) {
    throw 'Set OPUS_PG_ADMIN_PASSWORD in the current process before running setup.'
}

if ([string]::IsNullOrWhiteSpace($appPassword)) {
    throw 'Set OPUS_DB_APP_PASSWORD in the current process before running setup.'
}

if ($appPassword.Length -lt 24) {
    throw 'OPUS_DB_APP_PASSWORD must contain at least 24 characters.'
}

$appPasswordBase64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($appPassword)
)
$bootstrapPayload = "\set app_password_b64 '$appPasswordBase64'`n" +
    (Get-Content -Raw -LiteralPath $bootstrapPath)

$previousPgPassword = $env:PGPASSWORD
$env:PGPASSWORD = $adminPassword

try {
    $bootstrapPayload |
        & $PsqlPath `
            --no-psqlrc `
            --host $HostName `
            --port $Port `
            --username $AdminUser `
            --dbname postgres `
            --file -

    if ($LASTEXITCODE -ne 0) {
        throw "Database bootstrap failed with exit code $LASTEXITCODE"
    }

    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $schemaPath

    if ($LASTEXITCODE -ne 0) {
        throw "Schema migration failed with exit code $LASTEXITCODE"
    }

    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $connectorPermissionsPath

    if ($LASTEXITCODE -ne 0) {
        throw "Connector permissions migration failed with exit code $LASTEXITCODE"
    }

    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $checklistDetailPath

    if ($LASTEXITCODE -ne 0) {
        throw "Checklist detail migration failed with exit code $LASTEXITCODE"
    }

    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $stageMappingPath

    if ($LASTEXITCODE -ne 0) {
        throw "Stage mapping migration failed with exit code $LASTEXITCODE"
    }

    & $PsqlPath `
        --no-psqlrc `
        --set ON_ERROR_STOP=on `
        --host $HostName `
        --port $Port `
        --username $AdminUser `
        --dbname connect_logistics_ops `
        --file $weeklyResetPath

    if ($LASTEXITCODE -ne 0) {
        throw "Weekly reset migration failed with exit code $LASTEXITCODE"
    }

    Invoke-NumberedMigration `
        -Version '006' `
        -Path $rawRecordSourceHashPath `
        -Label 'Raw record source hash'
    Invoke-NumberedMigration `
        -Version '007' `
        -Path $transportRootBaselinePath `
        -Label 'Transport root baseline'
    Invoke-NumberedMigration `
        -Version '008' `
        -Path $operationalAnalyticsPath `
        -Label 'Operational analytics'
    Invoke-NumberedMigration `
        -Version '009' `
        -Path $stockTransitDimensionsPath `
        -Label 'Stock and transit dimensions'

    $verificationSql = @'
SELECT current_database() AS database_name;
SELECT nspname AS schema_name
FROM pg_namespace
WHERE nspname IN ('ops', 'ingest', 'audit')
ORDER BY nspname;
SELECT count(*) AS configured_checklists
FROM ops.checklist_definitions;
SELECT version, description
FROM audit.schema_migrations
ORDER BY version;
SELECT count(*) AS monthly_partitions
FROM pg_inherits
WHERE inhparent IN (
    'ingest.raw_records'::regclass,
    'ops.job_events'::regclass,
    'ops.transit_snapshots'::regclass
);
'@

    $verificationSql |
        & $PsqlPath `
            --no-psqlrc `
            --host $HostName `
            --port $Port `
            --username $AdminUser `
            --dbname connect_logistics_ops `
            --file -

    if ($LASTEXITCODE -ne 0) {
        throw "Database verification failed with exit code $LASTEXITCODE"
    }

    Write-Host 'Database setup completed.'
    Write-Host "Host: $HostName"
    Write-Host "Port: $Port"
    Write-Host 'Database: connect_logistics_ops'
    Write-Host 'Application role: connect_ops_app'
}
finally {
    $env:PGPASSWORD = $previousPgPassword
    $adminPassword = $null
    $appPassword = $null
    $appPasswordBase64 = $null
    $bootstrapPayload = $null
}
