BEGIN;

SET ROLE connect_ops_owner;

CREATE OR REPLACE FUNCTION ops.normalized_job_status(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $function$
    SELECT CASE
        WHEN lower(coalesce(value, '')) ~ '(signed[ -]?off)' THEN 'Signed off'
        WHEN lower(coalesce(value, '')) ~ '(cancelled|canceled)' THEN 'Cancelled'
        WHEN lower(coalesce(value, '')) ~ '(job closed|closed)' THEN 'Closed'
        WHEN lower(coalesce(value, '')) ~ '(under review|review)' THEN 'Under review'
        WHEN lower(coalesce(value, '')) ~ '(in progress)' THEN 'In progress'
        WHEN lower(coalesce(value, '')) ~ '(not started|pending start)' THEN 'Not started'
        ELSE 'Other'
    END
$function$;

CREATE OR REPLACE FUNCTION ops.normalized_question(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $function$
    SELECT lower(
        btrim(
            regexp_replace(
                coalesce(value, ''),
                '^[[:space:]]*[0-9]+([.][0-9]+)*[.)]?[[:space:]]*',
                ''
            )
        )
    )
$function$;

CREATE OR REPLACE FUNCTION ops.nett_weight_tonnes(value text)
RETURNS numeric
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $function$
DECLARE
    cleaned text;
    parsed numeric;
BEGIN
    cleaned := replace(btrim(coalesce(value, '')), ',', '.');
    IF cleaned = '' OR cleaned !~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN
        RETURN NULL;
    END IF;
    parsed := cleaned::numeric;
    -- Current OPUS Nett Weight answers are whole-number kilograms (e.g. 36750).
    -- Preserve compatibility with any future direct-tonne answers below 1,000.
    IF abs(parsed) >= 1000 THEN
        RETURN round(parsed / 1000.0, 3);
    END IF;
    RETURN round(parsed, 3);
END
$function$;

CREATE TABLE IF NOT EXISTS ingest.control_workbook_imports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    original_filename text NOT NULL,
    file_sha256 bytea NOT NULL,
    status text NOT NULL,
    order_rows integer NOT NULL DEFAULT 0,
    opening_balance_rows integer NOT NULL DEFAULT 0,
    replaced_order_rows integer NOT NULL DEFAULT 0,
    replaced_balance_rows integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,
    imported_by text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_at timestamptz,
    CONSTRAINT ck_control_workbook_import_status CHECK (
        status IN ('validated', 'applied', 'rejected')
    )
);

CREATE INDEX IF NOT EXISTS ix_control_workbook_imports_created
    ON ingest.control_workbook_imports (created_at DESC);

CREATE TABLE IF NOT EXISTS ingest.control_workbook_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id bigint NOT NULL
        REFERENCES ingest.control_workbook_imports(id) ON DELETE CASCADE,
    sheet_name text NOT NULL,
    row_number integer NOT NULL,
    row_key text,
    action text NOT NULL,
    error_message text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_control_workbook_row_action CHECK (
        action IN ('inserted', 'replaced', 'rejected')
    )
);

CREATE INDEX IF NOT EXISTS ix_control_workbook_rows_import
    ON ingest.control_workbook_rows (import_id, sheet_name, row_number);

CREATE TABLE IF NOT EXISTS ops.order_master (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_reference text NOT NULL,
    normalized_order_reference text GENERATED ALWAYS AS (
        upper(btrim(order_reference))
    ) STORED,
    client_name text NOT NULL,
    minimum_delivery_pct numeric(7, 3) NOT NULL DEFAULT 99.750,
    import_id bigint
        REFERENCES ingest.control_workbook_imports(id) ON DELETE SET NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_order_master_reference UNIQUE (normalized_order_reference),
    CONSTRAINT ck_order_master_client CHECK (btrim(client_name) <> ''),
    CONSTRAINT ck_order_master_delivery_pct CHECK (
        minimum_delivery_pct >= 0 AND minimum_delivery_pct <= 200
    )
);

CREATE INDEX IF NOT EXISTS ix_order_master_client
    ON ops.order_master (lower(btrim(client_name)), normalized_order_reference);

