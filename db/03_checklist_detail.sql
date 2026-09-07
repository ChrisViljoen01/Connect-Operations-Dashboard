BEGIN;

SET ROLE connect_ops_owner;

ALTER TABLE ingest.extraction_runs
    DROP CONSTRAINT IF EXISTS ck_extraction_runs_status;

ALTER TABLE ingest.extraction_runs
    ADD CONSTRAINT ck_extraction_runs_status
    CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'cancelled'));

ALTER TABLE ops.allocations
    ADD COLUMN IF NOT EXISTS current_stage_order numeric(6, 2);

CREATE TABLE IF NOT EXISTS ops.checklist_instances (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opus_checklist_instance_id uuid NOT NULL,
    job_id bigint NOT NULL REFERENCES ops.jobs(id) ON DELETE CASCADE,
    allocation_id bigint NOT NULL REFERENCES ops.allocations(id) ON DELETE CASCADE,
    checklist_definition_id bigint REFERENCES ops.checklist_definitions(id),
    job_reference text NOT NULL,
    opus_evaluation_id uuid,
    name text NOT NULL,
    description text,
    percentage_complete text,
    score text,
    total_score text,
    possible_score text,
    priority text,
    operator_name text,
    duration text,
    weighted boolean,
    started_at timestamptz,
    source_updated_at timestamptz,
    site_name text,
    evaluation_classification text,
    evaluation_classification_name text,
    evaluation_type_id text,
    log_coordinates jsonb NOT NULL DEFAULT '{}'::jsonb,
    section_count integer NOT NULL DEFAULT 0,
    answer_count integer NOT NULL DEFAULT 0,
    image_count integer NOT NULL DEFAULT 0,
    item_count integer NOT NULL DEFAULT 0,
    table_count integer NOT NULL DEFAULT 0,
    detail_complete boolean NOT NULL DEFAULT true,
    detail_errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_checklist_instances_opus_id
        UNIQUE (opus_checklist_instance_id),
    CONSTRAINT ck_checklist_instances_reference
        CHECK (job_reference ~ '^ORDBULK-[0-9]+$')
);

CREATE INDEX IF NOT EXISTS ix_checklist_instances_reference
    ON ops.checklist_instances (job_reference, source_updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_checklist_instances_job
    ON ops.checklist_instances (job_id);

CREATE INDEX IF NOT EXISTS ix_checklist_instances_definition
    ON ops.checklist_instances (checklist_definition_id, source_updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_checklist_instances_raw_payload
    ON ops.checklist_instances USING gin (raw_payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS ops.checklist_answers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checklist_instance_id bigint NOT NULL
        REFERENCES ops.checklist_instances(id) ON DELETE CASCADE,
    opus_answer_id uuid,
    parent_answer_id uuid,
    opus_question_id uuid,
    opus_section_id uuid,
    answer_section_id uuid,
    answer_subsection_id uuid,
    section_name text,
    section_report_full text,
    section_report_short text,
    section_sequence integer NOT NULL DEFAULT 0,
    subsection_name text,
    subsection_number text,
    subsection_repeat boolean,
    subsection_created_at timestamptz,
    question text NOT NULL,
    question_report_full text,
    question_report_short text,
    question_summary text,
    unformatted_question_text text,
    action_text text,
    optional_question boolean,
    require_comment boolean,
    priority_id text,
    question_type text,
    question_type_id text,
    question_subtype text,
    question_subtype_id text,
    question_unit text,
    question_function text,
    question_min_range text,
    question_max_range text,
    question_alert_min_range text,
    question_alert_max_range text,
    answer_text text,
    text_value text,
    unformatted_answer text,
    report_formatted_answer text,
    comments text,
    question_number text,
    answer_not_applicable boolean,
    score text,
    possible_score text,
    score_range text,
    question_classification text,
    scan_type_id text,
    source_reference text,
    answer_subsection_created_at timestamptz,
    operator_name text,
    answer_extra jsonb NOT NULL DEFAULT '{}'::jsonb,
    answer_images jsonb NOT NULL DEFAULT '[]'::jsonb,
    answer_items jsonb NOT NULL DEFAULT '[]'::jsonb,
    child_checklist_answers jsonb NOT NULL DEFAULT '[]'::jsonb,
    table_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    table_columns jsonb NOT NULL DEFAULT '[]'::jsonb,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_checklist_answers_opus_id
    ON ops.checklist_answers (checklist_instance_id, opus_answer_id)
    WHERE opus_answer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_checklist_answers_instance_section
    ON ops.checklist_answers (checklist_instance_id, section_sequence, question);

CREATE INDEX IF NOT EXISTS ix_checklist_answers_question
    ON ops.checklist_answers (opus_question_id, question);

CREATE INDEX IF NOT EXISTS ix_checklist_answers_raw_payload
    ON ops.checklist_answers USING gin (raw_payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS ingest.extraction_errors (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_run_id bigint NOT NULL
        REFERENCES ingest.extraction_runs(id) ON DELETE CASCADE,
    job_reference text,
    opus_job_id uuid,
    phase text NOT NULL,
    error_type text NOT NULL,
    error_message text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_extraction_errors_run
    ON ingest.extraction_errors (extraction_run_id, occurred_at);

CREATE OR REPLACE VIEW ops.v_current_transit AS
SELECT
    latest.captured_at,
    latest.allocation_id,
    latest.job_reference,
    latest.date_booked,
    latest.parcel_reference,
    latest.order_reference,
    latest.loading_point,
    latest.offloading_point,
    latest.transporter_name,
    latest.truck_registration,
    latest.truck_type,
    latest.driver_name,
    latest.vehicle_inspection_at,
    latest.loading_exit_at,
    latest.staging_arrival_at,
    latest.staging_exit_at,
    latest.truck_arrival_at,
    latest.offloading_exit_at,
    latest.opus_job_detail_url
FROM (
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
    ORDER BY job_reference, captured_at DESC
) AS latest
WHERE latest.offloading_exit_at IS NULL;

INSERT INTO audit.schema_migrations (version, description)
VALUES (
    '003',
    'Persist complete OPUS checklist instances, questions, answers and extraction errors'
)
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description;

RESET ROLE;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON ops.checklist_instances,
       ops.checklist_answers
    TO connect_ops_writer;

GRANT SELECT, INSERT
    ON ingest.extraction_errors
    TO connect_ops_writer;

GRANT SELECT
    ON ops.checklist_instances,
       ops.checklist_answers
    TO connect_ops_reader;

GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA ops, ingest
    TO connect_ops_writer;

COMMIT;