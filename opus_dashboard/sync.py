from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from opus_dashboard.config import Settings
from opus_dashboard.credentials import (
    StoredCredential,
    delete_windows_credential,
    read_windows_credential,
    write_windows_credential,
)
from opus_dashboard.opus_client import (
    OpusClient,
    OpusCredentials,
)
from opus_dashboard.repository import OperationsRepository


class SyncCancelledError(Exception):
    """Raised when the user requests cancellation of a running sync."""


STATUS_LABELS = {
    0: "All",
    1: "Operator Not Started",
    2: "Operator In Progress",
    3: "Operator Review",
    4: "Operator Signed Off",
    5: "In Progress Completed",
    6: "Recurring",
    9: "Job Closed",
}
LIVE_STATUS_IDS = (1, 2, 3, 6)
DISCOVERY_PAGE_SIZE = 500

BULK_IMPORT_CHECKLIST_NAME = "bulk import for minerals transport allocation"
TRANSPORT_ALLOCATION_CHECKLIST_NAME = "transport allocation"

STAGE_CONFIG = {
    TRANSPORT_ALLOCATION_CHECKLIST_NAME: ("transport_allocation", 1, "root"),
    "vehicle inspection": ("vehicle_inspection", 2.10, "conditional"),
    "loading and exit": ("loading_exit", 2.20, "conditional"),
    "staging arrival": ("staging_arrival", 3, "optional"),
    "staging exit": ("staging_exit", 4, "optional"),
    "truck arrival": ("truck_arrival", 5, "optional"),
    "truck arrival at mine": ("truck_arrival_mine", 5.10, "configured_unused"),
    "offloading and exit": ("offloading_exit", 6, "optional"),
}


@dataclass(slots=True)
class SyncProgress:
    running: bool = False
    mode: str = ""
    phase: str = "Idle"
    started_at: str = ""
    finished_at: str = ""
    date_from: str = ""
    date_to: str = ""
    cancelled: bool = False
    rows_seen: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_rejected: int = 0
    bundles_queued: int = 0
    bundles_deferred: int = 0
    bundles_completed: int = 0
    checklist_instances: int = 0
    checklist_sections: int = 0
    checklist_answers: int = 0
    detail_errors: int = 0
    request_retries: int = 0
    scan_chunks_total: int = 0
    scan_chunks_completed: int = 0
    scan_pages_completed: int = 0
    jobs_scanned: int = 0
    recent_jobs_discovered: int = 0
    active_jobs_discovered: int = 0
    backlog_jobs_discovered: int = 0
    active_audit_jobs: int = 0
    historical_audit_jobs: int = 0
    active_sweep: bool = False
    audit_due: bool = False
    references_scanned: int = 0
    eligible_references: int = 0
    excluded_missing_root_references: int = 0
    excluded_out_of_window_root_references: int = 0
    bulk_import_jobs_excluded: int = 0
    duplicate_root_jobs_excluded: int = 0
    pre_root_jobs_excluded: int = 0
    phase_key: str = "idle"
    phase_current: int = 0
    phase_total: int = 0
    current_reference: str = ""
    current_checklist: str = ""
    last_progress_at: str = ""
    current_item: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class SyncResult:
    rows_seen: int
    rows_inserted: int
    rows_updated: int
    rows_rejected: int
    bundles_queued: int
    bundles_deferred: int
    bundles_completed: int
    checklist_instances: int
    checklist_sections: int
    checklist_answers: int
    detail_errors: int
    request_retries: int
    scan_chunks_total: int
    scan_chunks_completed: int
    scan_pages_completed: int
    jobs_scanned: int
    recent_jobs_discovered: int
    active_jobs_discovered: int
    backlog_jobs_discovered: int
    active_audit_jobs: int
    historical_audit_jobs: int
    active_sweep: bool
    audit_due: bool
    references_scanned: int
    eligible_references: int
    excluded_missing_root_references: int
    excluded_out_of_window_root_references: int
    bulk_import_jobs_excluded: int
    duplicate_root_jobs_excluded: int
    pre_root_jobs_excluded: int
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowSelection:
    rows: list[dict[str, Any]]
    references_scanned: int
    eligible_references: int
    excluded_missing_root_references: int
    excluded_out_of_window_root_references: int
    bulk_import_jobs_excluded: int
    duplicate_root_jobs_excluded: int
    pre_root_jobs_excluded: int


class OpusCredentialStore:
    def __init__(
        self,
        target: str,
        *,
        fallback_email: str = "",
        fallback_password: str = "",
    ) -> None:
        self.target = target
        # Used on hosts without Windows Credential Manager (containers, Linux
        # servers). Windows deployments should keep using the Extraction
        # screen instead of setting these environment variables.
        self._fallback = (
            StoredCredential(username=fallback_email.strip(), password=fallback_password)
            if fallback_email.strip() and fallback_password
            else None
        )

    def read(self) -> StoredCredential | None:
        stored = read_windows_credential(self.target)
        if stored is not None:
            return stored
        return self._fallback

    def save(self, email: str, password: str) -> None:
        write_windows_credential(self.target, email.strip(), password)

    def delete(self) -> bool:
        return delete_windows_credential(self.target)


