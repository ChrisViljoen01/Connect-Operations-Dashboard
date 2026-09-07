CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

SET ROLE connect_ops_owner;

CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION connect_ops_owner;
CREATE SCHEMA IF NOT EXISTS ingest AUTHORIZATION connect_ops_owner;
CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION connect_ops_owner;

CREATE TABLE IF NOT EXISTS audit.schema_migrations (
    version text PRIMARY KEY,
    description text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION ops.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$function$;

CREATE TABLE IF NOT EXISTS ops.locations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    normalized_name text GENERATED ALWAYS AS (lower(btrim(name))) STORED,
    location_type text,
    opus_site_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_locations_normalized_name UNIQUE (normalized_name)
);

CREATE TABLE IF NOT EXISTS ops.transporters (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opus_transporter_code text,
    name text NOT NULL,
    normalized_name text GENERATED ALWAYS AS (lower(btrim(name))) STORED,
    active boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_transporters_normalized_name UNIQUE (normalized_name),
    CONSTRAINT uq_transporters_opus_code UNIQUE (opus_transporter_code)
);

CREATE TABLE IF NOT EXISTS ops.vehicles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    registration text NOT NULL,
    normalized_registration text GENERATED ALWAYS AS (
        upper(regexp_replace(registration, '[^A-Za-z0-9]', '', 'g'))
    ) STORED,
    transporter_id bigint REFERENCES ops.transporters(id),
    truck_type text,
    active boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_vehicles_registration UNIQUE (normalized_registration)
);

CREATE TABLE IF NOT EXISTS ops.drivers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL,
    normalized_name text GENERATED ALWAYS AS (lower(btrim(full_name))) STORED,
    external_reference text,
    active boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_drivers_external_reference
    ON ops.drivers (external_reference)
    WHERE external_reference IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_drivers_normalized_name
    ON ops.drivers USING gin (normalized_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS ops.checklist_definitions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opus_checklist_id uuid,
    canonical_name text NOT NULL,
    stage_code text NOT NULL,
    stage_order numeric(6, 2),
    workflow_role text NOT NULL DEFAULT 'optional',
    active boolean NOT NULL DEFAULT true,
    configured_in_minerals boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_checklist_stage_code UNIQUE (stage_code),
    CONSTRAINT uq_checklist_canonical_name UNIQUE (canonical_name),
    CONSTRAINT uq_checklist_opus_id UNIQUE (opus_checklist_id)
);

CREATE TABLE IF NOT EXISTS ops.workflow_edges (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_checklist_id bigint NOT NULL REFERENCES ops.checklist_definitions(id),
    target_checklist_id bigint NOT NULL REFERENCES ops.checklist_definitions(id),
    sequence_no integer,
    condition_expression text,
    condition_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    assignment_method text,
    assigned_role text,
    start_timing text,
    due_rule text,
    published boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_workflow_edge UNIQUE (
        source_checklist_id,
        target_checklist_id,
        sequence_no,
        condition_expression
    )
);

