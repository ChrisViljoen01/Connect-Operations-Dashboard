BEGIN;

SET ROLE connect_ops_owner;

ALTER TABLE ops.allocations
    ADD COLUMN IF NOT EXISTS transport_allocation_created_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_allocations_transport_root_created
    ON ops.allocations (transport_allocation_created_at, job_reference);

UPDATE ops.checklist_definitions
SET active = false,
    configured_in_minerals = false,
    metadata = metadata || jsonb_build_object(
        'excluded_from_extraction',
        true,
        'exclusion_reason',
        'Transport Allocation is the only workflow root'
    ),
    updated_at = clock_timestamp()
WHERE stage_code = 'bulk_import'
   OR lower(btrim(canonical_name))
        = 'bulk import for minerals transport allocation';

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '007',
    'Require an in-window Transport Allocation root and exclude Bulk Import workflows'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

COMMIT;
