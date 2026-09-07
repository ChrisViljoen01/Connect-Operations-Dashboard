BEGIN;

SET ROLE connect_ops_owner;

ALTER TABLE ops.stock_opening_balances
    ADD COLUMN IF NOT EXISTS stock_role text;

RESET ROLE;

COMMIT;

BEGIN;

SET ROLE connect_ops_owner;

ALTER TABLE ops.stock_opening_balances
    DROP CONSTRAINT IF EXISTS uq_stock_opening_balance_key;

DO $block$
DECLARE
    collision_count bigint;
BEGIN
    SELECT count(*)
    INTO collision_count
    FROM (
        SELECT
            effective_date,
            stock_role,
            normalized_location,
            order_master_id
        FROM ops.stock_opening_balances
        WHERE normalized_storage_identifier IN (
            normalized_location,
            'no loading slab',
            'no offloading slab',
            'point-level / no slab'
        )
        GROUP BY effective_date, stock_role, normalized_location, order_master_id
        HAVING count(*) > 1
    ) collisions;

    IF collision_count > 0 THEN
        RAISE EXCEPTION
            'Migration 009 found % duplicate point-level opening balance key(s). Consolidate No Slab and point-name rows before retrying.',
            collision_count;
    END IF;
END
$block$;

UPDATE ops.stock_opening_balances
SET storage_identifier = location_name
WHERE normalized_storage_identifier IN (
    'no loading slab',
    'no offloading slab',
    'point-level / no slab'
);

WITH inferred_roles AS (
    SELECT
        opening.id,
        bool_or(ledger.movement_type = 'Loading') AS has_loading,
        bool_or(ledger.movement_type = 'Offloading') AS has_offloading
    FROM ops.stock_opening_balances opening
    JOIN ops.order_master master ON master.id = opening.order_master_id
    LEFT JOIN ops.v_stock_ledger ledger
      ON lower(btrim(ledger.location_name)) = opening.normalized_location
     AND lower(btrim(ledger.storage_identifier))
         = opening.normalized_storage_identifier
     AND upper(btrim(ledger.order_reference))
         = master.normalized_order_reference
    GROUP BY opening.id
)
UPDATE ops.stock_opening_balances opening
SET stock_role = CASE
    WHEN inferred.has_loading AND NOT inferred.has_offloading THEN 'Origin'
    WHEN inferred.has_offloading AND NOT inferred.has_loading THEN 'Destination'
END
FROM inferred_roles inferred
WHERE opening.id = inferred.id
  AND opening.stock_role IS NULL;

DO $block$
DECLARE
    unresolved_count bigint;
BEGIN
    SELECT count(*)
    INTO unresolved_count
    FROM ops.stock_opening_balances
    WHERE stock_role IS NULL;

    IF unresolved_count > 0 THEN
        RAISE EXCEPTION
            'Migration 009 cannot infer Origin/Destination for % opening balance row(s). The stock_role column has been preserved; assign Origin or Destination to those rows, then retry migration 009.',
            unresolved_count;
    END IF;
END
$block$;

ALTER TABLE ops.stock_opening_balances
    ALTER COLUMN stock_role SET NOT NULL;

DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ops.stock_opening_balances'::regclass
          AND conname = 'ck_stock_opening_balance_role'
    ) THEN
        ALTER TABLE ops.stock_opening_balances
            ADD CONSTRAINT ck_stock_opening_balance_role
            CHECK (stock_role IN ('Origin', 'Destination'));
    END IF;
END
$block$;

DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ops.stock_opening_balances'::regclass
          AND conname = 'uq_stock_opening_balance_role_key'
    ) THEN
        ALTER TABLE ops.stock_opening_balances
            ADD CONSTRAINT uq_stock_opening_balance_role_key UNIQUE (
                effective_date,
                stock_role,
                normalized_location,
                normalized_storage_identifier,
                order_master_id
            );
    END IF;
END
$block$;

CREATE INDEX IF NOT EXISTS ix_stock_opening_balances_role_lookup
    ON ops.stock_opening_balances (
        stock_role,
        effective_date,
        normalized_location,
        normalized_storage_identifier,
        order_master_id
    );

