BEGIN;

GRANT SELECT, INSERT, UPDATE
    ON ops.checklist_definitions
    TO connect_ops_writer;

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '002',
    'Allow the application writer role to discover and maintain OPUS checklist definitions'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

COMMIT;
SELECT
    current_database() AS database_name,
    current_user AS executed_by,
    has_table_privilege(
        'connect_ops_app',
        'ops.checklist_definitions',
        'INSERT'
    ) AS can_insert,
    has_table_privilege(
        'connect_ops_app',
        'ops.checklist_definitions',
        'UPDATE'
    ) AS can_update;

SELECT version, description
FROM audit.schema_migrations
WHERE version = '002';
