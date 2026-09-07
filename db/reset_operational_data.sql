BEGIN;

SET ROLE connect_ops_owner;

SELECT pg_advisory_xact_lock(
    hashtext('connect_logistics_opus_detail_sync')
);

TRUNCATE TABLE
    ingest.extraction_errors,
    ingest.raw_records,
    ops.job_events,
    ops.transit_snapshots,
    ops.checklist_answers,
    ops.checklist_instances,
    ops.jobs,
    ops.allocations,
    ops.vehicles,
    ops.drivers,
    ops.locations,
    ops.transporters,
    ingest.source_checkpoints,
    ingest.extraction_runs
RESTART IDENTITY CASCADE;

UPDATE ops.checklist_definitions
SET active = false,
    configured_in_minerals = false,
    updated_at = clock_timestamp()
WHERE stage_code = 'bulk_import'
   OR lower(btrim(canonical_name))
        = 'bulk import for minerals transport allocation';

RESET ROLE;

COMMIT;

SELECT
    (SELECT count(*) FROM ops.allocations) AS allocations,
    (SELECT count(*) FROM ops.jobs) AS jobs,
    (SELECT count(*) FROM ops.checklist_instances) AS checklist_instances,
    (SELECT count(*) FROM ops.checklist_answers) AS checklist_answers,
    (SELECT count(*) FROM ingest.raw_records) AS raw_records,
    (SELECT count(*) FROM ingest.extraction_runs) AS extraction_runs;
