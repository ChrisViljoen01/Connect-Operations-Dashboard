BEGIN;

SET ROLE connect_ops_owner;

ALTER TABLE ops.jobs
    ADD COLUMN IF NOT EXISTS source_created_at timestamptz,
    ADD COLUMN IF NOT EXISTS operator_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS source_completed_at timestamptz,
    ADD COLUMN IF NOT EXISTS source_signed_off_at timestamptz,
    ADD COLUMN IF NOT EXISTS created_by_name text,
    ADD COLUMN IF NOT EXISTS operator_created boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS created_from_operator_name text;

CREATE INDEX IF NOT EXISTS ix_jobs_source_created
    ON ops.jobs (source_created_at, allocation_id);

CREATE INDEX IF NOT EXISTS ix_jobs_workflow_activity
    ON ops.jobs (
        allocation_id,
        source_created_at,
        source_signed_off_at,
        source_completed_at
    );

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '005',
    'Add checklist lifecycle and workflow activity fields'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

COMMIT;