DROP VIEW IF EXISTS ops.v_transit_route_register;
DROP VIEW IF EXISTS ops.v_stock_ledger;
DROP VIEW IF EXISTS ops.v_stock_reconciliation;

CREATE OR REPLACE VIEW ops.v_reference_workflow_state AS
SELECT
    a.id AS allocation_id,
    a.job_reference,
    a.transport_allocation_created_at,
    coalesce(loading.order_reference, a.order_reference) AS order_reference,
    coalesce(om.client_name, 'Unmapped client') AS client_name,
    coalesce(om.minimum_delivery_pct, 99.750) AS minimum_delivery_pct,
    lp.name AS allocation_loading_point,
    op.name AS allocation_offloading_point,
    t.name AS transporter_name,
    coalesce(loading.truck_registration, v.registration) AS truck_registration,
    coalesce(loading.truck_type, a.truck_type, v.truck_type) AS truck_type,
    d.full_name AS driver_name,
    current_attempt.job_id AS current_job_id,
    current_attempt.checklist_name AS current_checklist,
    current_attempt.stage_code AS current_stage_code,
    current_attempt.stage_order AS current_stage_order,
    current_attempt.opus_status AS current_opus_status,
    current_attempt.status_group AS current_status_group,
    current_attempt.chronology_at AS current_job_created_at,
    current_attempt.status_group IN ('Closed', 'Cancelled')
        AS stopped_after_closure,
    loading.job_id AS loading_job_id,
    loading.checklist_instance_id AS loading_checklist_instance_id,
    loading.loading_signed_off_at,
    loading.loading_point,
    coalesce(
        nullif(btrim(loading.offloading_point), ''),
        nullif(btrim(op.name), '')
    ) AS transit_destination,
    loading.loading_slab,
    loading.offloading_slab AS planned_offloading_slab,
    loading.nett_weight_tonnes AS loaded_tonnes,
    loading.validation_errors AS loading_validation_errors,
    loading.signed_off_attempt_count AS loading_signed_off_attempts,
    latest_offloading.job_id AS offloading_started_job_id,
    latest_offloading.operator_started_at AS offloading_started_at,
    offloading.job_id AS offloading_job_id,
    offloading.checklist_instance_id AS offloading_checklist_instance_id,
    offloading.offloading_signed_off_at,
    offloading.offloading_point,
    offloading.offloading_slab,
    offloading.nett_weight_tonnes AS offloaded_tonnes,
    offloading.validation_errors AS offloading_validation_errors,
    offloading.signed_off_attempt_count AS offloading_signed_off_attempts,
    coalesce((
        latest_root.status_group = 'Signed off'
        AND latest_loading.status_group = 'Signed off'
        AND loading.job_id = latest_loading.job_id
        AND current_attempt.status_group NOT IN ('Closed', 'Cancelled')
        AND (
            latest_offloading.job_id IS NULL
            OR (
                latest_offloading.status_group = 'Not started'
                AND latest_offloading.operator_started_at IS NULL
                AND latest_offloading.source_completed_at IS NULL
                AND latest_offloading.source_signed_off_at IS NULL
            )
        )
    ), false) AS in_transit,
    coalesce(
        nullif(btrim(loading.loading_point), ''),
        nullif(btrim(lp.name), '')
    ) AS transit_origin,
    CASE
        WHEN nullif(btrim(loading.loading_point), '') IS NOT NULL
            THEN 'Loading & Exit'
        WHEN nullif(btrim(lp.name), '') IS NOT NULL
            THEN 'Transport Allocation fallback'
        ELSE 'Missing'
    END AS transit_origin_source,
    CASE
        WHEN nullif(btrim(loading.offloading_point), '') IS NOT NULL
            THEN 'Loading & Exit'
        WHEN nullif(btrim(op.name), '') IS NOT NULL
            THEN 'Transport Allocation fallback'
        ELSE 'Missing'
    END AS transit_destination_source,
    (
        nullif(btrim(loading.loading_point), '') IS NULL
        OR nullif(btrim(loading.offloading_point), '') IS NULL
    ) AS transit_route_fallback,
    latest_root.opus_status AS latest_transport_allocation_status,
    latest_loading.opus_status AS latest_loading_status,
    latest_offloading.opus_status AS latest_offloading_status,
    CASE
        WHEN latest_root.job_id IS NULL
            THEN 'Missing Transport Allocation attempt'
        WHEN latest_root.status_group <> 'Signed off'
            THEN 'Latest Transport Allocation is not signed off'
        WHEN latest_loading.job_id IS NULL
            THEN 'Missing Loading and Exit attempt'
        WHEN latest_loading.status_group <> 'Signed off'
            THEN 'Latest Loading and Exit is not signed off'
        WHEN loading.job_id IS DISTINCT FROM latest_loading.job_id
            THEN 'Latest Loading and Exit has no signed-off movement fact'
        WHEN current_attempt.status_group IN ('Closed', 'Cancelled')
            THEN 'Final workflow job is closed or cancelled'
        WHEN latest_offloading.job_id IS NOT NULL
         AND (
             latest_offloading.status_group <> 'Not started'
             OR latest_offloading.operator_started_at IS NOT NULL
             OR latest_offloading.source_completed_at IS NOT NULL
             OR latest_offloading.source_signed_off_at IS NOT NULL
         )
            THEN 'Latest Offloading and Exit has started'
        ELSE NULL
    END AS transit_exclusion_reason
