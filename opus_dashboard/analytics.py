from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


STATUS_GROUPS = (
    "Signed off",
    "Not started",
    "In progress",
    "Under review",
    "Closed",
    "Cancelled",
    "Other",
)


def normalize_status(value: str | None) -> str:
    status = (value or "").strip().casefold()
    if "signed off" in status or "signed-off" in status:
        return "Signed off"
    if "cancelled" in status or "canceled" in status:
        return "Cancelled"
    if "job closed" in status or "closed" in status:
        return "Closed"
    if "under review" in status or "review" in status:
        return "Under review"
    if "in progress" in status:
        return "In progress"
    if "not started" in status or "pending start" in status:
        return "Not started"
    return "Other"


def parse_nett_weight_tonnes(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    if abs(parsed) >= 1000:
        parsed /= Decimal("1000")
    if parsed <= 0 or parsed > Decimal("100"):
        return None
    return parsed.quantize(Decimal("0.001"))


def storage_identifier(
    slab: str | None,
    point: str | None,
    *,
    loading: bool,
) -> tuple[str | None, str | None]:
    slab_value = (slab or "").strip()
    point_value = (point or "").strip()
    sentinel = "no loading slab" if loading else "no offloading slab"
    if slab_value.casefold() == sentinel:
        if point_value:
            return point_value, None
        return None, "missing_point_for_slab_fallback"
    if not slab_value or slab_value == "0":
        return None, "missing_or_invalid_slab"
    return slab_value, None


def delivery_metrics(
    loaded_tonnes: Decimal | None,
    offloaded_tonnes: Decimal | None,
    minimum_delivery_pct: Decimal = Decimal("99.750"),
) -> tuple[Decimal | None, Decimal | None, str]:
    if loaded_tonnes is None:
        return None, None, "Missing loading weight"
    if offloaded_tonnes is None:
        return None, None, "Pending / in transit"
    variance = (offloaded_tonnes - loaded_tonnes).quantize(Decimal("0.001"))
    if loaded_tonnes <= 0:
        return variance, None, "Invalid loading weight"
    percentage = (
        (offloaded_tonnes / loaded_tonnes) * Decimal("100")
    ).quantize(Decimal("0.001"))
    status = (
        "Within tolerance"
        if percentage >= minimum_delivery_pct
        else "Below tolerance"
    )
    return variance, percentage, status


@dataclass(frozen=True, slots=True)
class DataFilters:
    date_from: date | None = None
    date_to: date | None = None
    job_reference: str = ""
    checklist_name: str = ""
    status_group: str = ""


@dataclass(frozen=True, slots=True)
class OpsFilters:
    date_from: date | None = None
    date_to: date | None = None
    origin: str = ""
    destination: str = ""
    truck_type: str = ""


@dataclass(frozen=True, slots=True)
class StockFilters:
    movement_from: date | None = None
    movement_to: date | None = None
    as_of: date | None = None
    order_reference: str = ""
    client_name: str = ""
    loading_point: str = ""
    loading_storage: str = ""
    offloading_point: str = ""
    offloading_storage: str = ""
    truck_type: str = ""


@dataclass(frozen=True, slots=True)
class WorkflowAttempt:
    stage_code: str
    status: str
    chronology_at: datetime
    operator_started_at: datetime | None = None
    source_completed_at: datetime | None = None
    source_signed_off_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TransitDecision:
    in_transit: bool
    stopped_after_closure: bool
    root_attempt: WorkflowAttempt | None
    loading_attempt: WorkflowAttempt | None
    offloading_attempt: WorkflowAttempt | None
    exclusion_reason: str | None


def resolve_transit(attempts: list[WorkflowAttempt]) -> TransitDecision:
    ordered = sorted(attempts, key=lambda attempt: attempt.chronology_at)
    current = ordered[-1] if ordered else None
    stopped = bool(
        current and normalize_status(current.status) in {"Closed", "Cancelled"}
    )
    latest_by_stage: dict[str, WorkflowAttempt] = {}
    for attempt in ordered:
        latest_by_stage[attempt.stage_code] = attempt
    root = latest_by_stage.get("transport_allocation")
    loading = latest_by_stage.get("loading_exit")
    offloading = latest_by_stage.get("offloading_exit")
    root_ready = bool(root and normalize_status(root.status) == "Signed off")
    loading_ready = bool(
        loading and normalize_status(loading.status) == "Signed off"
    )
    offloading_not_started = bool(
        offloading is None
        or (
            normalize_status(offloading.status) == "Not started"
            and offloading.operator_started_at is None
            and offloading.source_completed_at is None
            and offloading.source_signed_off_at is None
        )
    )
    if root is None:
        exclusion_reason = "Missing Transport Allocation attempt"
    elif not root_ready:
        exclusion_reason = "Latest Transport Allocation is not signed off"
    elif loading is None:
        exclusion_reason = "Missing Loading and Exit attempt"
    elif not loading_ready:
        exclusion_reason = "Latest Loading and Exit is not signed off"
    elif stopped:
        exclusion_reason = "Final workflow job is closed or cancelled"
    elif not offloading_not_started:
        exclusion_reason = "Latest Offloading and Exit has started"
    else:
        exclusion_reason = None
    in_transit = bool(
        root_ready
        and loading_ready
        and not stopped
        and offloading_not_started
    )
    return TransitDecision(
        in_transit=in_transit,
        stopped_after_closure=stopped,
        root_attempt=root,
        loading_attempt=loading,
        offloading_attempt=offloading,
        exclusion_reason=exclusion_reason,
    )
