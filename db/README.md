# Connect Logistics Operations database

Target PostgreSQL database:

- Host: `localhost`
- Port: `5432`
- Database: `connect_logistics_ops`
- Application login: `connect_ops_app`
- Owner role: `connect_ops_owner` (no login)
- Writer group: `connect_ops_writer` (no login)
- Reader group: `connect_ops_reader` (no login)

## Local credential

The live local application password is stored in Windows Credential Manager:

- Target: `ConnectLogisticsOps/PostgreSQL/connect_ops_app`
- User: `connect_ops_app`

The password is not stored in this project or in plaintext pgAdmin query
history. The NiceGUI application should read this credential at runtime for
local development. Use an injected environment secret for deployments.

## Apply the database

Run the setup from a PowerShell process after setting the two secrets in that
same process:

```powershell
$env:OPUS_PG_ADMIN_PASSWORD = '<postgres administrator password>'
$env:OPUS_DB_APP_PASSWORD = '<new random password of at least 24 characters>'
.\scripts\setup_database.ps1
```

The setup script passes credentials to `psql` only for the life of the process.
It does not write either password to the project.

The setup applies all migrations through
`db/09_stock_transit_dimensions.sql`. Migration 005 adds checklist lifecycle
fields without clearing data. Migration 006 adds an app-controlled source payload hash
used for reliable raw-record change detection. Migration 007 records the
qualifying Transport Allocation creation timestamp and disables Bulk Import as a
workflow root. Migration 008 adds governed order/opening-stock inputs, semantic checklist facts,
chronological workflow state, and the transit/stock analytics views. For an
existing database, apply any missing numbered migrations in order as `postgres`
or another database owner.

Migration 009 separates origin, destination, point, and slab/bay dimensions,
adds an explicit Origin/Destination role to opening balances, and replaces the
timestamp-based transit test with strict latest-attempt eligibility. It changes
analytics views and governed opening-balance keys only; it does not reset or
re-extract OPUS data.

Migration 008 backfills semantic movement facts when it is first applied. To
explicitly reconcile them again without re-extracting OPUS, run:

```powershell
.\.venv\Scripts\python.exe .\scripts\rebuild_operational_facts.py
```

For an intentional baseline rebuild, apply all migrations first and then run:

```powershell
.\.venv\Scripts\python.exe .\scripts\save_database_admin_credential.py
.\.venv\Scripts\python.exe .\scripts\administer_database.py `
    migrate-and-reset --confirm-reset
```

The reset removes only operational and ingestion rows. It preserves roles,
schema, checklist definitions, credentials, and migration history.

## Storage model

- `ops.allocations`: one business record per `ORDBULK-####` reference.
- `ops.jobs`: the current OPUS checklist-job records linked to an allocation.
- `ops.checklist_instances`: complete answered checklist instances per job.
- `ops.checklist_answers`: normalized questions and answers with rich source
    payloads retained as JSONB.
- `ops.job_events`: append-only checklist history, partitioned monthly.
- `ops.transit_snapshots`: append-only Trucks in Transit observations,
  partitioned monthly.
- `ingest.raw_records`: immutable raw OPUS payloads, partitioned monthly.
- `ingest.extraction_runs`: extraction lineage and row-count reconciliation.
- `ingest.extraction_errors`: structured job-level extraction failures.
- `ingest.source_checkpoints`: OPUS API incremental/full-sync checkpoints.
- `audit.schema_migrations`: applied schema versions.

The initial migration creates thirteen months of historical partitions and six
months ahead. The application should call:

```sql
SELECT ingest.ensure_monthly_partitions(12, 6);
```

at startup or from a monthly maintenance job.
