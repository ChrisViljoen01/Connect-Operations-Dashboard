BEGIN;

SET ROLE connect_ops_owner;

UPDATE ops.checklist_definitions
SET opus_checklist_id = NULL,
    updated_at = clock_timestamp()
WHERE stage_code IN (
    'transport_allocation',
    'vehicle_inspection',
    'loading_exit',
    'staging_arrival',
    'staging_exit',
    'truck_arrival',
    'offloading_exit'
);

UPDATE ops.checklist_definitions definition
SET opus_checklist_id = mapping.opus_checklist_id,
    canonical_name = mapping.canonical_name,
    stage_order = mapping.stage_order,
    workflow_role = mapping.workflow_role,
    active = true,
    configured_in_minerals = true,
    updated_at = clock_timestamp()
FROM (
    VALUES
        (
            'transport_allocation',
            'Transport Allocation',
            1.00::numeric,
            'root',
            '4f7cd186-4bce-46eb-9d92-272e0ceded7e'::uuid
        ),
        (
            'vehicle_inspection',
            'Vehicle Inspection',
            2.10::numeric,
            'conditional',
            'e3a5c4e9-fc33-44bf-8d5f-52cdbf56aa02'::uuid
        ),
        (
            'loading_exit',
            'Loading and Exit',
            2.20::numeric,
            'conditional',
            '652d5117-d609-47f3-b093-5ad4ebcb97bf'::uuid
        ),
        (
            'staging_arrival',
            'Staging Arrival',
            3.00::numeric,
            'optional',
            'aeca9758-c49b-486e-8f60-6a29d6c3dcb2'::uuid
        ),
        (
            'staging_exit',
            'Staging Exit',
            4.00::numeric,
            'optional',
            'fe799df1-67ea-4ea9-93c6-913161422a8e'::uuid
        ),
        (
            'truck_arrival',
            'Truck Arrival',
            5.00::numeric,
            'optional',
            '7aa017a8-cb07-4bd6-afe8-37ff55c47acc'::uuid
        ),
        (
            'offloading_exit',
            'Offloading and Exit',
            6.00::numeric,
            'optional',
            '246209a9-2f68-4ef5-b174-8b06e4c014fc'::uuid
        )
) AS mapping(
    stage_code,
    canonical_name,
    stage_order,
    workflow_role,
    opus_checklist_id
)
WHERE definition.stage_code = mapping.stage_code;

WITH mapped_jobs AS (
    SELECT DISTINCT ON (instance.job_id)
        instance.job_id,
        definition.id AS checklist_definition_id
    FROM ops.checklist_instances instance
    JOIN ops.checklist_definitions definition
      ON lower(btrim(definition.canonical_name)) = lower(btrim(instance.name))
    ORDER BY instance.job_id, instance.last_seen_at DESC, instance.id DESC
)
UPDATE ops.jobs job
SET checklist_definition_id = mapped.checklist_definition_id,
    updated_at = clock_timestamp()
FROM mapped_jobs mapped
WHERE job.id = mapped.job_id
  AND job.checklist_definition_id IS DISTINCT FROM mapped.checklist_definition_id;

UPDATE ops.checklist_instances instance
SET checklist_definition_id = definition.id,
    updated_at = clock_timestamp()
FROM ops.checklist_definitions definition
WHERE lower(btrim(definition.canonical_name)) = lower(btrim(instance.name))
  AND instance.checklist_definition_id IS DISTINCT FROM definition.id;

UPDATE ops.allocations
SET opus_root_job_id = NULL
WHERE opus_root_job_id IS NOT NULL;

WITH root_jobs AS (
    SELECT DISTINCT ON (job.allocation_id)
        job.allocation_id,
        job.opus_job_id
    FROM ops.jobs job
    JOIN ops.checklist_definitions definition
      ON definition.id = job.checklist_definition_id
    WHERE definition.stage_code = 'transport_allocation'
      AND job.opus_job_id IS NOT NULL
    ORDER BY
        job.allocation_id,
        coalesce(job.last_updated_at, job.last_seen_at) DESC,
        job.id DESC
)
UPDATE ops.allocations allocation
SET opus_root_job_id = root.opus_job_id,
    updated_at = clock_timestamp()
FROM root_jobs root
WHERE allocation.id = root.allocation_id;

WITH latest_stage AS (
    SELECT DISTINCT ON (job.allocation_id)
        job.allocation_id,
        job.status,
        definition.stage_code,
        definition.stage_order,
        coalesce(job.last_updated_at, job.last_seen_at) AS source_updated_at
    FROM ops.jobs job
    JOIN ops.checklist_definitions definition
      ON definition.id = job.checklist_definition_id
    ORDER BY
        job.allocation_id,
        coalesce(job.last_updated_at, job.last_seen_at) DESC NULLS LAST,
        definition.stage_order DESC NULLS LAST,
        job.id DESC
)
UPDATE ops.allocations allocation
SET current_status = latest.status,
    current_stage_code = latest.stage_code,
    current_stage_order = latest.stage_order,
    source_updated_at = greatest(
        allocation.source_updated_at,
        latest.source_updated_at
    ),
    updated_at = clock_timestamp()
FROM latest_stage latest
WHERE allocation.id = latest.allocation_id;

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '004',
    'Repair checklist stage ownership using authoritative OPUS job checklist names'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

COMMIT;
