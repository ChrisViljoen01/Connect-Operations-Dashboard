from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from nicegui import app, background_tasks, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from opus_dashboard.analytics import DataFilters, OpsFilters, StockFilters
from opus_dashboard.charts import (
    activity_options,
    coverage_options,
    donut_options,
    grouped_bar_options,
    movement_trend_options,
    route_heatmap_options,
)
from opus_dashboard.config import BRAND_DIR, settings
from opus_dashboard.models import DashboardSnapshot
from opus_dashboard.repository import OperationsRepository, PERIOD_LABELS
from opus_dashboard.styles import apply_styles
from opus_dashboard.sync import (
    OpusCredentialStore,
    OpusSyncEngine,
    SyncCoordinator,
)
from opus_dashboard.xlsx_service import (
    WorkbookPreview,
    create_control_template,
    preview_control_workbook,
)


repository = OperationsRepository(settings)
opus_credentials = OpusCredentialStore(
    settings.opus_credential_target,
    fallback_email=settings.opus_source_email,
    fallback_password=settings.opus_source_password,
)
opus_sync_engine = OpusSyncEngine(settings, repository, opus_credentials)
opus_sync = SyncCoordinator(opus_sync_engine)
app.add_static_files("/brand", str(BRAND_DIR))

UNPROTECTED_PATH_PREFIXES = ("/login", "/_nicegui", "/brand", "/static")