class OpusSyncEngine:
    def __init__(
        self,
        settings: Settings,
        repository: OperationsRepository,
        credential_store: OpusCredentialStore,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.credential_store = credential_store
        # Cached result of whether migration 006 column exists; None = not yet checked.
        self._source_hash_col: bool | None = None

    def _check_source_hash_column(self, connection: psycopg.Connection[dict[str, Any]]) -> bool:
        """Return True if ingest.raw_records has the source_payload_sha256 column."""
        if self._source_hash_col is not None:
            return self._source_hash_col
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'ingest'
                  AND table_name   = 'raw_records'
                  AND column_name  = 'source_payload_sha256'
                """
            )
            self._source_hash_col = cursor.fetchone() is not None
        return self._source_hash_col

    def test_credentials(self, email: str, password: str) -> dict[str, Any]:
        client = OpusClient(
            self.settings.opus_api_url,
            self.settings.opus_request_timeout,
            self.settings.opus_read_attempts,
            self.settings.opus_retry_backoff_seconds,
        )
        try:
            return client.authenticate(OpusCredentials(email, password))
        finally:
            client.close()

    def run(
        self,
        *,
        full: bool,
        progress: Callable[[dict[str, Any]], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> SyncResult:
        if not self.repository.detail_schema_ready():
            raise RuntimeError(
                "Database migrations through 008 must be applied before OPUS extraction."
            )
        credentials = self.credential_store.read()
        if credentials is None:
            raise RuntimeError("Save the OPUS email and password before running a sync.")
        started_at = datetime.now(timezone.utc)
        today = datetime.now().astimezone().date()
        # Always advance to today so long-running processes pick up new data.
        # Respects an explicit OPUS_EXTRACT_TO env override as a hard cap.
        date_from = self.settings.opus_extract_from
        date_to = self.settings.effective_extract_to()
        if date_from > today or date_to > today:
            raise ValueError(
                "The OPUS extraction window cannot extend beyond the current date."
            )
        scan_from = (
            date_from
            if full
            else _live_scan_start(
                date_from,
                date_to,
                self.settings.opus_live_lookback_days,
            )
        )
        mode = "full_detail" if full else "live"
        counters = {
            "rows_seen": 0,
            "rows_inserted": 0,
            "rows_updated": 0,
            "rows_rejected": 0,
            "bundles_queued": 0,
            "bundles_deferred": 0,
            "bundles_completed": 0,
            "checklist_instances": 0,
            "checklist_sections": 0,
            "checklist_answers": 0,
            "detail_errors": 0,
            "request_retries": 0,
            "scan_chunks_total": 0,
            "scan_chunks_completed": 0,
            "scan_pages_completed": 0,
            "jobs_scanned": 0,
            "recent_jobs_discovered": 0,
            "active_jobs_discovered": 0,
            "backlog_jobs_discovered": 0,
            "active_audit_jobs": 0,
            "historical_audit_jobs": 0,
            "active_sweep": False,
            "audit_due": False,
            "references_scanned": 0,
            "eligible_references": 0,
            "excluded_missing_root_references": 0,
            "excluded_out_of_window_root_references": 0,
            "bulk_import_jobs_excluded": 0,
            "duplicate_root_jobs_excluded": 0,
            "pre_root_jobs_excluded": 0,
        }
        counter_lock = threading.Lock()

        def record_retry(event: dict[str, Any]) -> None:
            with counter_lock:
                counters["request_retries"] += 1
            self._notify(
                progress,
                phase="Retrying a timed-out OPUS request",
                current_item=str(event.get("operation") or ""),
                **counters,
            )

        warnings: list[str] = []
        client = OpusClient(
            self.settings.opus_api_url,
            self.settings.opus_request_timeout,
            self.settings.opus_read_attempts,
            self.settings.opus_retry_backoff_seconds,
            record_retry,
            self.settings.opus_section_workers,
        )
        self._notify(progress, phase="Authenticating with OPUS", mode=mode)
        client.authenticate(OpusCredentials(credentials.username, credentials.password))
        self._notify(
            progress,
            phase="Authenticated — scanning date window",
            date_from=scan_from.isoformat(),
            date_to=date_to.isoformat(),
        )

        with self.repository.connection() as connection:
            run_id = self._start_run(connection, mode, date_from, date_to)
            connection.commit()
            lock_acquired = False
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT pg_try_advisory_lock(
                            hashtext('connect_logistics_opus_detail_sync')
                        ) AS acquired
                        """
                    )
                    lock_row = cursor.fetchone() or {}
                lock_acquired = bool(lock_row.get("acquired"))
                if not lock_acquired:
                    raise RuntimeError(
                        "Another OPUS detail extraction is already running."
                    )
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE ingest.extraction_runs
                        SET status = 'cancelled',
                            finished_at = clock_timestamp(),
                            error_message = coalesce(
                                error_message,
                                'Closed when a new extraction recovered the advisory lock.'
                            )
                        WHERE status = 'running'
                          AND id <> %s
                        """,
                        (run_id,),
                    )
                connection.commit()
                active_sweep = full or self._checkpoint_due(
                    connection,
                    "last_active_sweep_at",
                    self.settings.opus_active_sweep_minutes,
                )
                audit_due = full or self._checkpoint_due(
                    connection,
                    "last_audit_at",
                    self.settings.opus_audit_minutes,
                )
                counters["active_sweep"] = active_sweep
                counters["audit_due"] = audit_due
                force_reload_all = full and self._has_completed_full(connection)
                self._notify(
                    progress,
                    phase="Refreshing checklist definitions",
                    phase_key="discovery",
                )
                try:
                    definitions = client.checklist_definitions()
                    with connection.cursor() as cursor:
                        for definition in definitions:
                            self._upsert_definition(cursor, definition)
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    warning = (
                        "OPUS checklist catalogue was unavailable; configured "
                        f"workflow stages were used instead ({type(exc).__name__})."
                    )
                    warnings.append(warning)
                    self._notify(
                        progress,
                        phase="Checklist catalogue unavailable; continuing",
                    )

                chunks = list(
                    _date_chunks(
                        scan_from,
                        date_to,
                        days=max(self.settings.opus_scan_chunk_days, 1),
                    )
                )
                counters["scan_chunks_total"] = len(chunks) + (
                    1 if not full and active_sweep else 0
                )
                discovered_by_job_id: dict[str, dict[str, Any]] = {}

                def remember(row: dict[str, Any]) -> None:
                    reference = str(
                        _value(row, "Reference", "JobReference") or ""
                    ).strip()
                    job_id = _uuid_text(_value(row, "ID", "JobID"))
                    if not re.fullmatch(r"ORDBULK-\d+", reference) or not job_id:
                        return
                    existing = discovered_by_job_id.get(job_id)
                    if existing is None:
                        discovered_by_job_id[job_id] = row
                    else:
                        for marker in (
                            "_audit_requested",
                            "_audit_kind",
                            "_backlog_requested",
                            "_backlog_captured_at",
                        ):
                            if row.get(marker):
                                existing[marker] = row[marker]

                for chunk_index, (chunk_from, chunk_to) in enumerate(chunks, start=1):
                    if stop_event and stop_event.is_set():
                        raise SyncCancelledError("Sync stopped by user between date chunks.")
                    self._notify(
                        progress,
                        phase=f"Discovering jobs {chunk_from:%d %b} - {chunk_to:%d %b}",
                        phase_key="discovery",
                        phase_current=chunk_index - 1,
                        phase_total=len(chunks),
                        current_item=f"Date chunk {chunk_index} of {len(chunks)}",
                        **counters,
                    )
                    for page_index, page in enumerate(
                        client.iter_jobs(
                            chunk_from,
                            chunk_to,
                            page_size=DISCOVERY_PAGE_SIZE,
                        ),
                        start=1,
                    ):
                        counters["scan_pages_completed"] += 1
                        counters["jobs_scanned"] += len(page)
                        for row in page:
                            remember(row)
                        self._notify(
                            progress,
                            phase=(
                                f"Discovering jobs {chunk_from:%d %b} - "
                                f"{chunk_to:%d %b}"
                            ),
                            phase_key="discovery",
                            phase_current=chunk_index - 1,
                            phase_total=len(chunks),
                            current_item=(
                                f"Date chunk {chunk_index} of {len(chunks)}, "
                                f"page {page_index}"
                            ),
                            **counters,
                        )
                    counters["scan_chunks_completed"] = chunk_index
                    with connection.cursor() as cursor:
                        self._update_run_progress(cursor, run_id, counters)
                    connection.commit()

                counters["recent_jobs_discovered"] = len(discovered_by_job_id)
                if not full:
                    active_job_ids: set[str] = set()
                    if active_sweep:
                        self._notify(
                            progress,
                            phase="Checking all currently active OPUS jobs",
                            phase_key="discovery",
                            phase_current=len(chunks),
                            phase_total=counters["scan_chunks_total"],
                            current_item="Active OPUS statuses",
                            **counters,
                        )
                        for page in client.iter_jobs(
                            date_from,
                            date_to,
                            page_size=DISCOVERY_PAGE_SIZE,
                            job_statuses=LIVE_STATUS_IDS,
                        ):
                            counters["scan_pages_completed"] += 1
                            counters["jobs_scanned"] += len(page)
                            for row in page:
                                job_id = _uuid_text(_value(row, "ID", "JobID"))
                                if job_id:
                                    active_job_ids.add(job_id)
                                remember(row)
                            self._notify(
                                progress,
                                phase="Checking all currently active OPUS jobs",
                                phase_key="discovery",
                                phase_current=len(chunks),
                                phase_total=counters["scan_chunks_total"],
                                current_item=(
                                    f"{len(active_job_ids):,} active jobs discovered"
                                ),
                                **counters,
                            )
                        counters["scan_chunks_completed"] = len(chunks) + 1
                    counters["active_jobs_discovered"] = len(active_job_ids)

                    with connection.cursor() as cursor:
                        backlog_rows = self._stored_backlog_rows(
                            cursor,
                            lookback_days=max(
                                self.settings.opus_backlog_lookback_days,
                                1,
                            ),
                            limit=max(
                                self.settings.opus_live_detail_batch_size * 4,
                                self.settings.opus_live_detail_batch_size,
                            ),
                        )
                        active_audit_rows = (
                            self._stored_audit_rows(
                                cursor,
                                terminal=False,
                                limit=max(
                                    self.settings.opus_active_audit_batch_size,
                                    0,
                                ),
                            )
                            if audit_due
                            else []
                        )
                        historical_audit_rows = (
                            self._stored_audit_rows(
                                cursor,
                                terminal=True,
                                limit=max(
                                    self.settings.opus_audit_batch_size,
                                    0,
                                ),
                            )
                            if audit_due
                            else []
                        )
                        for row in backlog_rows:
                            remember(row)
                        for row in active_audit_rows:
                            remember(row)
                        for row in historical_audit_rows:
                            remember(row)
                        references = {
                            str(
                                _value(row, "Reference", "JobReference") or ""
                            ).strip()
                            for row in discovered_by_job_id.values()
                        }
                        for row in self._stored_root_rows(cursor, references):
                            remember(row)
                    counters["backlog_jobs_discovered"] = len(backlog_rows)
                    counters["active_audit_jobs"] = len(active_audit_rows)
                    counters["historical_audit_jobs"] = len(
                        historical_audit_rows
                    )

                discovered_rows = list(discovered_by_job_id.values())
                selection = _qualify_transport_workflows(
                    discovered_rows,
                    date_from,
                    date_to,
                )
                for field in (
                    "references_scanned",
                    "eligible_references",
                    "excluded_missing_root_references",
                    "excluded_out_of_window_root_references",
                    "bulk_import_jobs_excluded",
                    "duplicate_root_jobs_excluded",
                    "pre_root_jobs_excluded",
                ):
                    counters[field] = int(getattr(selection, field))
                counters["rows_seen"] = len(selection.rows)
                self._notify(
                    progress,
                    phase="Qualifying Transport Allocation workflows",
                    phase_key="qualification",
                    phase_current=0,
                    phase_total=len(selection.rows),
                    current_item=(
                        f"{selection.eligible_references:,} eligible references"
                    ),
                    **counters,
                )

                selected_job_ids = [
                    job_id
                    for row in selection.rows
                    if (job_id := _uuid_text(_value(row, "ID", "JobID")))
                ]
                candidate_entries: list[
                    tuple[tuple[int, float], dict[str, Any]]
                ] = []
                with connection.cursor() as cursor:
                    completed_job_ids = self._completed_detail_job_ids(
                        cursor,
                        selected_job_ids,
                    )
                    errored_job_ids = self._errored_job_ids(
                        cursor,
                        selected_job_ids,
                    )
                    stored_job_lists = self._stored_job_lists(
                        cursor,
                        selected_job_ids,
                    )
                    for index, row in enumerate(selection.rows, start=1):
                        if stop_event and stop_event.is_set():
                            raise SyncCancelledError(
                                "Sync stopped by user during workflow qualification."
                            )
                        job_id = _uuid_text(_value(row, "ID", "JobID"))
                        if not job_id:
                            continue
                        source_row = _source_job_row(row)
                        changed, inserted = self._raw_record(
                            cursor,
                            run_id,
                            "opus_api",
                            job_id,
                            "job_list",
                            source_row,
                        )
                        if inserted:
                            counters["rows_inserted"] += 1
                        elif changed:
                            counters["rows_updated"] += 1
                        detail_exists = job_id in completed_job_ids
                        previously_errored = job_id in errored_job_ids
                        normalized_changed = (
                            stored_job_lists.get(job_id) != source_row
                        )
                        audit_requested = bool(row.get("_audit_requested"))
                        if _should_load_detail(
                            force_reload_all=(
                                force_reload_all
                                or audit_requested
                            ),
                            detail_exists=detail_exists,
                            changed=changed or normalized_changed,
                            status=_status(source_row),
                            previously_errored=previously_errored,
                        ):
                            active = not _is_terminal_status(_status(source_row))
                            if changed or inserted:
                                tier = 0 if active else 2
                            elif (
                                normalized_changed
                                or not detail_exists
                                or previously_errored
                            ):
                                tier = 1 if active else 3
                            else:
                                tier = 4 if active else 5
                            source_updated_at = _timestamp(
                                _value(
                                    source_row,
                                    "LastUpdated",
                                    "HeaderUpdatedDate",
                                    "UpdatedDate",
                                    "SignOffDate",
                                )
                            )
                            backlog_captured_at = _timestamp(
                                row.get("_backlog_captured_at")
                            )
                            priority_at = (
                                backlog_captured_at
                                if tier in {1, 3}
                                and backlog_captured_at is not None
                                else source_updated_at
                            )
                            timestamp = (
                                priority_at.timestamp()
                                if priority_at is not None
                                else 0.0
                            )
                            recency = (
                                -timestamp
                                if tier in {0, 2}
                                else timestamp
                            )
                            candidate_entries.append(((tier, recency), row))
                        if index % 100 == 0 or index == len(selection.rows):
                            self._notify(
                                progress,
                                phase="Qualifying Transport Allocation workflows",
                                phase_key="qualification",
                                phase_current=index,
                                phase_total=len(selection.rows),
                                current_reference=str(
                                    _value(row, "Reference", "JobReference") or ""
                                ),
                                current_checklist=_job_checklist_name(row),
                                current_item=f"Evaluated {index:,} eligible jobs",
                                **counters,
                            )

                candidate_entries.sort(key=lambda entry: entry[0])
                candidate_limit = (
                    len(candidate_entries)
                    if full
                    else max(self.settings.opus_live_detail_batch_size, 1)
                )
                candidates = _select_bounded_candidates(
                    candidate_entries,
                    candidate_limit,
                    full=full,
                )
                counters["bundles_queued"] = len(candidates)
                counters["bundles_deferred"] = max(
                    len(candidate_entries) - len(candidates),
                    0,
                )
                with connection.cursor() as cursor:
                    self._update_run_progress(cursor, run_id, counters)
                connection.commit()
                self._notify(
                    progress,
                    phase="Loading and storing checklist detail",
                    phase_key="detail",
                    phase_current=0,
                    phase_total=len(candidates),
                    current_item=f"{len(candidates):,} detail jobs queued",
                    **counters,
                )
                if candidates:
                    self._load_candidate_bundles(
                        connection,
                        client,
                        run_id,
                        candidates,
                        date_from,
                        date_to,
                        counters,
                        progress,
                        stop_event,
                    )

                finished_at = datetime.now(timezone.utc)
                run_status = (
                    "partial"
                    if counters["rows_rejected"] or counters["detail_errors"]
                    else "succeeded"
                )
                self._finish_run(
                    connection,
                    run_id,
                    run_status,
                    counters,
                    None,
                    {
                        **counters,
                        "full": full,
                        "force_reload_all": force_reload_all,
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "scan_from": scan_from.isoformat(),
                        "unique_jobs_discovered": len(discovered_by_job_id),
                        "qualified_jobs": counters["rows_seen"],
                        "warnings": warnings,
                        "active_sweep": active_sweep,
                        "audit_due": audit_due,
                    },
                )
                self._save_checkpoint(
                    connection,
                    finished_at,
                    full,
                    active_sweep=active_sweep,
                    audit=audit_due,
                )
                connection.commit()
                return SyncResult(
                    **counters,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            except Exception as exc:
                connection.rollback()
                run_status = (
                    "cancelled" if isinstance(exc, SyncCancelledError) else "failed"
                )
                self._finish_run(
                    connection,
                    run_id,
                    run_status,
                    counters,
                    None if isinstance(exc, SyncCancelledError) else str(exc),
                    {**counters, "full": full},
                )
                connection.commit()
                raise
            finally:
                if lock_acquired:
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                """
                                SELECT pg_advisory_unlock(
                                    hashtext('connect_logistics_opus_detail_sync')
                                )
                                """
                            )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                client.close()

    @staticmethod
    def _has_completed_full(
        connection: psycopg.Connection[dict[str, Any]],
    ) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT checkpoint_value ? 'last_full_at' AS completed
                FROM ingest.source_checkpoints
                WHERE source_name = 'opus_api'
                  AND checkpoint_key = 'jobs_sync'
                """
            )
            row = cursor.fetchone() or {}
        return bool(row.get("completed"))

    def full_refresh_due(self) -> bool:
        try:
            with self.repository.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT checkpoint_value ->> 'last_full_at' AS last_full_at
                        FROM ingest.source_checkpoints
                        WHERE source_name = 'opus_api'
                          AND checkpoint_key = 'jobs_sync'
                        """
                    )
                    row = cursor.fetchone()
            last_full_at = _timestamp(row["last_full_at"]) if row else None
            if last_full_at is None:
                return True
            now = datetime.now(timezone.utc)
            if last_full_at.tzinfo is None:
                last_full_at = last_full_at.replace(tzinfo=timezone.utc)
            return now - last_full_at >= timedelta(
                hours=max(self.settings.opus_full_sync_hours, 1)
            )
        except Exception:
            return True

    @staticmethod
    def _checkpoint_due(
        connection: psycopg.Connection[dict[str, Any]],
        key: str,
        minutes: int,
    ) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT checkpoint_value ->> %s AS last_at
                FROM ingest.source_checkpoints
                WHERE source_name = 'opus_api'
                  AND checkpoint_key = 'jobs_sync'
                """,
                (key,),
            )
            row = cursor.fetchone()
        last_at = _timestamp(row["last_at"]) if row else None
        if last_at is None:
            return True
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_at >= timedelta(
            minutes=max(minutes, 1)
        )

    @staticmethod
    def _stored_backlog_rows(
        cursor: psycopg.Cursor[dict[str, Any]],
        *,
        lookback_days: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        cursor.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (raw.source_record_key)
                       raw.source_record_key,
                       raw.payload,
                       raw.captured_at
                FROM ingest.raw_records raw
                WHERE raw.source_name = 'opus_api'
                  AND raw.record_type = 'job_list'
                  AND raw.captured_at >= (
                      clock_timestamp() - (%s * interval '1 day')
                  )
                ORDER BY
                    raw.source_record_key,
                    raw.captured_at DESC,
                    raw.id DESC
            )
            SELECT
                job.opus_job_id::text AS job_id,
                job.job_reference,
                definition.canonical_name AS checklist_name,
                job.status,
                job.status_detail,
                job.source_created_at,
                job.last_updated_at,
                jsonb_build_object('list', latest.payload) AS raw_attributes,
                latest.captured_at AS backlog_captured_at
            FROM latest
            JOIN ops.jobs job
              ON job.opus_job_id::text = latest.source_record_key
            LEFT JOIN ops.checklist_definitions definition
              ON definition.id = job.checklist_definition_id
            WHERE latest.payload IS DISTINCT FROM job.raw_attributes -> 'list'
            ORDER BY latest.captured_at
            LIMIT %s
            """,
            (lookback_days, limit),
        )
        result: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            stored = _stored_job_row(row)
            stored["_backlog_requested"] = True
            stored["_backlog_captured_at"] = row.get("backlog_captured_at")
            result.append(stored)
        return result

    @staticmethod
    def _stored_audit_rows(
        cursor: psycopg.Cursor[dict[str, Any]],
        *,
        terminal: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        status_operator = "~*" if terminal else "!~*"
        cursor.execute(
            f"""
            SELECT
                job.opus_job_id::text AS job_id,
                job.job_reference,
                definition.canonical_name AS checklist_name,
                job.status,
                job.status_detail,
                job.source_created_at,
                job.last_updated_at,
                job.raw_attributes
            FROM ops.jobs job
            LEFT JOIN ops.checklist_definitions definition
              ON definition.id = job.checklist_definition_id
            LEFT JOIN LATERAL (
                SELECT max(instance.last_seen_at) AS last_seen_at
                FROM ops.checklist_instances instance
                WHERE instance.job_id = job.id
            ) detail ON true
            WHERE job.opus_job_id IS NOT NULL
              AND coalesce(job.status, '') {status_operator} %s
            ORDER BY greatest(
                         job.last_seen_at,
                         coalesce(detail.last_seen_at, job.last_seen_at)
                     ),
                     job.id
            LIMIT %s
            """,
            (
                r"(signed[[:space:]]*off|completed|closed|cancelled|canceled)",
                limit,
            ),
        )
        result = [
            _stored_job_row(row, audit_requested=True)
            for row in cursor.fetchall()
        ]
        audit_kind = "historical" if terminal else "active"
        for row in result:
            row["_audit_kind"] = audit_kind
        return result

    @staticmethod
    def _stored_root_rows(
        cursor: psycopg.Cursor[dict[str, Any]],
        references: set[str],
    ) -> list[dict[str, Any]]:
        references = {reference for reference in references if reference}
        if not references:
            return []
        cursor.execute(
            """
            SELECT
                job.opus_job_id::text AS job_id,
                job.job_reference,
                definition.canonical_name AS checklist_name,
                job.status,
                job.status_detail,
                job.source_created_at,
                job.last_updated_at,
                job.raw_attributes
            FROM ops.allocations allocation
            JOIN ops.jobs job
              ON job.opus_job_id = allocation.opus_root_job_id
            LEFT JOIN ops.checklist_definitions definition
              ON definition.id = job.checklist_definition_id
            WHERE allocation.job_reference = ANY(%s)
            """,
            (sorted(references),),
        )
        return [_stored_job_row(row) for row in cursor.fetchall()]

    @staticmethod
    def _stored_job_lists(
        cursor: psycopg.Cursor[dict[str, Any]],
        job_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not job_ids:
            return {}
        cursor.execute(
            """
            SELECT
                opus_job_id::text AS job_id,
                raw_attributes -> 'list' AS list_row
            FROM ops.jobs
            WHERE opus_job_id = ANY(%s::uuid[])
            """,
            (job_ids,),
        )
        return {
            str(row["job_id"]): row["list_row"]
            for row in cursor.fetchall()
            if isinstance(row.get("list_row"), dict)
        }

    def _load_candidate_bundles(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        client: OpusClient,
        run_id: int,
        candidates: list[dict[str, Any]],
        date_from: date,
        date_to: date,
        counters: dict[str, int],
        progress: Callable[[dict[str, Any]], None] | None,
        stop_event: threading.Event | None = None,
    ) -> None:
        def fetch(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            job_id = _uuid_text(_value(row, "ID", "JobID"))
            checklist_instance_id = _uuid_text(
                _value(row, "AnswerEvaluationID", "AnswerEvaluationId")
            )
            worker = client.clone_authenticated()
            try:
                return row, worker.job_bundle(
                    job_id or "",
                    date_from,
                    date_to,
                    checklist_instance_id,
                )
            finally:
                worker.close()

        with ThreadPoolExecutor(
            max_workers=max(self.settings.opus_detail_workers, 1),
            thread_name_prefix="opus-sync",
        ) as pool:
            futures = {
                pool.submit(fetch, row): row
                for row in candidates
            }
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    # Cancel outstanding futures and abort cleanly.
                    for f in futures:
                        f.cancel()
                    raise SyncCancelledError("Sync stopped by user during bundle processing.")
                candidate = futures[future]
                reference = str(
                    _value(candidate, "Reference", "JobReference") or ""
                ).strip()
                try:
                    row, bundle = future.result()
                    self._notify(
                        progress,
                        phase="Loading checklist answers",
                        phase_key="detail",
                        phase_current=(
                            counters["bundles_completed"]
                            + counters["rows_rejected"]
                        ),
                        phase_total=counters["bundles_queued"],
                        current_reference=reference,
                        current_checklist=_job_checklist_name(candidate),
                        current_item=reference,
                        **counters,
                    )
                    job_id = _uuid_text(_value(row, "ID", "JobID")) or reference
                    with connection.cursor() as cursor:
                        changed, inserted = self._raw_record(
                            cursor,
                            run_id,
                            "opus_api",
                            job_id,
                            "job_bundle",
                            bundle,
                        )
                        if inserted:
                            counters["rows_inserted"] += 1
                        elif changed:
                            counters["rows_updated"] += 1
                        detail_counts = self._upsert_job_bundle(
                            cursor,
                            run_id,
                            row,
                            bundle,
                        )
                        counters["bundles_completed"] += 1
                        for key in (
                            "checklist_instances",
                            "checklist_sections",
                            "checklist_answers",
                            "detail_errors",
                        ):
                            counters[key] += detail_counts[key]
                        self._update_run_progress(cursor, run_id, counters)
                    connection.commit()
                    self._notify(
                        progress,
                        phase="Checklist detail saved",
                        phase_key="detail",
                        phase_current=(
                            counters["bundles_completed"]
                            + counters["rows_rejected"]
                        ),
                        phase_total=counters["bundles_queued"],
                        current_reference=reference,
                        current_checklist=_job_checklist_name(candidate),
                        current_item=reference,
                        error="",
                        **counters,
                    )
                except Exception as exc:
                    connection.rollback()
                    counters["rows_rejected"] += 1
                    error = (
                        f"{reference or 'Unknown OPUS job'}: "
                        f"{type(exc).__name__}: {exc}"
                    )[:1200]
                    with connection.cursor() as cursor:
                        self._record_extraction_error(
                            cursor,
                            run_id,
                            candidate,
                            "job_bundle",
                            exc,
                        )
                        self._update_run_progress(cursor, run_id, counters)
                        cursor.execute(
                            """
                            UPDATE ingest.extraction_runs
                            SET error_message = coalesce(error_message, %s)
                            WHERE id = %s
                            """,
                            (error, run_id),
                        )
                    connection.commit()
                    self._notify(
                        progress,
                        phase="Loading checklist answers",
                        phase_key="detail",
                        phase_current=(
                            counters["bundles_completed"]
                            + counters["rows_rejected"]
                        ),
                        phase_total=counters["bundles_queued"],
                        current_reference=reference,
                        current_checklist=_job_checklist_name(candidate),
                        current_item=reference,
                        error=error,
                        **counters,
                    )

    @staticmethod
    def _update_run_progress(
        cursor: psycopg.Cursor[dict[str, Any]],
        run_id: int,
        counters: dict[str, int],
    ) -> None:
        cursor.execute(
            """
            UPDATE ingest.extraction_runs
            SET rows_seen = %s,
                rows_inserted = %s,
                rows_updated = %s,
                rows_rejected = %s,
                metadata = metadata || %s
            WHERE id = %s
            """,
            (
                counters["rows_seen"],
                counters["rows_inserted"],
                counters["rows_updated"],
                counters["rows_rejected"],
                Jsonb(counters),
                run_id,
            ),
        )

    @staticmethod
    def _record_extraction_error(
        cursor: psycopg.Cursor[dict[str, Any]],
        run_id: int,
        candidate: dict[str, Any],
        phase: str,
        error: Exception,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO ingest.extraction_errors (
                extraction_run_id, job_reference, opus_job_id, phase,
                error_type, error_message, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                str(_value(candidate, "Reference", "JobReference") or "") or None,
                _uuid_text(_value(candidate, "ID", "JobID")),
                phase,
                type(error).__name__,
                str(error)[:4000],
                Jsonb({"source": "opus_api"}),
            ),
        )

    @staticmethod
    def _start_run(
        connection: psycopg.Connection[dict[str, Any]],
        mode: str,
        date_from: date,
        date_to: date,
    ) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ingest.extraction_runs (
                    source_name, extraction_scope, filter_payload
                )
                VALUES ('opus_api', %s, %s)
                RETURNING id
                """,
                (mode, Jsonb({"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})),
            )
            return int(cursor.fetchone()["id"])

    @staticmethod
    def _finish_run(
        connection: psycopg.Connection[dict[str, Any]],
        run_id: int,
        status: str,
        counters: dict[str, int],
        error: str | None,
        metadata: dict[str, Any],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ingest.extraction_runs
                SET finished_at = clock_timestamp(),
                    status = %s,
                    rows_seen = %s,
                    rows_inserted = %s,
                    rows_updated = %s,
                    rows_rejected = %s,
                    error_message = coalesce(%s, error_message),
                    metadata = metadata || %s
                WHERE id = %s
                """,
                (
                    status,
                    counters["rows_seen"],
                    counters["rows_inserted"],
                    counters["rows_updated"],
                    counters["rows_rejected"],
                    error,
                    Jsonb(metadata),
                    run_id,
                ),
            )

    @staticmethod
    def _save_checkpoint(
        connection: psycopg.Connection[dict[str, Any]],
        finished_at: datetime,
        full: bool,
        *,
        active_sweep: bool,
        audit: bool,
    ) -> None:
        checkpoint = {"last_successful_at": finished_at.isoformat()}
        if full:
            checkpoint["last_full_at"] = finished_at.isoformat()
        if active_sweep:
            checkpoint["last_active_sweep_at"] = finished_at.isoformat()
        if audit:
            checkpoint["last_audit_at"] = finished_at.isoformat()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ingest.source_checkpoints (
                    source_name, checkpoint_key, checkpoint_value
                )
                VALUES ('opus_api', 'jobs_sync', %s)
                ON CONFLICT (source_name, checkpoint_key) DO UPDATE
                SET checkpoint_value = ingest.source_checkpoints.checkpoint_value
                        || EXCLUDED.checkpoint_value,
                    updated_at = clock_timestamp()
                """,
                (Jsonb(checkpoint),),
            )

    def _upsert_definition(
        self,
        cursor: psycopg.Cursor[dict[str, Any]],
        definition: dict[str, Any],
    ) -> None:
        name = str(_value(definition, "Name", "EvaluationName", "ChecklistName") or "").strip()
        if not name or _normalise(name) == BULK_IMPORT_CHECKLIST_NAME:
            return
        stage_code, stage_order, workflow_role = _stage_config(name)
        definition_id = _uuid_text(_value(definition, "ID", "EvaluationID"))
        if definition_id:
            cursor.execute(
                """
                SELECT id
                FROM ops.checklist_definitions
                WHERE opus_checklist_id = %s
                """,
                (definition_id,),
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    """
                    UPDATE ops.checklist_definitions
                    SET active = true,
                        metadata = %s,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                    """,
                    (Jsonb(definition), existing["id"]),
                )
                return
        cursor.execute(
            """
            INSERT INTO ops.checklist_definitions (
                opus_checklist_id, canonical_name, stage_code, stage_order,
                workflow_role, active, configured_in_minerals, metadata
            )
            VALUES (%s, %s, %s, %s, %s, true, %s, %s)
            ON CONFLICT (stage_code) DO UPDATE
            SET active = true,
                metadata = EXCLUDED.metadata,
                updated_at = clock_timestamp()
            """,
            (
                definition_id,
                name,
                stage_code,
                stage_order,
                workflow_role,
                stage_code in {config[0] for config in STAGE_CONFIG.values()},
                Jsonb(definition),
            ),
        )

    def _upsert_job_bundle(
        self,
        cursor: psycopg.Cursor[dict[str, Any]],
        run_id: int,
        list_row: dict[str, Any],
        bundle: dict[str, Any],
    ) -> dict[str, int]:
        qualified_root_created_at = _timestamp(
            list_row.get("_qualified_transport_root_created_at")
        )
        list_row = _source_job_row(list_row)
        detail = bundle.get("job")
        if not isinstance(detail, dict):
            detail = {}
        merged = {**detail, **list_row}
        reference = str(_value(merged, "Reference", "JobReference") or "").strip()
        if not re.fullmatch(r"ORDBULK-\d+", reference):
            return _empty_detail_counts()
        business = _business_fields(bundle, merged)
        transporter_id = self._dimension_transporter(cursor, business.get("transporter"))
        vehicle_id = self._dimension_vehicle(
            cursor,
            business.get("truck_registration"),
            transporter_id,
            business.get("truck_type"),
        )
        driver_id = self._dimension_driver(cursor, business.get("driver_name"))
        loading_id = self._dimension_location(cursor, business.get("loading_point"))
        offloading_id = self._dimension_location(cursor, business.get("offloading_point"))

        supplied_checklist_name = _job_checklist_name(merged)
        checklist_name = supplied_checklist_name or "Transport Allocation"
        if _normalise(checklist_name) == BULK_IMPORT_CHECKLIST_NAME:
            return _empty_detail_counts()
        stage_code, stage_order, workflow_role = _stage_config(checklist_name)
        definition_id = _uuid_text(
            _value(merged, "EvaluationID", "EvaluationId", "ChecklistDefinitionID")
        )
        if definition_id:
            cursor.execute(
                """
                UPDATE ops.checklist_definitions
                SET opus_checklist_id = NULL,
                    updated_at = clock_timestamp()
                WHERE opus_checklist_id = %s
                  AND stage_code <> %s
                """,
                (definition_id, stage_code),
            )
        cursor.execute(
            """
            INSERT INTO ops.checklist_definitions (
                opus_checklist_id, canonical_name, stage_code, stage_order,
                workflow_role, active, configured_in_minerals, metadata
            )
            VALUES (%s, %s, %s, %s, %s, true, true, %s)
            ON CONFLICT (stage_code) DO UPDATE
            SET canonical_name = EXCLUDED.canonical_name,
                opus_checklist_id = coalesce(
                    EXCLUDED.opus_checklist_id,
                    ops.checklist_definitions.opus_checklist_id
                ),
                stage_order = EXCLUDED.stage_order,
                workflow_role = EXCLUDED.workflow_role,
                active = true,
                configured_in_minerals = true,
                updated_at = clock_timestamp()
            RETURNING id
            """,
            (
                definition_id,
                checklist_name,
                stage_code,
                stage_order,
                workflow_role,
                Jsonb({"source": "opus_api"}),
            ),
        )
        definition_row = cursor.fetchone()
        if not definition_row:
            raise RuntimeError(
                f"Checklist definition {stage_code!r} could not be resolved."
            )
        checklist_definition_id = int(definition_row["id"])
        is_transport_allocation = stage_code == "transport_allocation"
        status = _status(merged)
        booked_at = _timestamp(
            _value(merged, "ExpectedStartDate", "DateBooked", "CreatedDate")
        )
        source_updated = _timestamp(
            _value(
                merged,
                "LastUpdated",
                "HeaderUpdatedDate",
                "UpdatedDate",
                "SignOffDate",
            )
        )
        source_created_at = _timestamp(
            _value(merged, "CreateDate", "CreatedDate")
        )
        operator_started_at = _timestamp(
            _value(merged, "DateStarted", "StartDate")
        )
        source_completed_at = _timestamp(_value(merged, "CompletedDate"))
        source_signed_off_at = _timestamp(_value(merged, "SignOffDate"))
        opus_job_id = _uuid_text(_value(merged, "ID", "JobID"))
        raw_snapshot = {
            "list": list_row,
            "detail": detail,
            "business_fields": business,
        }
        cursor.execute(
            """
            INSERT INTO ops.allocations (
                job_reference, opus_root_job_id, transport_allocation_created_at,
                parcel_reference, order_reference, booked_at,
                loading_location_id, offloading_location_id,
                transporter_id, vehicle_id, driver_id, truck_type,
                current_status, current_stage_code, current_stage_order,
                source_updated_at, raw_snapshot
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (job_reference) DO UPDATE
            SET opus_root_job_id = coalesce(
                    ops.allocations.opus_root_job_id,
                    EXCLUDED.opus_root_job_id
                ),
                transport_allocation_created_at = coalesce(
                    EXCLUDED.transport_allocation_created_at,
                    ops.allocations.transport_allocation_created_at
                ),
                parcel_reference = coalesce(
                    EXCLUDED.parcel_reference,
                    ops.allocations.parcel_reference
                ),
                order_reference = coalesce(
                    EXCLUDED.order_reference,
                    ops.allocations.order_reference
                ),
                booked_at = CASE
                    WHEN ops.allocations.booked_at IS NULL THEN EXCLUDED.booked_at
                    WHEN EXCLUDED.booked_at IS NULL THEN ops.allocations.booked_at
                    ELSE least(ops.allocations.booked_at, EXCLUDED.booked_at)
                END,
                loading_location_id = coalesce(
                    EXCLUDED.loading_location_id,
                    ops.allocations.loading_location_id
                ),
                offloading_location_id = coalesce(
                    EXCLUDED.offloading_location_id,
                    ops.allocations.offloading_location_id
                ),
                transporter_id = coalesce(
                    EXCLUDED.transporter_id,
                    ops.allocations.transporter_id
                ),
                vehicle_id = coalesce(EXCLUDED.vehicle_id, ops.allocations.vehicle_id),
                driver_id = coalesce(EXCLUDED.driver_id, ops.allocations.driver_id),
                truck_type = coalesce(EXCLUDED.truck_type, ops.allocations.truck_type),
                current_status = CASE
                    WHEN (
                        coalesce(EXCLUDED.source_updated_at, '-infinity'::timestamptz),
                        coalesce(EXCLUDED.current_stage_order, -1)
                    ) >= (
                        coalesce(ops.allocations.source_updated_at, '-infinity'::timestamptz),
                        coalesce(ops.allocations.current_stage_order, -1)
                    ) THEN EXCLUDED.current_status
                    ELSE ops.allocations.current_status
                END,
                current_stage_code = CASE
                    WHEN (
                        coalesce(EXCLUDED.source_updated_at, '-infinity'::timestamptz),
                        coalesce(EXCLUDED.current_stage_order, -1)
                    ) >= (
                        coalesce(ops.allocations.source_updated_at, '-infinity'::timestamptz),
                        coalesce(ops.allocations.current_stage_order, -1)
                    ) THEN EXCLUDED.current_stage_code
                    ELSE ops.allocations.current_stage_code
                END,
                current_stage_order = CASE
                    WHEN (
                        coalesce(EXCLUDED.source_updated_at, '-infinity'::timestamptz),
                        coalesce(EXCLUDED.current_stage_order, -1)
                    ) >= (
                        coalesce(ops.allocations.source_updated_at, '-infinity'::timestamptz),
                        coalesce(ops.allocations.current_stage_order, -1)
                    ) THEN EXCLUDED.current_stage_order
                    ELSE ops.allocations.current_stage_order
                END,
                last_seen_at = clock_timestamp(),
                source_updated_at = greatest(
                    EXCLUDED.source_updated_at,
                    ops.allocations.source_updated_at
                ),
                raw_snapshot = CASE
                    WHEN (
                        coalesce(EXCLUDED.source_updated_at, '-infinity'::timestamptz),
                        coalesce(EXCLUDED.current_stage_order, -1)
                    ) >= (
                        coalesce(ops.allocations.source_updated_at, '-infinity'::timestamptz),
                        coalesce(ops.allocations.current_stage_order, -1)
                    ) THEN EXCLUDED.raw_snapshot
                    ELSE ops.allocations.raw_snapshot
                END
            RETURNING id
            """,
            (
                reference,
                opus_job_id if is_transport_allocation else None,
                qualified_root_created_at,
                business.get("parcel_reference"),
                business.get("order_reference")
                or _value(merged, "OrderNumber", "OrderReference"),
                booked_at,
                loading_id,
                offloading_id,
                transporter_id,
                vehicle_id,
                driver_id,
                business.get("truck_type"),
                status,
                stage_code,
                stage_order,
                source_updated,
                Jsonb(raw_snapshot),
            ),
        )
        allocation_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO ops.jobs (
                opus_job_id, allocation_id, checklist_definition_id, job_reference,
                status, status_detail, expected_start_at, last_updated_at, due_at,
                source_created_at, operator_started_at, source_completed_at,
                source_signed_off_at, created_by_name, operator_created,
                created_from_operator_name,
                site_name, stock_name, operator_name, context_user_name,
                workflow_parent_job_id, raw_attributes
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (opus_job_id) WHERE opus_job_id IS NOT NULL DO UPDATE
            SET allocation_id = EXCLUDED.allocation_id,
                checklist_definition_id = EXCLUDED.checklist_definition_id,
                status = EXCLUDED.status,
                status_detail = EXCLUDED.status_detail,
                expected_start_at = EXCLUDED.expected_start_at,
                last_updated_at = EXCLUDED.last_updated_at,
                due_at = EXCLUDED.due_at,
                source_created_at = EXCLUDED.source_created_at,
                operator_started_at = EXCLUDED.operator_started_at,
                source_completed_at = EXCLUDED.source_completed_at,
                source_signed_off_at = EXCLUDED.source_signed_off_at,
                created_by_name = EXCLUDED.created_by_name,
                operator_created = EXCLUDED.operator_created,
                created_from_operator_name = EXCLUDED.created_from_operator_name,
                site_name = EXCLUDED.site_name,
                stock_name = EXCLUDED.stock_name,
                operator_name = EXCLUDED.operator_name,
                context_user_name = EXCLUDED.context_user_name,
                workflow_parent_job_id = EXCLUDED.workflow_parent_job_id,
                raw_attributes = EXCLUDED.raw_attributes,
                last_seen_at = clock_timestamp()
            RETURNING id
            """,
            (
                opus_job_id,
                allocation_id,
                checklist_definition_id,
                reference,
                status,
                str(_value(merged, "StatusDescription", "JobStatusDescription") or ""),
                booked_at,
                source_updated,
                _timestamp(_value(merged, "ExpectedCompletionDate", "DueDate")),
                source_created_at,
                operator_started_at,
                source_completed_at,
                source_signed_off_at,
                _text_value(_value(merged, "CreatedByUserName")),
                bool(_bool_value(_value(merged, "OperatorCreated"))),
                _text_value(_value(merged, "CreatedFromOperator")),
                _value(merged, "SiteName"),
                _value(merged, "ItemName", "StockName", "Description"),
                _value(
                    merged,
                    "OperatorName",
                    "OperatorList",
                    "Username",
                    "UserName",
                ),
                _value(merged, "ContextUserName"),
                _uuid_text(_value(merged, "CreatedFromJobID", "ParentJobID")),
                Jsonb({**bundle, "list": list_row}),
            ),
        )
        job_row_id = int(cursor.fetchone()["id"])
        detail_counts = self._upsert_checklist_details(
            cursor,
            allocation_id,
            job_row_id,
            checklist_definition_id,
            reference,
            bundle,
        )
        cursor.execute(
            "SELECT ops.refresh_operational_facts(%s)",
            (job_row_id,),
        )
        cursor.execute(
            """
            INSERT INTO ops.job_events (
                observed_at, extraction_run_id, allocation_id, job_id,
                checklist_definition_id, event_type, event_at, status,
                operator_name, payload
            )
            VALUES (
                clock_timestamp(), %s, %s, %s, %s, 'job_observed',
                %s, %s, %s, %s
            )
            """,
            (
                run_id,
                allocation_id,
                job_row_id,
                checklist_definition_id,
                source_updated,
                status,
                _value(merged, "OperatorName", "Username", "UserName"),
                Jsonb({"source": "opus_api", "stage_code": stage_code}),
            ),
        )
        self._transit_snapshot(
            cursor,
            run_id,
            allocation_id,
            reference,
            booked_at,
            business,
            bundle,
        )
        return detail_counts

    @staticmethod
    def _upsert_checklist_details(
        cursor: psycopg.Cursor[dict[str, Any]],
        allocation_id: int,
        job_row_id: int,
        fallback_definition_id: int,
        reference: str,
        bundle: dict[str, Any],
    ) -> dict[str, int]:
        counts = _empty_detail_counts()
        for item in bundle.get("checklists") or []:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            checklist = {**summary, **detail}
            instance_id = _uuid_text(
                _value(checklist, "ID", "AnswerEvaluationID", "AnswerEvaluationId")
            )
            if not instance_id:
                continue
            evaluation_id = _uuid_text(
                _value(checklist, "EvaluationID", "EvaluationId")
            )
            instance_name = str(
                _value(checklist, "Name", "ChecklistName") or "Checklist"
            )
            if _normalise(instance_name) == BULK_IMPORT_CHECKLIST_NAME:
                continue
            instance_stage_code, _, _ = _stage_config(instance_name)
            definition_id = fallback_definition_id
            cursor.execute(
                """
                SELECT id
                FROM ops.checklist_definitions
                WHERE stage_code = %s
                """,
                (instance_stage_code,),
            )
            definition = cursor.fetchone()
            if definition:
                definition_id = int(definition["id"])

            answer_rows = _checklist_answer_rows(item)
            sections = [
                section
                for section in item.get("sections") or []
                if isinstance(section, dict)
            ]
            detail_errors = [
                error
                for error in item.get("detail_errors") or []
                if isinstance(error, dict)
            ]
            expected_sections = [
                section
                for section in detail.get("Sections") or []
                if isinstance(section, dict)
            ]
            normalized_section_count = len(sections) or len(
                {
                    int(row["section_sequence"])
                    for row in answer_rows
                }
            )
            image_count = sum(
                len(row["answer"].get("AnswerImages") or [])
                for row in answer_rows
                if isinstance(row["answer"].get("AnswerImages"), list)
            )
            item_count = sum(
                len(row["answer"].get("AnswerItems") or [])
                for row in answer_rows
                if isinstance(row["answer"].get("AnswerItems"), list)
            )
            table_count = sum(
                1
                for row in answer_rows
                if row["answer"].get("Tabledata") not in (None, "", [], {})
                or row["answer"].get("TableColumnlist") not in (None, "", [], {})
            )
            instance_parameters = {
                "instance_id": instance_id,
                "job_id": job_row_id,
                "allocation_id": allocation_id,
                "definition_id": definition_id,
                "reference": reference,
                "evaluation_id": evaluation_id,
                "name": instance_name,
                "description": _text_value(_value(checklist, "Description")),
                "percentage_complete": _text_value(_value(checklist, "PercentageComplete")),
                "score": _text_value(_value(checklist, "Score")),
                "total_score": _text_value(_value(checklist, "TotalScore")),
                "possible_score": _text_value(_value(checklist, "PossibleScore")),
                "priority": _text_value(_value(checklist, "Priority")),
                "operator_name": _display_name(_value(checklist, "Operator")),
                "duration": _text_value(_value(checklist, "Duration")),
                "weighted": _bool_value(_value(checklist, "Weighted")),
                "started_at": _timestamp(_value(checklist, "StartDate", "DateStarted")),
                "source_updated_at": _timestamp(_value(checklist, "LastUpdated")),
                "site_name": _text_value(_value(checklist, "SiteName")),
                "classification": _text_value(
                    _value(checklist, "EvaluationClassification")
                ),
                "classification_name": _text_value(
                    _value(checklist, "EvaluationClassificationName")
                ),
                "evaluation_type_id": _text_value(
                    _value(checklist, "EvaluationTypeID")
                ),
                "log_coordinates": Jsonb(
                    _value(checklist, "LogCoords") or {}
                ),
                "section_count": normalized_section_count,
                "answer_count": len(answer_rows),
                "image_count": image_count,
                "item_count": item_count,
                "table_count": table_count,
                "detail_complete": (
                    not detail_errors and len(sections) >= len(expected_sections)
                ),
                "detail_errors": Jsonb(detail_errors),
                "raw_payload": Jsonb(item),
            }
            cursor.execute(
                """
                INSERT INTO ops.checklist_instances (
                    opus_checklist_instance_id, job_id, allocation_id,
                    checklist_definition_id, job_reference, opus_evaluation_id,
                    name, description, percentage_complete, score, total_score,
                    possible_score, priority, operator_name, duration, weighted,
                    started_at, source_updated_at, site_name,
                    evaluation_classification, evaluation_classification_name,
                    evaluation_type_id, log_coordinates, section_count,
                    answer_count, image_count, item_count, table_count,
                    detail_complete, detail_errors, raw_payload
                )
                VALUES (
                    %(instance_id)s, %(job_id)s, %(allocation_id)s,
                    %(definition_id)s, %(reference)s, %(evaluation_id)s,
                    %(name)s, %(description)s, %(percentage_complete)s,
                    %(score)s, %(total_score)s, %(possible_score)s,
                    %(priority)s, %(operator_name)s, %(duration)s, %(weighted)s,
                    %(started_at)s, %(source_updated_at)s, %(site_name)s,
                    %(classification)s, %(classification_name)s,
                    %(evaluation_type_id)s, %(log_coordinates)s,
                    %(section_count)s, %(answer_count)s, %(image_count)s,
                    %(item_count)s, %(table_count)s, %(detail_complete)s,
                    %(detail_errors)s, %(raw_payload)s
                )
                ON CONFLICT (opus_checklist_instance_id) DO UPDATE
                SET job_id = EXCLUDED.job_id,
                    allocation_id = EXCLUDED.allocation_id,
                    checklist_definition_id = EXCLUDED.checklist_definition_id,
                    job_reference = EXCLUDED.job_reference,
                    opus_evaluation_id = EXCLUDED.opus_evaluation_id,
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    percentage_complete = EXCLUDED.percentage_complete,
                    score = EXCLUDED.score,
                    total_score = EXCLUDED.total_score,
                    possible_score = EXCLUDED.possible_score,
                    priority = EXCLUDED.priority,
                    operator_name = EXCLUDED.operator_name,
                    duration = EXCLUDED.duration,
                    weighted = EXCLUDED.weighted,
                    started_at = EXCLUDED.started_at,
                    source_updated_at = EXCLUDED.source_updated_at,
                    site_name = EXCLUDED.site_name,
                    evaluation_classification = EXCLUDED.evaluation_classification,
                    evaluation_classification_name = EXCLUDED.evaluation_classification_name,
                    evaluation_type_id = EXCLUDED.evaluation_type_id,
                    log_coordinates = EXCLUDED.log_coordinates,
                    section_count = EXCLUDED.section_count,
                    answer_count = EXCLUDED.answer_count,
                    image_count = EXCLUDED.image_count,
                    item_count = EXCLUDED.item_count,
                    table_count = EXCLUDED.table_count,
                    detail_complete = EXCLUDED.detail_complete,
                    detail_errors = EXCLUDED.detail_errors,
                    raw_payload = EXCLUDED.raw_payload,
                    last_seen_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                RETURNING id
                """,
                instance_parameters,
            )
            checklist_instance_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "DELETE FROM ops.checklist_answers WHERE checklist_instance_id = %s",
                (checklist_instance_id,),
            )
            for row in answer_rows:
                section = row["section"]
                subsection = row["subsection"]
                group = row["group"]
                answer = row["answer"]
                cursor.execute(
                    """
                    INSERT INTO ops.checklist_answers (
                        checklist_instance_id, opus_answer_id, parent_answer_id,
                        opus_question_id, opus_section_id, answer_section_id,
                        answer_subsection_id, section_name, section_report_full,
                        section_report_short, section_sequence, subsection_name,
                        subsection_number, subsection_repeat, subsection_created_at,
                        question, question_report_full, question_report_short,
                        question_summary, unformatted_question_text, action_text,
                        optional_question, require_comment, priority_id,
                        question_type, question_type_id, question_subtype,
                        question_subtype_id, question_unit, question_function,
                        question_min_range, question_max_range,
                        question_alert_min_range, question_alert_max_range,
                        answer_text, text_value, unformatted_answer,
                        report_formatted_answer, comments, question_number,
                        answer_not_applicable, score, possible_score, score_range,
                        question_classification, scan_type_id, source_reference,
                        answer_subsection_created_at, operator_name, answer_extra,
                        answer_images, answer_items, child_checklist_answers,
                        table_data, table_columns, raw_payload
                    )
                    VALUES (
                        %(instance_id)s, %(answer_id)s, %(parent_answer_id)s,
                        %(question_id)s, %(section_id)s, %(answer_section_id)s,
                        %(answer_subsection_id)s, %(section_name)s,
                        %(section_report_full)s, %(section_report_short)s,
                        %(section_sequence)s, %(subsection_name)s,
                        %(subsection_number)s, %(subsection_repeat)s,
                        %(subsection_created_at)s, %(question)s,
                        %(question_report_full)s, %(question_report_short)s,
                        %(question_summary)s, %(unformatted_question_text)s,
                        %(action_text)s, %(optional_question)s,
                        %(require_comment)s, %(priority_id)s, %(question_type)s,
                        %(question_type_id)s, %(question_subtype)s,
                        %(question_subtype_id)s, %(question_unit)s,
                        %(question_function)s, %(question_min_range)s,
                        %(question_max_range)s, %(question_alert_min_range)s,
                        %(question_alert_max_range)s, %(answer_text)s,
                        %(text_value)s, %(unformatted_answer)s,
                        %(report_formatted_answer)s, %(comments)s,
                        %(question_number)s, %(answer_not_applicable)s,
                        %(score)s, %(possible_score)s, %(score_range)s,
                        %(question_classification)s, %(scan_type_id)s,
                        %(source_reference)s, %(answer_subsection_created_at)s,
                        %(operator_name)s, %(answer_extra)s, %(answer_images)s,
                        %(answer_items)s, %(child_checklist_answers)s,
                        %(table_data)s, %(table_columns)s, %(raw_payload)s
                    )
                    """,
                    {
                        "instance_id": checklist_instance_id,
                        "answer_id": _uuid_text(_value(answer, "AnswerID")),
                        "parent_answer_id": _uuid_text(
                            _value(answer, "ParentAnswerID")
                        ),
                        "question_id": _uuid_text(_value(answer, "QuestionID")),
                        "section_id": _uuid_text(_value(section, "ID", "SectionID")),
                        "answer_section_id": _uuid_text(
                            _value(answer, "AnswerSectionID")
                        ),
                        "answer_subsection_id": _uuid_text(
                            _value(answer, "AnswerSubsectionID")
                            or _value(group, "AnswersubsectionID")
                        ),
                        "section_name": _text_value(_value(section, "Name")),
                        "section_report_full": _text_value(
                            _value(section, "NameReportFull")
                        ),
                        "section_report_short": _text_value(
                            _value(section, "NameReportShort")
                        ),
                        "section_sequence": row["section_sequence"],
                        "subsection_name": _text_value(_value(subsection, "Name")),
                        "subsection_number": _text_value(
                            _value(subsection, "Number")
                        ),
                        "subsection_repeat": _bool_value(
                            _value(subsection, "SubsectionRepeat")
                        ),
                        "subsection_created_at": _timestamp(
                            _value(subsection, "CreatedDate")
                        ),
                        "question": str(_value(answer, "Question") or "Question"),
                        "question_report_full": _text_value(
                            _value(answer, "QuestionReportFull")
                        ),
                        "question_report_short": _text_value(
                            _value(answer, "QuestionReportShort")
                        ),
                        "question_summary": _text_value(
                            _value(answer, "QuestionSummary")
                        ),
                        "unformatted_question_text": _text_value(
                            _value(answer, "UnformattedQuestionText")
                        ),
                        "action_text": _text_value(_value(answer, "ActionText")),
                        "optional_question": _bool_value(
                            _value(answer, "OptionalQuestion")
                        ),
                        "require_comment": _bool_value(
                            _value(answer, "RequireComment")
                        ),
                        "priority_id": _text_value(_value(answer, "PriorityID")),
                        "question_type": _text_value(
                            _value(answer, "QuestionType")
                        ),
                        "question_type_id": _text_value(
                            _value(answer, "QuestionTypeID")
                        ),
                        "question_subtype": _text_value(
                            _value(answer, "QuestionSubType")
                        ),
                        "question_subtype_id": _text_value(
                            _value(answer, "QuestionSubTypeID")
                        ),
                        "question_unit": _text_value(
                            _value(answer, "QuestionUnit")
                        ),
                        "question_function": _text_value(
                            _value(answer, "QuestionFunction")
                        ),
                        "question_min_range": _text_value(
                            _value(answer, "QuestionMinRange")
                        ),
                        "question_max_range": _text_value(
                            _value(answer, "QuestionMaxRange")
                        ),
                        "question_alert_min_range": _text_value(
                            _value(answer, "QuestionAlertMinRange")
                        ),
                        "question_alert_max_range": _text_value(
                            _value(answer, "QuestionAlertMaxRange")
                        ),
                        "answer_text": _text_value(_value(answer, "Answer")),
                        "text_value": _text_value(_value(answer, "Text")),
                        "unformatted_answer": _text_value(
                            _value(answer, "UnformattedAnswer")
                        ),
                        "report_formatted_answer": _text_value(
                            _value(answer, "ReportFormattedAnswer")
                        ),
                        "comments": _text_value(_value(answer, "Comments")),
                        "question_number": _text_value(_value(answer, "Number")),
                        "answer_not_applicable": _bool_value(
                            _value(answer, "NumberAnswerNotApplicable")
                        ),
                        "score": _text_value(_value(answer, "Score")),
                        "possible_score": _text_value(
                            _value(answer, "PossibleScore")
                        ),
                        "score_range": _text_value(_value(answer, "ScoreRange")),
                        "question_classification": _text_value(
                            _value(answer, "QuestionClassification")
                        ),
                        "scan_type_id": _text_value(_value(answer, "ScanTypeID")),
                        "source_reference": _text_value(
                            _value(answer, "Reference")
                        ),
                        "answer_subsection_created_at": _timestamp(
                            _value(answer, "AnswerSubsectionCreatedDate")
                        ),
                        "operator_name": _text_value(
                            _value(answer, "OperatorName")
                        ),
                        "answer_extra": Jsonb(
                            answer.get("AnswerExtra")
                            if answer.get("AnswerExtra") is not None
                            else {}
                        ),
                        "answer_images": Jsonb(answer.get("AnswerImages") or []),
                        "answer_items": Jsonb(answer.get("AnswerItems") or []),
                        "child_checklist_answers": Jsonb(
                            answer.get("ChildChecklistAnswers") or []
                        ),
                        "table_data": Jsonb(
                            answer.get("Tabledata")
                            if answer.get("Tabledata") is not None
                            else {}
                        ),
                        "table_columns": Jsonb(
                            answer.get("TableColumnlist") or []
                        ),
                        "raw_payload": Jsonb(answer),
                    },
                )
            counts["checklist_instances"] += 1
            counts["checklist_sections"] += normalized_section_count
            counts["checklist_answers"] += len(answer_rows)
            counts["detail_errors"] += len(detail_errors)
        return counts

    def _raw_record(
        self,
        cursor: psycopg.Cursor[dict[str, Any]],
        run_id: int,
        source_name: str,
        source_key: str,
        record_type: str,
        payload: dict[str, Any],
    ) -> tuple[bool, bool]:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).digest()
        has_col = self._check_source_hash_column(cursor.connection)
        if has_col:
            cursor.execute(
                """
                SELECT payload, source_payload_sha256
                FROM ingest.raw_records
                WHERE source_name = %s
                  AND source_record_key = %s
                  AND record_type = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (source_name, source_key, record_type),
            )
        else:
            cursor.execute(
                """
                SELECT payload
                FROM ingest.raw_records
                WHERE source_name = %s
                  AND source_record_key = %s
                  AND record_type = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (source_name, source_key, record_type),
            )
        existing = cursor.fetchone()
        if existing:
            if has_col:
                existing_source_hash = existing.get("source_payload_sha256")
                if existing_source_hash is not None:
                    if bytes(existing_source_hash) == payload_hash:
                        return False, False
                elif existing["payload"] == payload:
                    return False, False
            else:
                if existing["payload"] == payload:
                    return False, False
        if has_col:
            cursor.execute(
                """
                INSERT INTO ingest.raw_records (
                    captured_at, extraction_run_id, source_name, source_record_key,
                    record_type, payload, payload_sha256, source_payload_sha256,
                    processing_status, processed_at
                )
                VALUES (
                    clock_timestamp(), %s, %s, %s, %s, %s, %s, %s, 'processed',
                    clock_timestamp()
                )
                """,
                (
                    run_id,
                    source_name,
                    source_key,
                    record_type,
                    Jsonb(payload),
                    payload_hash,
                    payload_hash,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO ingest.raw_records (
                    captured_at, extraction_run_id, source_name, source_record_key,
                    record_type, payload, processing_status, processed_at
                )
                VALUES (
                    clock_timestamp(), %s, %s, %s, %s, %s, 'processed',
                    clock_timestamp()
                )
                """,
                (
                    run_id,
                    source_name,
                    source_key,
                    record_type,
                    Jsonb(payload),
                ),
            )
        return True, existing is None

    @staticmethod
    def _errored_job_ids(
        cursor: psycopg.Cursor[dict[str, Any]],
        opus_job_ids: list[str],
    ) -> set[str]:
        """Return the subset of opus_job_ids that have at least one recorded
        extraction error so they are always re-attempted on the next sync."""
        if not opus_job_ids:
            return set()
        cursor.execute(
            """
            SELECT DISTINCT opus_job_id::text AS opus_job_id
            FROM ingest.extraction_errors
            WHERE opus_job_id = ANY(%s::uuid[])
            """,
            (opus_job_ids,),
        )
        return {str(row["opus_job_id"]) for row in cursor.fetchall()}

    @staticmethod
    def _completed_detail_job_ids(
        cursor: psycopg.Cursor[dict[str, Any]],
        opus_job_ids: list[str],
    ) -> set[str]:
        if not opus_job_ids:
            return set()
        cursor.execute(
            """
            SELECT DISTINCT j.opus_job_id::text AS opus_job_id
            FROM ops.checklist_instances ci
            JOIN ops.jobs j ON j.id = ci.job_id
            WHERE j.opus_job_id = ANY(%s::uuid[])
              AND ci.detail_complete
            """,
            (opus_job_ids,),
        )
        return {str(row["opus_job_id"]) for row in cursor.fetchall()}

    @staticmethod
    def _dimension_location(
        cursor: psycopg.Cursor[dict[str, Any]],
        name: Any,
    ) -> int | None:
        text = str(name or "").strip()
        if not text:
            return None
        cursor.execute(
            """
            INSERT INTO ops.locations (name, metadata)
            VALUES (%s, '{"source":"opus_api"}'::jsonb)
            ON CONFLICT (normalized_name) DO UPDATE
            SET name = EXCLUDED.name, updated_at = clock_timestamp()
            RETURNING id
            """,
            (text,),
        )
        return int(cursor.fetchone()["id"])

    @staticmethod
    def _dimension_transporter(
        cursor: psycopg.Cursor[dict[str, Any]],
        name: Any,
    ) -> int | None:
        text = str(name or "").strip()
        if not text:
            return None
        cursor.execute(
            """
            INSERT INTO ops.transporters (name, metadata)
            VALUES (%s, '{"source":"opus_api"}'::jsonb)
            ON CONFLICT (normalized_name) DO UPDATE
            SET name = EXCLUDED.name, active = true, updated_at = clock_timestamp()
            RETURNING id
            """,
            (text,),
        )
        return int(cursor.fetchone()["id"])

    @staticmethod
    def _dimension_vehicle(
        cursor: psycopg.Cursor[dict[str, Any]],
        registration: Any,
        transporter_id: int | None,
        truck_type: Any,
    ) -> int | None:
        text = str(registration or "").strip()
        if not text:
            return None
        cursor.execute(
            """
            INSERT INTO ops.vehicles (
                registration, transporter_id, truck_type, metadata
            )
            VALUES (%s, %s, %s, '{"source":"opus_api"}'::jsonb)
            ON CONFLICT (normalized_registration) DO UPDATE
            SET registration = EXCLUDED.registration,
                transporter_id = coalesce(
                    EXCLUDED.transporter_id,
                    ops.vehicles.transporter_id
                ),
                truck_type = coalesce(EXCLUDED.truck_type, ops.vehicles.truck_type),
                active = true,
                updated_at = clock_timestamp()
            RETURNING id
            """,
            (text, transporter_id, str(truck_type or "").strip() or None),
        )
        return int(cursor.fetchone()["id"])

    @staticmethod
    def _dimension_driver(
        cursor: psycopg.Cursor[dict[str, Any]],
        name: Any,
    ) -> int | None:
        text = str(name or "").strip()
        if not text:
            return None
        cursor.execute(
            """
            SELECT id
            FROM ops.drivers
            WHERE normalized_name = lower(btrim(%s))
            ORDER BY id
            LIMIT 1
            """,
            (text,),
        )
        row = cursor.fetchone()
        if row:
            return int(row["id"])
        cursor.execute(
            """
            INSERT INTO ops.drivers (full_name, metadata)
            VALUES (%s, '{"source":"opus_api"}'::jsonb)
            RETURNING id
            """,
            (text,),
        )
        return int(cursor.fetchone()["id"])

    @staticmethod
    def _transit_snapshot(
        cursor: psycopg.Cursor[dict[str, Any]],
        run_id: int,
        allocation_id: int,
        reference: str,
        booked_at: datetime | None,
        business: dict[str, Any],
        bundle: dict[str, Any],
    ) -> None:
        stage_times = _stage_times(bundle)
        payload = {
            "business_fields": business,
            "stage_times": {
                key: value.isoformat() if value else None
                for key, value in stage_times.items()
            },
        }
        cursor.execute(
            """
            SELECT raw_payload
            FROM ops.transit_snapshots
            WHERE job_reference = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (reference,),
        )
        latest = cursor.fetchone()
        if latest and latest["raw_payload"] == payload:
            return
        cursor.execute(
            """
            INSERT INTO ops.transit_snapshots (
                captured_at, extraction_run_id, allocation_id, job_reference,
                date_booked, parcel_reference, order_reference, loading_point,
                offloading_point, transporter_name, truck_registration, truck_type,
                driver_name, vehicle_inspection_at, loading_exit_at,
                staging_arrival_at, staging_exit_at, truck_arrival_at,
                offloading_exit_at, opus_job_detail_url, raw_payload
            )
            VALUES (
                clock_timestamp(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                run_id,
                allocation_id,
                reference,
                booked_at,
                business.get("parcel_reference"),
                business.get("order_reference"),
                business.get("loading_point"),
                business.get("offloading_point"),
                business.get("transporter"),
                business.get("truck_registration"),
                business.get("truck_type"),
                business.get("driver_name"),
                stage_times.get("vehicle_inspection"),
                stage_times.get("loading_exit"),
                stage_times.get("staging_arrival"),
                stage_times.get("staging_exit"),
                stage_times.get("truck_arrival"),
                stage_times.get("offloading_exit"),
                "https://app.opus4business.com/#/JobsDetail?JobID="
                + str(_value(bundle.get("job") or {}, "ID", "JobID") or ""),
                Jsonb(payload),
            ),
        )

    @staticmethod
    def _notify(
        callback: Callable[[dict[str, Any]], None] | None,
        **values: Any,
    ) -> None:
        if callback:
            callback(values)


class SyncCoordinator:
    def __init__(self, engine: OpusSyncEngine) -> None:
        self.engine = engine
        self._async_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._progress = SyncProgress()
        self._cancel_event = threading.Event()

    def snapshot(self) -> SyncProgress:
        with self._state_lock:
            return SyncProgress(**asdict(self._progress))

    def cancel(self) -> None:
        """Signal the running sync to stop at the next safe checkpoint."""
        self._cancel_event.set()

    def record_loop_error(self, exc: Exception) -> None:
        """
        Record a pre-run guard failure (e.g. credential read error, DB check)
        in the progress snapshot so it appears in the extraction status panel.
        Only writes when no sync is actively running.
        """
        if not self.snapshot().running:
            self._set_progress(
                {
                    "phase": "Auto-sync guard failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )

    async def run(self, *, full: bool) -> SyncResult | None:
        self._cancel_event.clear()
        async with self._async_lock:
            mode = "Full detail" if full else "Live sync"
            fresh_progress = asdict(SyncProgress())
            fresh_progress.update(
                {
                    "running": True,
                    "mode": mode,
                    "phase": "Starting",
                    "phase_key": "discovery",
                    "started_at": datetime.now().astimezone().isoformat(),
                }
            )
            self._set_progress(fresh_progress)
            try:
                result = await asyncio.to_thread(
                    self.engine.run,
                    full=full,
                    progress=self._set_progress,
                    stop_event=self._cancel_event,
                )
                completed = {
                    key: value
                    for key, value in asdict(result).items()
                    if hasattr(self._progress, key)
                    and key not in {"started_at", "finished_at"}
                }
                completed.update(
                    {
                        "running": False,
                        "phase": "Completed",
                        "phase_key": "completed",
                        "phase_current": result.bundles_completed,
                        "phase_total": result.bundles_queued,
                        "finished_at": result.finished_at.astimezone().isoformat(),
                    }
                )
                self._set_progress(completed)
                return result
            except SyncCancelledError:
                self._set_progress(
                    {
                        "running": False,
                        "phase": "Cancelled",
                        "phase_key": "cancelled",
                        "finished_at": datetime.now().astimezone().isoformat(),
                        "cancelled": True,
                        "error": "",
                    }
                )
                return None  # intentional stop — not an error
            except Exception as exc:
                self._set_progress(
                    {
                        "running": False,
                        "phase": "Failed",
                        "phase_key": "failed",
                        "finished_at": datetime.now().astimezone().isoformat(),
                        "error": str(exc),
                    }
                )
                raise

    def _set_progress(self, values: dict[str, Any]) -> None:
        with self._state_lock:
            for key, value in values.items():
                if hasattr(self._progress, key):
                    setattr(self._progress, key, value)
            self._progress.last_progress_at = (
                datetime.now().astimezone().isoformat()
            )


def _date_chunks(start: date, end: date, *, days: int) -> Iterable[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _live_scan_start(date_from: date, date_to: date, lookback_days: int) -> date:
    return max(
        date_from,
        date_to - timedelta(days=max(lookback_days, 1) - 1),
    )


def _select_bounded_candidates(
    entries: list[tuple[tuple[int, float], dict[str, Any]]],
    limit: int,
    *,
    full: bool,
) -> list[dict[str, Any]]:
    if full or len(entries) <= limit:
        return [row for _, row in entries]

    active_audits = [
        row
        for (tier, _), row in entries
        if tier >= 4 and row.get("_audit_kind") == "active"
    ]
    historical_audits = [
        row
        for (tier, _), row in entries
        if tier >= 4 and row.get("_audit_kind") == "historical"
    ]
    reserved = active_audits[:3] + historical_audits[:2]
    reserved_ids = {id(row) for row in reserved}
    live_entries = [
        row
        for (tier, _), row in entries
        if tier < 4
    ]
    selected = live_entries[: max(limit - len(reserved), 0)] + reserved
    selected_ids = {id(row) for row in selected}

    if len(selected) < limit:
        for _, row in entries:
            if id(row) in selected_ids or id(row) in reserved_ids:
                continue
            selected.append(row)
            selected_ids.add(id(row))
            if len(selected) >= limit:
                break
    return selected


def _source_job_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if not str(key).startswith("_")
    }


def _stored_job_row(
    row: dict[str, Any],
    *,
    audit_requested: bool = False,
) -> dict[str, Any]:
    raw_attributes = row.get("raw_attributes")
    stored_list = (
        raw_attributes.get("list")
        if isinstance(raw_attributes, dict)
        and isinstance(raw_attributes.get("list"), dict)
        else {}
    )
    result = dict(stored_list)
    fallbacks = {
        "ID": row.get("job_id"),
        "Reference": row.get("job_reference"),
        "ChecklistName": row.get("checklist_name"),
        "Status": row.get("status"),
        "StatusDescription": row.get("status_detail") or row.get("status"),
        "CreateDate": row.get("source_created_at"),
        "LastUpdated": row.get("last_updated_at"),
    }
    for key, value in fallbacks.items():
        if value not in (None, ""):
            result.setdefault(key, value)
    if audit_requested:
        result["_audit_requested"] = True
    return result


def _job_created_at(row: dict[str, Any]) -> datetime | None:
    return _timestamp(_value(row, "CreateDate", "CreatedDate"))


def _qualify_transport_workflows(
    rows: list[dict[str, Any]],
    date_from: date,
    date_to: date,
) -> WorkflowSelection:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        reference = str(_value(row, "Reference", "JobReference") or "").strip()
        if re.fullmatch(r"ORDBULK-\d+", reference):
            grouped.setdefault(reference, []).append(row)

    selected: list[dict[str, Any]] = []
    eligible_references = 0
    missing_root_references = 0
    out_of_window_root_references = 0
    bulk_import_jobs_excluded = 0
    duplicate_root_jobs_excluded = 0
    pre_root_jobs_excluded = 0

    for reference_rows in grouped.values():
        roots: list[tuple[datetime, dict[str, Any]]] = []
        for row in reference_rows:
            normalized_name = _normalise(_job_checklist_name(row))
            if normalized_name == BULK_IMPORT_CHECKLIST_NAME:
                bulk_import_jobs_excluded += 1
                continue
            if normalized_name != TRANSPORT_ALLOCATION_CHECKLIST_NAME:
                continue
            created_at = _job_created_at(row)
            if created_at is not None:
                roots.append((created_at, row))

        if not roots:
            missing_root_references += 1
            continue

        eligible_roots = [
            (created_at, row)
            for created_at, row in roots
            if date_from <= created_at.date() <= date_to
        ]
        if not eligible_roots:
            out_of_window_root_references += 1
            continue

        root_created_at, root_row = min(
            eligible_roots,
            key=lambda candidate: (
                candidate[0],
                str(_value(candidate[1], "ID", "JobID") or ""),
            ),
        )
        root_job_id = _uuid_text(_value(root_row, "ID", "JobID"))
        eligible_references += 1

        for row in reference_rows:
            normalized_name = _normalise(_job_checklist_name(row))
            if normalized_name == BULK_IMPORT_CHECKLIST_NAME:
                continue
            job_id = _uuid_text(_value(row, "ID", "JobID"))
            if (
                normalized_name == TRANSPORT_ALLOCATION_CHECKLIST_NAME
                and job_id != root_job_id
            ):
                duplicate_root_jobs_excluded += 1
                continue
            created_at = _job_created_at(row)
            if row is not root_row and (
                created_at is None or created_at < root_created_at
            ):
                pre_root_jobs_excluded += 1
                continue
            qualified = dict(row)
            qualified["_qualified_transport_root_created_at"] = (
                root_created_at.isoformat()
            )
            qualified["_qualified_transport_root_job_id"] = root_job_id
            selected.append(qualified)

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        root_created_at = _timestamp(
            row.get("_qualified_transport_root_created_at")
        )
        created_at = _job_created_at(row)
        stage_code, stage_order, _ = _stage_config(_job_checklist_name(row))
        reference = str(_value(row, "Reference", "JobReference") or "")
        suffix = reference.rsplit("-", 1)[-1]
        return (
            root_created_at or datetime.max.replace(tzinfo=timezone.utc),
            int(suffix) if suffix.isdigit() else 0,
            created_at or datetime.max.replace(tzinfo=timezone.utc),
            stage_order,
            stage_code,
            str(_value(row, "ID", "JobID") or ""),
        )

    selected.sort(key=sort_key)
    return WorkflowSelection(
        rows=selected,
        references_scanned=len(grouped),
        eligible_references=eligible_references,
        excluded_missing_root_references=missing_root_references,
        excluded_out_of_window_root_references=out_of_window_root_references,
        bulk_import_jobs_excluded=bulk_import_jobs_excluded,
        duplicate_root_jobs_excluded=duplicate_root_jobs_excluded,
        pre_root_jobs_excluded=pre_root_jobs_excluded,
    )


def _value(data: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).casefold(): value for key, value in data.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _empty_detail_counts() -> dict[str, int]:
    return {
        "checklist_instances": 0,
        "checklist_sections": 0,
        "checklist_answers": 0,
        "detail_errors": 0,
    }


def _should_load_detail(
    *,
    force_reload_all: bool,
    detail_exists: bool,
    changed: bool,
    status: str,
    previously_errored: bool = False,
) -> bool:
    return (
        force_reload_all
        or not detail_exists
        or changed
        or previously_errored  # always retry jobs that failed in a prior run
    )


def _is_terminal_status(status: str) -> bool:
    normalized = _normalise(status)
    return any(
        marker in normalized
        for marker in ("signed off", "completed", "closed", "cancelled", "canceled")
    )


def _job_checklist_name(data: dict[str, Any]) -> str:
    return str(
        _value(
            data,
            "ChecklistName",
            "EvaluationName",
            "CreatedFromChecklistName",
        )
        or ""
    ).strip()


def _checklist_answer_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def append_answer(
        section_sequence: int,
        section: dict[str, Any],
        subsection: dict[str, Any],
        group: dict[str, Any],
        answer: dict[str, Any],
    ) -> None:
        normalized_answer = dict(answer)
        if not _value(normalized_answer, "Question"):
            normalized_answer["Question"] = _value(
                normalized_answer,
                "QuestionText",
                "Text",
            )
        if not _value(normalized_answer, "Answer"):
            normalized_answer["Answer"] = _value(
                normalized_answer,
                "AnswerText",
                "Value",
            )
        rows.append(
            {
                "section_sequence": section_sequence,
                "section": section,
                "subsection": subsection,
                "group": group,
                "answer": normalized_answer,
            }
        )

    for section_sequence, section in enumerate(
        item.get("sections") or [],
        start=1,
    ):
        if not isinstance(section, dict):
            continue
        for subsection in _value(section, "SubSections") or []:
            if not isinstance(subsection, dict):
                continue
            for group in _value(subsection, "AnswerSubsections") or []:
                if not isinstance(group, dict):
                    continue
                for answer in _value(group, "Answers") or []:
                    if not isinstance(answer, dict):
                        continue
                    append_answer(
                        section_sequence,
                        section,
                        subsection,
                        group,
                        answer,
                    )

    if rows:
        return rows

    detail = item.get("detail")
    if not isinstance(detail, dict):
        return rows
    fallback_sections = [
        section
        for section in (_value(detail, "Sections") or [])
        if isinstance(section, dict)
    ]
    if not fallback_sections:
        fallback_sections = [
            {
                "Name": _value(detail, "Name", "ChecklistName") or "Checklist",
                "Questions": _value(detail, "Questions") or [],
            }
        ]
    for section_sequence, section in enumerate(fallback_sections, start=1):
        questions = [
            question
            for question in (_value(section, "Questions") or [])
            if isinstance(question, dict)
        ]
        for answer in questions:
            append_answer(
                section_sequence,
                section,
                {"Name": _value(section, "Name") or "Questions"},
                {"Answers": questions},
                answer,
            )
    return rows


def _text_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _bool_value(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _display_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text_value(
            _value(value, "FullName", "Name", "Username", "Email")
        )
    if isinstance(value, list):
        names = [name for item in value if (name := _display_name(item))]
        return ", ".join(names) or None
    return _text_value(value)


def _uuid_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = UUID(str(value))
        return None if parsed.int == 0 else str(parsed)
    except (ValueError, TypeError, AttributeError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if value in (None, "", "1900/01/01 00:00", "1900-01-01T00:00:00"):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (
        None,
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            if parsed.year <= 1900:
                return None
            return (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed
            )
        except ValueError:
            continue
    return None


def _normalise(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _stage_config(name: str) -> tuple[str, float, str]:
    normalized = _normalise(name)
    if normalized in STAGE_CONFIG:
        return STAGE_CONFIG[normalized]
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_") or "discovered"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{slug[:40]}_{digest}", 90.0, "discovered"


def _status(data: dict[str, Any]) -> str:
    raw = _value(
        data,
        "StatusDescription",
        "JobStatusDescription",
        "Status",
        "JobStatus",
        "JobStatusID",
    )
    if isinstance(raw, (int, float)):
        return STATUS_LABELS.get(int(raw), str(int(raw)))
    return str(raw or "Unknown")


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _answer_pairs(bundle: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    question_keys = (
        "Question",
        "QuestionName",
        "QuestionText",
        "Label",
        "Name",
    )
    answer_keys = (
        "AnswerText",
        "Answer",
        "Value",
        "SelectedText",
        "Text",
        "Comments",
    )
    for node in _walk(bundle):
        question = _value(node, *question_keys)
        answer = _value(node, *answer_keys)
        if isinstance(question, str) and answer not in (None, ""):
            if not isinstance(answer, (dict, list)):
                pairs.append((question, str(answer).strip()))
    return pairs


def _business_fields(
    bundle: dict[str, Any],
    merged: dict[str, Any],
) -> dict[str, Any]:
    pairs = _answer_pairs(bundle)

    def find_exact(*question_names: str) -> str | None:
        expected = {_question_key(name) for name in question_names}
        for question, answer in pairs:
            if _question_key(question) in expected:
                return answer or None
        return None

    return {
        "loading_point": find_exact("loading point")
        or _value(merged, "LoadingPoint", "SourceSiteName"),
        "offloading_point": find_exact("offloading point")
        or _value(merged, "OffloadingPoint", "SiteName"),
        "transporter": find_exact("transporter")
        or _value(merged, "Transporter", "TransporterName"),
        "truck_registration": find_exact(
            "truck registration",
            "vehicle registration",
        )
        or _value(merged, "TruckRegistration", "VehicleRegistration"),
        "truck_type": find_exact("truck type") or _value(merged, "TruckType"),
        "driver_name": find_exact("driver name")
        or _value(merged, "DriverName", "OperatorName"),
        "parcel_reference": find_exact("parcel reference")
        or _value(merged, "ParcelReference"),
        "order_reference": find_exact("order number", "order reference")
        or _value(merged, "OrderReference", "OrderNumber"),
    }


def _question_key(value: Any) -> str:
    normalized = _normalise(value)
    return re.sub(r"^(?:[0-9]+\s+)+", "", normalized)


def _stage_times(bundle: dict[str, Any]) -> dict[str, datetime | None]:
    result: dict[str, datetime | None] = {
        "vehicle_inspection": None,
        "loading_exit": None,
        "staging_arrival": None,
        "staging_exit": None,
        "truck_arrival": None,
        "offloading_exit": None,
    }
    for item in bundle.get("checklists") or []:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        merged = {**summary, **detail}
        name = str(
            _value(merged, "ChecklistName", "EvaluationName", "Name") or ""
        )
        stage_code, _, _ = _stage_config(name)
        if stage_code not in result:
            continue
        result[stage_code] = _timestamp(
            _value(
                merged,
                "SignOffDate",
                "CompletedDate",
                "LastUpdated",
                "UpdatedDate",
            )
        )
    return result
