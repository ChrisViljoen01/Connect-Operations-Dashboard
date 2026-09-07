BEGIN;

SET ROLE connect_ops_owner;

ALTER TABLE ingest.raw_records
    ADD COLUMN IF NOT EXISTS source_payload_sha256 bytea;

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '006',
    'Store app-controlled canonical payload hash for raw record change detection'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

COMMIT;