FROM ops.allocations a
LEFT JOIN ops.locations lp ON lp.id = a.loading_location_id
LEFT JOIN ops.locations op ON op.id = a.offloading_location_id
LEFT JOIN ops.transporters t ON t.id = a.transporter_id
LEFT JOIN ops.vehicles v ON v.id = a.vehicle_id
LEFT JOIN ops.drivers d ON d.id = a.driver_id
LEFT JOIN LATERAL (
    SELECT attempt.*
    FROM ops.v_workflow_attempts attempt
    WHERE attempt.allocation_id = a.id
      AND attempt.is_current_job
    LIMIT 1
) current_attempt ON true
LEFT JOIN LATERAL (
    SELECT
        job.id AS job_id,
        job.status AS opus_status,
        ops.normalized_job_status(job.status) AS status_group
    FROM ops.jobs job
    JOIN ops.checklist_definitions definition
      ON definition.id = job.checklist_definition_id
    WHERE job.allocation_id = a.id
      AND definition.stage_code = 'transport_allocation'
    ORDER BY
        coalesce(job.source_created_at, job.first_seen_at) DESC,
        job.id DESC
    LIMIT 1
) latest_root ON true
LEFT JOIN LATERAL (
    SELECT
        job.id AS job_id,
        job.status AS opus_status,
        ops.normalized_job_status(job.status) AS status_group
    FROM ops.jobs job
    JOIN ops.checklist_definitions definition
      ON definition.id = job.checklist_definition_id
    WHERE job.allocation_id = a.id
      AND definition.stage_code = 'loading_exit'
    ORDER BY
        coalesce(job.source_created_at, job.first_seen_at) DESC,
        job.id DESC
    LIMIT 1
) latest_loading ON true
LEFT JOIN LATERAL (
    SELECT
        attempt.job_id,
        fact.checklist_instance_id,
        coalesce(attempt.source_signed_off_at, attempt.source_completed_at)
            AS loading_signed_off_at,
        fact.loading_point,
        fact.offloading_point,
        fact.loading_slab,
        fact.offloading_slab,
        fact.order_reference,
        fact.truck_registration,
        fact.truck_type,
        fact.nett_weight_tonnes,
        fact.validation_errors,
        attempt.signed_off_attempt_count
    FROM ops.v_workflow_attempts attempt
    JOIN ops.checklist_operational_facts fact ON fact.job_id = attempt.job_id
    WHERE attempt.allocation_id = a.id
      AND attempt.stage_code = 'loading_exit'
      AND attempt.status_group = 'Signed off'
    ORDER BY
        coalesce(attempt.source_signed_off_at, attempt.source_completed_at)
            DESC NULLS LAST,
        attempt.chronology_at DESC,
        attempt.job_id DESC
    LIMIT 1
) loading ON true
LEFT JOIN ops.order_master om
  ON om.normalized_order_reference = upper(
      btrim(coalesce(loading.order_reference, a.order_reference))
  )
 AND om.active