CREATE TABLE IF NOT EXISTS ops.allocations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_reference text NOT NULL,
    opus_root_job_id uuid,
    transport_allocation_created_at timestamptz,
    parcel_reference text,
    order_reference text,
    booked_at timestamptz,
    loading_location_id bigint REFERENCES ops.locations(id),
    offloading_location_id bigint REFERENCES ops.locations(id),
    transporter_id bigint REFERENCES ops.transporters(id),
    vehicle_id bigint REFERENCES ops.vehicles(id),
    driver_id bigint REFERENCES ops.drivers(id),
    truck_type text,
    current_status text,
    current_stage_code text,
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_updated_at timestamptz,
    raw_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_allocations_job_reference UNIQUE (job_reference),
    CONSTRAINT ck_allocations_job_reference
        CHECK (job_reference ~ '^ORDBULK-[0-9]+$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_allocations_opus_root_job_id
    ON ops.allocations (opus_root_job_id)
    WHERE opus_root_job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_allocations_last_seen_at
    ON ops.allocations (last_seen_at DESC);

CREATE INDEX IF NOT EXISTS ix_allocations_transport_root_created
    ON ops.allocations (transport_allocation_created_at, job_reference);

CREATE INDEX IF NOT EXISTS ix_allocations_route
    ON ops.allocations (loading_location_id, offloading_location_id);

CREATE INDEX IF NOT EXISTS ix_allocations_vehicle
    ON ops.allocations (vehicle_id, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS ix_allocations_raw_snapshot
    ON ops.allocations USING gin (raw_snapshot jsonb_path_ops);

CREATE TABLE IF NOT EXISTS ingest.extraction_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_key uuid NOT NULL DEFAULT gen_random_uuid(),
    source_name text NOT NULL,
    extraction_scope text NOT NULL,
    filter_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running',
    rows_seen bigint NOT NULL DEFAULT 0,
    rows_inserted bigint NOT NULL DEFAULT 0,
    rows_updated bigint NOT NULL DEFAULT 0,
    rows_rejected bigint NOT NULL DEFAULT 0,
    error_message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_extraction_runs_run_key UNIQUE (run_key),
    CONSTRAINT ck_extraction_runs_status
        CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS ix_extraction_runs_source_started
    ON ingest.extraction_runs (source_name, started_at DESC);

CREATE TABLE IF NOT EXISTS ingest.source_checkpoints (
    source_name text NOT NULL,
    checkpoint_key text NOT NULL,
    checkpoint_value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_name, checkpoint_key)
);

CREATE TABLE IF NOT EXISTS ops.jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opus_job_id uuid,
    allocation_id bigint NOT NULL REFERENCES ops.allocations(id) ON DELETE CASCADE,
    checklist_definition_id bigint REFERENCES ops.checklist_definitions(id),
    job_reference text NOT NULL,
    status text,
    status_detail text,
    expected_start_at timestamptz,
    last_updated_at timestamptz,
    due_at timestamptz,
    site_name text,
    stock_name text,
    operator_name text,
    context_user_name text,
    workflow_parent_job_id uuid,
    raw_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_jobs_reference
        CHECK (job_reference ~ '^ORDBULK-[0-9]+$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_opus_job_id
    ON ops.jobs (opus_job_id)
    WHERE opus_job_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_natural_event
    ON ops.jobs (
        allocation_id,
        checklist_definition_id,
        expected_start_at
    )
    WHERE opus_job_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_jobs_allocation_stage
    ON ops.jobs (allocation_id, checklist_definition_id, expected_start_at);

CREATE INDEX IF NOT EXISTS ix_jobs_status_expected_start
    ON ops.jobs (status, expected_start_at DESC);

CREATE INDEX IF NOT EXISTS ix_jobs_last_updated
    ON ops.jobs (last_updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_jobs_raw_attributes
    ON ops.jobs USING gin (raw_attributes jsonb_path_ops);

CREATE TABLE IF NOT EXISTS ingest.raw_records (
    captured_at timestamptz NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    extraction_run_id bigint REFERENCES ingest.extraction_runs(id),
    source_name text NOT NULL,
    source_record_key text,
    record_type text NOT NULL,
    payload jsonb NOT NULL,
    payload_sha256 bytea NOT NULL,
    processing_status text NOT NULL DEFAULT 'pending',
    processing_error text,
    processed_at timestamptz,
    PRIMARY KEY (captured_at, id)
) PARTITION BY RANGE (captured_at);

CREATE OR REPLACE FUNCTION ingest.set_raw_record_hash()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.payload_sha256 := digest(
        convert_to(NEW.payload::text, 'UTF8'),
        'sha256'
    );
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS trg_raw_records_payload_hash ON ingest.raw_records;
CREATE TRIGGER trg_raw_records_payload_hash
BEFORE INSERT OR UPDATE OF payload ON ingest.raw_records
FOR EACH ROW EXECUTE FUNCTION ingest.set_raw_record_hash();

CREATE INDEX IF NOT EXISTS ix_raw_records_source_key
    ON ingest.raw_records (source_name, source_record_key, captured_at DESC);

CREATE INDEX IF NOT EXISTS ix_raw_records_run
    ON ingest.raw_records (extraction_run_id, captured_at);

CREATE INDEX IF NOT EXISTS ix_raw_records_capture_brin
    ON ingest.raw_records USING brin (captured_at);

CREATE INDEX IF NOT EXISTS ix_raw_records_payload
    ON ingest.raw_records USING gin (payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS ops.job_events (
    observed_at timestamptz NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    extraction_run_id bigint REFERENCES ingest.extraction_runs(id),
    allocation_id bigint NOT NULL REFERENCES ops.allocations(id) ON DELETE CASCADE,
    job_id bigint REFERENCES ops.jobs(id) ON DELETE CASCADE,
    checklist_definition_id bigint REFERENCES ops.checklist_definitions(id),
    event_type text NOT NULL,
    event_at timestamptz,
    status text,
    operator_name text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (observed_at, id)
) PARTITION BY RANGE (observed_at);

CREATE INDEX IF NOT EXISTS ix_job_events_allocation_time
    ON ops.job_events (allocation_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_events_job_time
    ON ops.job_events (job_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_events_stage_time
    ON ops.job_events (checklist_definition_id, event_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_events_observed_brin
    ON ops.job_events USING brin (observed_at);

CREATE TABLE IF NOT EXISTS ops.transit_snapshots (
    captured_at timestamptz NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    extraction_run_id bigint REFERENCES ingest.extraction_runs(id),
    allocation_id bigint REFERENCES ops.allocations(id) ON DELETE CASCADE,
    job_reference text NOT NULL,
    date_booked timestamptz,
    parcel_reference text,
    order_reference text,
    loading_point text,
    offloading_point text,
    transporter_name text,
    truck_registration text,
    truck_type text,
    driver_name text,
    vehicle_inspection_at timestamptz,
    loading_exit_at timestamptz,
    staging_arrival_at timestamptz,
    staging_exit_at timestamptz,
    truck_arrival_at timestamptz,
    offloading_exit_at timestamptz,
    opus_job_detail_url text,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (captured_at, id),
    CONSTRAINT ck_transit_snapshot_reference
        CHECK (job_reference ~ '^ORDBULK-[0-9]+$')
) PARTITION BY RANGE (captured_at);

CREATE INDEX IF NOT EXISTS ix_transit_snapshots_reference_time
    ON ops.transit_snapshots (job_reference, captured_at DESC);

CREATE INDEX IF NOT EXISTS ix_transit_snapshots_allocation_time
    ON ops.transit_snapshots (allocation_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS ix_transit_snapshots_capture_brin
    ON ops.transit_snapshots USING brin (captured_at);

CREATE OR REPLACE FUNCTION ingest.ensure_monthly_partitions(
    p_months_back integer DEFAULT 12,
    p_months_ahead integer DEFAULT 6
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ingest, ops
AS $function$
DECLARE
    v_month date;
    v_next_month date;
    v_schema text;
    v_parent text;
    v_partition text;
BEGIN
    IF p_months_back < 0 OR p_months_ahead < 0 THEN
        RAISE EXCEPTION 'Partition month values must be non-negative';
    END IF;

    FOR v_month IN
        SELECT generate_series(
            date_trunc('month', current_date)::date
                - (p_months_back || ' months')::interval,
            date_trunc('month', current_date)::date
                + (p_months_ahead || ' months')::interval,
            interval '1 month'
        )::date
    LOOP
        v_next_month := (v_month + interval '1 month')::date;

        FOR v_schema, v_parent IN
            SELECT *
            FROM (VALUES
                ('ingest', 'raw_records'),
                ('ops', 'job_events'),
                ('ops', 'transit_snapshots')
            ) AS parents(schema_name, parent_name)
        LOOP
            v_partition := format(
                '%s_%s',
                v_parent,
                to_char(v_month, 'YYYY_MM')
            );

            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I
                 FOR VALUES FROM (%L) TO (%L)',
                v_schema,
                v_partition,
                v_schema,
                v_parent,
                v_month,
                v_next_month
            );
        END LOOP;
    END LOOP;
END
$function$;

SELECT ingest.ensure_monthly_partitions(12, 6);

INSERT INTO ops.checklist_definitions (
    canonical_name,
    stage_code,
    stage_order,
    workflow_role,
    configured_in_minerals
)
VALUES
    ('Transport Allocation', 'transport_allocation', 1, 'root', true),
    ('Vehicle Inspection', 'vehicle_inspection', 2.10, 'conditional', true),
    ('Loading and Exit', 'loading_exit', 2.20, 'conditional', true),
    ('Staging Arrival', 'staging_arrival', 3, 'optional', true),
    ('Staging Exit', 'staging_exit', 4, 'optional', true),
    ('Truck Arrival', 'truck_arrival', 5, 'optional', true),
    ('Truck Arrival at Mine', 'truck_arrival_mine', 5.10, 'configured_unused', true),
    ('Offloading and Exit', 'offloading_exit', 6, 'optional', true)
ON CONFLICT (stage_code) DO UPDATE
SET canonical_name = EXCLUDED.canonical_name,
    stage_order = EXCLUDED.stage_order,
    workflow_role = EXCLUDED.workflow_role,
    configured_in_minerals = EXCLUDED.configured_in_minerals,
    updated_at = clock_timestamp();

DROP TRIGGER IF EXISTS trg_locations_updated_at ON ops.locations;
CREATE TRIGGER trg_locations_updated_at
BEFORE UPDATE ON ops.locations
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_transporters_updated_at ON ops.transporters;
CREATE TRIGGER trg_transporters_updated_at
BEFORE UPDATE ON ops.transporters
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_vehicles_updated_at ON ops.vehicles;
CREATE TRIGGER trg_vehicles_updated_at
BEFORE UPDATE ON ops.vehicles
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_drivers_updated_at ON ops.drivers;
CREATE TRIGGER trg_drivers_updated_at
BEFORE UPDATE ON ops.drivers
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_checklists_updated_at ON ops.checklist_definitions;
CREATE TRIGGER trg_checklists_updated_at
BEFORE UPDATE ON ops.checklist_definitions
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_workflow_edges_updated_at ON ops.workflow_edges;
CREATE TRIGGER trg_workflow_edges_updated_at
BEFORE UPDATE ON ops.workflow_edges
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_allocations_updated_at ON ops.allocations;
CREATE TRIGGER trg_allocations_updated_at
BEFORE UPDATE ON ops.allocations
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_jobs_updated_at ON ops.jobs;
CREATE TRIGGER trg_jobs_updated_at
BEFORE UPDATE ON ops.jobs
FOR EACH ROW EXECUTE FUNCTION ops.set_updated_at();

CREATE OR REPLACE VIEW ops.v_allocation_progress AS
SELECT
    a.id AS allocation_id,
    a.job_reference,
    a.booked_at,
    lp.name AS loading_point,
    op.name AS offloading_point,
    t.name AS transporter,
    v.registration AS truck_registration,
    d.full_name AS driver_name,
    a.current_status,
    a.current_stage_code,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code = 'transport_allocation'
    ) AS transport_allocation_at,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code = 'vehicle_inspection'
    ) AS vehicle_inspection_at,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code = 'loading_exit'
    ) AS loading_exit_at,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code = 'staging_arrival'
    ) AS staging_arrival_at,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code = 'staging_exit'
    ) AS staging_exit_at,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code IN ('truck_arrival', 'truck_arrival_mine')
    ) AS truck_arrival_at,
    max(j.last_updated_at) FILTER (
        WHERE cd.stage_code = 'offloading_exit'
    ) AS offloading_exit_at,
    count(j.id) AS checklist_job_count,
    a.last_seen_at
FROM ops.allocations a
LEFT JOIN ops.locations lp ON lp.id = a.loading_location_id
LEFT JOIN ops.locations op ON op.id = a.offloading_location_id
LEFT JOIN ops.transporters t ON t.id = a.transporter_id
LEFT JOIN ops.vehicles v ON v.id = a.vehicle_id
LEFT JOIN ops.drivers d ON d.id = a.driver_id
LEFT JOIN ops.jobs j ON j.allocation_id = a.id
LEFT JOIN ops.checklist_definitions cd ON cd.id = j.checklist_definition_id
GROUP BY
    a.id,
    lp.name,
    op.name,
    t.name,
    v.registration,
    d.full_name;

CREATE OR REPLACE VIEW ops.v_current_transit AS
SELECT DISTINCT ON (job_reference)
    captured_at,
    allocation_id,
    job_reference,
    date_booked,
    parcel_reference,
    order_reference,
    loading_point,
    offloading_point,
    transporter_name,
    truck_registration,
    truck_type,
    driver_name,
    vehicle_inspection_at,
    loading_exit_at,
    staging_arrival_at,
    staging_exit_at,
    truck_arrival_at,
    offloading_exit_at,
    opus_job_detail_url
FROM ops.transit_snapshots
ORDER BY job_reference, captured_at DESC;

CREATE OR REPLACE VIEW ops.v_checklist_coverage AS
SELECT
    cd.stage_code,
    cd.canonical_name,
    cd.stage_order,
    count(j.id) AS job_rows,
    count(DISTINCT j.allocation_id) AS allocation_count,
    min(j.expected_start_at) AS first_job_at,
    max(j.expected_start_at) AS latest_job_at
FROM ops.checklist_definitions cd
LEFT JOIN ops.jobs j ON j.checklist_definition_id = cd.id
GROUP BY cd.id
ORDER BY cd.stage_order;

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '001',
    'Initial high-volume OPUS transport allocation and transit schema'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

REVOKE ALL ON SCHEMA ops, ingest, audit FROM PUBLIC;
GRANT USAGE ON SCHEMA ops, ingest TO connect_ops_writer;
GRANT USAGE ON SCHEMA ops TO connect_ops_reader;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON ops.locations,
       ops.transporters,
       ops.vehicles,
       ops.drivers,
       ops.checklist_definitions,
       ops.allocations,
       ops.jobs,
       ops.workflow_edges
    TO connect_ops_writer;

GRANT SELECT
    ON ops.v_allocation_progress,
       ops.v_current_transit,
       ops.v_checklist_coverage
    TO connect_ops_writer;

GRANT SELECT, INSERT
    ON ops.job_events,
       ops.transit_snapshots,
       ingest.raw_records
    TO connect_ops_writer;

GRANT SELECT, INSERT, UPDATE
    ON ingest.extraction_runs,
       ingest.source_checkpoints
    TO connect_ops_writer;

GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA ops, ingest
    TO connect_ops_writer;

GRANT EXECUTE
    ON FUNCTION ingest.ensure_monthly_partitions(integer, integer)
    TO connect_ops_writer;

GRANT SELECT
    ON ALL TABLES IN SCHEMA ops
    TO connect_ops_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE connect_ops_owner IN SCHEMA ops
    GRANT SELECT ON TABLES TO connect_ops_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE connect_ops_owner IN SCHEMA ops, ingest
    GRANT USAGE, SELECT ON SEQUENCES TO connect_ops_writer;