CREATE TABLE IF NOT EXISTS ops.stock_opening_balances (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effective_date date NOT NULL,
    location_name text NOT NULL,
    normalized_location text GENERATED ALWAYS AS (
        lower(btrim(location_name))
    ) STORED,
    storage_identifier text NOT NULL,
    normalized_storage_identifier text GENERATED ALWAYS AS (
        lower(btrim(storage_identifier))
    ) STORED,
    order_master_id bigint NOT NULL
        REFERENCES ops.order_master(id) ON DELETE RESTRICT,
    opening_tonnes numeric(16, 3) NOT NULL,
    import_id bigint
        REFERENCES ingest.control_workbook_imports(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_stock_opening_balance_key UNIQUE (
        effective_date,
        normalized_location,
        normalized_storage_identifier,
        order_master_id
    ),
    CONSTRAINT ck_stock_opening_location CHECK (btrim(location_name) <> ''),
    CONSTRAINT ck_stock_opening_storage CHECK (btrim(storage_identifier) <> ''),
    CONSTRAINT ck_stock_opening_tonnes CHECK (opening_tonnes >= 0)
);

CREATE INDEX IF NOT EXISTS ix_stock_opening_balances_lookup
    ON ops.stock_opening_balances (
        effective_date,
        normalized_location,
        normalized_storage_identifier,
        order_master_id
    );

CREATE TABLE IF NOT EXISTS ops.checklist_operational_facts (
    checklist_instance_id bigint PRIMARY KEY
        REFERENCES ops.checklist_instances(id) ON DELETE CASCADE,
    job_id bigint NOT NULL REFERENCES ops.jobs(id) ON DELETE CASCADE,
    allocation_id bigint NOT NULL REFERENCES ops.allocations(id) ON DELETE CASCADE,
    checklist_name text NOT NULL,
    order_reference text,
    loading_point text,
    offloading_point text,
    loading_slab text,
    offloading_slab text,
    truck_registration text,
    truck_type text,
    nett_weight_raw text,
    nett_weight_tonnes numeric(16, 3),
    validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_updated_at timestamptz,
    refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_checklist_operational_facts_name
    ON ops.checklist_operational_facts (checklist_name, source_updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_checklist_operational_facts_job
    ON ops.checklist_operational_facts (job_id);

CREATE INDEX IF NOT EXISTS ix_checklist_operational_facts_order
    ON ops.checklist_operational_facts (upper(btrim(order_reference)));

CREATE INDEX IF NOT EXISTS ix_checklist_operational_facts_route
    ON ops.checklist_operational_facts (
        lower(btrim(loading_point)),
        lower(btrim(offloading_point))
    );

CREATE OR REPLACE FUNCTION ops.refresh_operational_facts(p_job_id bigint DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ops
AS $function$
DECLARE
    refreshed_count integer;
BEGIN
    IF p_job_id IS NULL THEN
        DELETE FROM ops.checklist_operational_facts;
    ELSE
        DELETE FROM ops.checklist_operational_facts
        WHERE job_id = p_job_id;
    END IF;

    WITH answer_values AS (
        SELECT
            ci.id AS checklist_instance_id,
            ci.job_id,
            ci.allocation_id,
            ci.name AS checklist_name,
            ci.source_updated_at,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'order number'
            ) AS order_reference,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'loading point'
            ) AS loading_point,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'offloading point'
            ) AS offloading_point,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'loading slab'
            ) AS loading_slab,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'offloading slab'
            ) AS offloading_slab,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'truck registration'
            ) AS truck_registration,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'truck type'
            ) AS truck_type,
            max(answer.value) FILTER (
                WHERE ops.normalized_question(ca.question) = 'nett weight'
            ) AS nett_weight_raw
        FROM ops.checklist_instances ci
        JOIN ops.checklist_answers ca ON ca.checklist_instance_id = ci.id
        CROSS JOIN LATERAL (
            SELECT coalesce(
                nullif(btrim(ca.answer_text), ''),
                nullif(btrim(ca.unformatted_answer), ''),
                nullif(btrim(ca.report_formatted_answer), ''),
                nullif(btrim(ca.text_value), '')
            ) AS value
        ) answer
        WHERE ci.name IN ('Loading and Exit', 'Offloading and Exit')
          AND (p_job_id IS NULL OR ci.job_id = p_job_id)
        GROUP BY ci.id
    ),
    shaped AS (
        SELECT
            values.*,
            coalesce(values.order_reference, a.order_reference) AS effective_order,
            coalesce(values.truck_registration, v.registration) AS effective_registration,
            coalesce(values.truck_type, a.truck_type, v.truck_type) AS effective_truck_type,
            ops.nett_weight_tonnes(values.nett_weight_raw) AS parsed_weight
        FROM answer_values values
        JOIN ops.allocations a ON a.id = values.allocation_id
        LEFT JOIN ops.vehicles v ON v.id = a.vehicle_id
    )
    INSERT INTO ops.checklist_operational_facts (
        checklist_instance_id,
        job_id,
        allocation_id,
        checklist_name,
        order_reference,
        loading_point,
        offloading_point,
        loading_slab,
        offloading_slab,
        truck_registration,
        truck_type,
        nett_weight_raw,
        nett_weight_tonnes,
        validation_errors,
        source_updated_at,
        refreshed_at
    )
    SELECT
        shaped.checklist_instance_id,
        shaped.job_id,
        shaped.allocation_id,
        shaped.checklist_name,
        shaped.effective_order,
        shaped.loading_point,
        shaped.offloading_point,
        shaped.loading_slab,
        shaped.offloading_slab,
        shaped.effective_registration,
        shaped.effective_truck_type,
        shaped.nett_weight_raw,
        CASE
            WHEN shaped.parsed_weight > 0 AND shaped.parsed_weight <= 100
            THEN shaped.parsed_weight
        END,
        to_jsonb(array_remove(ARRAY[
            CASE
                WHEN nullif(btrim(shaped.nett_weight_raw), '') IS NULL
                THEN 'missing_nett_weight'
            END,
            CASE
                WHEN nullif(btrim(shaped.nett_weight_raw), '') IS NOT NULL
                 AND shaped.parsed_weight IS NULL
                THEN 'invalid_nett_weight'
            END,
            CASE
                WHEN shaped.parsed_weight IS NOT NULL
                 AND (
                     shaped.parsed_weight <= 0
                     OR shaped.parsed_weight > 100
                 )
                THEN 'implausible_nett_weight'
            END,
            CASE
                WHEN nullif(btrim(shaped.loading_point), '') IS NULL
                THEN 'missing_loading_point'
            END,
            CASE
                WHEN nullif(btrim(shaped.offloading_point), '') IS NULL
                THEN 'missing_offloading_point'
            END,
            CASE
                WHEN nullif(btrim(shaped.loading_slab), '') IS NULL
                  OR btrim(shaped.loading_slab) = '0'
                THEN 'invalid_loading_slab'
            END,
            CASE
                WHEN nullif(btrim(shaped.offloading_slab), '') IS NULL
                  OR btrim(shaped.offloading_slab) = '0'
                THEN 'invalid_offloading_slab'
            END,
            CASE
                WHEN nullif(btrim(shaped.effective_order), '') IS NULL
                THEN 'missing_order_reference'
            END,
            CASE
                WHEN nullif(btrim(shaped.effective_registration), '') IS NULL
                THEN 'missing_truck_registration'
            END
        ], NULL)),
        shaped.source_updated_at,
        clock_timestamp()
    FROM shaped;

    GET DIAGNOSTICS refreshed_count = ROW_COUNT;
    RETURN refreshed_count;