LEFT JOIN LATERAL (
    SELECT
        job.id AS job_id,
        job.status AS opus_status,
        ops.normalized_job_status(job.status) AS status_group,
        job.operator_started_at,
        job.source_completed_at,
        job.source_signed_off_at
    FROM ops.jobs job
    JOIN ops.checklist_definitions definition
      ON definition.id = job.checklist_definition_id
    WHERE job.allocation_id = a.id
      AND definition.stage_code = 'offloading_exit'
    ORDER BY
        coalesce(job.source_created_at, job.first_seen_at) DESC,
        job.id DESC
    LIMIT 1
) latest_offloading ON true
LEFT JOIN LATERAL (
    SELECT
        attempt.job_id,
        fact.checklist_instance_id,
        coalesce(attempt.source_signed_off_at, attempt.source_completed_at)
            AS offloading_signed_off_at,
        fact.offloading_point,
        fact.offloading_slab,
        fact.nett_weight_tonnes,
        fact.validation_errors,
        attempt.signed_off_attempt_count
    FROM ops.v_workflow_attempts attempt
    JOIN ops.checklist_operational_facts fact ON fact.job_id = attempt.job_id
    WHERE attempt.allocation_id = a.id
      AND attempt.stage_code = 'offloading_exit'
      AND attempt.status_group = 'Signed off'
    ORDER BY
        coalesce(attempt.source_signed_off_at, attempt.source_completed_at)
            DESC NULLS LAST,
        attempt.chronology_at DESC,
        attempt.job_id DESC
    LIMIT 1
) offloading ON true;

CREATE OR REPLACE VIEW ops.v_transit_route_register AS
SELECT
    state.allocation_id,
    state.job_reference,
    state.transport_allocation_created_at,
    state.order_reference,
    state.client_name,
    state.transporter_name,
    state.truck_registration,
    state.truck_type,
    state.driver_name,
    state.transit_origin,
    state.transit_destination,
    state.transit_origin_source,
    state.transit_destination_source,
    state.transit_route_fallback,
    state.loaded_tonnes,
    state.loading_signed_off_at AS departed_at,
    state.current_checklist,
    state.current_opus_status,
    state.loading_signed_off_attempts,
    state.loading_validation_errors
FROM ops.v_reference_workflow_state state
WHERE state.in_transit;

CREATE OR REPLACE VIEW ops.v_stock_reconciliation AS
SELECT
    state.*,
    CASE
        WHEN lower(btrim(state.loading_slab)) = 'no loading slab'
            THEN state.loading_point
        WHEN nullif(btrim(state.loading_slab), '') IS NULL
          OR btrim(state.loading_slab) = '0'
            THEN '__MISSING_STORAGE__'
        ELSE state.loading_slab
    END AS loading_storage_identifier,
    CASE
        WHEN lower(btrim(state.offloading_slab)) = 'no offloading slab'
            THEN state.offloading_point
        WHEN nullif(btrim(state.offloading_slab), '') IS NULL
          OR btrim(state.offloading_slab) = '0'
            THEN '__MISSING_STORAGE__'
        ELSE state.offloading_slab
    END AS offloading_storage_identifier,
    CASE
        WHEN state.loaded_tonnes IS NOT NULL
         AND state.offloaded_tonnes IS NOT NULL
        THEN round(state.offloaded_tonnes - state.loaded_tonnes, 3)
    END AS variance_tonnes,
    CASE
        WHEN state.loaded_tonnes > 0
         AND state.offloaded_tonnes IS NOT NULL
        THEN round((state.offloaded_tonnes / state.loaded_tonnes) * 100.0, 3)
    END AS delivery_pct,
    CASE
        WHEN state.loaded_tonnes IS NULL THEN 'Missing loading weight'
        WHEN state.offloaded_tonnes IS NULL THEN 'Pending / in transit'
        WHEN (state.offloaded_tonnes / nullif(state.loaded_tonnes, 0)) * 100.0
             >= state.minimum_delivery_pct
            THEN 'Within tolerance'
        ELSE 'Below tolerance'
    END AS variance_status,
    coalesce(state.loading_signed_off_attempts, 0) > 1
        AS duplicate_loading_attempts,
    coalesce(state.offloading_signed_off_attempts, 0) > 1
        AS duplicate_offloading_attempts,
    nullif(btrim(state.loading_slab), '') IS NULL
        OR btrim(state.loading_slab) = '0' AS invalid_loading_slab,
    nullif(btrim(state.offloading_slab), '') IS NULL
        OR btrim(state.offloading_slab) = '0' AS invalid_offloading_slab,
    state.client_name = 'Unmapped client' AS unmapped_client,
    CASE
        WHEN lower(btrim(state.loading_slab)) = 'no loading slab'
            THEN 'Point-level / no slab'
        WHEN nullif(btrim(state.loading_slab), '') IS NULL
          OR btrim(state.loading_slab) = '0'
            THEN 'Missing slab/bay'
        ELSE state.loading_slab
    END AS loading_storage_display,
    CASE
        WHEN lower(btrim(state.offloading_slab)) = 'no offloading slab'
            THEN 'Point-level / no slab'
        WHEN nullif(btrim(state.offloading_slab), '') IS NULL
          OR btrim(state.offloading_slab) = '0'
            THEN 'Missing slab/bay'
        ELSE state.offloading_slab
    END AS offloading_storage_display,
    CASE
        WHEN lower(btrim(state.loading_slab)) = 'no loading slab'
            THEN 'Point-level'
        WHEN nullif(btrim(state.loading_slab), '') IS NULL
          OR btrim(state.loading_slab) = '0'
            THEN 'Missing'
        ELSE 'Slab / bay'
    END AS loading_storage_scope,
    CASE
        WHEN lower(btrim(state.offloading_slab)) = 'no offloading slab'
            THEN 'Point-level'
        WHEN nullif(btrim(state.offloading_slab), '') IS NULL
          OR btrim(state.offloading_slab) = '0'
            THEN 'Missing'
        ELSE 'Slab / bay'
    END AS offloading_storage_scope