class _AccessPasswordMiddleware(BaseHTTPMiddleware):
    """Gate every page behind a shared password when one is configured.

    Inactive unless OPUS_APP_ACCESS_PASSWORD is set, so existing internal
    network deployments keep their current behaviour unchanged.
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Any:
        path = request.url.path
        if path.startswith(UNPROTECTED_PATH_PREFIXES):
            return await call_next(request)
        if app.storage.user.get("authenticated", False):
            return await call_next(request)
        return RedirectResponse(f"/login?next={path}")


def _storage_secret() -> str:
    if settings.app_storage_secret:
        return settings.app_storage_secret
    # A stable secret derived from the access password so restarts do not
    # invalidate every open session. Set OPUS_APP_STORAGE_SECRET explicitly
    # for multi-worker or production hosting instead of relying on this.
    return hashlib.sha256(
        f"opus-dashboard:{settings.app_access_password}".encode("utf-8")
    ).hexdigest()


if settings.app_access_password:
    app.add_middleware(_AccessPasswordMiddleware)

    @ui.page("/login")
    def login_page(next: str = "/") -> None:  # noqa: A002 - matches query param name
        if app.storage.user.get("authenticated", False):
            ui.navigate.to(next)
            return

        def attempt_login() -> None:
            if hmac.compare_digest(password_input.value or "", settings.app_access_password):
                app.storage.user["authenticated"] = True
                ui.navigate.to(next)
            else:
                ui.notify("Incorrect password.", type="negative")

        with ui.card().classes("absolute-center").style("min-width: 320px"):
            ui.label("Connect Logistics Operations Dashboard").classes(
                "text-lg font-semibold"
            )
            ui.label("Enter the access password to continue.").classes(
                "text-sm text-grey-7"
            )
            password_input = ui.input("Password", password=True).classes("w-full").on(
                "keydown.enter", attempt_login
            )
            ui.button("Sign in", on_click=attempt_login).classes("w-full")

NAV_ITEMS = (
    ("extraction", "Extraction", "cloud_download"),
    ("data", "Data", "table_view"),
    ("ops", "Ops Dashboard", "local_shipping"),
    ("stock", "Stock on Hand", "inventory_2"),
    ("investigations", "SOH by Order", "query_stats"),
)

ORDER_STUDY_OVERRIDES = {
    "KFTS26-11MG": {
        "parcel_tonnes": 30_000,
        "expected_bays": ("BCF Bay 5", "Island View N", "Island View S"),
        "route_plan": (
            ("Mine to BCF", "Kookfontein", "BCF", "From Mine", "BCF Bay 5"),
            (
                "Mine to BC Direct",
                "Kookfontein",
                "BC",
                "From Mine",
                "Island View N",
            ),
            ("BCF to BC", "BCF", "BC", "BCF Bay 5", "Island View N"),
            ("BCF to BC", "BCF", "BC", "BCF Bay 5", "Island View S"),
            (
                "Mine to BC Direct",
                "Kookfontein",
                "BC",
                "From Mine",
                "Island View S",
            ),
        ),
    },
    "KFTS26-10M": {
        "parcel_tonnes": 10_000,
        "expected_bays": ("BCF Bay 1", "Island View R"),
        "route_plan": (
            ("BCF to BC", "BCF", "BC", "BCF Bay 1", "Island View R"),
        ),
    },
}


def _order_study_config(
    order_reference: str,
    first_root_date: Any,
) -> dict[str, Any]:
    override = ORDER_STUDY_OVERRIDES.get(order_reference, {})
    if isinstance(first_root_date, date):
        date_from = first_root_date
    elif first_root_date:
        date_from = date.fromisoformat(str(first_root_date))
    else:
        date_from = settings.opus_extract_from
    return {
        "order_reference": order_reference,
        "date_from": date_from,
        "expected_bays": tuple(override.get("expected_bays") or ()),
        "route_plan": tuple(override.get("route_plan") or ()),
        "parcel_tonnes": override.get("parcel_tonnes") or 0,
    }


def _filename_token(value: str) -> str:
    token = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in value.strip()
    )
    while "--" in token:
        token = token.replace("--", "-")
    return token.strip("-") or "order"


VIEW_TITLES = {
    "extraction": (
        "OPUS Data Extraction",
        "Job, checklist, section, question and answer collection",
    ),
    "data": (
        "Operational Data",
        "Checklist jobs, full answers and chronological workflow by reference",
    ),
    "ops": (
        "Ops Dashboard",
        "Attempt-aware trucks and tonnes currently in transit",
    ),
    "stock": (
        "Stock on Hand",
        "Opening balances, movements, stock position and delivery variance",
    ),
    "investigations": (
        "SOH Overview by Order Number",
        "Select any OPUS order to review stock, routes, bays and movements",
    ),
}


@dataclass(slots=True)
class PageState:
    view: str
    period: str
    collapsed: bool
    refreshing: bool
    snapshot: DashboardSnapshot
    selected_job_reference: str
    checklist_detail: dict[str, Any] | None
    checklist_detail_loading: bool
    checklist_detail_error: str


def _extraction_ready(snapshot: DashboardSnapshot) -> bool:
    return bool(
        snapshot.storage.get("detail_schema_ready", 0)
        and snapshot.storage.get("stage_mapping_ready", 0)
        and snapshot.storage.get("source_hash_ready", 0)
        and snapshot.storage.get("root_baseline_ready", 0)
        and snapshot.storage.get("analytics_schema_ready", 0)
    )


def _extraction_window_label() -> str:
    date_from = settings.opus_extract_from
    date_to = settings.effective_extract_to()
    if date_from.year == date_to.year and date_from.month == date_to.month:
        return f"{date_from:%d}-{date_to:%d %b %Y}"
    return f"{date_from:%d %b %Y} - {date_to:%d %b %Y}"


def _job_references(snapshot: DashboardSnapshot) -> list[str]:
    references = {
        str(row.get("job_reference") or "").strip()
        for row in snapshot.order_workflows
        if str(row.get("job_reference") or "").strip()
    }
    return sorted(
        references,
        key=lambda reference: (
            int(reference.rsplit("-", 1)[-1])
            if reference.rsplit("-", 1)[-1].isdigit()
            else 0,
            reference,
        ),
    )


def _progress_fraction(current: int, total: int) -> float | None:
    if total <= 0:
        return None
    return min(max(current / total, 0.0), 1.0)


def _columns(*specs: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "label": label,
            "field": name,
            "align": align,
            "sortable": True,
        }
        for name, label, align in specs
    ]


def _table(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    row_key: str,
    *,
    rows_per_page: int = 12,
) -> Any:
    return ui.table(
        columns=columns,
        rows=rows,
        row_key=row_key,
        pagination={"rowsPerPage": rows_per_page, "sortBy": columns[0]["name"]},
    ).classes("data-table w-full").props("flat bordered binary-state-sort")


def _empty_state(icon: str, title: str, message: str) -> None:
    with ui.column().classes("empty-state items-center justify-center gap-2 text-center"):
        ui.icon(icon, size="38px", color="blue-grey-5")
        ui.label(title).classes("section-title")
        ui.label(message).classes("section-subtitle")


def _section_heading(title: str, subtitle: str = "") -> None:
    with ui.column().classes("gap-0"):
        ui.label(title).classes("section-title")
        if subtitle:
            ui.label(subtitle).classes("section-subtitle")


def _metric_card(label: str, value: int | str, detail: str, icon: str) -> None:
    with ui.card().classes("metric-card shadow-none"):
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            with ui.column().classes("gap-2"):
                ui.label(label).classes("metric-label")
                ui.label(f"{value:,}" if isinstance(value, int) else value).classes("metric-value mono")
                ui.label(detail).classes("metric-detail")
            ui.icon(icon, size="23px").classes("metric-icon")


def _analytics_metric_card(
    label: str,
    value: Any,
    detail: str,
    icon: str,
    *,
    on_click: Callable[..., Any] | None = None,
    selected: bool = False,
) -> None:
    classes = "metric-card shadow-none"
    if on_click:
        classes += " clickable"
    if selected:
        classes += " selected"
    with ui.card().classes(classes) as card:
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            with ui.column().classes("gap-2"):
                ui.label(label).classes("metric-label")
                ui.label(_metric_value(value)).classes("metric-value mono")
                ui.label(detail).classes("metric-detail")
            ui.icon(icon, size="23px").classes("metric-icon")
    if on_click:
        card.on("click", on_click)


def _metric_value(value: Any, suffix: str = "") -> str:
    if isinstance(value, bool):
        rendered = "Yes" if value else "No"
    elif isinstance(value, int):
        rendered = f"{value:,}"
    elif isinstance(value, float):
        rendered = f"{value:,.3f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value or "0")
    return f"{rendered}{suffix}"


def _is_stock_exception(row: dict[str, Any]) -> bool:
    offloaded = row.get("offloaded_tonnes") is not None
    return bool(
        row.get("duplicate_loading_attempts")
        or row.get("duplicate_offloading_attempts")
        or row.get("invalid_loading_slab")
        or (offloaded and row.get("invalid_offloading_slab"))
        or row.get("unmapped_client")
        or row.get("loading_validation_errors")
        or (offloaded and row.get("offloading_validation_errors"))
    )


def _analytics_unavailable(snapshot: DashboardSnapshot) -> None:
    with ui.row().classes("status-banner warning items-start no-wrap gap-3"):
        ui.icon("database", size="22px", color="deep-orange-8")
        with ui.column().classes("gap-0"):
            ui.label("Operational analytics migration required").classes("font-bold")
            ui.label(
                "Apply db/09_stock_transit_dimensions.sql as the database owner, "
                "then reload this page. Existing extracted OPUS data is not reset."
            ).classes("section-subtitle")


def _chart_card(title: str, subtitle: str, options: dict[str, Any]) -> None:
    with ui.card().classes("app-card w-full shadow-none analytics-chart"):
        _section_heading(title, subtitle)
        ui.echart(options).classes("w-full chart-frame")


def _date_or_none(value: Any) -> date | None:
    text = str(value or "").strip()
    return date.fromisoformat(text) if text else None


async def _remove_download_later(path: Path) -> None:
    await asyncio.sleep(180)
    path.unlink(missing_ok=True)


def _status_banner(snapshot: DashboardSnapshot) -> None:
    if not snapshot.connected:
        with ui.row().classes("status-banner error items-start no-wrap gap-3"):
            ui.icon("cloud_off", size="22px", color="red-8")
            with ui.column().classes("gap-0"):
                ui.label("Database connection unavailable").classes("font-bold")
                ui.label(snapshot.error).classes("section-subtitle")
        return
    if snapshot.metrics.get("allocations", 0) == 0:
        with ui.row().classes("status-banner warning items-start no-wrap gap-3"):
            ui.icon("hourglass_top", size="22px", color="deep-orange-8")
            with ui.column().classes("gap-0"):
                ui.label("Application and PostgreSQL are ready").classes("font-bold")
                ui.label(
                    "No OPUS allocation rows have been ingested yet. The dashboard is live "
                    "and will populate after the first authenticated year-to-date import."
                ).classes("section-subtitle")
        return
    with ui.row().classes("status-banner items-start no-wrap gap-3"):
        ui.icon("verified", size="22px", color="teal-8")
        with ui.column().classes("gap-0"):
            ui.label("Live OPUS data connected").classes("font-bold")
            ui.label(
                f"Refreshed {snapshot.captured_at.strftime('%d %b %Y %H:%M:%S')}."
            ).classes("section-subtitle")


def _overview(snapshot: DashboardSnapshot) -> None:
    _status_banner(snapshot)
    metrics = snapshot.metrics
    with ui.element("div").classes("metric-grid w-full"):
        _metric_card(
            "Transport allocations",
            metrics.get("allocations", 0),
            PERIOD_LABELS[snapshot.period],
            "assignment",
        )
        _metric_card(
            "Trucks in transit",
            metrics.get("in_transit", 0),
            "Latest transit snapshot",
            "local_shipping",
        )
        _metric_card(
            "Checklist jobs",
            metrics.get("checklist_jobs", 0),
            "Linked by ORDBULK reference",
            "fact_check",
        )
        _metric_card(
            "Completed / exited",
            metrics.get("completed", 0),
            "Status-derived completion",
            "task_alt",
        )
        _metric_card(
            "Active trucks",
            metrics.get("active_trucks", 0),
            "Distinct allocated vehicles",
            "airport_shuttle",
        )

    with ui.element("div").classes("chart-grid w-full"):
        with ui.card().classes("app-card shadow-none"):
            _section_heading(
                "Allocation activity",
                "Allocations and linked checklist jobs recorded during the last 30 days",
            )
            ui.echart(activity_options(snapshot.activity)).classes("chart-frame")
        with ui.card().classes("app-card shadow-none"):
            _section_heading(
                "Checklist coverage",
                f"Linked job rows for {PERIOD_LABELS[snapshot.period].lower()}",
            )
            ui.echart(coverage_options(snapshot.stage_coverage)).classes("chart-frame")

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Latest transport allocations",
            "Most recently booked or observed ORDBULK records",
        )
        if snapshot.allocations:
            _table(
                snapshot.allocations[:25],
                _columns(
                    ("job_reference", "Allocation", "left"),
                    ("booked_at", "Booked", "left"),
                    ("loading_point", "Loading point", "left"),
                    ("offloading_point", "Offloading point", "left"),
                    ("transporter", "Transporter", "left"),
                    ("truck_registration", "Truck", "left"),
                    ("current_status", "Status", "left"),
                    ("checklist_job_count", "Checklists", "right"),
                ),
                "job_reference",
                rows_per_page=10,
            )
        else:
            _empty_state(
                "assignment_late",
                "No allocations loaded yet",
                "The table will populate from ops.v_allocation_progress after the first OPUS extraction.",
            )


def _transit_view(snapshot: DashboardSnapshot) -> None:
    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Current transit register",
            "One latest observation per ORDBULK job reference from Trucks in Transit",
        )
        if snapshot.transit:
            _table(
                snapshot.transit,
                _columns(
                    ("job_reference", "Allocation", "left"),
                    ("date_booked", "Booked", "left"),
                    ("loading_point", "Loading point", "left"),
                    ("offloading_point", "Offloading point", "left"),
                    ("transporter_name", "Transporter", "left"),
                    ("truck_registration", "Truck", "left"),
                    ("truck_type", "Type", "left"),
                    ("driver_name", "Driver", "left"),
                    ("loading_exit_at", "Loading exit", "left"),
                    ("staging_arrival_at", "Staging arrival", "left"),
                    ("truck_arrival_at", "Destination arrival", "left"),
                    ("offloading_exit_at", "Offloading exit", "left"),
                ),
                "job_reference",
            )
        else:
            _empty_state(
                "local_shipping",
                "No trucks in transit are stored",
                "The view is connected to ops.v_current_transit and will update after transit extraction.",
            )


def _allocations_view(snapshot: DashboardSnapshot) -> None:
    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Allocation register",
            f"{PERIOD_LABELS[snapshot.period]} · linked from Transport Allocation onward",
        )
        if snapshot.allocations:
            _table(
                snapshot.allocations,
                _columns(
                    ("job_reference", "Allocation", "left"),
                    ("booked_at", "Booked", "left"),
                    ("loading_point", "Loading point", "left"),
                    ("offloading_point", "Offloading point", "left"),
                    ("transporter", "Transporter", "left"),
                    ("truck_registration", "Truck", "left"),
                    ("driver_name", "Driver", "left"),
                    ("current_stage_code", "Current stage", "left"),
                    ("current_status", "Status", "left"),
                    ("checklist_job_count", "Checklists", "right"),
                    ("last_seen_at", "Last seen", "left"),
                ),
                "job_reference",
            )
        else:
            _empty_state(
                "manage_search",
                "No ORDBULK records match this period",
                "Choose another period or complete the first OPUS ingestion run.",
            )


def _workflow_view(snapshot: DashboardSnapshot) -> None:
    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Transport Allocation journey",
            "Canonical stages currently configured for start-to-finish ORDBULK traceability",
        )
        if snapshot.stage_coverage:
            with ui.element("div").classes("process-track mt-3"):
                for index, stage in enumerate(snapshot.stage_coverage, start=1):
                    with ui.column().classes("process-node gap-3"):
                        with ui.row().classes("items-center justify-between w-full no-wrap"):
                            with ui.row().classes(
                                "stage-number items-center justify-center"
                            ):
                                ui.label(str(index))
                            ui.badge(
                                str(stage.get("workflow_role") or "optional")
                                .replace("_", " ")
                                .title(),
                                color="blue-grey-7",
                            ).props("outline")
                        ui.label(stage["canonical_name"]).classes("font-bold text-sm")
                        ui.label(stage["stage_code"]).classes(
                            "mono section-subtitle text-xs"
                        )
                        ui.label(
                            f"{int(stage.get('job_rows') or 0):,} job rows · "
                            f"{int(stage.get('allocation_count') or 0):,} allocations"
                        ).classes("section-subtitle")
        else:
            _empty_state(
                "account_tree",
                "Checklist definitions unavailable",
                "Reconnect the database to load the configured transport workflow stages.",
            )

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Conditional workflow links",
            "Published source-to-target rules discovered from the OPUS workflow configuration",
        )
        if snapshot.workflow_edges:
            _table(
                snapshot.workflow_edges,
                _columns(
                    ("sequence_no", "Sequence", "right"),
                    ("source_checklist", "From checklist", "left"),
                    ("target_checklist", "To checklist", "left"),
                    ("condition_expression", "Condition", "left"),
                    ("assigned_role", "Assigned role", "left"),
                    ("start_timing", "Start", "left"),
                    ("due_rule", "Due rule", "left"),
                ),
                "sequence_no",
            )
        else:
            _empty_state(
                "rule",
                "Workflow rules await structured extraction",
                "The checklist stages are configured; conditional OPUS workflow edges have not yet been ingested.",
            )


def _health_view(snapshot: DashboardSnapshot) -> None:
    storage = snapshot.storage
    with ui.element("div").classes("metric-grid w-full"):
        _metric_card(
            "Database",
            "Online" if snapshot.connected else "Offline",
            f"{settings.db_host}:{settings.db_port}/{settings.db_name}",
            "dns",
        )
        _metric_card(
            "Checklist stages",
            storage.get("checklist_definitions", 0),
            "Active canonical definitions",
            "checklist",
        )
        _metric_card(
            "Partitions",
            storage.get("partitions", 0),
            "Historical and forward storage",
            "view_timeline",
        )
        _metric_card(
            "Accessible tables",
            storage.get("accessible_tables", 0),
            "Application-role visibility",
            "table_view",
        )
        _metric_card(
            "Extraction runs",
            len(snapshot.extraction_runs),
            "Latest 25 runs displayed",
            "sync_alt",
        )

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "OPUS extraction history",
            "Run status and row reconciliation for authenticated OPUS API collection",
        )
        if snapshot.extraction_runs:
            _table(
                snapshot.extraction_runs,
                _columns(
                    ("started_at", "Started", "left"),
                    ("finished_at", "Finished", "left"),
                    ("source_name", "Source", "left"),
                    ("extraction_scope", "Scope", "left"),
                    ("status", "Status", "left"),
                    ("rows_seen", "Seen", "right"),
                    ("rows_inserted", "Inserted", "right"),
                    ("rows_updated", "Updated", "right"),
                    ("rows_rejected", "Rejected", "right"),
                    ("error_message", "Error", "left"),
                ),
                "run_key",
            )
        else:
            _empty_state(
                "sync_disabled",
                "No extraction runs recorded",
                "Save the OPUS connection above; live sync will start automatically.",
            )


def _extraction_view(snapshot: DashboardSnapshot) -> None:
    metrics = snapshot.metrics

    with ui.element("div").classes("metric-grid extraction-metrics w-full"):
        _metric_card(
            "Extraction window",
            _extraction_window_label(),
            "Transport Allocation roots created in this inclusive window",
            "date_range",
        )
        _metric_card(
            "Checklist jobs",
            metrics.get("checklist_jobs", 0),
            "OPUS jobs discovered",
            "work_history",
        )
        _metric_card(
            "Detailed checklists",
            metrics.get("checklist_instances", 0),
            "Full instances stored",
            "checklist",
        )
        _metric_card(
            "Answers",
            metrics.get("checklist_answers", 0),
            "Questions normalized",
            "question_answer",
        )
        _metric_card(
            "Needs attention",
            metrics.get("incomplete_details", 0)
            + metrics.get("extraction_errors", 0),
            "Incomplete details and errors",
            "report_problem",
        )

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading("Extraction runs", "Latest detail extraction runs")
        if snapshot.extraction_runs:
            _table(
                snapshot.extraction_runs,
                _columns(
                    ("started_at", "Started", "left"),
                    ("status", "Status", "left"),
                    ("extraction_scope", "Mode", "left"),
                    ("jobs_scanned", "Scanned", "right"),
                    ("active_jobs_discovered", "Active", "right"),
                    ("backlog_jobs_discovered", "Backlog", "right"),
                    ("active_audit_jobs", "Active audit", "right"),
                    ("historical_audit_jobs", "History audit", "right"),
                    ("eligible_references", "Eligible refs", "right"),
                    ("excluded_missing_roots", "No root", "right"),
                    ("excluded_old_roots", "Old root", "right"),
                    ("excluded_bulk_jobs", "Bulk excluded", "right"),
                    ("rows_seen", "Qualified jobs", "right"),
                    ("bundles_completed", "Bundles", "right"),
                    ("bundles_deferred", "Deferred", "right"),
                    ("checklist_instances", "Checklists", "right"),
                    ("checklist_sections", "Sections", "right"),
                    ("checklist_answers", "Answers", "right"),
                    ("request_retries", "Retries", "right"),
                    ("rows_rejected", "Rejected", "right"),
                    ("error_message", "Error", "left"),
                ),
                "run_key",
                rows_per_page=10,
            )
        else:
            _empty_state(
                "cloud_download",
                "No detail extraction has run",
                "Live sync will populate reconciliation automatically.",
            )

    if snapshot.extraction_errors:
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading("Extraction failures", "Latest job-level failures")
            _table(
                snapshot.extraction_errors,
                _columns(
                    ("occurred_at", "Occurred", "left"),
                    ("job_reference", "Job reference", "left"),
                    ("phase", "Phase", "left"),
                    ("error_type", "Type", "left"),
                    ("error_message", "Error", "left"),
                ),
                "extraction_error_id",
                rows_per_page=10,
            )


def _checklist_data_view(snapshot: DashboardSnapshot) -> None:
    if not _extraction_ready(snapshot):
        _empty_state(
            "schema",
            "Checklist detail storage is not ready",
            "Apply database migrations through 009, then reload this view.",
        )
        return

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Checklist coverage",
            f"Extracted for {_extraction_window_label()}",
        )
        if snapshot.checklist_summary:
            _table(
                snapshot.checklist_summary,
                _columns(
                    ("checklist_name", "Checklist", "left"),
                    ("stage_code", "Stage", "left"),
                    ("job_references", "Jobs", "right"),
                    ("checklist_instances", "Instances", "right"),
                    ("sections", "Sections", "right"),
                    ("answers", "Answers", "right"),
                    ("images", "Images", "right"),
                    ("structured_items", "Items", "right"),
                    ("incomplete", "Incomplete", "right"),
                    ("latest_source_update", "Latest update", "left"),
                ),
                "stage_code",
                rows_per_page=15,
            )
        else:
            _empty_state(
                "fact_check",
                "No detailed checklists extracted",
                "Live sync will populate checklist detail automatically.",
            )

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Questions and answers",
            f"Latest {len(snapshot.checklist_answers):,} normalized answer rows",
        )
        if snapshot.checklist_answers:
            _table(
                snapshot.checklist_answers,
                _columns(
                    ("job_reference", "Job reference", "left"),
                    ("checklist_name", "Checklist", "left"),
                    ("job_status", "Status", "left"),
                    ("section_name", "Section", "left"),
                    ("subsection_name", "Subsection", "left"),
                    ("question", "Question", "left"),
                    ("question_detail", "Question detail", "left"),
                    ("answer", "Answer", "left"),
                    ("question_type", "Type", "left"),
                    ("comments", "Comments", "left"),
                    ("images", "Images", "right"),
                    ("structured_items", "Items", "right"),
                    ("has_table_data", "Table", "left"),
                    ("source_updated_at", "Source updated", "left"),
                ),
                "answer_row_id",
                rows_per_page=20,
            )
        else:
            _empty_state(
                "question_answer",
                "No questions and answers stored",
                "Live sync will populate answer data automatically.",
            )


def _checklist_instance_groups(
    snapshot: DashboardSnapshot,
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot.order_workflows:
        checklist_name = str(row.get("checklist_name") or "Discovered checklist")
        if checklist_name.casefold() == (
            "Bulk Import for Minerals Transport Allocation".casefold()
        ):
            continue
        grouped.setdefault(checklist_name, []).append(row)
    return sorted(
        grouped.items(),
        key=lambda group: (
            min(
                float(row.get("stage_order") or 90)
                for row in group[1]
            ),
            group[0].casefold(),
        ),
    )


def _checklist_page_rows(
    rows: list[dict[str, Any]],
    pagination: dict[str, Any],
) -> list[dict[str, Any]]:
    page = max(int(pagination.get("page") or 1), 1)
    rows_per_page = max(int(pagination.get("rowsPerPage") or 15), 1)
    sort_by = str(pagination.get("sortBy") or "job_reference")
    descending = bool(pagination.get("descending"))

    def sort_value(row: dict[str, Any]) -> tuple[int, float | str]:
        value = row.get(sort_by)
        if isinstance(value, (int, float)):
            return 0, float(value)
        text = str(value or "").strip()
        if sort_by == "job_reference" and text.rsplit("-", 1)[-1].isdigit():
            return 0, float(text.rsplit("-", 1)[-1])
        return 1, text.casefold()

    populated = [row for row in rows if row.get(sort_by) not in (None, "")]
    missing = [row for row in rows if row.get(sort_by) in (None, "")]
    ordered = sorted(populated, key=sort_value, reverse=descending) + missing
    start = (page - 1) * rows_per_page
    return ordered[start : start + rows_per_page]


def _checklist_instance_tables(
    snapshot: DashboardSnapshot,
    on_checklist_open: Callable[[int], Any],
) -> None:
    groups = _checklist_instance_groups(snapshot)
    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Checklist-specific extraction review",
            "One high-level row per extracted checklist instance; select a reference "
            "to inspect every normalized question and answer.",
        )
        if not groups:
            _empty_state(
                "table_view",
                "No checklist instances available",
                "Run the full detail extraction to populate checklist tables.",
            )
            return

        tab_names = [f"checklist-{index}" for index in range(len(groups))]
        with ui.tabs().classes("w-full") as tabs:
            for tab_name, (checklist_name, rows) in zip(tab_names, groups):
                ui.tab(
                    tab_name,
                    label=f"{checklist_name} ({len(rows):,})",
                )
        with ui.tab_panels(
            tabs,
            value=tab_names[0],
        ).classes("w-full bg-transparent"):
            for tab_name, (checklist_name, rows) in zip(tab_names, groups):
                with ui.tab_panel(tab_name).classes("px-0"):
                    columns = _columns(
                        ("job_reference", "Job reference", "left"),
                        ("workflow_status", "Workflow status", "left"),
                        ("status", "OPUS status", "left"),
                        ("operator_name", "Operator", "left"),
                        ("source_created_at", "Created", "left"),
                        ("operator_started_at", "Started", "left"),
                        ("end_at", "Completed / signed off", "left"),
                        ("percentage_complete", "Progress", "left"),
                        ("section_count", "Sections", "right"),
                        ("answer_count", "Answers", "right"),
                        ("image_count", "Images", "right"),
                        ("item_count", "Items", "right"),
                        ("table_count", "Tables", "right"),
                        ("detail_complete", "Complete", "left"),
                        (
                            "checklist_source_updated_at",
                            "Source updated",
                            "left",
                        ),
                    )
                    pagination = {
                        "page": 1,
                        "rowsPerPage": 15,
                        "rowsNumber": len(rows),
                        "sortBy": "job_reference",
                        "descending": False,
                    }
                    table = ui.table(
                        columns=columns,
                        rows=_checklist_page_rows(rows, pagination),
                        row_key="job_row_id",
                        pagination=pagination,
                    ).classes("data-table w-full").props(
                        "flat bordered binary-state-sort"
                    )

                    def update_page(
                        event: Any,
                        *,
                        all_rows: list[dict[str, Any]] = rows,
                        target: Any = table,
                    ) -> None:
                        next_pagination = {
                            **event.value,
                            "rowsNumber": len(all_rows),
                        }
                        target.rows = _checklist_page_rows(
                            all_rows,
                            next_pagination,
                        )
                        target.pagination = next_pagination
                        target.update()

                    table.on_pagination_change(update_page)
                    with table.add_slot("body-cell-job_reference"):
                        with table.cell("job_reference"):
                            ui.button().props(
                                ":label=props.value flat no-caps color=primary "
                                "align=left"
                            ).classes("checklist-link").on(
                                "click",
                                js_handler="() => emit(props.row.job_row_id)",
                                handler=lambda event: on_checklist_open(
                                    int(event.args)
                                ),
                            )


def _job_workflow_view(
    snapshot: DashboardSnapshot,
    selected_job_reference: str,
    on_job_reference_change: Callable[[str], None],
    on_checklist_open: Callable[[int], Any],
) -> None:
    job_references = _job_references(snapshot)
    if not job_references:
        _empty_state(
            "account_tree",
            "No job workflows are stored",
            "Run the July-to-date extraction to populate ORDBULK-linked checklists.",
        )
        return

    active_reference = (
        selected_job_reference
        if selected_job_reference in job_references
        else job_references[0]
    )
    ui.select(
        options=job_references,
        value=active_reference,
        label="Filter by job reference",
        on_change=lambda event: on_job_reference_change(str(event.value or "")),
    ).props(
        "outlined dense options-dense use-input fill-input hide-selected "
        "input-debounce=0"
    ).classes("order-filter")

    rows = [
        row
        for row in snapshot.order_workflows
        if str(row.get("job_reference") or "") == active_reference
    ]
    current_rows = [row for row in rows if row.get("is_current_stage")]
    current_stage = current_rows[0] if current_rows else {}
    order_numbers = sorted(
        {
            str(row.get("order_number") or "").strip()
            for row in rows
            if str(row.get("order_number") or "").strip()
        }
    )
    order_label = ", ".join(order_numbers) if order_numbers else "Not captured"
    completed = sum(
        1 for row in rows if str(row.get("workflow_status") or "") == "Completed"
    )

    with ui.element("div").classes("metric-grid order-metrics w-full"):
        _metric_card("Job reference", active_reference, _extraction_window_label(), "link")
        _metric_card(
            "Order number",
            order_label,
            "Derived from checklist answers",
            "receipt_long",
        )
        _metric_card("Checklists", len(rows), "Workflow checklist jobs", "fact_check")
        _metric_card("Completed", completed, "Signed-off checklist jobs", "task_alt")
        _metric_card(
            "Current stage",
            str(current_stage.get("checklist_name") or "Unknown"),
            str(current_stage.get("workflow_status") or "No current status"),
            "pending_actions",
        )

    with ui.card().classes("app-card w-full shadow-none"):
        _section_heading(
            "Checklists for job reference",
            "Select a checklist name to open its lifecycle and normalized answers",
        )
        table = _table(
            rows,
            _columns(
                ("workflow_sequence", "Sequence", "right"),
                ("checklist_name", "Checklist", "left"),
                ("workflow_status", "Workflow status", "left"),
                ("status", "OPUS status", "left"),
                ("operator_name", "Operator", "left"),
                ("source_created_at", "Created", "left"),
                ("operator_started_at", "Operator started", "left"),
                ("end_at", "Completed / signed off", "left"),
            ),
            "job_row_id",
            rows_per_page=25,
        )
        with table.add_slot("body-cell-checklist_name"):
            with table.cell("checklist_name"):
                ui.button().props(
                    ":label=props.value flat no-caps color=primary align=left"
                ).classes("checklist-link").on(
                    "click",
                    js_handler="() => emit(props.row.job_row_id)",
                    handler=lambda event: on_checklist_open(int(event.args)),
                )


CONTENT_RENDERERS: dict[str, Callable[[DashboardSnapshot], None]] = {
    "extraction": _extraction_view,
    "checklists": _checklist_data_view,
}


@ui.page("/", response_timeout=60)
async def dashboard() -> None:
    ui.colors(
        primary="#1c2545",
        secondary="#007d6d",
        accent="#e04403",
        positive="#007d6d",
        negative="#b91c1c",
        warning="#e04403",
        info="#2563eb",
    )
    ui.dark_mode().disable()
    apply_styles()
    initial_snapshot = await asyncio.to_thread(repository.load_shell, "all")
    initial_job_references = _job_references(initial_snapshot)
    state = PageState(
        view="data",
        period="all",
        collapsed=False,
        refreshing=False,
        snapshot=initial_snapshot,
        selected_job_reference=(
            initial_job_references[0] if initial_job_references else ""
        ),
        checklist_detail=None,
        checklist_detail_loading=False,
        checklist_detail_error="",
    )
    today = settings.effective_extract_to()
    data_filters_state: dict[str, Any] = {
        "date_from": settings.opus_extract_from.isoformat(),
        "date_to": today.isoformat(),
        "job_reference": "",
        "checklist_name": "",
        "status_group": "",
    }
    ops_filters_state: dict[str, Any] = {
        "date_from": settings.opus_extract_from.isoformat(),
        "date_to": today.isoformat(),
        "origin": "",
        "destination": "",
        "truck_type": "",
    }
    stock_filters_state: dict[str, Any] = {
        "movement_from": settings.opus_extract_from.isoformat(),
        "movement_to": today.isoformat(),
        "as_of": today.isoformat(),
        "order_reference": "",
        "client_name": "",
        "loading_point": "",
        "loading_storage": "",
        "offloading_point": "",
        "offloading_storage": "",
        "truck_type": "",
    }
    data_state: dict[str, Any] = {
        "loading": False,
        "error": "",
        "result": None,
        "options": {},
        "page": 1,
        "generation": 0,
        "selected_reference": "",
        "workflow": [],
    }
    ops_state: dict[str, Any] = {
        "loading": False,
        "error": "",
        "result": None,
        "options": {},
        "generation": 0,
        "selected_reference": "",
        "workflow": [],
    }
    stock_state: dict[str, Any] = {
        "loading": False,
        "error": "",
        "result": None,
        "options": {},
        "generation": 0,
        "preview": None,
        "history": [],
    }
    investigation_state: dict[str, Any] = {
        "loading": False,
        "error": "",
        "options": [],
        "selected_order": "",
        "config": None,
        "result": None,
    }
    sidebar_panel: Any = None
    checklist_detail_dialog = ui.dialog().props("full-width")

    @ui.refreshable
    def checklist_detail_content() -> None:
        detail = state.checklist_detail or {}
        summary = detail.get("summary") or {}
        answers = detail.get("answers") or []
        with ui.card().classes("checklist-detail-card"):
            with ui.row().classes("w-full items-start justify-between no-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label(
                        str(summary.get("checklist_name") or "Checklist detail")
                    ).classes("section-title")
                    ui.label(
                        " · ".join(
                            value
                            for value in (
                                str(summary.get("job_reference") or ""),
                                str(summary.get("order_number") or ""),
                            )
                            if value
                        )
                    ).classes("section-subtitle mono")
                ui.button(
                    icon="close",
                    on_click=checklist_detail_dialog.close,
                ).props("flat round dense").tooltip("Close checklist detail")

            if state.checklist_detail_loading:
                with ui.column().classes(
                    "w-full items-center justify-center gap-3 checklist-detail-loading"
                ):
                    ui.spinner(size="42px", color="primary")
                    ui.label("Loading checklist detail...").classes("section-subtitle")
                return
            if state.checklist_detail_error:
                _empty_state(
                    "error_outline",
                    "Checklist detail could not be loaded",
                    state.checklist_detail_error,
                )
                return
            if not summary:
                return

            with ui.element("div").classes(
                "metric-grid checklist-detail-metrics w-full"
            ):
                _metric_card(
                    "OPUS status",
                    str(summary.get("job_status") or "Unknown"),
                    str(summary.get("status_detail") or ""),
                    "task_alt",
                )
                _metric_card(
                    "Operator",
                    str(summary.get("operator_name") or "Not assigned"),
                    str(summary.get("created_by_name") or "Creator not captured"),
                    "person",
                )
                _metric_card(
                    "Progress",
                    str(summary.get("percentage_complete") or "Not reported"),
                    (
                        f"{int(summary.get('section_count') or 0):,} sections · "
                        f"{int(summary.get('answer_count') or 0):,} answers"
                    ),
                    "analytics",
                )
                _metric_card(
                    "Source updated",
                    str(summary.get("source_updated_at") or "Not reported"),
                    (
                        "Complete detail"
                        if summary.get("detail_complete")
                        else "Incomplete detail"
                    ),
                    "update",
                )

            with ui.card().classes("app-card w-full shadow-none"):
                _section_heading(
                    "Lifecycle",
                    "OPUS checklist creation, operator activity, completion and sign-off",
                )
                lifecycle_rows = [
                    {
                        "created": summary.get("source_created_at"),
                        "operator_started": summary.get("operator_started_at"),
                        "completed": summary.get("source_completed_at"),
                        "signed_off": summary.get("source_signed_off_at"),
                        "due": summary.get("due_at"),
                    }
                ]
                _table(
                    lifecycle_rows,
                    _columns(
                        ("created", "Created", "left"),
                        ("operator_started", "Operator started", "left"),
                        ("completed", "Completed", "left"),
                        ("signed_off", "Signed off", "left"),
                        ("due", "Due", "left"),
                    ),
                    "created",
                    rows_per_page=1,
                )

            with ui.card().classes("app-card w-full shadow-none"):
                _section_heading(
                    "Questions and answers",
                    f"{len(answers):,} normalized answer rows for this checklist",
                )
                if answers:
                    _table(
                        answers,
                        _columns(
                            ("section_name", "Section", "left"),
                            ("subsection_name", "Subsection", "left"),
                            ("question", "Question", "left"),
                            ("question_detail", "Question detail", "left"),
                            ("question_report_full", "Full question", "left"),
                            ("question_summary", "Question summary", "left"),
                            ("action_text", "Action / help", "left"),
                            ("answer", "Answer", "left"),
                            (
                                "report_formatted_answer",
                                "Formatted answer",
                                "left",
                            ),
                            ("question_type", "Type", "left"),
                            ("comments", "Comments", "left"),
                            ("images", "Images", "right"),
                            ("structured_items", "Items", "right"),
                            ("has_table_data", "Table", "left"),
                            ("answer_extra", "Answer extra", "left"),
                            ("answer_images", "Image detail", "left"),
                            ("answer_items", "Item detail", "left"),
                            (
                                "child_checklist_answers",
                                "Child checklists",
                                "left",
                            ),
                            ("table_data", "Table data", "left"),
                            ("table_columns", "Table columns", "left"),
                        ),
                        "answer_row_id",
                        rows_per_page=25,
                    )
                else:
                    _empty_state(
                        "question_answer",
                        "No normalized answers stored",
                        "This checklist does not currently contain answer rows.",
                    )

    with checklist_detail_dialog:
        checklist_detail_content()

    async def refresh_content_preserving_scroll(client: Any) -> None:
        if not client.has_socket_connection:
            content.refresh()
            return
        await client.run_javascript(
            """
            const root = document.querySelector('.content-wrap');
            window.__opusDashboardScrollY = window.scrollY;
            if (root) {
                root.style.minHeight = `${Math.max(
                    root.scrollHeight,
                    root.offsetHeight,
                )}px`;
            }
            return true;
            """
        )
        with client:
            content.refresh()
        if not client.has_socket_connection:
            return
        await client.run_javascript(
            """
            return new Promise(resolve => requestAnimationFrame(() => {
                const top = Number(window.__opusDashboardScrollY || 0);
                window.scrollTo({top, behavior: 'auto'});
                const root = document.querySelector('.content-wrap');
                if (root) {
                    root.style.minHeight = '';
                }
                requestAnimationFrame(() => {
                    window.scrollTo({top, behavior: 'auto'});
                    delete window.__opusDashboardScrollY;
                    resolve(true);
                });
            }));
            """
        )

    async def set_view(view: str) -> None:
        client = ui.context.client
        state.view = view
        sidebar.refresh()
        toolbar.refresh()
        content.refresh()
        if view == "extraction":
            await refresh_data(notify=False)
            return
        if (
            view == "investigations"
            and state.snapshot.storage.get("analytics_schema_ready", 0)
        ):
            if not investigation_state["options"] and not investigation_state["loading"]:
                await refresh_order_investigation()
        elif (
            view in {"data", "ops", "stock"}
            and state.snapshot.storage.get("analytics_schema_ready", 0)
        ):
            target = {
                "data": data_state,
                "ops": ops_state,
                "stock": stock_state,
            }[view]
            if target["result"] is None and not target["loading"]:
                await refresh_analytics(view)
        if client.has_socket_connection:
            client.run_javascript("window.scrollTo({top: 0, behavior: 'instant'});")

    def toggle_sidebar() -> None:
        state.collapsed = not state.collapsed
        if sidebar_panel is not None:
            sidebar_panel.classes(
                replace="sidebar collapsed" if state.collapsed else "sidebar"
            )
        sidebar.refresh()

    def select_job_reference(job_reference: str) -> None:
        state.selected_job_reference = job_reference
        content.refresh()

    async def open_checklist_detail(job_row_id: int) -> None:
        client = ui.context.client
        state.checklist_detail = None
        state.checklist_detail_error = ""
        state.checklist_detail_loading = True
        checklist_detail_content.refresh()
        checklist_detail_dialog.open()
        try:
            state.checklist_detail = await asyncio.to_thread(
                repository.load_checklist_detail,
                job_row_id,
            )
        except Exception as exc:
            state.checklist_detail_error = str(exc)
        finally:
            state.checklist_detail_loading = False
            if client.has_socket_connection:
                with client:
                    checklist_detail_content.refresh()

    async def refresh_data(
        period: str | None = None,
        *,
        notify: bool = True,
    ) -> None:
        client = ui.context.client
        if state.refreshing:
            return
        if period in PERIOD_LABELS:
            state.period = str(period)
        state.refreshing = True
        toolbar.refresh()
        try:
            loader = (
                repository.load
                if state.view == "extraction"
                else repository.load_shell
            )
            state.snapshot = await asyncio.to_thread(loader, state.period)
            available_references = _job_references(state.snapshot)
            if state.selected_job_reference not in available_references:
                state.selected_job_reference = (
                    available_references[0] if available_references else ""
                )
            if client.has_socket_connection and notify:
                with client:
                    if state.snapshot.connected:
                        ui.notify("Live PostgreSQL data refreshed.", type="positive")
                    else:
                        ui.notify(
                            "The database could not be refreshed.",
                            type="negative",
                            close_button="Dismiss",
                        )
            if (
                state.view == "investigations"
                and state.snapshot.storage.get("analytics_schema_ready", 0)
            ):
                await refresh_order_investigation(refresh_ui=False)
            elif (
                state.view in {"data", "ops", "stock"}
                and state.snapshot.storage.get("analytics_schema_ready", 0)
            ):
                await refresh_analytics(state.view, refresh_ui=False)
        finally:
            state.refreshing = False
            if client.has_socket_connection:
                with client:
                    sidebar.refresh()
                    toolbar.refresh()
                await refresh_content_preserving_scroll(client)

    async def run_opus_sync() -> None:
        client = ui.context.client
        if opus_sync.snapshot().running:
            ui.notify("An OPUS refresh is already running.", type="warning")
            return
        if opus_credentials.read() is None:
            ui.notify(
                "Save and verify the OPUS login before starting a refresh.",
                type="warning",
            )
            return
        if not _extraction_ready(state.snapshot):
            ui.notify(
                "Apply database migrations through 009 before starting extraction.",
                type="warning",
            )
            return
        sync_task = asyncio.create_task(opus_sync.run(full=False))
        # Allow the coordinator to publish its running state before rebuilding
        # the controls, so the loader and disabled buttons appear immediately.
        await asyncio.sleep(0)
        await refresh_content_preserving_scroll(client)
        try:
            result = await sync_task
            if result is None:
                # User cancelled the sync
                if client.has_socket_connection:
                    with client:
                        ui.notify("Sync stopped.", type="info")
                return
            loader = (
                repository.load
                if state.view == "extraction"
                else repository.load_shell
            )
            state.snapshot = await asyncio.to_thread(loader, state.period)
            if (
                state.view == "investigations"
                and state.snapshot.storage.get("analytics_schema_ready", 0)
            ):
                await refresh_order_investigation(refresh_ui=False)
            elif (
                state.view in {"data", "ops", "stock"}
                and state.snapshot.storage.get("analytics_schema_ready", 0)
            ):
                await refresh_analytics(state.view, refresh_ui=False)
            if client.has_socket_connection:
                with client:
                    ui.notify(
                        (
                            f"Extraction completed: {result.rows_seen:,} jobs, "
                            f"{result.checklist_instances:,} detailed checklists and "
                            f"{result.checklist_answers:,} answers."
                        ),
                        type="warning" if result.rows_rejected else "positive",
                        close_button="Dismiss",
                    )
        except Exception as exc:
            if client.has_socket_connection:
                with client:
                    ui.notify(
                        f"OPUS refresh failed: {exc}",
                        type="negative",
                        close_button="Dismiss",
                        timeout=0,
                    )
        finally:
            if client.has_socket_connection:
                with client:
                    sidebar.refresh()
                    toolbar.refresh()
                await refresh_content_preserving_scroll(client)

    async def forget_connection() -> None:
        if opus_sync.snapshot().running:
            ui.notify(
                "Wait for the active OPUS import to finish before removing the login.",
                type="warning",
            )
            return
        try:
            removed = await asyncio.to_thread(opus_credentials.delete)
            ui.notify(
                "Saved OPUS login removed."
                if removed
                else "No saved OPUS login was found.",
                type="positive" if removed else "info",
            )
            await refresh_content_preserving_scroll(ui.context.client)
        except Exception as exc:
            ui.notify(
                f"The saved OPUS login could not be removed: {exc}",
                type="negative",
            )

    @ui.refreshable
    def sync_status() -> None:
        try:
            saved = opus_credentials.read()
            credential_error = ""
        except Exception as exc:
            saved = None
            credential_error = str(exc)
        progress = opus_sync.snapshot()
        with ui.column().classes("status-banner w-full gap-2"):
            with ui.row().classes(
                "w-full items-start justify-between no-wrap gap-3"
            ):
                with ui.row().classes("items-start no-wrap gap-3"):
                    ui.icon(
                        "sync"
                        if progress.running
                        else ("lock" if saved else "lock_open"),
                        size="22px",
                        color=(
                            "teal-8"
                            if saved and not credential_error
                            else "deep-orange-8"
                        ),
                    )
                    with ui.column().classes("credential-copy gap-0"):
                        if credential_error:
                            ui.label("Credential vault unavailable").classes(
                                "font-bold"
                            )
                            ui.label(credential_error).classes("section-subtitle")
                        elif saved:
                            ui.label(
                                f"OPUS login saved for {saved.username}"
                            ).classes("credential-account font-bold")
                            ui.label(
                                "Password is encrypted in Windows Credential Manager "
                                "and is never written to this project or database."
                            ).classes("section-subtitle")
                        else:
                            ui.label("OPUS source is not connected").classes(
                                "font-bold"
                            )
                            ui.label(
                                "Enter an authorized OPUS email and password below."
                            ).classes("section-subtitle")
                if progress.running:
                    with ui.column().classes("items-end gap-0"):
                        ui.spinner("dots", size="30px", color="primary")
                        ui.label(
                            f"{progress.mode} import running"
                        ).classes("text-xs font-bold")
                elif progress.finished_at:
                    with ui.column().classes("items-end gap-0"):
                        phase_color = (
                            "text-orange-8" if progress.cancelled
                            else ("text-red-8" if progress.phase == "Failed" else "")
                        )
                        ui.label(progress.phase).classes(f"text-xs font-bold {phase_color}")
                        ui.label(
                            f"{progress.checklist_answers:,} answers stored"
                        ).classes("section-subtitle mono")
                        if progress.error:
                            ui.label(progress.error).classes("text-xs text-red-8")
            if progress.running:
                fraction = _progress_fraction(
                    progress.phase_current,
                    progress.phase_total,
                )
                if fraction is not None:
                    ui.linear_progress(value=fraction, show_value=False).props(
                        "rounded color=primary track-color=blue-grey-2"
                    ).classes("w-full")
                    pct_label = (
                        f"{fraction * 100:.0f}% - "
                        f"{progress.phase_current:,} / {progress.phase_total:,}"
                    )
                else:
                    ui.linear_progress(show_value=False).props(
                        "indeterminate rounded color=primary track-color=blue-grey-2"
                    ).classes("w-full")
                    pct_label = "Waiting for OPUS total"
                with ui.row().classes(
                    "w-full items-center justify-between gap-2 flex-wrap"
                ):
                    ui.label(progress.phase).classes("font-bold")
                    ui.label(pct_label).classes("section-subtitle mono")
                if progress.date_from and progress.date_to:
                    ui.label(
                        f"Date window: {progress.date_from} to {progress.date_to}"
                    ).classes("section-subtitle mono")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    ui.label(
                        f"Scanned: {progress.jobs_scanned:,} jobs / "
                        f"{progress.scan_pages_completed:,} pages"
                    ).classes("text-xs mono")
                    ui.label(
                        f"Discovered: {progress.recent_jobs_discovered:,} recent / "
                        f"{progress.active_jobs_discovered:,} active / "
                        f"{progress.backlog_jobs_discovered:,} backlog"
                    ).classes("text-xs mono")
                    ui.label(
                        f"References: {progress.eligible_references:,} eligible / "
                        f"{progress.references_scanned:,} found"
                    ).classes("text-xs mono")
                    ui.label(
                        f"Detail: {progress.bundles_completed:,} stored / "
                        f"{progress.bundles_queued:,} queued / "
                        f"{progress.bundles_deferred:,} deferred"
                    ).classes("text-xs mono")
                    ui.label(
                        f"Answers: {progress.checklist_answers:,} / "
                        f"Sections: {progress.checklist_sections:,}"
                    ).classes("text-xs mono")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    ui.label(
                        f"Excluded references: "
                        f"{progress.excluded_missing_root_references:,} no root, "
                        f"{progress.excluded_out_of_window_root_references:,} "
                        "root outside window"
                    ).classes("text-xs mono")
                    ui.label(
                        f"Excluded jobs: {progress.bulk_import_jobs_excluded:,} bulk, "
                        f"{progress.duplicate_root_jobs_excluded:,} duplicate roots, "
                        f"{progress.pre_root_jobs_excluded:,} before root"
                    ).classes("text-xs mono")
                    ui.label(
                        f"New: {progress.rows_inserted:,} / "
                        f"Updated: {progress.rows_updated:,} / "
                        f"Rejected: {progress.rows_rejected:,} / "
                        f"Retries: {progress.request_retries:,}"
                    ).classes("text-xs mono")
                    ui.label(
                        f"Background audit: {progress.active_audit_jobs:,} active / "
                        f"{progress.historical_audit_jobs:,} historical"
                    ).classes("text-xs mono")
                if progress.started_at:
                    try:
                        elapsed = (
                            datetime.now().astimezone()
                            - datetime.fromisoformat(progress.started_at)
                        )
                        _mins, _secs = divmod(
                            int(elapsed.total_seconds()),
                            60,
                        )
                        ui.label(
                            f"Elapsed: {_mins}m {_secs}s"
                        ).classes("section-subtitle mono")
                    except ValueError:
                        ui.label("Elapsed time unavailable").classes(
                            "section-subtitle"
                        )
                if progress.current_reference or progress.current_checklist:
                    ui.label(
                        "Current: "
                        + " / ".join(
                            value
                            for value in (
                                progress.current_reference,
                                progress.current_checklist,
                            )
                            if value
                        )
                    ).classes("section-subtitle mono")
                elif progress.current_item:
                    ui.label(progress.current_item).classes(
                        "section-subtitle mono"
                    )
                if progress.last_progress_at:
                    try:
                        heartbeat = datetime.fromisoformat(
                            progress.last_progress_at
                        ).astimezone()
                        ui.label(
                            f"Last activity: {heartbeat:%H:%M:%S}"
                        ).classes("section-subtitle mono")
                    except ValueError:
                        ui.label("Last activity timestamp unavailable").classes(
                            "section-subtitle"
                        )
                if progress.error:
                    ui.label(progress.error).classes("text-xs text-red-8")

    @ui.refreshable
    def sync_controls() -> None:
        try:
            saved = opus_credentials.read()
        except Exception:
            saved = None
        progress = opus_sync.snapshot()
        running = progress.running
        schema_ready = _extraction_ready(state.snapshot)
        controls_ready = bool(saved) and schema_ready
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            ui.button(
                "Live sync running" if running else "Sync latest OPUS data",
                icon="sync",
                on_click=run_opus_sync,
            ).props(
                "outline no-caps "
                + (
                    "disable loading"
                    if running
                    else ("disable" if not controls_ready else "")
                )
            )
            if running:
                ui.button(
                    "Stop sync",
                    icon="stop_circle",
                    on_click=opus_sync.cancel,
                ).props("outline no-caps color=negative")
            elif saved:
                ui.button(
                    "Forget saved login",
                    icon="delete_outline",
                    on_click=forget_connection,
                ).props("flat no-caps color=negative")
        ui.label(
            (
                (
                    f"Current-day OPUS sync: every {settings.opus_sync_minutes} "
                    f"minutes · Active sweep: every "
                    f"{settings.opus_active_sweep_minutes} minutes · Historical "
                    f"audit: every {settings.opus_audit_minutes} minutes"
                    if settings.opus_auto_sync
                    else "Automatic OPUS refresh is paused"
                )
                + f" · Source: "
                f"{settings.opus_api_url}"
            )
        ).classes("section-subtitle")

    def connection_view() -> None:
        try:
            saved = opus_credentials.read()
        except Exception:
            saved = None
        progress = opus_sync.snapshot()
        sync_running = progress.running
        email_input: Any = None
        password_input: Any = None

        async def verify_and_save() -> None:
            email = str(email_input.value or "").strip()
            password = str(password_input.value or "")
            if not password and saved and email.casefold() == saved.username.casefold():
                password = saved.password
            if not email or not password:
                ui.notify("OPUS email and password are required.", type="warning")
                return
            try:
                identity = await asyncio.to_thread(
                    opus_sync_engine.test_credentials,
                    email,
                    password,
                )
                await asyncio.to_thread(opus_credentials.save, email, password)
                account = str(identity.get("email") or email)
                ui.notify(
                    f"OPUS connection verified for {account}.",
                    type="positive",
                )
                await refresh_content_preserving_scroll(ui.context.client)
            except Exception as exc:
                ui.notify(
                    f"OPUS login could not be verified: {exc}",
                    type="negative",
                    close_button="Dismiss",
                    timeout=0,
                )

        sync_status()
        if not _extraction_ready(state.snapshot):
            with ui.row().classes(
                "status-banner warning w-full items-start no-wrap gap-3"
            ):
                ui.icon("schema", size="22px", color="deep-orange-8")
                with ui.column().classes("gap-0"):
                    ui.label("Database migrations through 009 are required").classes(
                        "font-bold"
                    )
                    ui.label(
                        "Apply database migrations 006, 007, 008 and 009 as the database owner."
                    ).classes("section-subtitle")
        if not state.snapshot.storage.get("connector_writable", 0):
            with ui.row().classes(
                "status-banner warning w-full items-start no-wrap gap-3"
            ):
                ui.icon("admin_panel_settings", size="22px", color="deep-orange-8")
                with ui.column().classes("gap-0"):
                    ui.label("Database migration 002 is required").classes(
                        "font-bold"
                    )
                    ui.label(
                        "Run db/02_connector_permissions.sql once in pgAdmin as "
                        "the database owner before importing OPUS data."
                    ).classes("section-subtitle")
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "OPUS source connection",
                (
                    "Authenticated read-only collection from app.opus4business.com. "
                    "Raw payloads and normalized checklist answers are linked by "
                    "ORDBULK job reference."
                ),
            )
            with ui.element("div").classes("connection-grid w-full"):
                email_input = ui.input(
                    "OPUS email",
                    value=saved.username if saved else "",
                ).props("outlined autocomplete=username").classes("w-full")
                password_input = ui.input(
                    "OPUS password",
                    password=True,
                    password_toggle_button=True,
                    placeholder=(
                        "Saved securely — leave blank to keep it"
                        if saved
                        else "Enter password"
                    ),
                ).props("outlined autocomplete=current-password").classes("w-full")
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.button(
                    "Test & save connection",
                    icon="verified_user",
                    on_click=verify_and_save,
                ).props(
                    f"unelevated no-caps {'disable' if sync_running else ''}"
                ).classes("primary-action")
            sync_controls()

    async def refresh_analytics(
        view: str,
        *,
        refresh_ui: bool = True,
    ) -> None:
        client = ui.context.client
        target = {
            "data": data_state,
            "ops": ops_state,
            "stock": stock_state,
        }[view]
        generation = int(target.get("generation", 0))
        if target["loading"]:
            return
        target["loading"] = True
        target["error"] = ""
        if refresh_ui:
            await refresh_content_preserving_scroll(client)
        try:
            if view == "data":
                filters = DataFilters(
                    date_from=_date_or_none(data_filters_state["date_from"]),
                    date_to=_date_or_none(data_filters_state["date_to"]),
                    job_reference=str(data_filters_state["job_reference"]),
                    checklist_name=str(data_filters_state["checklist_name"]),
                    status_group=str(data_filters_state["status_group"]),
                )
                result, options = await asyncio.gather(
                    asyncio.to_thread(
                        repository.load_data_page,
                        filters,
                        page=int(data_state["page"]),
                    ),
                    asyncio.to_thread(repository.data_filter_options),
                )
                data_state["result"] = result
                data_state["options"] = options
                selected = str(data_state["selected_reference"] or "")
                if selected:
                    data_state["workflow"] = await asyncio.to_thread(
                        repository.load_reference_workflow,
                        selected,
                    )
            elif view == "ops":
                filters = OpsFilters(
                    date_from=_date_or_none(ops_filters_state["date_from"]),
                    date_to=_date_or_none(ops_filters_state["date_to"]),
                    origin=str(ops_filters_state["origin"]),
                    destination=str(ops_filters_state["destination"]),
                    truck_type=str(ops_filters_state["truck_type"]),
                )
                result, options = await asyncio.gather(
                    asyncio.to_thread(repository.load_ops_dashboard, filters),
                    asyncio.to_thread(repository.ops_filter_options),
                )
                ops_state["result"] = result
                ops_state["options"] = options
                selected = str(ops_state["selected_reference"] or "")
                if selected:
                    ops_state["workflow"] = await asyncio.to_thread(
                        repository.load_reference_workflow,
                        selected,
                    )
            else:
                filters = StockFilters(
                    movement_from=_date_or_none(
                        stock_filters_state["movement_from"]
                    ),
                    movement_to=_date_or_none(stock_filters_state["movement_to"]),
                    as_of=_date_or_none(stock_filters_state["as_of"]),
                    order_reference=str(stock_filters_state["order_reference"]),
                    client_name=str(stock_filters_state["client_name"]),
                    loading_point=str(stock_filters_state["loading_point"]),
                    loading_storage=str(stock_filters_state["loading_storage"]),
                    offloading_point=str(stock_filters_state["offloading_point"]),
                    offloading_storage=str(stock_filters_state["offloading_storage"]),
                    truck_type=str(stock_filters_state["truck_type"]),
                )
                result, options, history = await asyncio.gather(
                    asyncio.to_thread(repository.load_stock_dashboard, filters),
                    asyncio.to_thread(repository.stock_filter_options),
                    asyncio.to_thread(repository.control_workbook_history),
                )
                stock_state["result"] = result
                stock_state["options"] = options
                stock_state["history"] = history
        except Exception as exc:
            target["error"] = str(exc)
        finally:
            target["loading"] = False
            if int(target.get("generation", 0)) != generation:
                await refresh_analytics(view, refresh_ui=refresh_ui)
            elif refresh_ui:
                await refresh_content_preserving_scroll(client)

    async def refresh_order_investigation(
        order_reference: str | None = None,
        *,
        refresh_ui: bool = True,
    ) -> None:
        client = ui.context.client
        if investigation_state["loading"]:
            return
        investigation_state["loading"] = True
        investigation_state["error"] = ""
        if order_reference is not None:
            investigation_state["selected_order"] = order_reference.strip()
        if refresh_ui:
            await refresh_content_preserving_scroll(client)
        try:
            options = await asyncio.to_thread(
                repository.order_investigation_options
            )
            investigation_state["options"] = options
            option_by_order = {
                str(option.get("order_reference") or ""): option
                for option in options
                if str(option.get("order_reference") or "")
            }
            selected_order = str(
                investigation_state.get("selected_order") or ""
            )
            if selected_order not in option_by_order:
                selected_order = next(iter(option_by_order), "")
                investigation_state["selected_order"] = selected_order
            if not selected_order:
                investigation_state["config"] = None
                investigation_state["result"] = None
                return
            selected_option = option_by_order[selected_order]
            config = _order_study_config(
                selected_order,
                selected_option.get("first_root_date"),
            )
            investigation_state["config"] = config
            investigation_state["result"] = await asyncio.to_thread(
                repository.load_order_investigation,
                selected_order,
                config["date_from"],
                settings.effective_extract_to(),
                config["expected_bays"],
                config["route_plan"],
                config["parcel_tonnes"],
            )
        except Exception as exc:
            investigation_state["error"] = str(exc)
            investigation_state["result"] = None
        finally:
            investigation_state["loading"] = False
            if refresh_ui:
                await refresh_content_preserving_scroll(client)

    async def update_analytics_filter(
        view: str,
        key: str,
        value: Any,
    ) -> None:
        filters = {
            "data": data_filters_state,
            "ops": ops_filters_state,
            "stock": stock_filters_state,
        }[view]
        target = {
            "data": data_state,
            "ops": ops_state,
            "stock": stock_state,
        }[view]
        filters[key] = value or ""
        target["generation"] = int(target.get("generation", 0)) + 1
        if view == "data":
            data_state["page"] = 1
        await refresh_analytics(view)

    async def set_data_status(status: str) -> None:
        data_filters_state["status_group"] = (
            "" if data_filters_state["status_group"] == status else status
        )
        data_state["page"] = 1
        await refresh_analytics("data")

    async def change_data_page(delta: int) -> None:
        result = data_state["result"] or {}
        current = int(data_state["page"])
        total = int(result.get("total") or 0)
        page_size = int(result.get("page_size") or 100)
        last_page = max((total + page_size - 1) // page_size, 1)
        data_state["page"] = min(max(current + delta, 1), last_page)
        await refresh_analytics("data")

    async def select_analytics_reference(scope: str, reference: str) -> None:
        target = data_state if scope == "data" else ops_state
        target["selected_reference"] = reference
        target["workflow"] = await asyncio.to_thread(
            repository.load_reference_workflow,
            reference,
        )
        await refresh_content_preserving_scroll(ui.context.client)

    def render_analytics_workflow(
        rows: list[dict[str, Any]],
        reference: str,
    ) -> None:
        if not reference:
            _empty_state(
                "account_tree",
                "Select a job reference",
                "Click a reference in the table to review its checklist attempts.",
            )
            return
        _section_heading(
            f"Chronological workflow - {reference}",
            "Terminal attempts with a later checklist are shown as superseded.",
        )
        if not rows:
            _empty_state(
                "search_off",
                "No workflow rows found",
                "The selected reference has no current workflow records.",
            )
            return
        table = _table(
            rows,
            _columns(
                ("workflow_sequence", "#", "right"),
                ("checklist_name", "Checklist", "left"),
                ("attempt_sequence", "Attempt", "right"),
                ("opus_status", "OPUS status", "left"),
                ("status_group", "Status group", "left"),
                ("superseded_terminal", "Superseded closure", "left"),
                ("operator_name", "Operator", "left"),
                ("source_created_at", "Created", "left"),
                ("operator_started_at", "Started", "left"),
                ("source_signed_off_at", "Signed off", "left"),
                ("answer_count", "Answers", "right"),
            ),
            "job_row_id",
            rows_per_page=25,
        )
        with table.add_slot("body-cell-checklist_name"):
            with table.cell("checklist_name"):
                ui.button().props(
                    ":label=props.value flat no-caps color=primary align=left"
                ).on(
                    "click",
                    js_handler="() => emit(props.row.job_row_id)",
                    handler=lambda event: open_checklist_detail(int(event.args)),
                )

    async def export_data(all_rows: bool) -> None:
        if not state.snapshot.storage.get("analytics_schema_ready", 0):
            ui.notify("Apply migration 009 before exporting.", type="warning")
            return
        if all_rows:
            filters = DataFilters(
                date_from=settings.opus_extract_from,
                date_to=settings.effective_extract_to(),
            )
            label = "all"
        else:
            filters = DataFilters(
                date_from=_date_or_none(data_filters_state["date_from"]),
                date_to=_date_or_none(data_filters_state["date_to"]),
                job_reference=str(data_filters_state["job_reference"]),
                checklist_name=str(data_filters_state["checklist_name"]),
                status_group=str(data_filters_state["status_group"]),
            )
            label = "filtered"
        handle, filename = tempfile.mkstemp(
            prefix=f"opus-data-{label}-",
            suffix=".xlsx",
        )
        os.close(handle)
        path = Path(filename)
        ui.notify("Building bounded-memory XLSX export...", type="info")
        try:
            counts = await asyncio.to_thread(
                repository.export_data_workbook,
                path,
                filters,
            )
            ui.download(
                str(path),
                filename=f"OPUS-data-{label}-{datetime.now():%Y%m%d-%H%M}.xlsx",
            )
            background_tasks.create(_remove_download_later(path))
            ui.notify(
                f"Export ready: {sum(counts.values()):,} worksheet rows.",
                type="positive",
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            ui.notify(f"Export failed: {exc}", type="negative", timeout=0)

    async def export_order_investigation(config: dict[str, Any]) -> None:
        order_reference = str(config["order_reference"])
        filename_order = _filename_token(order_reference)
        handle, filename = tempfile.mkstemp(
            prefix=f"opus-{filename_order}-study-",
            suffix=".xlsx",
        )
        os.close(handle)
        path = Path(filename)
        ui.notify(f"Building {order_reference} investigation workbook...", type="info")
        try:
            counts = await asyncio.to_thread(
                repository.export_order_investigation_workbook,
                path,
                order_reference,
                config["date_from"],
                settings.effective_extract_to(),
                config["expected_bays"],
                config["route_plan"],
                config["parcel_tonnes"],
            )
            ui.download(
                str(path),
                filename=(
                    f"OPUS-{filename_order}-investigation-"
                    f"{datetime.now():%Y%m%d-%H%M}.xlsx"
                ),
            )
            background_tasks.create(_remove_download_later(path))
            ui.notify(
                f"{order_reference} export ready: {sum(counts.values()):,} rows.",
                type="positive",
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            ui.notify(
                f"{order_reference} export failed: {exc}",
                type="negative",
                timeout=0,
            )

    async def export_order_investigation_pdf(config: dict[str, Any]) -> None:
        order_reference = str(config["order_reference"])
        filename_order = _filename_token(order_reference)
        handle, filename = tempfile.mkstemp(
            prefix=f"opus-{filename_order}-coo-report-",
            suffix=".pdf",
        )
        os.close(handle)
        path = Path(filename)
        ui.notify(f"Building {order_reference} COO PDF report...", type="info")
        try:
            result = await asyncio.to_thread(
                repository.export_order_investigation_pdf,
                path,
                order_reference,
                config["date_from"],
                settings.effective_extract_to(),
                config["expected_bays"],
                config["route_plan"],
                config["parcel_tonnes"],
            )
            ui.download(
                str(path),
                filename=(
                    f"OPUS-{filename_order}-COO-report-"
                    f"{datetime.now():%Y%m%d-%H%M}.pdf"
                ),
            )
            background_tasks.create(_remove_download_later(path))
            ui.notify(
                f"{order_reference} PDF ready: {result['pages']} pages.",
                type="positive",
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            ui.notify(
                f"{order_reference} PDF export failed: {exc}",
                type="negative",
                timeout=0,
            )

    async def download_control_template() -> None:
        handle, filename = tempfile.mkstemp(
            prefix="opus-stock-controls-",
            suffix=".xlsx",
        )
        os.close(handle)
        path = Path(filename)
        try:
            await asyncio.to_thread(create_control_template, path)
            ui.download(
                str(path),
                filename="OPUS-Order-Master-and-Opening-Balances.xlsx",
            )
            background_tasks.create(_remove_download_later(path))
        except Exception as exc:
            path.unlink(missing_ok=True)
            ui.notify(f"Template could not be created: {exc}", type="negative")

    async def handle_control_upload(event: Any) -> None:
        handle, filename = tempfile.mkstemp(prefix="opus-controls-", suffix=".xlsx")
        os.close(handle)
        path = Path(filename)
        try:
            await event.file.save(path)
            stock_state["preview"] = await asyncio.to_thread(
                preview_control_workbook,
                path,
                event.file.name,
            )
            await asyncio.to_thread(
                repository.record_control_workbook_preview,
                stock_state["preview"],
            )
        except Exception as exc:
            stock_state["preview"] = WorkbookPreview(
                filename=getattr(event.file, "name", "upload.xlsx"),
                file_sha256=b"",
                errors=[str(exc)],
            )
        finally:
            path.unlink(missing_ok=True)
            await refresh_content_preserving_scroll(ui.context.client)

    async def apply_control_preview() -> None:
        preview = stock_state.get("preview")
        if not isinstance(preview, WorkbookPreview) or not preview.valid:
            ui.notify("Upload a valid workbook before applying.", type="warning")
            return
        try:
            import_id = await asyncio.to_thread(
                repository.apply_control_workbook,
                preview,
            )
            stock_state["preview"] = None
            ui.notify(
                f"Control workbook applied as import {import_id}.",
                type="positive",
            )
            await refresh_analytics("stock")
        except Exception as exc:
            ui.notify(f"Workbook apply failed: {exc}", type="negative", timeout=0)

    def render_analytics_state(target: dict[str, Any]) -> bool:
        if target["loading"] and target["result"] is None:
            with ui.column().classes(
                "app-card w-full items-center justify-center gap-3"
            ):
                ui.spinner("dots", size="42px", color="primary")
                ui.label("Loading server-filtered analytics...").classes(
                    "section-subtitle"
                )
            return True
        if target["error"]:
            with ui.row().classes("status-banner error items-start no-wrap gap-3"):
                ui.icon("error", size="22px", color="red-8")
                with ui.column().classes("gap-0"):
                    ui.label("Analytics query failed").classes("font-bold")
                    ui.label(str(target["error"])).classes("section-subtitle")
            return target["result"] is None
        return False

    def data_view() -> None:
        if not state.snapshot.storage.get("analytics_schema_ready", 0):
            _analytics_unavailable(state.snapshot)
            return
        if render_analytics_state(data_state):
            return
        result = data_state["result"] or {}
        options = data_state["options"] or {}
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            ui.button(
                "Refresh view",
                icon="refresh",
                on_click=lambda: refresh_analytics("data"),
            ).props("outline no-caps")
            ui.button(
                "Sync latest OPUS data",
                icon="sync",
                on_click=run_opus_sync,
            ).props("outline no-caps")
            ui.button(
                "Export filtered XLSX",
                icon="filter_alt",
                on_click=lambda: export_data(False),
            ).props("outline no-caps")
            ui.button(
                "Export all XLSX",
                icon="download",
                on_click=lambda: export_data(True),
            ).props("unelevated no-caps").classes("primary-action")
        freshness = result.get("freshness") or {}
        ui.label(
            "Extraction freshness: "
            f"{freshness.get('extraction_freshness') or 'Unknown'} · "
            "Analytics freshness: "
            f"{freshness.get('analytics_freshness') or 'Unknown'}"
        ).classes("section-subtitle mono")

        with ui.card().classes("app-card shadow-none"):
            _section_heading(
                "Data filters",
                "Root dates always refer to the qualifying Transport Allocation.",
            )
            with ui.element("div").classes("analytics-filter-grid"):
                ui.input(
                    "Root date from",
                    value=data_filters_state["date_from"],
                    on_change=lambda event: update_analytics_filter(
                        "data", "date_from", event.value
                    ),
                ).props("outlined dense type=date")
                ui.input(
                    "Root date to",
                    value=data_filters_state["date_to"],
                    on_change=lambda event: update_analytics_filter(
                        "data", "date_to", event.value
                    ),
                ).props("outlined dense type=date")
                ui.input(
                    "Job reference",
                    value=data_filters_state["job_reference"],
                    on_change=lambda event: update_analytics_filter(
                        "data", "job_reference", event.value
                    ),
                ).props("outlined dense clearable debounce=600")
                ui.select(
                    [""] + list(options.get("checklists") or []),
                    value=data_filters_state["checklist_name"],
                    label="Checklist",
                    on_change=lambda event: update_analytics_filter(
                        "data", "checklist_name", event.value
                    ),
                ).props("outlined dense clearable")
                ui.select(
                    [""] + list(options.get("statuses") or []),
                    value=data_filters_state["status_group"],
                    label="Status",
                    on_change=lambda event: update_analytics_filter(
                        "data", "status_group", event.value
                    ),
                ).props("outlined dense clearable")

        with ui.element("div").classes("metric-grid w-full"):
            for status, icon in (
                ("Signed off", "task_alt"),
                ("Not started", "pending"),
                ("In progress", "play_circle"),
                ("Under review", "rate_review"),
                ("Closed", "block"),
                ("Cancelled", "cancel"),
                ("Other", "help_outline"),
            ):
                _analytics_metric_card(
                    status,
                    int((result.get("statuses") or {}).get(status, 0)),
                    "Checklist jobs · click to filter",
                    icon,
                    on_click=lambda _event=None, selected=status: set_data_status(
                        selected
                    ),
                    selected=data_filters_state["status_group"] == status,
                )

        with ui.card().classes("app-card shadow-none"):
            _section_heading(
                "Checklist totals",
                "Job totals and extracted detail coverage by workflow checklist.",
            )
            with ui.element("div").classes("checklist-total-grid"):
                for row in result.get("checklist_totals") or []:
                    with ui.element("div").classes("checklist-total-card"):
                        ui.label(str(row.get("checklist_name") or "")).classes(
                            "font-bold"
                        )
                        ui.label(
                            f"{int(row.get('checklist_jobs') or 0):,} jobs · "
                            f"{int(row.get('references') or 0):,} references"
                        ).classes("mono")
                        ui.label(
                            f"{int(row.get('detailed_jobs') or 0):,} detailed"
                        ).classes("section-subtitle")

        with ui.card().classes("app-card shadow-none"):
            total = int(result.get("total") or 0)
            page = int(result.get("page") or 1)
            page_size = int(result.get("page_size") or 100)
            last_page = max((total + page_size - 1) // page_size, 1)
            with ui.row().classes("w-full items-center justify-between gap-3"):
                _section_heading(
                    "Checklist job data",
                    f"{total:,} matching jobs · page {page:,} of {last_page:,}",
                )
                with ui.row().classes("items-center gap-1"):
                    ui.button(
                        icon="chevron_left",
                        on_click=lambda: change_data_page(-1),
                    ).props(f"flat round {'disable' if page <= 1 else ''}")
                    ui.button(
                        icon="chevron_right",
                        on_click=lambda: change_data_page(1),
                    ).props(
                        f"flat round {'disable' if page >= last_page else ''}"
                    )
            rows = result.get("rows") or []
            if rows:
                table = _table(
                    rows,
                    _columns(
                        ("job_reference", "Job reference", "left"),
                        ("order_number", "Order", "left"),
                        ("root_date", "Root date", "left"),
                        ("checklist_name", "Checklist", "left"),
                        ("opus_status", "OPUS status", "left"),
                        ("status_group", "Status", "left"),
                        ("operator_name", "Operator", "left"),
                        ("source_created_at", "Created", "left"),
                        ("source_signed_off_at", "Signed off", "left"),
                        ("percentage_complete", "Progress %", "right"),
                        ("answer_count", "Answers", "right"),
                        ("detail_complete", "Detailed", "left"),
                    ),
                    "job_row_id",
                    rows_per_page=0,
                )
                with table.add_slot("body-cell-job_reference"):
                    with table.cell("job_reference"):
                        ui.button().props(
                            ":label=props.value flat no-caps color=primary"
                        ).on(
                            "click",
                            js_handler="() => emit(props.row.job_reference)",
                            handler=lambda event: select_analytics_reference(
                                "data", str(event.args)
                            ),
                        )
                with table.add_slot("body-cell-checklist_name"):
                    with table.cell("checklist_name"):
                        ui.button().props(
                            ":label=props.value flat no-caps color=primary"
                        ).on(
                            "click",
                            js_handler="() => emit(props.row.job_row_id)",
                            handler=lambda event: open_checklist_detail(
                                int(event.args)
                            ),
                        )
            else:
                _empty_state(
                    "filter_alt_off",
                    "No checklist jobs match",
                    "Clear one or more filters to broaden the result.",
                )

        with ui.card().classes("app-card shadow-none"):
            render_analytics_workflow(
                data_state["workflow"],
                str(data_state["selected_reference"]),
            )

    def ops_view() -> None:
        if not state.snapshot.storage.get("analytics_schema_ready", 0):
            _analytics_unavailable(state.snapshot)
            return
        if render_analytics_state(ops_state):
            return
        result = ops_state["result"] or {}
        options = ops_state["options"] or {}
        metrics = result.get("metrics") or {}
        with ui.card().classes("app-card shadow-none"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                _section_heading(
                    "Transit filters",
                    "Transit starts after signed-off Loading and Exit and ends "
                    "when Offloading and Exit starts.",
                )
                ui.button(
                    "Refresh",
                    icon="refresh",
                    on_click=lambda: refresh_analytics("ops"),
                ).props("outline no-caps")
            with ui.element("div").classes("analytics-filter-grid"):
                ui.input(
                    "Root date from",
                    value=ops_filters_state["date_from"],
                    on_change=lambda event: update_analytics_filter(
                        "ops", "date_from", event.value
                    ),
                ).props("outlined dense type=date")
                ui.input(
                    "Root date to",
                    value=ops_filters_state["date_to"],
                    on_change=lambda event: update_analytics_filter(
                        "ops", "date_to", event.value
                    ),
                ).props("outlined dense type=date")
                ui.select(
                    [""] + list(options.get("origins") or []),
                    value=ops_filters_state["origin"],
                    label="Origin",
                    on_change=lambda event: update_analytics_filter(
                        "ops", "origin", event.value
                    ),
                ).props("outlined dense clearable")
                ui.select(
                    [""] + list(options.get("destinations") or []),
                    value=ops_filters_state["destination"],
                    label="Destination",
                    on_change=lambda event: update_analytics_filter(
                        "ops", "destination", event.value
                    ),
                ).props("outlined dense clearable")
                ui.select(
                    [""] + list(options.get("truck_types") or []),
                    value=ops_filters_state["truck_type"],
                    label="Truck type",
                    on_change=lambda event: update_analytics_filter(
                        "ops", "truck_type", event.value
                    ),
                ).props("outlined dense clearable")

        with ui.element("div").classes("metric-grid w-full"):
            for label, key, detail, icon in (
                ("Trucks in transit", "distinct_trucks", "Distinct registrations", "local_shipping"),
                ("Transit references", "qualifying_references", "Workflow-qualified loads", "route"),
                ("Transit tonnes", "transit_tonnes", "Latest signed-off loading", "scale"),
                ("Unknown origin", "unknown_origin", "Included and flagged", "wrong_location"),
                ("Unknown destination", "unknown_destination", "Included and flagged", "wrong_location"),
                ("Route fallbacks", "route_fallbacks", "Transport Allocation fallback", "alt_route"),
                ("Missing registration", "missing_registration", "Excluded from truck KPI", "no_transfer"),
                ("Duplicate active refs", "duplicate_active_references", "References sharing a registration", "content_copy"),
                ("Stopped closures", "stopped_closure_exceptions", "Final closure after loading", "block"),
            ):
                _analytics_metric_card(
                    label,
                    metrics.get(key, 0),
                    detail,
                    icon,
                )

        route_rows = [
            {
                **row,
                "_key": f"{row.get('origin') or 'Unknown'}|"
                f"{row.get('destination') or 'Unknown'}",
            }
            for row in result.get("routes") or []
        ]
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Transit route matrix",
                "Tonnes currently moving from each origin to each destination.",
                route_heatmap_options(route_rows),
            )
            with ui.card().classes("app-card shadow-none"):
                _section_heading(
                    "Route summary",
                    "Separate origin and destination columns with truck and tonne totals.",
                )
                _table(
                    route_rows,
                    _columns(
                        ("origin", "Origin", "left"),
                        ("destination", "Destination", "left"),
                        ("trucks", "Trucks", "right"),
                        ("reference_count", "References", "right"),
                        ("tonnes", "Tonnes", "right"),
                    ),
                    "_key",
                    rows_per_page=15,
                )

        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Transit by destination",
                "Distinct trucks and loaded tonnes.",
                grouped_bar_options(
                    result.get("destinations") or [],
                    category_key="category",
                    series=[
                        ("Trucks", "trucks", "#1c2545"),
                        ("Tonnes", "tonnes", "#e04403"),
                    ],
                ),
            )
            _chart_card(
                "Truck type split",
                "Qualifying in-transit references.",
                donut_options(
                    result.get("truck_types") or [],
                    category_key="category",
                    value_key="reference_count",
                ),
            )
            _chart_card(
                "Current workflow stage",
                "Where each in-transit reference currently sits.",
                donut_options(
                    result.get("stages") or [],
                    category_key="category",
                    value_key="reference_count",
                ),
            )

        with ui.card().classes("app-card shadow-none"):
            _section_heading(
                "In-transit references",
                "Click a reference to inspect the chronological workflow.",
            )
            rows = result.get("rows") or []
            if rows:
                table = _table(
                    rows,
                    _columns(
                        ("job_reference", "Job reference", "left"),
                        ("truck_registration", "Truck", "left"),
                        ("origin", "Origin", "left"),
                        ("destination", "Destination", "left"),
                        ("loaded_tonnes", "Tonnes", "right"),
                        ("route_fallback", "Route fallback", "left"),
                        ("departed_at", "Loading signed off", "left"),
                        ("current_checklist", "Current stage", "left"),
                        ("current_opus_status", "Current status", "left"),
                        ("truck_type", "Truck type", "left"),
                        ("duplicate_active_truck", "Duplicate truck", "left"),
                        ("order_reference", "Order", "left"),
                        ("client_name", "Client", "left"),
                    ),
                    "job_reference",
                    rows_per_page=25,
                )
                with table.add_slot("body-cell-job_reference"):
                    with table.cell("job_reference"):
                        ui.button().props(
                            ":label=props.value flat no-caps color=primary"
                        ).on(
                            "click",
                            js_handler="() => emit(props.row.job_reference)",
                            handler=lambda event: select_analytics_reference(
                                "ops", str(event.args)
                            ),
                        )
            else:
                _empty_state(
                    "local_shipping",
                    "No trucks currently qualify as in transit",
                    "The attempt-aware transit rules found no matching references.",
                )
        with ui.card().classes("app-card shadow-none"):
            render_analytics_workflow(
                ops_state["workflow"],
                str(ops_state["selected_reference"]),
            )

    def render_order_study(
        config: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        order_reference = str(config["order_reference"])
        metrics = result.get("metrics") or {}
        with ui.card().classes("app-card w-full shadow-none"):
            with ui.row().classes(
                "w-full items-start justify-between gap-3 flex-wrap"
            ):
                with ui.column().classes("gap-1"):
                    _section_heading(
                        order_reference,
                        (
                            f"Transport Allocation roots from {result.get('date_from')} "
                            f"through {result.get('date_to')}; later linked loading and "
                            "offloading events remain attached to their root reference."
                        ),
                    )
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.badge(
                            f"Client: {result.get('client_name') or 'Unmapped'}",
                            color="blue-grey-8",
                        ).props("outline")
                        expected_bays = result.get("expected_bays") or []
                        if expected_bays:
                            ui.label("Expected bays").classes(
                                "section-subtitle font-medium"
                            )
                            for bay in expected_bays:
                                ui.badge(str(bay), color="teal-8").props("outline")
                        else:
                            ui.badge(
                                "Routes and bays derived from OPUS",
                                color="teal-8",
                            ).props("outline")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    if config.get("route_plan"):
                        ui.button(
                            "COO PDF",
                            icon="picture_as_pdf",
                            on_click=lambda selected=config: (
                                export_order_investigation_pdf(selected)
                            ),
                        ).props("unelevated no-caps").classes("primary-action")
                    ui.button(
                        "Detailed XLSX",
                        icon="download",
                        on_click=lambda selected=config: (
                            export_order_investigation(selected)
                        ),
                    ).props("outline no-caps")
        if result.get("coverage_warning"):
            with ui.row().classes(
                "status-banner error w-full items-start no-wrap gap-3"
            ):
                ui.icon("warning", size="22px", color="red-8")
                with ui.column().classes("gap-0"):
                    ui.label("Study coverage is incomplete").classes("font-bold")
                    ui.label(str(result["coverage_warning"])).classes(
                        "section-subtitle"
                    )
        elif result.get("coverage_note"):
            with ui.row().classes(
                "status-banner w-full items-start no-wrap gap-3"
            ):
                ui.icon("info", size="22px", color="teal-8")
                with ui.column().classes("gap-0"):
                    ui.label("Source activity note").classes("font-bold")
                    ui.label(str(result["coverage_note"])).classes(
                        "section-subtitle"
                    )

        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "Order stock overview",
                (
                    "SOH is calculated from this order's own checklist locations. "
                    "Source-only dispatch points are reported but excluded from SOH; "
                    "destination and intermediate locations are counted once."
                ),
            )
            parcel_tonnes = float(metrics.get("parcel_tonnes") or 0)
            parcel_text = (
                f"{order_reference} is a {parcel_tonnes:,.0f} t parcel. "
                if parcel_tonnes
                else ""
            )
            ui.label(
                (
                    f"{parcel_text}The signed checklist chain records "
                    f"{metrics.get('loaded_tonnes', 0):,.3f} t loaded and "
                    f"{metrics.get('offloaded_tonnes', 0):,.3f} t offloaded. "
                    f"Known order-location SOH is "
                    f"{metrics.get('order_location_soh_tonnes', 0):,.3f} t, with "
                    f"{metrics.get('in_transit_tonnes', 0):,.3f} t separately "
                    "identified in transit."
                )
            ).classes("section-subtitle")
            with ui.element("div").classes("metric-grid w-full"):
                overview_metrics = [
                    (
                        "Opening stock",
                        metrics.get("opening_stock_tonnes", 0),
                        "Governed order-location openings (t)",
                        "account_balance",
                    ),
                    (
                        "Loading and Exit",
                        metrics.get("loading_exit_signed", 0),
                        "Allocations with signed loading evidence",
                        "outbox",
                    ),
                    (
                        "Offloading and Exit",
                        metrics.get("offloading_exit_signed", 0),
                        "Allocations with signed offloading evidence",
                        "move_to_inbox",
                    ),
                    (
                        "Known order SOH",
                        metrics.get("order_location_soh_tonnes", 0),
                        (
                            f"Across {int(metrics.get('order_locations') or 0):,} "
                            "destination/intermediate locations (t)"
                        ),
                        "summarize",
                    ),
                    (
                        "In transit",
                        metrics.get("in_transit_tonnes", 0),
                        "Strict qualifying loaded tonnes (t)",
                        "local_shipping",
                    ),
                    (
                        "Completed chains",
                        metrics.get("completed_checklist_chains", 0),
                        "Signed loading and signed offloading",
                        "task_alt",
                    ),
                    (
                        "Needs review",
                        metrics.get("review_references", 0),
                        "Unique references with an audit reason",
                        "warning",
                    ),
                ]
                if parcel_tonnes:
                    overview_metrics.insert(
                        0,
                        (
                        "Parcel allocation",
                        parcel_tonnes,
                        "Order quantity (t)",
                        "assignment",
                        ),
                    )
                for label, value, detail, icon in overview_metrics:
                    _analytics_metric_card(label, value, detail, icon)
            unresolved_soh = int(metrics.get("unresolved_soh_locations") or 0)
            if unresolved_soh:
                with ui.row().classes(
                    "status-banner warning w-full items-start no-wrap gap-3"
                ):
                    ui.icon("warning", size="22px", color="orange-8")
                    ui.label(
                        (
                            f"{unresolved_soh:,} intermediate location/bay balance"
                            f"{'s' if unresolved_soh != 1 else ''} cannot be included "
                            "in known SOH because signed dispatches exceed stored "
                            "receipts and no governed opening balance is available."
                        )
                    ).classes("section-subtitle")

        load_type_rows = [
            {
                **row,
                "_key": str(row.get("truck_type") or "Unknown"),
            }
            for row in result.get("load_types") or []
        ]
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Load split by truck type",
                (
                    "Each slice is one signed Loading and Exit job, grouped by the "
                    "truck type recorded in OPUS."
                ),
                donut_options(
                    load_type_rows,
                    category_key="truck_type",
                    value_key="load_count",
                ),
            )
            with ui.card().classes("app-card shadow-none"):
                _section_heading(
                    "Truck type load detail",
                    "Counts and nett tonnes use signed loading evidence only.",
                )
                _table(
                    load_type_rows,
                    _columns(
                        ("truck_type", "Truck type", "left"),
                        ("load_count", "Signed loads", "right"),
                        ("load_pct", "Load split %", "right"),
                        ("loaded_tonnes", "Loaded nett t", "right"),
                    ),
                    "_key",
                    rows_per_page=10,
                )

        route_rows = [
            {
                **row,
                "route": (
                    f"{row.get('origin_display') or row.get('origin')} -> "
                    f"{row.get('destination_display') or row.get('destination')}"
                ),
                "_key": f"{row.get('origin')}|{row.get('destination')}",
            }
            for row in result.get("routes") or []
        ]
        _section_heading(
            "Order-specific checklist routes",
            (
                "Origins come from signed Loading and Exit checklists; destinations "
                "come from their linked signed Offloading and Exit checklists."
            ),
        )
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Loaded and offloaded tonnes by route",
                "Each route is isolated using the selected order number.",
                grouped_bar_options(
                    route_rows,
                    category_key="route",
                    series=[
                        ("Loaded", "loaded_tonnes", "#e04403"),
                        ("Offloaded", "offloaded_tonnes", "#007d6d"),
                    ],
                    horizontal=True,
                    show_labels=True,
                    value_suffix="t",
                ),
            )
            with ui.card().classes("app-card shadow-none"):
                _section_heading(
                    "Checklist route totals",
                    "Actual OPUS loading and offloading points remain separate.",
                )
                _table(
                    route_rows,
                    _columns(
                        ("origin_display", "Loading point", "left"),
                        ("destination_display", "Offloading point", "left"),
                        ("movement_references", "References", "right"),
                        ("loaded_movements", "Loaded", "right"),
                        ("offloaded_movements", "Offloaded", "right"),
                        ("loaded_tonnes", "Loaded t", "right"),
                        ("offloaded_tonnes", "Offloaded t", "right"),
                        ("net_difference_tonnes", "Difference t", "right"),
                    ),
                    "_key",
                    rows_per_page=15,
                )

        leg_descriptions = {
            "Mine to BCF": (
                "Mine dispatch is loading nett weight; BCF receipt is offloading "
                "nett weight into the listed BCF bay."
            ),
            "BCF to BC": (
                "BCF dispatch is loading nett weight from the source bay; BC receipt "
                "is offloading nett weight into the destination area."
            ),
            "Mine to BC Direct": (
                "Direct Mine dispatch and BC receipt bypass BCF and remain separate "
                "from staged stock."
            ),
        }
        leg_rows = {
            str(row.get("route_name")): row for row in result.get("legs") or []
        }
        visible_leg_names = [
            leg_name
            for leg_name, leg in leg_rows.items()
            if int(leg.get("movement_references") or 0)
            or any(
                route.get("route_name") == leg_name
                for route in result.get("planned_routes") or []
            )
        ]
        all_leg_lanes = result.get("leg_lanes") or []
        _section_heading(
            "Physical movement by leg",
            (
                "Each section is one truck leg. Do not add leg totals together to "
                "calculate order stock because staged tonnes can move more than once."
            ),
        )
        for leg_name in visible_leg_names:
            leg = leg_rows.get(leg_name) or {}
            lanes = [
                {
                    **row,
                    "lane": f"{row.get('from_bay')} -> {row.get('to_bay')}",
                    "_key": (
                        f"{row.get('route_name')}|{row.get('from_bay')}|"
                        f"{row.get('to_bay')}"
                    ),
                }
                for row in all_leg_lanes
                if row.get("route_name") == leg_name
            ]
            with ui.card().classes("app-card w-full shadow-none"):
                _section_heading(
                    leg_name,
                    leg_descriptions.get(
                        leg_name,
                        "Observed OPUS movement grouped by its recorded origin, "
                        "destination and storage locations.",
                    ),
                )
                with ui.element("div").classes("metric-grid w-full"):
                    for label, value, detail, icon in (
                        (
                            "Dispatched",
                            leg.get("loaded_tonnes", 0),
                            "Signed-off loading nett (t)",
                            "outbox",
                        ),
                        (
                            "Received",
                            leg.get("offloaded_tonnes", 0),
                            "Signed-off offloading nett (t)",
                            "move_to_inbox",
                        ),
                        (
                            "Awaiting receipt",
                            leg.get("pending_loaded_tonnes", 0),
                            "Loaded tonnes without signed-off receipt (t)",
                            "pending_actions",
                        ),
                        (
                            "Movement difference",
                            leg.get("movement_difference_tonnes", 0),
                            "Received less dispatched (t)",
                            "difference",
                        ),
                    ):
                        _analytics_metric_card(label, value, detail, icon)
                if lanes:
                    with ui.element("div").classes("analytics-chart-grid"):
                        _chart_card(
                            f"{leg_name} by bay / area",
                            "Values are labelled in tonnes and reconcile to the table.",
                            grouped_bar_options(
                                lanes,
                                category_key="lane",
                                series=[
                                    ("Dispatched", "loaded_tonnes", "#e04403"),
                                    ("Received", "offloaded_tonnes", "#007d6d"),
                                ],
                                horizontal=True,
                                show_labels=True,
                                value_suffix="t",
                            ),
                        )
                        with ui.card().classes("app-card shadow-none"):
                            _section_heading(
                                f"{leg_name} detail",
                                "Source and destination storage remain separate.",
                            )
                            _table(
                                lanes,
                                _columns(
                                    ("origin", "Loading point", "left"),
                                    ("from_bay", "From", "left"),
                                    ("destination", "Offloading point", "left"),
                                    ("to_bay", "To", "left"),
                                    ("movement_references", "References", "right"),
                                    ("loaded_tonnes", "Dispatched t", "right"),
                                    ("offloaded_tonnes", "Received t", "right"),
                                    (
                                        "movement_difference_tonnes",
                                        "Difference t",
                                        "right",
                                    ),
                                    (
                                        "pending_references",
                                        "Awaiting receipt",
                                        "right",
                                    ),
                                ),
                                "_key",
                                rows_per_page=15,
                            )
                else:
                    _empty_state(
                        "route",
                        f"No {leg_name} movement",
                        "OPUS has no signed-off loading or offloading for this leg.",
                    )

        route_plan_rows = [
            {
                **row,
                "_key": (
                    f"{row.get('route_name')}|{row.get('from_bay')}|"
                    f"{row.get('to_bay')}"
                ),
            }
            for row in result.get("route_plan") or []
        ]
        if route_plan_rows:
            with ui.card().classes("app-card w-full shadow-none"):
                _section_heading(
                    "Route plan alignment",
                    (
                        "Planned route observed means OPUS matches the supplied route "
                        "and bay matrix. Observed outside plan remains visible for "
                        "investigation."
                    ),
                )
                _table(
                    route_plan_rows,
                    _columns(
                        ("plan_status", "Route classification", "left"),
                        ("route_name", "Physical leg", "left"),
                        ("from_bay", "From", "left"),
                        ("to_bay", "To", "left"),
                        ("movement_references", "References", "right"),
                        ("loaded_tonnes", "Dispatched t", "right"),
                        ("offloaded_tonnes", "Received t", "right"),
                    ),
                    "_key",
                    rows_per_page=15,
                )

        location_rows = [
            {
                **row,
                "location_and_bay": f"{row.get('location')} | {row.get('bay')}",
                "soh_scope": row.get("balance_status"),
                "_key": f"{row.get('location')}|{row.get('bay')}",
            }
            for row in result.get("location_balances") or []
        ]
        _section_heading(
            "Order-specific SOH by location and bay",
            (
                "Every location is classified from this order's actual checklist "
                "flow. Source-only loading points are visible but are not treated as "
                "stock destinations."
            ),
        )
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Location and bay movement balance",
                "Opening + signed offloading receipts - signed loading dispatches.",
                grouped_bar_options(
                    location_rows,
                    category_key="location_and_bay",
                    series=[
                        ("Opening", "opening_tonnes", "#1c2545"),
                        ("Received", "received_tonnes", "#007d6d"),
                        ("Dispatched", "dispatched_tonnes", "#e04403"),
                        ("Order SOH", "soh_tonnes", "#0f766e"),
                    ],
                    horizontal=True,
                    show_labels=True,
                    value_suffix="t",
                ),
            )
            with ui.card().classes("app-card shadow-none"):
                _section_heading(
                    "Location and bay SOH detail",
                    (
                        "Intermediate and destination balances count toward order SOH; "
                        "origin dispatches remain movement evidence only."
                    ),
                )
                _table(
                    location_rows,
                    _columns(
                        ("location_role", "Role", "left"),
                        ("location", "Location", "left"),
                        ("bay", "Bay / storage", "left"),
                        ("opening_status", "Opening source", "left"),
                        ("opening_tonnes", "Opening t", "right"),
                        ("received_tonnes", "Received t", "right"),
                        ("dispatched_tonnes", "Dispatched t", "right"),
                        ("movement_balance_tonnes", "Movement balance t", "right"),
                        ("soh_tonnes", "Order SOH t", "right"),
                        ("soh_scope", "SOH treatment", "left"),
                    ),
                    "_key",
                    rows_per_page=20,
                )

        _chart_card(
            "Daily signed-off movement",
            "Labels show tonnes on the loading and offloading sign-off dates.",
            movement_trend_options(
                result.get("daily") or [],
                show_labels=True,
                value_suffix="t",
            ),
        )

        loading_checklists = result.get("loading_checklists") or []
        offloading_checklists = result.get("offloading_checklists") or []
        _section_heading(
            "Loading and offloading checklist chain",
            (
                "Every Transport Allocation is shown with its latest checklist status "
                "and the signed-off operational fact used in this order calculation."
            ),
        )
        with ui.tabs().classes("w-full").props(
            "dense align=left inline-label outside-arrows mobile-arrows"
        ) as checklist_tabs:
            loading_tab = ui.tab(
                f"Loading and Exit ({len(loading_checklists):,})",
                icon="outbox",
            )
            offloading_tab = ui.tab(
                f"Offloading and Exit ({len(offloading_checklists):,})",
                icon="move_to_inbox",
            )
        with ui.tab_panels(checklist_tabs, value=loading_tab).classes("w-full"):
            with ui.tab_panel(loading_tab):
                _table(
                    loading_checklists,
                    _columns(
                        ("job_reference", "Job reference", "left"),
                        ("allocation_loading_point", "Allocated from", "left"),
                        ("allocation_offloading_point", "Allocated to", "left"),
                        ("latest_status", "Latest checklist status", "left"),
                        ("latest_opus_status", "Latest OPUS status", "left"),
                        ("evidence_status", "Signed evidence", "left"),
                        ("loading_point", "Checklist loading point", "left"),
                        ("loading_bay", "Loading bay", "left"),
                        ("nett_weight_tonnes", "Loaded nett t", "right"),
                        ("operator_name", "Loading operator", "left"),
                        ("signed_off_at", "Signed off", "left"),
                        ("signed_off_attempts", "Signed attempts", "right"),
                        ("validation_errors", "Validation", "left"),
                    ),
                    "job_reference",
                    rows_per_page=25,
                )
            with ui.tab_panel(offloading_tab):
                _table(
                    offloading_checklists,
                    _columns(
                        ("job_reference", "Job reference", "left"),
                        ("planned_offloading_point", "Planned destination", "left"),
                        ("planned_offloading_bay", "Planned bay", "left"),
                        ("latest_status", "Latest checklist status", "left"),
                        ("latest_opus_status", "Latest OPUS status", "left"),
                        ("evidence_status", "Signed evidence", "left"),
                        ("offloading_point", "Checklist offloading point", "left"),
                        ("offloading_bay", "Offloading bay", "left"),
                        ("nett_weight_tonnes", "Offloaded nett t", "right"),
                        ("operator_name", "Offloading operator", "left"),
                        ("signed_off_at", "Signed off", "left"),
                        ("signed_off_attempts", "Signed attempts", "right"),
                        ("validation_errors", "Validation", "left"),
                    ),
                    "job_reference",
                    rows_per_page=25,
                )

        movements = result.get("movements") or []
        exceptions = result.get("exceptions") or []
        allocation_gaps = result.get("allocation_gaps") or []
        with ui.tabs().classes("w-full").props(
            "dense align=left inline-label outside-arrows mobile-arrows"
        ) as tabs:
            movement_tab = ui.tab(
                f"Movement reconciliation ({len(movements):,})",
                icon="compare_arrows",
            )
            exception_tab = ui.tab(
                f"Audit review ({len(exceptions):,})",
                icon="warning",
            )
            gap_tab = ui.tab(
                f"No signed-off load ({len(allocation_gaps):,})",
                icon="playlist_remove",
            )
        movement_columns = _columns(
            ("job_reference", "Job reference", "left"),
            ("root_date", "TA date", "left"),
            ("route_name", "Supply chain leg", "left"),
            ("truck_registration", "Truck", "left"),
            ("origin_display", "Origin", "left"),
            ("origin_bay", "Loading bay / fallback", "left"),
            ("loaded_tonnes", "Loaded nett t", "right"),
            ("loading_operator", "Loading operator", "left"),
            ("loading_signed_off_at", "Loading signed off", "left"),
            ("destination_display", "Destination", "left"),
            ("destination_bay", "Offloading bay / fallback", "left"),
            ("offloaded_tonnes", "Offloaded nett t", "right"),
            ("offloading_operator", "Offloading operator", "left"),
            ("offloading_signed_off_at", "Offloading signed off", "left"),
            ("variance_tonnes", "Variance t", "right"),
            ("delivery_pct", "Delivery %", "right"),
            ("in_transit", "In transit", "left"),
            ("current_checklist", "Current checklist", "left"),
            ("current_opus_status", "Current status", "left"),
            ("audit_status", "Audit", "left"),
            ("audit_reasons", "Review reasons", "left"),
        )
        with ui.tab_panels(tabs, value=movement_tab).classes("w-full"):
            with ui.tab_panel(movement_tab):
                _table(
                    movements,
                    movement_columns,
                    "job_reference",
                    rows_per_page=25,
                )
            with ui.tab_panel(exception_tab):
                if exceptions:
                    _table(
                        exceptions,
                        movement_columns,
                        "job_reference",
                        rows_per_page=25,
                    )
                else:
                    _empty_state(
                        "verified",
                        "No movement exceptions",
                        "All movement records reconcile within the study rules.",
                    )
            with ui.tab_panel(gap_tab):
                _table(
                    allocation_gaps,
                    _columns(
                        ("job_reference", "Job reference", "left"),
                        ("root_date", "TA date", "left"),
                        ("truck_registration", "Truck", "left"),
                        (
                            "allocation_loading_point",
                            "Allocated loading point",
                            "left",
                        ),
                        (
                            "allocation_offloading_point",
                            "Allocated offloading point",
                            "left",
                        ),
                        ("current_checklist", "Current checklist", "left"),
                        ("current_opus_status", "Current status", "left"),
                        ("transit_exclusion_reason", "Transit exclusion", "left"),
                    ),
                    "job_reference",
                    rows_per_page=25,
                )

    def render_pelagic_soh_comparison(result: dict[str, Any]) -> None:
        generated_at = datetime.fromisoformat(str(result["report_generated_at"]))
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "OPUS vs Pelagic SOH",
                (
                    f"One-time comparison to the Pelagic controller report generated "
                    f"{generated_at:%d %b %Y %H:%M} SAST. OPUS is calculated at the "
                    "same cutoff and remains the source of truth."
                ),
            )
            with ui.row().classes(
                "status-banner warning w-full items-start no-wrap gap-3"
            ):
                ui.icon("info", size="22px", color="orange-8")
                ui.label(
                    (
                        f"Excluded manual fields: "
                        f"{', '.join(result.get('excluded_manual_fields') or [])}. "
                        f"{result.get('exclusion_reason')}"
                    )
                ).classes("section-subtitle")

        order_rows = [
            {**row, "_key": str(row.get("order_reference"))}
            for row in result.get("orders") or []
        ]
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Order stock at recorded locations",
                "OPUS and Pelagic are compared at the same dated cutoff.",
                grouped_bar_options(
                    order_rows,
                    category_key="order_reference",
                    series=[
                        ("OPUS SOH", "opus_total_soh_tonnes", "#007d6d"),
                        ("Pelagic SOH", "pelagic_total_soh_tonnes", "#e04403"),
                    ],
                    show_labels=True,
                    value_suffix="t",
                ),
            )
            with ui.card().classes("app-card shadow-none"):
                _section_heading(
                    "Order-level SOH reconciliation",
                    (
                        "Variance is OPUS less Pelagic. Every difference above "
                        "0.01 t remains visible."
                    ),
                )
                _table(
                    order_rows,
                    _columns(
                        ("order_reference", "Order", "left"),
                        ("allocation_tonnes", "Allocation t", "right"),
                        ("opus_bcf_soh_tonnes", "OPUS BCF t", "right"),
                        ("pelagic_bcf_soh_tonnes", "Pelagic BCF t", "right"),
                        ("bcf_variance_tonnes", "BCF variance t", "right"),
                        ("opus_bc_soh_tonnes", "OPUS BC t", "right"),
                        ("pelagic_bc_soh_tonnes", "Pelagic BC t", "right"),
                        ("bc_variance_tonnes", "BC variance t", "right"),
                        ("opus_total_soh_tonnes", "OPUS total t", "right"),
                        ("pelagic_total_soh_tonnes", "Pelagic total t", "right"),
                        ("total_variance_tonnes", "Total variance t", "right"),
                        ("comparison_status", "Result", "left"),
                    ),
                    "_key",
                    rows_per_page=10,
                )

        bay_rows = [
            {
                **row,
                "comparison_area": (
                    f"{row.get('order_reference')} | {row.get('site')} | "
                    f"{row.get('bay')}"
                ),
                "_key": (
                    f"{row.get('order_reference')}|{row.get('site')}|"
                    f"{row.get('bay')}"
                ),
            }
            for row in result.get("bays") or []
        ]
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "SOH comparison by bay / area",
                "Zero rows remain visible so omitted stock is not hidden.",
                grouped_bar_options(
                    bay_rows,
                    category_key="comparison_area",
                    series=[
                        ("OPUS SOH", "opus_soh_tonnes", "#007d6d"),
                        ("Pelagic SOH", "pelagic_soh_tonnes", "#e04403"),
                    ],
                    horizontal=True,
                    show_labels=True,
                    value_suffix="t",
                ),
            )
            with ui.card().classes("app-card shadow-none"):
                _section_heading(
                    "Bay / area discrepancies",
                    "Variance is OPUS less the dated Pelagic snapshot.",
                )
                _table(
                    bay_rows,
                    _columns(
                        ("order_reference", "Order", "left"),
                        ("site", "Site", "left"),
                        ("bay", "Bay / area", "left"),
                        ("opus_soh_tonnes", "OPUS SOH t", "right"),
                        ("pelagic_soh_tonnes", "Pelagic SOH t", "right"),
                        ("variance_tonnes", "Variance t", "right"),
                        ("variance_pct", "Variance %", "right"),
                        ("comparison_status", "Result", "left"),
                    ),
                    "_key",
                    rows_per_page=15,
                )

        leg_rows = [
            {
                **row,
                "_key": (
                    f"{row.get('order_reference')}|{row.get('route_name')}"
                ),
            }
            for row in result.get("legs") or []
        ]
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "OPUS physical-leg context",
                (
                    "These rows explain where stock differences may arise. They are "
                    "movement legs and must not be added as an SOH total."
                ),
            )
            _table(
                leg_rows,
                _columns(
                    ("order_reference", "Order", "left"),
                    ("route_name", "Physical leg", "left"),
                    ("dispatched_tonnes", "Dispatched t", "right"),
                    ("received_tonnes", "Received t", "right"),
                    ("movement_difference_tonnes", "Difference t", "right"),
                    ("pending_references", "Awaiting receipt", "right"),
                    ("pending_loaded_tonnes", "Awaiting receipt t", "right"),
                ),
                "_key",
                rows_per_page=10,
            )

    def render_vessel_reconciliation(result: dict[str, Any]) -> None:
        summary = result.get("summary") or {}
        order_rows = [
            {**row, "_key": str(row.get("order_reference"))}
            for row in result.get("orders") or []
        ]
        eleven_mg = next(
            row
            for row in order_rows
            if row.get("order_reference") == "KFTS26-11MG"
        )
        vessel_gap = abs(float(summary.get("grounded_to_draft_tonnes") or 0))
        opus_grounded_gap = abs(
            float(summary.get("opus_vs_grounded_tonnes") or 0)
        )
        gap_multiple = (
            vessel_gap / opus_grounded_gap if opus_grounded_gap else 0
        )

        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "Final vessel reconciliation",
                (
                    "Sleek comparison of OPUS truck receipts into BC, the dated "
                    "Pelagic SOH snapshot, Pelagic grounded tonnes, and vessel survey "
                    "results for both Pelagic orders."
                ),
            )
            with ui.row().classes(
                "status-banner warning w-full items-start no-wrap gap-3"
            ):
                ui.icon("warning", size="22px", color="orange-8")
                ui.label(
                    (
                        "OPUS currently records truck receipts into BC bays but has no "
                        "vessel load-out checklist. Vessel draft, DG, shore-scale, and "
                        "grounded values therefore remain external Pelagic evidence."
                    )
                ).classes("section-subtitle")
            ui.label(
                (
                    f"The grounded-to-draft vessel gap is {vessel_gap:,.3f} t, "
                    f"{gap_multiple:,.1f} times the combined "
                    f"{opus_grounded_gap:,.3f} t OPUS-versus-Pelagic inbound gap. "
                    f"KFTS26-11MG contributes "
                    f"{eleven_mg.get('share_of_vessel_variance_pct', 0):,.3f}% "
                    "of the vessel variance."
                )
            ).classes("section-subtitle font-medium")

        with ui.element("div").classes("metric-grid w-full"):
            for label, value, detail, icon in (
                (
                    "OPUS BC receipts",
                    summary.get("opus_bc_receipts_tonnes", 0),
                    "Signed-off OPUS offloads into BC (t)",
                    "inventory_2",
                ),
                (
                    "Pelagic SOH at BC",
                    summary.get("pelagic_soh_bc_tonnes", 0),
                    "5 Aug 20:23 controller snapshot (t)",
                    "fact_check",
                ),
                (
                    "Pelagic grounded",
                    summary.get("pelagic_grounded_tonnes", 0),
                    "Final controller tonnes at BC (t)",
                    "warehouse",
                ),
                (
                    "Vessel draft",
                    summary.get("vessel_draft_tonnes", 0),
                    "Combined vessel draft survey (t)",
                    "directions_boat",
                ),
                (
                    "Inbound source gap",
                    summary.get("opus_vs_grounded_tonnes", 0),
                    "OPUS less Pelagic grounded (t)",
                    "compare_arrows",
                ),
                (
                    "Vessel variance",
                    summary.get("grounded_to_draft_tonnes", 0),
                    "Draft less grounded (t)",
                    "difference",
                ),
                (
                    "Draft recovery",
                    f"{summary.get('draft_recovery_pct', 0):,.3f}%",
                    "Draft / Pelagic grounded",
                    "percent",
                ),
            ):
                _analytics_metric_card(label, value, detail, icon)

        combined_rows = [
            {
                "source": label,
                "tonnes": summary.get(key, 0),
            }
            for label, key in (
                ("OPUS BC receipts", "opus_bc_receipts_tonnes"),
                ("Pelagic SOH at BC", "pelagic_soh_bc_tonnes"),
                ("Pelagic grounded", "pelagic_grounded_tonnes"),
                ("Vessel draft", "vessel_draft_tonnes"),
            )
        ]
        _chart_card(
            "Combined 40,000 t vessel comparison",
            (
                "Four independent evidence points shown at full width; values are "
                "labelled in tonnes."
            ),
            grouped_bar_options(
                combined_rows,
                category_key="source",
                series=[("Tonnes", "tonnes", "#1c2545")],
                show_labels=True,
                value_suffix="t",
            ),
        )

        _chart_card(
            "Order comparison: BC receipts to vessel draft",
            (
                "The chart keeps each order separate and shows where the major "
                "variance develops."
            ),
            grouped_bar_options(
                order_rows,
                category_key="order_reference",
                series=[
                    ("OPUS BC", "opus_bc_receipts_tonnes", "#1c2545"),
                    ("Pelagic SOH", "pelagic_soh_bc_tonnes", "#e04403"),
                    ("Pelagic grounded", "pelagic_grounded_tonnes", "#007d6d"),
                    ("Vessel draft", "vessel_draft_tonnes", "#b91c1c"),
                ],
                show_labels=True,
                value_suffix="t",
            ),
        )

        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "Order-level vessel summary",
                (
                    "Variance columns use comparable bases. Grounded-to-draft is "
                    "the vessel variance; OPUS-to-grounded is the inbound source gap."
                ),
            )
            _table(
                order_rows,
                _columns(
                    ("order_reference", "Order", "left"),
                    ("opus_bc_receipts_tonnes", "OPUS BC t", "right"),
                    ("pelagic_soh_bc_tonnes", "Pelagic SOH BC t", "right"),
                    ("pelagic_grounded_tonnes", "Grounded BC t", "right"),
                    ("vessel_draft_tonnes", "Vessel draft t", "right"),
                    ("opus_vs_grounded_tonnes", "OPUS - grounded t", "right"),
                    (
                        "soh_to_grounded_tonnes",
                        "Grounded - prior SOH t",
                        "right",
                    ),
                    (
                        "grounded_to_draft_tonnes",
                        "Draft - grounded t",
                        "right",
                    ),
                    (
                        "vessel_shortage_pct",
                        "Vessel shortage %",
                        "right",
                    ),
                    ("draft_recovery_pct", "Draft recovery %", "right"),
                    (
                        "share_of_vessel_variance_pct",
                        "Share of vessel gap %",
                        "right",
                    ),
                    ("dg_tonnes", "DG t", "right"),
                    ("shore_scale_tonnes", "Shore scale t", "right"),
                ),
                "_key",
                rows_per_page=10,
            )

        flow_rows = [
            {
                **row,
                "_key": (
                    f"{row.get('order_reference')}|{row.get('physical_leg')}"
                ),
            }
            for row in result.get("flows") or []
        ]
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "Pelagic physical-leg reconciliation",
                (
                    "Truck transit differences stay separate from the vessel "
                    "grounded-to-draft variance."
                ),
            )
            _table(
                flow_rows,
                _columns(
                    ("order_reference", "Order", "left"),
                    ("physical_leg", "Physical leg", "left"),
                    ("dispatched_tonnes", "Dispatched t", "right"),
                    ("received_tonnes", "Received t", "right"),
                    (
                        "transit_variance_tonnes",
                        "Received - dispatched t",
                        "right",
                    ),
                    ("transit_variance_pct", "Transit variance %", "right"),
                ),
                "_key",
                rows_per_page=10,
            )

        evidence_rows = [
            {**row, "_key": f"{index}|{row.get('finding')}"}
            for index, row in enumerate(result.get("evidence") or [])
        ]
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "Potential reasons, causes, and checks",
                (
                    "These are evidence-led investigation leads, not automatic "
                    "adjustments to OPUS or Pelagic source values."
                ),
            )
            _table(
                evidence_rows,
                _columns(
                    ("priority", "Priority", "left"),
                    ("finding", "Finding", "left"),
                    ("evidence", "Evidence", "left"),
                    ("potential_causes", "Potential reasons / next check", "left"),
                ),
                "_key",
                rows_per_page=10,
            )

    def order_investigations_view() -> None:
        if not state.snapshot.storage.get("analytics_schema_ready", 0):
            _analytics_unavailable(state.snapshot)
            return
        options = investigation_state.get("options") or []
        selected_order = str(investigation_state.get("selected_order") or "")
        option_labels = {
            str(option.get("order_reference")): (
                f"{option.get('order_reference')} "
                f"({int(option.get('allocations') or 0):,} allocations)"
            )
            for option in options
        }
        with ui.card().classes("app-card w-full shadow-none"):
            _section_heading(
                "Select an OPUS order",
                (
                    "Search by order number to rebuild the complete SOH, route, bay, "
                    "movement and exception view from the latest OPUS data."
                ),
            )
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                ui.select(
                    options=option_labels,
                    value=selected_order or None,
                    label="Order number",
                    on_change=lambda event: refresh_order_investigation(
                        str(event.value or "")
                    ),
                ).props(
                    "outlined dense use-input fill-input input-debounce=0 "
                    + ("disable" if investigation_state["loading"] else "")
                ).classes("min-w-[320px] flex-grow")
                ui.button(
                    "Refresh selected order",
                    icon="refresh",
                    on_click=lambda: refresh_order_investigation(),
                ).props(
                    "outline no-caps "
                    + ("loading disable" if investigation_state["loading"] else "")
                )
        if investigation_state["loading"] and not investigation_state["result"]:
            with ui.column().classes(
                "app-card w-full items-center justify-center gap-3"
            ):
                ui.spinner("dots", size="42px", color="primary")
                ui.label("Building the selected order overview...").classes(
                    "section-subtitle"
                )
            return
        error = str(investigation_state.get("error") or "")
        if error:
            with ui.row().classes("status-banner error items-start no-wrap gap-3"):
                ui.icon("error", size="22px", color="red-8")
                ui.label(error).classes("section-subtitle")
            return
        config = investigation_state.get("config")
        result = investigation_state.get("result")
        if config and result:
            render_order_study(config, result)
        elif not options:
            _empty_state(
                "query_stats",
                "No OPUS orders are available",
                "No order numbers were found in the stored Transport Allocations.",
            )

    def stock_view() -> None:
        if not state.snapshot.storage.get("analytics_schema_ready", 0):
            _analytics_unavailable(state.snapshot)
            return
        if render_analytics_state(stock_state):
            return
        result = stock_state["result"] or {}
        options = stock_state["options"] or {}
        metrics = result.get("metrics") or {}
        with ui.card().classes("app-card shadow-none"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                _section_heading(
                    "Stock and movement filters",
                    "Stock position is calculated at the as-of date; movement "
                    "KPIs and variance use the selected movement range.",
                )
                ui.button(
                    "Refresh",
                    icon="refresh",
                    on_click=lambda: refresh_analytics("stock"),
                ).props("outline no-caps")
            with ui.element("div").classes("analytics-filter-grid"):
                for key, label in (
                    ("movement_from", "Movement from"),
                    ("movement_to", "Movement to"),
                    ("as_of", "Stock as of"),
                ):
                    ui.input(
                        label,
                        value=stock_filters_state[key],
                        on_change=lambda event, selected=key: update_analytics_filter(
                            "stock", selected, event.value
                        ),
                    ).props("outlined dense type=date")
                ui.input(
                    "Order number",
                    value=stock_filters_state["order_reference"],
                    on_change=lambda event: update_analytics_filter(
                        "stock", "order_reference", event.value
                    ),
                ).props("outlined dense clearable debounce=600")
                for key, label, option_key in (
                    ("client_name", "Client", "clients"),
                    ("loading_point", "Origin point", "loading_points"),
                    ("loading_storage", "Origin slab / bay", "loading_storage"),
                    ("offloading_point", "Destination point", "offloading_points"),
                    (
                        "offloading_storage",
                        "Destination slab / bay",
                        "offloading_storage",
                    ),
                    ("truck_type", "Truck type", "truck_types"),
                ):
                    ui.select(
                        [""] + list(options.get(option_key) or []),
                        value=stock_filters_state[key],
                        label=label,
                        on_change=lambda event, selected=key: update_analytics_filter(
                            "stock", selected, event.value
                        ),
                    ).props("outlined dense clearable")

        _section_heading(
            "Stock position by role",
            "Origin and destination balances are calculated independently through "
            "the selected as-of date.",
        )
        with ui.element("div").classes("metric-grid w-full"):
            for label, key, detail, icon in (
                ("Origin opening", "origin_opening_tonnes", "Governed origin balances (t)", "account_balance"),
                ("Loaded from origins", "origin_loaded_tonnes", "Through as-of date (t)", "upload"),
                ("Origin stock", "origin_stock_on_hand_tonnes", "Opening - loaded (t)", "inventory"),
                ("Destination opening", "destination_opening_tonnes", "Governed destination balances (t)", "account_balance"),
                ("Offloaded at destinations", "destination_offloaded_tonnes", "Through as-of date (t)", "download"),
                ("Destination stock", "destination_stock_on_hand_tonnes", "Opening + offloaded (t)", "inventory_2"),
                ("Stock in transit", "transit_tonnes", "Strict latest-attempt rule (t)", "local_shipping"),
                ("Missing openings", "incomplete_positions", "Calculated from zero", "playlist_remove"),
                ("Negative positions", "negative_positions", "Visible and flagged", "trending_down"),
            ):
                _analytics_metric_card(
                    label,
                    metrics.get(key, 0),
                    detail,
                    icon,
                )

        _section_heading(
            "Movement reconciliation",
            "Loading, offloading, and variance for the selected movement range.",
        )
        with ui.element("div").classes("metric-grid w-full"):
            for label, key, detail, icon in (
                ("Loaded", "loaded_tonnes", "Signed-off loading (t)", "upload"),
                ("Offloaded", "offloaded_tonnes", "Signed-off offloading (t)", "download"),
                ("Variance", "variance_tonnes", "Completed offloads in range (t)", "compare_arrows"),
                ("Delivery", "delivery_pct", "Completed offloads in range (%)", "percent"),
                ("Exceptions", "exceptions", "Actionable movement issues", "warning"),
            ):
                _analytics_metric_card(
                    label,
                    metrics.get(key, 0),
                    detail,
                    icon,
                )

        _section_heading(
            "Origin analytics",
            "Loading points are summarized first; choose an origin point to drill "
            "into its slab or bay detail.",
        )
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Loaded tonnes by origin point",
                "Signed-off Loading and Exit tonnes in the movement range.",
                grouped_bar_options(
                    (result.get("origin_points") or [])[:20],
                    category_key="point",
                    series=[
                        ("Loaded", "loaded_tonnes", "#e04403"),
                    ],
                    horizontal=True,
                ),
            )
            _chart_card(
                "Origin stock by point",
                "Origin opening balance minus signed-off loading through as-of.",
                grouped_bar_options(
                    (result.get("origin_point_positions") or [])[:20],
                    category_key="point",
                    series=[
                        ("Origin stock", "stock_on_hand_tonnes", "#1c2545"),
                    ],
                    horizontal=True,
                ),
            )
            if stock_filters_state["loading_point"]:
                _chart_card(
                    "Loaded tonnes by origin slab / bay",
                    str(stock_filters_state["loading_point"]),
                    grouped_bar_options(
                        result.get("origin_storage") or [],
                        category_key="storage",
                        series=[
                            ("Loaded", "loaded_tonnes", "#e04403"),
                        ],
                    ),
                )
                _chart_card(
                    "Origin stock by slab / bay",
                    str(stock_filters_state["loading_point"]),
                    grouped_bar_options(
                        result.get("origin_storage_positions") or [],
                        category_key="storage",
                        series=[
                            ("Origin stock", "stock_on_hand_tonnes", "#1c2545"),
                        ],
                    ),
                )

        _section_heading(
            "Destination analytics",
            "Offloading points are summarized first; choose a destination point "
            "to drill into its slab or bay detail.",
        )
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Offloaded tonnes by destination point",
                "Signed-off Offloading and Exit tonnes in the movement range.",
                grouped_bar_options(
                    (result.get("destination_points") or [])[:20],
                    category_key="point",
                    series=[
                        ("Offloaded", "offloaded_tonnes", "#007d6d"),
                    ],
                    horizontal=True,
                ),
            )
            _chart_card(
                "Destination stock by point",
                "Destination opening balance plus signed-off offloading through as-of.",
                grouped_bar_options(
                    (result.get("destination_point_positions") or [])[:20],
                    category_key="point",
                    series=[
                        ("Destination stock", "stock_on_hand_tonnes", "#007d6d"),
                    ],
                    horizontal=True,
                ),
            )
            if stock_filters_state["offloading_point"]:
                _chart_card(
                    "Offloaded tonnes by destination slab / bay",
                    str(stock_filters_state["offloading_point"]),
                    grouped_bar_options(
                        result.get("destination_storage") or [],
                        category_key="storage",
                        series=[
                            ("Offloaded", "offloaded_tonnes", "#007d6d"),
                        ],
                    ),
                )
                _chart_card(
                    "Destination stock by slab / bay",
                    str(stock_filters_state["offloading_point"]),
                    grouped_bar_options(
                        result.get("destination_storage_positions") or [],
                        category_key="storage",
                        series=[
                            ("Destination stock", "stock_on_hand_tonnes", "#007d6d"),
                        ],
                    ),
                )

        _section_heading(
            "Origin to destination lanes",
            "Routes retain separate origin and destination dimensions.",
        )
        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Loaded tonnes by route",
                "Origin on the vertical axis and destination on the horizontal axis.",
                route_heatmap_options(
                    result.get("lanes") or [],
                    value_key="loaded_tonnes",
                ),
            )
            _chart_card(
                "Order delivery reconciliation",
                "Loaded and delivered tonnes for completed loads.",
                grouped_bar_options(
                    result.get("orders") or [],
                    category_key="category",
                    series=[
                        ("Loaded", "loaded_tonnes", "#e04403"),
                        ("Offloaded", "offloaded_tonnes", "#007d6d"),
                    ],
                ),
            )

        with ui.element("div").classes("analytics-chart-grid"):
            _chart_card(
                "Delivery percentage by order",
                "Actual delivered percentage against the governed minimum.",
                grouped_bar_options(
                    result.get("orders") or [],
                    category_key="category",
                    series=[
                        ("Delivery %", "delivery_pct", "#007d6d"),
                        ("Minimum %", "minimum_delivery_pct", "#e04403"),
                    ],
                ),
            )
            _chart_card(
                "Daily movement trend",
                "Signed-off loading and offloading by movement date.",
                movement_trend_options(result.get("daily") or []),
            )
            _chart_card(
                "Movement by truck type",
                "Loaded and offloaded tonnes split by allocation truck type.",
                grouped_bar_options(
                    result.get("truck_types") or [],
                    category_key="category",
                    series=[
                        ("Loaded", "loaded_tonnes", "#e04403"),
                        ("Offloaded", "offloaded_tonnes", "#007d6d"),
                    ],
                ),
            )

        with ui.card().classes("app-card shadow-none"):
            origin_position_rows = [
                {
                    **row,
                    "_key": "|".join(
                        str(row.get(key) or "")
                        for key in (
                            "stock_role",
                            "location_name",
                            "storage_identifier",
                            "order_reference",
                        )
                    ),
                }
                for row in result.get("origin_positions") or []
            ]
            destination_position_rows = [
                {
                    **row,
                    "_key": "|".join(
                        str(row.get(key) or "")
                        for key in (
                            "stock_role",
                            "location_name",
                            "storage_identifier",
                            "order_reference",
                        )
                    ),
                }
                for row in result.get("destination_positions") or []
            ]
            origin_movement_rows = [
                {
                    **row,
                    "_key": f"{row.get('point') or 'Unknown'}|"
                    f"{row.get('storage') or 'Missing'}",
                }
                for row in result.get("origin_storage") or []
            ]
            destination_movement_rows = [
                {
                    **row,
                    "_key": f"{row.get('point') or 'Unknown'}|"
                    f"{row.get('storage') or 'Missing'}",
                }
                for row in result.get("destination_storage") or []
            ]
            lane_rows = [
                {
                    **row,
                    "_key": f"{row.get('origin') or 'Unknown'}|"
                    f"{row.get('destination') or 'Unknown'}",
                }
                for row in result.get("lanes") or []
            ]
            tabs = ui.tabs().classes("w-full").props(
                "dense align=left inline-label outside-arrows mobile-arrows"
            )
            origin_stock_tab = ui.tab("Origin stock", icon="upload")
            destination_stock_tab = ui.tab("Destination stock", icon="download")
            origin_movement_tab = ui.tab("Origin slab / bay", icon="warehouse")
            destination_movement_tab = ui.tab(
                "Destination slab / bay",
                icon="inventory_2",
            )
            lanes_tab = ui.tab("Routes", icon="route")
            jobs_tab = ui.tab("Job reconciliation", icon="compare_arrows")
            orders_tab = ui.tab("Order summary", icon="receipt_long")
            exceptions_tab = ui.tab("Exceptions", icon="warning")
            with ui.tab_panels(tabs, value=origin_stock_tab).classes("w-full"):
                with ui.tab_panel(origin_stock_tab):
                    _table(
                        origin_position_rows,
                        _columns(
                            ("location_name", "Origin point", "left"),
                            ("storage_display", "Origin slab / bay", "left"),
                            ("order_reference", "Order", "left"),
                            ("client_name", "Client", "left"),
                            ("effective_date", "Opening date", "left"),
                            ("opening_tonnes", "Opening t", "right"),
                            ("movement_tonnes", "Loaded t", "right"),
                            ("stock_on_hand_tonnes", "Origin stock t", "right"),
                            ("missing_opening_balance", "Missing opening", "left"),
                            ("negative_stock", "Negative", "left"),
                        ),
                        "_key",
                        rows_per_page=25,
                    )
                with ui.tab_panel(destination_stock_tab):
                    _table(
                        destination_position_rows,
                        _columns(
                            ("location_name", "Destination point", "left"),
                            ("storage_display", "Destination slab / bay", "left"),
                            ("order_reference", "Order", "left"),
                            ("client_name", "Client", "left"),
                            ("effective_date", "Opening date", "left"),
                            ("opening_tonnes", "Opening t", "right"),
                            ("movement_tonnes", "Offloaded t", "right"),
                            ("stock_on_hand_tonnes", "Destination stock t", "right"),
                            ("missing_opening_balance", "Missing opening", "left"),
                            ("negative_stock", "Negative", "left"),
                        ),
                        "_key",
                        rows_per_page=25,
                    )
                with ui.tab_panel(origin_movement_tab):
                    _table(
                        origin_movement_rows,
                        _columns(
                            ("point", "Origin point", "left"),
                            ("storage", "Origin slab / bay", "left"),
                            ("storage_scope", "Storage scope", "left"),
                            ("job_count", "Jobs", "right"),
                            ("order_count", "Orders", "right"),
                            ("loaded_tonnes", "Loaded t", "right"),
                        ),
                        "_key",
                        rows_per_page=25,
                    )
                with ui.tab_panel(destination_movement_tab):
                    _table(
                        destination_movement_rows,
                        _columns(
                            ("point", "Destination point", "left"),
                            ("storage", "Destination slab / bay", "left"),
                            ("storage_scope", "Storage scope", "left"),
                            ("job_count", "Jobs", "right"),
                            ("order_count", "Orders", "right"),
                            ("offloaded_tonnes", "Offloaded t", "right"),
                        ),
                        "_key",
                        rows_per_page=25,
                    )
                with ui.tab_panel(lanes_tab):
                    _table(
                        lane_rows,
                        _columns(
                            ("origin", "Origin", "left"),
                            ("destination", "Destination", "left"),
                            ("trucks", "Trucks", "right"),
                            ("reference_count", "References", "right"),
                            ("loaded_tonnes", "Loaded t", "right"),
                            ("offloaded_tonnes", "Offloaded t", "right"),
                            ("variance_tonnes", "Completed variance t", "right"),
                        ),
                        "_key",
                        rows_per_page=25,
                    )
                with ui.tab_panel(jobs_tab):
                    _table(
                        result.get("reconciliation") or [],
                        _columns(
                            ("job_reference", "Job reference", "left"),
                            ("order_reference", "Order", "left"),
                            ("client_name", "Client", "left"),
                            ("truck_registration", "Truck", "left"),
                            ("loading_point", "Origin point", "left"),
                            ("loading_storage_display", "Origin slab / bay", "left"),
                            ("offloading_point", "Destination point", "left"),
                            ("offloading_storage_display", "Destination slab / bay", "left"),
                            ("loaded_tonnes", "Loaded t", "right"),
                            ("offloaded_tonnes", "Offloaded t", "right"),
                            ("variance_tonnes", "Variance t", "right"),
                            ("delivery_pct", "Delivery %", "right"),
                            ("variance_status", "Status", "left"),
                        ),
                        "job_reference",
                        rows_per_page=25,
                    )
                with ui.tab_panel(orders_tab):
                    _table(
                        result.get("orders") or [],
                        _columns(
                            ("category", "Order", "left"),
                            ("client_name", "Client", "left"),
                            ("loaded_tonnes", "Loaded t", "right"),
                            ("offloaded_tonnes", "Offloaded t", "right"),
                            ("delivery_pct", "Delivery %", "right"),
                            ("minimum_delivery_pct", "Minimum %", "right"),
                        ),
                        "category",
                        rows_per_page=25,
                    )
                with ui.tab_panel(exceptions_tab):
                    exception_rows = [
                        row
                        for row in result.get("reconciliation") or []
                        if _is_stock_exception(row)
                    ]
                    _table(
                        exception_rows,
                        _columns(
                            ("job_reference", "Job reference", "left"),
                            ("order_reference", "Order", "left"),
                            ("duplicate_loading_attempts", "Duplicate loading", "left"),
                            ("duplicate_offloading_attempts", "Duplicate offloading", "left"),
                            ("invalid_loading_slab", "Loading slab issue", "left"),
                            ("invalid_offloading_slab", "Offloading slab issue", "left"),
                            ("unmapped_client", "Unmapped client", "left"),
                            ("loading_validation_errors", "Loading validation", "left"),
                            ("offloading_validation_errors", "Offloading validation", "left"),
                        ),
                        "job_reference",
                        rows_per_page=25,
                    )

        with ui.card().classes("app-card shadow-none"):
            _section_heading(
                "Order Master and opening balances",
                "Download the controlled workbook, validate an upload, then "
                "explicitly apply it. Every opening row requires Origin or "
                "Destination Stock Role; existing matching keys are replaced.",
            )
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.button(
                    "Download template",
                    icon="download",
                    on_click=download_control_template,
                ).props("outline no-caps")
                ui.upload(
                    label="Upload completed workbook",
                    on_upload=handle_control_upload,
                    auto_upload=True,
                ).props("accept=.xlsx max-files=1").classes("max-w-md")
            preview = stock_state.get("preview")
            if isinstance(preview, WorkbookPreview):
                with ui.row().classes(
                    "status-banner w-full items-start justify-between gap-3"
                    if preview.valid
                    else "status-banner error w-full items-start justify-between gap-3"
                ):
                    with ui.column().classes("gap-1"):
                        ui.label(preview.filename).classes("font-bold")
                        ui.label(
                            f"{len(preview.orders):,} orders · "
                            f"{len(preview.opening_balances):,} opening balances · "
                            f"{len(preview.errors):,} errors"
                        ).classes("section-subtitle mono")
                        for error in preview.errors[:20]:
                            ui.label(error).classes("text-xs text-red-8")
                        if len(preview.errors) > 20:
                            ui.label(
                                f"{len(preview.errors) - 20:,} more errors"
                            ).classes("text-xs text-red-8")
                    if preview.valid:
                        ui.button(
                            "Apply validated workbook",
                            icon="check_circle",
                            on_click=apply_control_preview,
                        ).props("unelevated no-caps").classes("primary-action")
            history = stock_state.get("history") or []
            if history:
                _section_heading("Workbook import history")
                _table(
                    history,
                    _columns(
                        ("import_id", "Import", "right"),
                        ("original_filename", "File", "left"),
                        ("status", "Status", "left"),
                        ("order_rows", "Orders", "right"),
                        ("opening_balance_rows", "Openings", "right"),
                        ("replaced_order_rows", "Orders replaced", "right"),
                        ("replaced_balance_rows", "Openings replaced", "right"),
                        ("imported_by", "Applied by", "left"),
                        ("created_at", "Validated", "left"),
                        ("applied_at", "Applied", "left"),
                    ),
                    "import_id",
                    rows_per_page=10,
                )

    @ui.refreshable
    def sidebar() -> None:
        snapshot = state.snapshot
        with ui.column().classes("sidebar-stack w-full"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                if not state.collapsed:
                    ui.label("OPUS CONTROL").classes(
                        "sidebar-brand text-xs font-bold uppercase"
                    )
                ui.button(
                    icon=(
                        "keyboard_double_arrow_right"
                        if state.collapsed
                        else "keyboard_double_arrow_left"
                    ),
                    on_click=toggle_sidebar,
                ).props("flat round dense").classes("sidebar-collapse")
            with ui.element("div").classes("logo-plate w-full"):
                ui.image(
                    "/brand/Connect-Logistics-Logo.png"
                ).classes("brand-logo full-brand-logo")
                ui.image(
                    "/brand/Connect-Logistics-Icon.png"
                ).classes("brand-logo compact-brand-logo")
            with ui.column().classes("w-full gap-1"):
                for key, label, icon in NAV_ITEMS:
                    with ui.button(
                        on_click=lambda selected=key: set_view(selected)
                    ).props("flat no-caps").classes(
                        "nav-btn active" if state.view == key else "nav-btn"
                    ) as button:
                        ui.icon(icon)
                        ui.label(label).classes("sidebar-label")
                    if state.collapsed:
                        button.tooltip(label)
            with ui.column().classes(
                "sidebar-meta sidebar-panel w-full p-3 gap-1"
            ):
                ui.label("POSTGRESQL DATA").classes(
                    "sidebar-muted text-xs font-bold"
                )
                ui.label(settings.db_name).classes("text-xs mono")
                ui.label(
                    f"{snapshot.metrics.get('checklist_instances', 0):,} checklists · "
                    f"{snapshot.metrics.get('checklist_answers', 0):,} answers"
                ).classes("sidebar-muted text-xs")
                ui.label(
                    "Connected" if snapshot.connected else "Connection unavailable"
                ).classes(
                    "sidebar-online text-xs font-bold"
                    if snapshot.connected
                    else "sidebar-offline text-xs font-bold"
                )
            with ui.row().classes(
                "sidebar-bottom sidebar-panel w-full items-center gap-2 p-3"
            ):
                ui.icon("shield", size="19px", color="teal-3")
                with ui.column().classes("sidebar-label gap-0"):
                    ui.label("Internal operations").classes("text-xs font-bold")
                    ui.label("Connect Logistics").classes(
                        "sidebar-muted text-xs"
                    )

    @ui.refreshable
    def toolbar() -> None:
        title, subtitle = VIEW_TITLES[state.view]
        with ui.row().classes(
            "toolbar w-full items-center justify-between flex-wrap gap-3"
        ):
            with ui.column().classes("gap-0"):
                ui.label(title).classes("toolbar-title")
                ui.label(subtitle).classes("toolbar-subtitle")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.badge(
                    _extraction_window_label(),
                    color="blue-grey-7",
                ).props("outline")
                ui.button(
                    (
                        "Reloading data..."
                        if state.refreshing
                        else "Reload dashboard"
                    ),
                    icon="refresh",
                    on_click=refresh_data,
                ).props(
                    f"flat no-caps {'loading disable' if state.refreshing else ''}"
                ).classes("primary-action")

    @ui.refreshable
    def content() -> None:
        with ui.column().classes("w-full gap-4 fade-in"):
            if state.view == "extraction":
                connection_view()
                _extraction_view(state.snapshot)
                _checklist_instance_tables(
                    state.snapshot,
                    open_checklist_detail,
                )
                _job_workflow_view(
                    state.snapshot,
                    state.selected_job_reference,
                    select_job_reference,
                    open_checklist_detail,
                )
            elif state.view == "data":
                data_view()
            elif state.view == "ops":
                ops_view()
            elif state.view == "stock":
                stock_view()
            elif state.view == "investigations":
                order_investigations_view()

    if state.snapshot.storage.get("analytics_schema_ready", 0):
        await refresh_analytics("data", refresh_ui=False)

    ui.timer(
        1.0,
        lambda: (
            sync_status.refresh(),
            sync_controls.refresh(),
        )
        if state.view == "extraction" and opus_sync.snapshot().running
        else None,
    )
    refreshed_sync = {"finished_at": opus_sync.snapshot().finished_at}

    async def refresh_after_live_sync() -> None:
        progress = opus_sync.snapshot()
        if (
            progress.running
            or progress.phase != "Completed"
            or not progress.finished_at
            or progress.finished_at == refreshed_sync["finished_at"]
        ):
            return
        refreshed_sync["finished_at"] = progress.finished_at
        await refresh_data(notify=False)

    ui.timer(5.0, refresh_after_live_sync)

    with ui.row().classes("app-shell w-full"):
        sidebar_panel = ui.column().classes("sidebar")
        with sidebar_panel:
            sidebar()
        with ui.column().classes("app-main gap-0"):
            toolbar()
            with ui.column().classes("content-wrap"):
                content()
                ui.label(
                    "Internal operational analytics · OPUS remains the source of truth."
                ).classes("footer-note w-full mt-2")


async def _automatic_sync_loop() -> None:
    await asyncio.sleep(5)
    interval_seconds = max(settings.opus_sync_minutes, 1) * 60
    while True:
        started_at = asyncio.get_running_loop().time()
        try:
            if (
                opus_credentials.read() is not None
                and not opus_sync.snapshot().running
                and await asyncio.to_thread(repository.detail_schema_ready)
            ):
                await opus_sync.run(full=False)
        except Exception as exc:
            # Engine failures are already recorded by SyncCoordinator.run().
            # Capture pre-run guard errors (credential read, DB check) here so
            # they're visible in the extraction status panel rather than silent.
            opus_sync.record_loop_error(exc)
        elapsed = asyncio.get_running_loop().time() - started_at
        await asyncio.sleep(max(interval_seconds - elapsed, 1))


def _start_automatic_sync() -> None:
    if settings.opus_auto_sync:
        background_tasks.create(_automatic_sync_loop())


app.on_startup(_start_automatic_sync)


def run() -> None:
    try:
        repository.ensure_partitions()
    except Exception:
        # The page provides a user-visible connection state; startup remains available
        # so database credentials or service status can be corrected without a crash.
        pass
    favicon = BRAND_DIR / "Connect-Logistics-Icon.png"
    ui.run(
        host=settings.app_host,
        port=settings.app_port,
        title=settings.app_title,
        favicon=str(favicon),
        show=os.getenv("OPUS_APP_SHOW", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        reload=False,
        dark=False,
        language="en-US",
        prod_js=True,
        storage_secret=_storage_secret() if settings.app_access_password else None,
    )