END
$function$;

CREATE OR REPLACE VIEW ops.v_workflow_attempts AS
WITH attempts AS (
    SELECT
        a.id AS allocation_id,
        a.job_reference,
        a.transport_allocation_created_at,
        a.order_reference,
        j.id AS job_id,
        j.opus_job_id,
        j.checklist_definition_id,
        cd.canonical_name AS checklist_name,
        cd.stage_code,
        cd.stage_order,
        j.status AS opus_status,
        ops.normalized_job_status(j.status) AS status_group,
        j.status_detail,
        j.operator_name,
        j.source_created_at,
        j.operator_started_at,
        j.source_completed_at,
        j.source_signed_off_at,
        coalesce(j.source_created_at, j.first_seen_at) AS chronology_at,
        row_number() OVER (
            PARTITION BY a.id, cd.stage_code
            ORDER BY coalesce(j.source_created_at, j.first_seen_at), j.id
        ) AS attempt_sequence,
        count(*) OVER (
            PARTITION BY a.id, cd.stage_code
        ) AS attempt_count,
        count(*) FILTER (
            WHERE ops.normalized_job_status(j.status) = 'Signed off'
        ) OVER (
            PARTITION BY a.id, cd.stage_code
        ) AS signed_off_attempt_count,
        row_number() OVER (
            PARTITION BY a.id
            ORDER BY coalesce(j.source_created_at, j.first_seen_at), j.id
        ) AS workflow_sequence,
        row_number() OVER (
            PARTITION BY a.id
            ORDER BY coalesce(j.source_created_at, j.first_seen_at) DESC, j.id DESC
        ) = 1 AS is_current_job,
        lead(j.id) OVER (
            PARTITION BY a.id
            ORDER BY coalesce(j.source_created_at, j.first_seen_at), j.id
        ) IS NOT NULL AS has_later_job
    FROM ops.allocations a
    JOIN ops.jobs j ON j.allocation_id = a.id
    JOIN ops.checklist_definitions cd ON cd.id = j.checklist_definition_id
    WHERE cd.stage_code <> 'bulk_import'
)
SELECT *
FROM attempts;

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
    loading.offloading_point AS transit_destination,
    loading.loading_slab,
    loading.offloading_slab AS planned_offloading_slab,
    loading.nett_weight_tonnes AS loaded_tonnes,
    loading.validation_errors AS loading_validation_errors,
    loading.signed_off_attempt_count AS loading_signed_off_attempts,
    offloading_started.job_id AS offloading_started_job_id,
    offloading_started.offloading_started_at,
    offloading.job_id AS offloading_job_id,
    offloading.checklist_instance_id AS offloading_checklist_instance_id,
    offloading.offloading_signed_off_at,
    offloading.offloading_point,
    offloading.offloading_slab,
    offloading.nett_weight_tonnes AS offloaded_tonnes,
    offloading.validation_errors AS offloading_validation_errors,
    offloading.signed_off_attempt_count AS offloading_signed_off_attempts,
    (
        loading.job_id IS NOT NULL
        AND current_attempt.status_group NOT IN ('Closed', 'Cancelled')
        AND (
            offloading_started.offloading_started_at IS NULL
            OR offloading_started.offloading_started_at < loading.loading_signed_off_at
        )
    ) AS in_transit
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
        coalesce(attempt.source_signed_off_at, attempt.source_completed_at) DESC NULLS LAST,
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
        attempt.job_id,
        attempt.operator_started_at AS offloading_started_at
    FROM ops.v_workflow_attempts attempt
    WHERE attempt.allocation_id = a.id
      AND attempt.stage_code = 'offloading_exit'
    ORDER BY attempt.chronology_at DESC, attempt.job_id DESC
    LIMIT 1
) offloading_started ON true
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
        coalesce(attempt.source_signed_off_at, attempt.source_completed_at) DESC NULLS LAST,
        attempt.chronology_at DESC,
        attempt.job_id DESC
    LIMIT 1
) offloading ON true;