FROM ops.v_reference_workflow_state state;

CREATE OR REPLACE VIEW ops.v_stock_ledger AS
SELECT
    reconciliation.allocation_id,
    reconciliation.job_reference,
    reconciliation.order_reference,
    reconciliation.client_name,
    reconciliation.loading_signed_off_at AS movement_at,
    'Loading'::text AS movement_type,
    reconciliation.loading_point AS location_name,
    reconciliation.loading_storage_identifier AS storage_identifier,
    -reconciliation.loaded_tonnes AS quantity_tonnes,
    reconciliation.truck_registration,
    reconciliation.truck_type,
    reconciliation.loading_job_id AS source_job_id,
    'Origin'::text AS stock_role,
    reconciliation.loading_storage_display AS storage_display,
    reconciliation.loading_storage_scope AS storage_scope
FROM ops.v_stock_reconciliation reconciliation
WHERE reconciliation.loaded_tonnes IS NOT NULL
  AND reconciliation.loading_signed_off_at IS NOT NULL
UNION ALL
SELECT
    reconciliation.allocation_id,
    reconciliation.job_reference,
    reconciliation.order_reference,
    reconciliation.client_name,
    reconciliation.offloading_signed_off_at AS movement_at,
    'Offloading'::text AS movement_type,
    reconciliation.offloading_point AS location_name,
    reconciliation.offloading_storage_identifier AS storage_identifier,
    reconciliation.offloaded_tonnes AS quantity_tonnes,
    reconciliation.truck_registration,
    reconciliation.truck_type,
    reconciliation.offloading_job_id AS source_job_id,
    'Destination'::text AS stock_role,
    reconciliation.offloading_storage_display AS storage_display,
    reconciliation.offloading_storage_scope AS storage_scope
FROM ops.v_stock_reconciliation reconciliation
WHERE reconciliation.offloaded_tonnes IS NOT NULL
  AND reconciliation.offloading_signed_off_at IS NOT NULL;

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '009',
    'Separate stock dimensions and enforce strict latest-attempt transit routes'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

GRANT SELECT
    ON ops.v_reference_workflow_state,
       ops.v_transit_route_register,
       ops.v_stock_reconciliation,
       ops.v_stock_ledger
    TO connect_ops_writer;

GRANT SELECT
    ON ops.v_reference_workflow_state,
       ops.v_transit_route_register,
       ops.v_stock_reconciliation,
       ops.v_stock_ledger
    TO connect_ops_reader;

COMMIT;