CREATE OR REPLACE VIEW ops.v_stock_reconciliation AS
SELECT
    state.*,
    CASE
        WHEN lower(btrim(state.loading_slab)) = 'no loading slab'
            THEN state.loading_point
        WHEN nullif(btrim(state.loading_slab), '') IS NULL
          OR btrim(state.loading_slab) = '0'
            THEN NULL
        ELSE state.loading_slab
    END AS loading_storage_identifier,
    CASE
        WHEN lower(btrim(state.offloading_slab)) = 'no offloading slab'
            THEN state.offloading_point
        WHEN nullif(btrim(state.offloading_slab), '') IS NULL
          OR btrim(state.offloading_slab) = '0'
            THEN NULL
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
    state.client_name = 'Unmapped client' AS unmapped_client
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
    reconciliation.loading_job_id AS source_job_id
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
    reconciliation.offloading_job_id AS source_job_id
FROM ops.v_stock_reconciliation reconciliation
WHERE reconciliation.offloaded_tonnes IS NOT NULL
  AND reconciliation.offloading_signed_off_at IS NOT NULL;

DROP TRIGGER IF EXISTS trg_order_master_updated_at ON ops.order_master;
CREATE TRIGGER trg_order_master_updated_at
BEFORE UPDATE ON ops.order_master
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_stock_opening_balances_updated_at
    ON ops.stock_opening_balances;
CREATE TRIGGER trg_stock_opening_balances_updated_at
BEFORE UPDATE ON ops.stock_opening_balances
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

SELECT ops.refresh_operational_facts(NULL);

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '008',
    'Add governed operational analytics, workflow state, transit and stock facts'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON ops.order_master,
       ops.stock_opening_balances
    TO connect_ops_writer;

GRANT SELECT
    ON ops.checklist_operational_facts,
       ops.v_workflow_attempts,
       ops.v_reference_workflow_state,
       ops.v_stock_reconciliation,
       ops.v_stock_ledger
    TO connect_ops_writer;

GRANT SELECT, INSERT, UPDATE
    ON ingest.control_workbook_imports,
       ingest.control_workbook_rows
    TO connect_ops_writer;

GRANT USAGE, SELECT
    ON SEQUENCE ops.order_master_id_seq,
       ops.stock_opening_balances_id_seq,
       ingest.control_workbook_imports_id_seq,
       ingest.control_workbook_rows_id_seq
    TO connect_ops_writer;

GRANT EXECUTE ON FUNCTION ops.refresh_operational_facts(bigint)
    TO connect_ops_writer;

GRANT SELECT
    ON ops.order_master,
       ops.stock_opening_balances,
       ops.checklist_operational_facts,
       ops.v_workflow_attempts,
       ops.v_reference_workflow_state,
       ops.v_stock_reconciliation,
       ops.v_stock_ledger,
       ingest.control_workbook_imports,
       ingest.control_workbook_rows
    TO connect_ops_reader;

COMMIT;
