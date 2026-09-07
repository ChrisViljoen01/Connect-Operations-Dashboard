from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from opus_dashboard.config import Settings
from opus_dashboard.credentials import read_windows_credential
from opus_dashboard.analytics import DataFilters, OpsFilters, StockFilters
from opus_dashboard.models import DashboardSnapshot
from opus_dashboard.pdf_service import create_order_study_pdf
from opus_dashboard.xlsx_service import WorkbookPreview, XlsxStreamWriter


PERIOD_LABELS = {
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "ytd": "Year to date",
    "all": "All available data",
}
BUSINESS_TIMEZONE = ZoneInfo("Africa/Johannesburg")
RoutePlan = tuple[tuple[str, str, str, str, str], ...]
STUDY_LEGS = ("Mine to BCF", "BCF to BC", "Mine to BC Direct")
ROUTE_PLANNED_OBSERVED = "Planned route observed"
ROUTE_PLANNED_EMPTY = "Planned route - no OPUS movement"
ROUTE_UNPLANNED = "Observed in OPUS but not in route plan - review"


def _period_start(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return None


def _clean_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone().strftime("%d %b %Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    if isinstance(value, Decimal):
        return float(value)
    return value


def _clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _clean_value(value) for key, value in row.items()} for row in rows]


def _position_summaries(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    point_totals: dict[str, dict[str, Any]] = {}
    storage_totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        point = str(row.get("location_name") or "Unknown")
        storage = str(row.get("storage_display") or "Missing slab/bay")
        opening = Decimal(str(row.get("opening_tonnes") or 0))
        movement = Decimal(str(row.get("movement_tonnes") or 0))
        stock = Decimal(str(row.get("stock_on_hand_tonnes") or 0))
        point_row = point_totals.setdefault(
            point,
            {
                "point": point,
                "opening_tonnes": Decimal("0"),
                "movement_tonnes": Decimal("0"),
                "stock_on_hand_tonnes": Decimal("0"),
                "_storage": set(),
                "_orders": set(),
            },
        )
        point_row["opening_tonnes"] += opening
        point_row["movement_tonnes"] += movement
        point_row["stock_on_hand_tonnes"] += stock
        point_row["_storage"].add(storage)
        point_row["_orders"].add(str(row.get("order_reference") or "Unmapped"))

        storage_row = storage_totals.setdefault(
            (point, storage),
            {
                "point": point,
                "storage": storage,
                "opening_tonnes": Decimal("0"),
                "movement_tonnes": Decimal("0"),
                "stock_on_hand_tonnes": Decimal("0"),
                "_orders": set(),
            },
        )
        storage_row["opening_tonnes"] += opening
        storage_row["movement_tonnes"] += movement
        storage_row["stock_on_hand_tonnes"] += stock
        storage_row["_orders"].add(str(row.get("order_reference") or "Unmapped"))

    points: list[dict[str, Any]] = []
    for row in point_totals.values():
        points.append(
            {
                **{key: value for key, value in row.items() if not key.startswith("_")},
                "storage_count": len(row["_storage"]),
                "order_count": len(row["_orders"]),
            }
        )
    points.sort(
        key=lambda row: (
            -abs(Decimal(str(row["stock_on_hand_tonnes"]))),
            str(row["point"]),
        )
    )

    storage_rows: list[dict[str, Any]] = []
    for row in storage_totals.values():
        storage_rows.append(
            {
                **{key: value for key, value in row.items() if not key.startswith("_")},
                "order_count": len(row["_orders"]),
            }
        )
    storage_rows.sort(key=lambda row: (str(row["point"]), str(row["storage"])))
    return points, storage_rows


def _json_payload(row: dict[str, Any]) -> str:
    return json.dumps(row, default=str, separators=(",", ":"))


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _study_weight_at(
    row: dict[str, Any],
    weight_key: str,
    signed_off_key: str,
    event_cutoff: datetime | None,
) -> Decimal | None:
    value = row.get(weight_key)
    if value is None:
        return None
    if event_cutoff is None:
        return _decimal(value)
    signed_off_at = row.get(signed_off_key)
    if not isinstance(signed_off_at, datetime):
        return None
    if signed_off_at.astimezone(timezone.utc) > event_cutoff.astimezone(timezone.utc):
        return None
    return _decimal(value)


def _study_bay(point: Any, slab: Any) -> tuple[str, bool]:
    point_text = str(point or "").strip()
    slab_text = str(slab or "").strip()
    if (
        not slab_text
        or slab_text == "0"
        or slab_text.casefold()
        in {
            "no loading slab",
            "no offloading slab",
            "point-level / no slab",
            "point-level / no bay",
        }
        or (
            point_text
            and slab_text.casefold() == point_text.casefold()
        )
    ):
        site = _study_site_name(point_text)
        return f"{site} point-level / no bay", True
    bay_match = re.fullmatch(r"bay\s*(.+)", slab_text, flags=re.IGNORECASE)
    if "bulk connection" in point_text.casefold() and bay_match:
        return f"Invalid / unresolved (OPUS: {slab_text})", False
    if "base chrome fields" in point_text.casefold() and bay_match:
        return f"BCF Bay {bay_match.group(1).strip()}", False
    return slab_text, False


def _flow_route_name(origin: Any, destination: Any) -> str:
    origin_text = str(origin or "").casefold()
    destination_text = str(destination or "").casefold()
    origin_kind = (
        "Mine"
        if "kookfontein" in origin_text
        else ("BCF" if "base chrome fields" in origin_text else "Other")
    )
    destination_kind = (
        "BCF"
        if "base chrome fields" in destination_text
        else ("BC" if "bulk connection" in destination_text else "Other")
    )
    return {
        ("Mine", "BCF"): "Mine to BCF",
        ("BCF", "BC"): "BCF to BC",
        ("Mine", "BC"): "Mine to BC Direct",
    }.get((origin_kind, destination_kind), "Other / review")


def _study_site_name(value: Any) -> str:
    text = str(value or "").strip()
    normalized = text.casefold()
    if "kookfontein" in normalized:
        return "Kookfontein"
    if "base chrome fields" in normalized:
        return "BCF"
    if "bulk connection" in normalized:
        return "BC"
    text = re.sub(r"^(?:LP|OP)-", "", text, flags=re.IGNORECASE).strip()
    parts = [part.strip() for part in text.split(" - ") if part.strip()]
    midpoint = len(parts) // 2
    if len(parts) >= 2 and len(parts) % 2 == 0:
        first_half = [part.casefold() for part in parts[:midpoint]]
        second_half = [part.casefold() for part in parts[midpoint:]]
        if first_half == second_half:
            parts = parts[:midpoint]
    return " - ".join(parts) or "Unknown"


def _order_location_balances(
    movements: list[dict[str, Any]],
    opening_balances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    balances: dict[tuple[str, str], dict[str, Any]] = {}

    def balance_row(location: str, bay: str) -> dict[str, Any]:
        key = (location.casefold(), bay.casefold())
        return balances.setdefault(
            key,
            {
                "location": location,
                "bay": bay,
                "opening_tonnes": Decimal("0"),
                "received_tonnes": Decimal("0"),
                "dispatched_tonnes": Decimal("0"),
                "opening_status": "No governed opening balance",
            },
        )

    for opening in opening_balances:
        source_location = opening.get("location_name")
        location = _study_site_name(source_location)
        bay, _fallback = _study_bay(
            source_location,
            opening.get("storage_identifier"),
        )
        row = balance_row(location, bay)
        row["opening_tonnes"] += _decimal(opening.get("opening_tonnes"))
        row["opening_status"] = "Governed opening balance"

    for movement in movements:
        loaded = movement.get("loaded_tonnes")
        if loaded is not None:
            balance_row(
                str(movement.get("origin_display") or "Unknown"),
                str(movement.get("origin_bay") or "Unresolved OPUS location"),
            )["dispatched_tonnes"] += _decimal(loaded)
        offloaded = movement.get("offloaded_tonnes")
        if offloaded is not None:
            balance_row(
                str(movement.get("destination_display") or "Unknown"),
                str(movement.get("destination_bay") or "Unresolved OPUS location"),
            )["received_tonnes"] += _decimal(offloaded)

    site_flows: dict[str, dict[str, bool]] = {}
    for row in balances.values():
        site = str(row["location"]).casefold()
        flow = site_flows.setdefault(
            site,
            {"received": False, "dispatched": False},
        )
        flow["received"] = flow["received"] or row["received_tonnes"] != 0
        flow["dispatched"] = flow["dispatched"] or row["dispatched_tonnes"] != 0

    for row in balances.values():
        flow = site_flows[str(row["location"]).casefold()]
        if flow["received"] and flow["dispatched"]:
            role = "Intermediate"
        elif flow["received"]:
            role = "Destination"
        elif flow["dispatched"]:
            role = "Origin"
        else:
            role = "Opening only"
        if role == "Origin" and row["opening_tonnes"] != 0:
            role = "Governed source"
        row["location_role"] = role
        row["movement_balance_tonnes"] = (
            row["opening_tonnes"]
            + row["received_tonnes"]
            - row["dispatched_tonnes"]
        )
        unresolved = (
            role == "Intermediate"
            and row["movement_balance_tonnes"] < 0
            and row["opening_status"] == "No governed opening balance"
        )
        row["balance_status"] = (
            "Opening balance or missing receipts required"
            if unresolved
            else (
                "Source dispatch - excluded from SOH"
                if role == "Origin"
                else "Included in known SOH"
            )
        )
        row["included_in_soh"] = role != "Origin" and not unresolved
        row["soh_tonnes"] = (
            row["movement_balance_tonnes"] if row["included_in_soh"] else None
        )

    return sorted(
        balances.values(),
        key=lambda row: (
            {
                "Intermediate": 0,
                "Destination": 1,
                "Opening only": 2,
                "Governed source": 3,
                "Origin": 4,
            }.get(
                str(row["location_role"]),
                4,
            ),
            str(row["location"]),
            str(row["bay"]),
        ),
    )


def _route_bay_reconciliation(
    movements: list[dict[str, Any]],
    route_plan: RoutePlan,
) -> tuple[list[dict[str, Any]], int]:
    if not route_plan:
        return [], 0

    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, (route, origin, destination, from_bay, to_bay) in enumerate(
        route_plan
    ):
        key = (route.casefold(), from_bay.casefold(), to_bay.casefold())
        if key in rows:
            raise ValueError(f"Duplicate planned route/bay combination: {route}.")
        rows[key] = {
            "plan_status": ROUTE_PLANNED_EMPTY,
            "route_name": route,
            "origin": origin,
            "from_bay": from_bay,
            "destination": destination,
            "to_bay": to_bay,
            "movement_references": 0,
            "loaded_movements": 0,
            "offloaded_movements": 0,
            "loaded_tonnes": Decimal("0"),
            "offloaded_tonnes": Decimal("0"),
            "_sort_order": index,
        }

    for movement in movements:
        route = str(movement.get("route_name") or "Other / review")
        from_bay = (
            "From Mine"
            if route.startswith("Mine to")
            else str(movement.get("origin_bay") or "Unknown")
        )
        to_bay = str(movement.get("destination_bay") or "Unknown")
        key = (route.casefold(), from_bay.casefold(), to_bay.casefold())
        row = rows.get(key)
        if row is None:
            row = {
                "plan_status": ROUTE_UNPLANNED,
                "route_name": route,
                "origin": _study_site_name(movement.get("origin")),
                "from_bay": from_bay,
                "destination": _study_site_name(movement.get("destination")),
                "to_bay": to_bay,
                "movement_references": 0,
                "loaded_movements": 0,
                "offloaded_movements": 0,
                "loaded_tonnes": Decimal("0"),
                "offloaded_tonnes": Decimal("0"),
                "_sort_order": len(route_plan),
            }
            rows[key] = row
        elif row["plan_status"] != ROUTE_UNPLANNED:
            row["plan_status"] = ROUTE_PLANNED_OBSERVED

        row["movement_references"] += 1
        if movement.get("loaded_tonnes") is not None:
            row["loaded_movements"] += 1
            row["loaded_tonnes"] += _decimal(movement["loaded_tonnes"])
        if movement.get("offloaded_tonnes") is not None:
            row["offloaded_movements"] += 1
            row["offloaded_tonnes"] += _decimal(movement["offloaded_tonnes"])

    result = sorted(
        rows.values(),
        key=lambda row: (
            row["_sort_order"],
            row["plan_status"] == ROUTE_UNPLANNED,
            row["route_name"],
            row["from_bay"],
            row["to_bay"],
        ),
    )
    unlisted_movements = sum(
        row["movement_references"]
        for row in result
        if row["plan_status"] == ROUTE_UNPLANNED
    )
    for row in result:
        row.pop("_sort_order", None)
    return result, unlisted_movements


def _order_study(
    rows: list[dict[str, Any]],
    *,
    order_reference: str,
    date_from: date,
    date_to: date,
    expected_bays: tuple[str, ...],
    opening_balances: list[dict[str, Any]] | None = None,
    route_plan: RoutePlan = (),
    parcel_tonnes: Decimal | int | float = 0,
    event_cutoff: datetime | None = None,
) -> dict[str, Any]:
    opening_balances = opening_balances or []
    expected = {value.strip().casefold() for value in expected_bays}
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    bay_flows: dict[tuple[str, str, str], dict[str, Any]] = {}
    daily: dict[date, dict[str, Any]] = {}
    movements: list[dict[str, Any]] = []
    allocation_gaps: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    loading_checklists: list[dict[str, Any]] = []
    offloading_checklists: list[dict[str, Any]] = []
    loaded_total = Decimal("0")
    offloaded_total = Decimal("0")
    completed_loaded_total = Decimal("0")
    completed_offloaded_total = Decimal("0")
    loaded_movements = 0
    offloaded_movements = 0
    transit_movements = 0
    transit_tonnes = Decimal("0")
    unexpected_bay_movements = 0
    weight_variance_exceptions = 0
    root_dates = [
        value.astimezone(BUSINESS_TIMEZONE).date()
        for row in rows
        if isinstance(
            value := row.get("transport_allocation_created_at"),
            datetime,
        )
    ]

    def record_bay(
        direction: str,
        point: str,
        bay: str,
        *,
        fallback: bool,
        tonnes: Decimal,
    ) -> None:
        key = (direction, point, bay)
        flow = bay_flows.setdefault(
            key,
            {
                "direction": direction,
                "point": point,
                "bay": bay,
                "storage_source": "Location fallback" if fallback else "Checklist bay",
                "expected_status": (
                    "Location fallback"
                    if fallback
                    else (
                        "Recorded in OPUS"
                        if not expected
                        else (
                            "Expected"
                            if bay.casefold() in expected
                            else "Unexpected"
                        )
                    )
                ),
                "movement_count": 0,
                "tonnes": Decimal("0"),
            },
        )
        flow["movement_count"] += 1
        flow["tonnes"] += tonnes

    for source in rows:
        row = dict(source)
        loaded = _study_weight_at(
            row,
            "loaded_tonnes",
            "loading_signed_off_at",
            event_cutoff,
        )
        offloaded = _study_weight_at(
            row,
            "offloaded_tonnes",
            "offloading_signed_off_at",
            event_cutoff,
        )
        origin = str(
            row.get("loading_point")
            or row.get("allocation_loading_point")
            or "Unknown origin"
        )
        destination = str(
            row.get("offloading_point")
            or row.get("transit_destination")
            or row.get("allocation_offloading_point")
            or "Unknown destination"
        )
        route_name = _flow_route_name(origin, destination)
        origin_bay, origin_fallback = _study_bay(
            origin,
            row.get("loading_slab"),
        )
        destination_bay, destination_fallback = _study_bay(
            destination,
            row.get("offloading_slab") or row.get("planned_offloading_slab"),
        )
        reasons: list[str] = []
        unexpected_bay = False
        if (
            expected
            and loaded is not None
            and not origin_fallback
            and origin_bay.casefold() not in expected
        ):
            reasons.append(f"Unexpected loading bay: {origin_bay}")
            unexpected_bay = True
        if (
            (loaded is not None or offloaded is not None)
            and expected
            and not destination_fallback
            and destination_bay.casefold() not in expected
        ):
            bay_stage = "offloading" if offloaded is not None else "planned offloading"
            reasons.append(f"Unexpected {bay_stage} bay: {destination_bay}")
            unexpected_bay = True
        if unexpected_bay:
            unexpected_bay_movements += 1
        if row.get("duplicate_loading_attempts"):
            reasons.append("Multiple signed-off loading attempts")
        if row.get("duplicate_offloading_attempts"):
            reasons.append("Multiple signed-off offloading attempts")
        if row.get("loading_validation_errors") not in (None, [], {}):
            reasons.append("Loading source validation error")
        if offloaded is not None and row.get("offloading_validation_errors") not in (
            None,
            [],
            {},
        ):
            reasons.append("Offloading source validation error")
        if loaded is not None and offloaded is None:
            reasons.append("No signed-off offloading")
        if loaded is None and offloaded is not None:
            reasons.append("Offloaded without signed-off loading")
        if loaded is not None and offloaded is not None:
            if loaded <= 0:
                reasons.append("Loaded tonnes is zero or negative")
                weight_variance_exceptions += 1
            elif abs(offloaded - loaded) / loaded > Decimal("0.0025"):
                reasons.append("Absolute weight variance exceeds 0.250%")
                weight_variance_exceptions += 1

        enriched = {
            **row,
            "loaded_tonnes": loaded,
            "offloaded_tonnes": offloaded,
            "root_date": (
                row["transport_allocation_created_at"]
                .astimezone(BUSINESS_TIMEZONE)
                .date()
                if isinstance(row.get("transport_allocation_created_at"), datetime)
                else row.get("transport_allocation_created_at")
            ),
            "origin": origin,
            "origin_display": _study_site_name(origin),
            "origin_bay": origin_bay,
            "destination": destination,
            "destination_display": _study_site_name(destination),
            "destination_bay": destination_bay,
            "route_name": route_name,
            "audit_status": "Review" if reasons else "Reconciled",
            "audit_reasons": "; ".join(reasons),
        }
        loading_checklists.append(
            {
                "job_reference": row.get("job_reference"),
                "allocation_loading_point": row.get("allocation_loading_point"),
                "allocation_offloading_point": row.get(
                    "allocation_offloading_point"
                ),
                "latest_job_id": row.get("latest_loading_job_id"),
                "latest_status": row.get("latest_loading_status") or "Missing",
                "latest_opus_status": row.get("latest_loading_opus_status"),
                "signed_job_id": row.get("loading_job_id"),
                "evidence_status": (
                    "Signed-off fact available"
                    if row.get("loading_job_id")
                    else "No signed-off Loading and Exit"
                ),
                "loading_point": row.get("loading_point"),
                "loading_bay": origin_bay if loaded is not None else None,
                "nett_weight_tonnes": loaded,
                "operator_name": row.get("loading_operator"),
                "signed_off_at": row.get("loading_signed_off_at"),
                "signed_off_attempts": row.get("loading_signed_off_attempts"),
                "validation_errors": row.get("loading_validation_errors"),
            }
        )
        offloading_checklists.append(
            {
                "job_reference": row.get("job_reference"),
                "planned_offloading_point": (
                    row.get("transit_destination")
                    or row.get("allocation_offloading_point")
                ),
                "planned_offloading_bay": row.get("planned_offloading_slab"),
                "latest_job_id": row.get("latest_offloading_job_id"),
                "latest_status": row.get("latest_offloading_status") or "Missing",
                "latest_opus_status": row.get("latest_offloading_opus_status"),
                "signed_job_id": row.get("offloading_job_id"),
                "evidence_status": (
                    "Signed-off fact available"
                    if row.get("offloading_job_id")
                    else "No signed-off Offloading and Exit"
                ),
                "offloading_point": row.get("offloading_point"),
                "offloading_bay": destination_bay if offloaded is not None else None,
                "nett_weight_tonnes": offloaded,
                "operator_name": row.get("offloading_operator"),
                "signed_off_at": row.get("offloading_signed_off_at"),
                "signed_off_attempts": row.get("offloading_signed_off_attempts"),
                "validation_errors": row.get("offloading_validation_errors"),
            }
        )
        if loaded is None and offloaded is None:
            allocation_gaps.append(enriched)
            continue

        movements.append(enriched)
        if reasons:
            exceptions.append(enriched)
        route = routes.setdefault(
            (origin, destination),
            {
                "origin": origin,
                "origin_display": _study_site_name(origin),
                "destination": destination,
                "destination_display": _study_site_name(destination),
                "route_name": route_name,
                "movement_references": 0,
                "loaded_movements": 0,
                "offloaded_movements": 0,
                "loaded_tonnes": Decimal("0"),
                "offloaded_tonnes": Decimal("0"),
                "_completed_loaded_tonnes": Decimal("0"),
                "_completed_offloaded_tonnes": Decimal("0"),
            },
        )
        route["movement_references"] += 1
        if loaded is not None:
            loaded_total += loaded
            loaded_movements += 1
            route["loaded_movements"] += 1
            route["loaded_tonnes"] += loaded
            record_bay(
                "Delivered out",
                origin,
                origin_bay,
                fallback=origin_fallback,
                tonnes=loaded,
            )
            loading_at = row.get("loading_signed_off_at")
            if isinstance(loading_at, datetime):
                activity_date = loading_at.astimezone(BUSINESS_TIMEZONE).date()
                day = daily.setdefault(
                    activity_date,
                    {
                        "activity_date": activity_date,
                        "loaded_movements": 0,
                        "offloaded_movements": 0,
                        "loaded_tonnes": Decimal("0"),
                        "offloaded_tonnes": Decimal("0"),
                    },
                )
                day["loaded_movements"] += 1
                day["loaded_tonnes"] += loaded
        if offloaded is not None:
            offloaded_total += offloaded
            offloaded_movements += 1
            route["offloaded_movements"] += 1
            route["offloaded_tonnes"] += offloaded
            record_bay(
                "Delivered in",
                destination,
                destination_bay,
                fallback=destination_fallback,
                tonnes=offloaded,
            )
            offloading_at = row.get("offloading_signed_off_at")
            if isinstance(offloading_at, datetime):
                activity_date = offloading_at.astimezone(BUSINESS_TIMEZONE).date()
                day = daily.setdefault(
                    activity_date,
                    {
                        "activity_date": activity_date,
                        "loaded_movements": 0,
                        "offloaded_movements": 0,
                        "loaded_tonnes": Decimal("0"),
                        "offloaded_tonnes": Decimal("0"),
                    },
                )
                day["offloaded_movements"] += 1
                day["offloaded_tonnes"] += offloaded
        if loaded is not None and offloaded is not None:
            completed_loaded_total += loaded
            completed_offloaded_total += offloaded
            route["_completed_loaded_tonnes"] += loaded
            route["_completed_offloaded_tonnes"] += offloaded
        qualifies_as_transit = (
            bool(row.get("in_transit"))
            if event_cutoff is None
            else loaded is not None and offloaded is None
        )
        if qualifies_as_transit:
            transit_movements += 1
            transit_tonnes += loaded or Decimal("0")

    route_rows: list[dict[str, Any]] = []
    for route in routes.values():
        completed_loaded = route.pop("_completed_loaded_tonnes")
        completed_offloaded = route.pop("_completed_offloaded_tonnes")
        route["net_difference_tonnes"] = (
            route["offloaded_tonnes"] - route["loaded_tonnes"]
        )
        route["completed_variance_tonnes"] = (
            completed_offloaded - completed_loaded
        )
        route["delivery_pct"] = (
            (completed_offloaded / completed_loaded * Decimal("100")).quantize(
                Decimal("0.001")
            )
            if completed_loaded
            else None
        )
        route_rows.append(route)
    route_rows.sort(
        key=lambda row: (-row["loaded_tonnes"], row["origin"], row["destination"])
    )

    flow_rows = list(bay_flows.values())
    flow_rows.sort(
        key=lambda row: (
            row["direction"],
            -row["tonnes"],
            row["point"],
            row["bay"],
        )
    )
    bay_chart: dict[str, dict[str, Any]] = {}
    for flow in flow_rows:
        chart = bay_chart.setdefault(
            flow["bay"],
            {
                "bay": flow["bay"],
                "delivered_out_tonnes": Decimal("0"),
                "delivered_in_tonnes": Decimal("0"),
            },
        )
        key = (
            "delivered_out_tonnes"
            if flow["direction"] == "Delivered out"
            else "delivered_in_tonnes"
        )
        chart[key] += flow["tonnes"]

    route_totals: dict[str, dict[str, Decimal]] = {}
    for route in route_rows:
        totals = route_totals.setdefault(
            route["route_name"],
            {
                "loaded_tonnes": Decimal("0"),
                "offloaded_tonnes": Decimal("0"),
            },
        )
        totals["loaded_tonnes"] += route["loaded_tonnes"]
        totals["offloaded_tonnes"] += route["offloaded_tonnes"]

    leg_names = tuple(
        dict.fromkeys(
            (
                *STUDY_LEGS,
                *(
                    str(movement.get("route_name") or "Other / review")
                    for movement in movements
                ),
            )
        )
    )
    leg_totals: dict[str, dict[str, Any]] = {
        leg_name: {
            "route_name": leg_name,
            "movement_references": 0,
            "loaded_movements": 0,
            "offloaded_movements": 0,
            "loaded_tonnes": Decimal("0"),
            "offloaded_tonnes": Decimal("0"),
            "pending_references": 0,
            "pending_loaded_tonnes": Decimal("0"),
            "_completed_loaded_tonnes": Decimal("0"),
            "_completed_offloaded_tonnes": Decimal("0"),
        }
        for leg_name in leg_names
    }
    leg_lanes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for movement in movements:
        leg_name = str(movement.get("route_name") or "")
        if leg_name not in leg_totals:
            continue
        loaded = movement.get("loaded_tonnes")
        offloaded = movement.get("offloaded_tonnes")
        leg = leg_totals[leg_name]
        leg["movement_references"] += 1
        from_bay = (
            "From Mine"
            if leg_name.startswith("Mine to")
            else str(movement.get("origin_bay") or "Unresolved OPUS location")
        )
        to_bay = str(
            movement.get("destination_bay") or "Unresolved OPUS location"
        )
        lane = leg_lanes.setdefault(
            (leg_name, from_bay, to_bay),
            {
                "route_name": leg_name,
                "origin": str(movement.get("origin_display") or "Unknown"),
                "from_bay": from_bay,
                "destination": str(
                    movement.get("destination_display") or "Unknown"
                ),
                "to_bay": to_bay,
                "movement_references": 0,
                "loaded_movements": 0,
                "offloaded_movements": 0,
                "loaded_tonnes": Decimal("0"),
                "offloaded_tonnes": Decimal("0"),
                "pending_references": 0,
                "pending_loaded_tonnes": Decimal("0"),
                "_completed_loaded_tonnes": Decimal("0"),
                "_completed_offloaded_tonnes": Decimal("0"),
            },
        )
        lane["movement_references"] += 1
        if loaded is not None:
            loaded_decimal = _decimal(loaded)
            leg["loaded_movements"] += 1
            leg["loaded_tonnes"] += loaded_decimal
            lane["loaded_movements"] += 1
            lane["loaded_tonnes"] += loaded_decimal
        if offloaded is not None:
            offloaded_decimal = _decimal(offloaded)
            leg["offloaded_movements"] += 1
            leg["offloaded_tonnes"] += offloaded_decimal
            lane["offloaded_movements"] += 1
            lane["offloaded_tonnes"] += offloaded_decimal
        if loaded is not None and offloaded is None:
            leg["pending_references"] += 1
            leg["pending_loaded_tonnes"] += _decimal(loaded)
            lane["pending_references"] += 1
            lane["pending_loaded_tonnes"] += _decimal(loaded)
        if loaded is not None and offloaded is not None:
            leg["_completed_loaded_tonnes"] += _decimal(loaded)
            leg["_completed_offloaded_tonnes"] += _decimal(offloaded)
            lane["_completed_loaded_tonnes"] += _decimal(loaded)
            lane["_completed_offloaded_tonnes"] += _decimal(offloaded)

    def finalize_leg(row: dict[str, Any]) -> dict[str, Any]:
        completed_loaded = row.pop("_completed_loaded_tonnes")
        completed_offloaded = row.pop("_completed_offloaded_tonnes")
        row["movement_difference_tonnes"] = (
            row["offloaded_tonnes"] - row["loaded_tonnes"]
        )
        row["completed_variance_tonnes"] = (
            completed_offloaded - completed_loaded
        )
        row["delivery_pct"] = (
            (completed_offloaded / completed_loaded * Decimal("100")).quantize(
                Decimal("0.001")
            )
            if completed_loaded
            else None
        )
        return row

    leg_rows = [finalize_leg(leg_totals[name]) for name in leg_names]
    leg_lane_rows = [
        finalize_leg(row)
        for row in sorted(
            leg_lanes.values(),
            key=lambda row: (
                leg_names.index(str(row["route_name"])),
                str(row["from_bay"]),
                str(row["to_bay"]),
            ),
        )
    ]

    staging: dict[str, dict[str, Any]] = {}

    def staging_row(bay: str) -> dict[str, Any]:
        return staging.setdefault(
            bay,
            {
                "bay": bay,
                "effective_date": None,
                "opening_tonnes": Decimal("0"),
                "opening_status": "No opening recorded - treated as zero",
                "mine_receipts_tonnes": Decimal("0"),
                "other_receipts_tonnes": Decimal("0"),
                "bcf_dispatch_tonnes": Decimal("0"),
            },
        )

    bc_areas: dict[str, dict[str, Any]] = {}

    def bc_area_row(area: str) -> dict[str, Any]:
        return bc_areas.setdefault(
            area,
            {
                "area": area,
                "effective_date": None,
                "opening_tonnes": Decimal("0"),
                "opening_status": "No opening recorded - treated as zero",
                "received_from_bcf_tonnes": Decimal("0"),
                "received_direct_from_mine_tonnes": Decimal("0"),
                "other_receipts_tonnes": Decimal("0"),
            },
        )

    for _route, origin, destination, from_bay, to_bay in route_plan:
        if origin.casefold() == "bcf":
            staging_row(from_bay)
        if destination.casefold() == "bcf":
            staging_row(to_bay)
        if destination.casefold() == "bc":
            bc_area_row(to_bay)

    for opening in opening_balances:
        location = str(opening.get("location_name") or "")
        role = str(opening.get("stock_role") or "Origin")
        bay, _fallback = _study_bay(
            location,
            opening.get("storage_identifier"),
        )
        if "base chrome fields" in location.casefold() and role == "Origin":
            row = staging_row(bay)
        elif "bulk connection" in location.casefold() and role == "Destination":
            row = bc_area_row(bay)
        else:
            continue
        row["opening_tonnes"] = _decimal(opening.get("opening_tonnes"))
        row["effective_date"] = opening.get("effective_date")
        row["opening_status"] = "Governed opening balance"

    for movement in movements:
        loaded = movement.get("loaded_tonnes")
        offloaded = movement.get("offloaded_tonnes")
        origin_site = str(movement.get("origin_display") or "")
        destination_site = str(movement.get("destination_display") or "")
        if destination_site == "BCF" and offloaded is not None:
            receipt_key = (
                "mine_receipts_tonnes"
                if origin_site == "Kookfontein"
                else "other_receipts_tonnes"
            )
            staging_row(str(movement["destination_bay"]))[receipt_key] += _decimal(
                offloaded
            )
        if origin_site == "BCF" and loaded is not None:
            staging_row(str(movement["origin_bay"]))[
                "bcf_dispatch_tonnes"
            ] += _decimal(loaded)
        if offloaded is None or destination_site != "BC":
            continue
        if origin_site == "BCF":
            bc_area_row(str(movement["destination_bay"]))[
                "received_from_bcf_tonnes"
            ] += _decimal(offloaded)
        elif origin_site == "Kookfontein":
            bc_area_row(str(movement["destination_bay"]))[
                "received_direct_from_mine_tonnes"
            ] += _decimal(offloaded)
        else:
            bc_area_row(str(movement["destination_bay"]))[
                "other_receipts_tonnes"
            ] += _decimal(offloaded)

    for row in staging.values():
        row["movement_delta_tonnes"] = (
            row["mine_receipts_tonnes"]
            + row["other_receipts_tonnes"]
            - row["bcf_dispatch_tonnes"]
        )
        row["closing_tonnes"] = (
            row["opening_tonnes"] + row["movement_delta_tonnes"]
        )
    staging_rows = sorted(staging.values(), key=lambda row: row["bay"])

    for row in bc_areas.values():
        row["total_received_tonnes"] = (
            row["received_from_bcf_tonnes"]
            + row["received_direct_from_mine_tonnes"]
            + row["other_receipts_tonnes"]
        )
        row["closing_tonnes"] = (
            row["opening_tonnes"] + row["total_received_tonnes"]
        )
    bc_area_rows = sorted(bc_areas.values(), key=lambda row: row["area"])

    staging_opening = sum(
        (row["opening_tonnes"] for row in staging_rows),
        Decimal("0"),
    )
    staging_receipts = sum(
        (row["mine_receipts_tonnes"] for row in staging_rows),
        Decimal("0"),
    )
    staging_other_receipts = sum(
        (row["other_receipts_tonnes"] for row in staging_rows),
        Decimal("0"),
    )
    staging_dispatch = sum(
        (row["bcf_dispatch_tonnes"] for row in staging_rows),
        Decimal("0"),
    )
    staging_closing = sum(
        (row["closing_tonnes"] for row in staging_rows),
        Decimal("0"),
    )
    bc_opening = sum(
        (row["opening_tonnes"] for row in bc_area_rows),
        Decimal("0"),
    )
    bc_closing = sum(
        (row["closing_tonnes"] for row in bc_area_rows),
        Decimal("0"),
    )

    movements.sort(
        key=lambda row: (
            row.get("loading_signed_off_at")
            or row.get("transport_allocation_created_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            str(row.get("job_reference") or ""),
        ),
        reverse=True,
    )
    allocation_gaps.sort(
        key=lambda row: (
            row.get("transport_allocation_created_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            str(row.get("job_reference") or ""),
        ),
        reverse=True,
    )
    exceptions.sort(
        key=lambda row: (
            abs(_decimal(row.get("variance_tonnes"))),
            str(row.get("job_reference") or ""),
        ),
        reverse=True,
    )
    route_plan_rows, unlisted_route_movements = _route_bay_reconciliation(
        movements,
        route_plan,
    )
    location_balances = _order_location_balances(movements, opening_balances)
    load_types: dict[str, dict[str, Any]] = {}
    for movement in movements:
        if movement.get("loaded_tonnes") is None:
            continue
        category = str(movement.get("truck_type") or "Unknown").strip() or "Unknown"
        load_type = load_types.setdefault(
            category.casefold(),
            {
                "truck_type": category,
                "load_count": 0,
                "loaded_tonnes": Decimal("0"),
            },
        )
        load_type["load_count"] += 1
        load_type["loaded_tonnes"] += _decimal(movement.get("loaded_tonnes"))
    load_type_rows = sorted(
        load_types.values(),
        key=lambda row: (-row["load_count"], str(row["truck_type"])),
    )
    for row in load_type_rows:
        row["load_pct"] = (
            Decimal(row["load_count"])
            / Decimal(loaded_movements)
            * Decimal("100")
        ).quantize(Decimal("0.1")) if loaded_movements else Decimal("0")
    order_soh_tonnes = sum(
        (
            row["soh_tonnes"]
            for row in location_balances
            if row["included_in_soh"]
        ),
        Decimal("0"),
    )
    order_opening_tonnes = sum(
        (row["opening_tonnes"] for row in location_balances),
        Decimal("0"),
    )
    completed_variance = completed_offloaded_total - completed_loaded_total
    first_root_date = min(root_dates) if root_dates else None
    last_root_date = max(root_dates) if root_dates else None
    coverage_warning = ""
    coverage_note = ""
    if first_root_date is None:
        coverage_warning = "No Transport Allocation roots are stored for this study."
    elif first_root_date > date_from:
        inactive_to = first_root_date - timedelta(days=1)
        coverage_note = (
            f"No Transport Allocation roots are recorded from "
            f"{date_from:%d %b %Y} through {inactive_to:%d %b %Y}; "
            f"the first recorded order activity is {first_root_date:%d %b %Y}."
        )
    return {
        "order_reference": order_reference,
        "client_name": next(
            (
                str(row.get("client_name"))
                for row in rows
                if str(row.get("client_name") or "").strip()
            ),
            "Unmapped client",
        ),
        "date_from": date_from,
        "date_to": date_to,
        "event_cutoff": event_cutoff,
        "expected_bays": list(expected_bays),
        "planned_routes": [
            {
                "route_name": route,
                "origin": origin,
                "destination": destination,
                "from_bay": from_bay,
                "to_bay": to_bay,
            }
            for route, origin, destination, from_bay, to_bay in route_plan
        ],
        "first_root_date": first_root_date,
        "last_root_date": last_root_date,
        "coverage_warning": coverage_warning,
        "coverage_note": coverage_note,
        "metrics": {
            "parcel_tonnes": _decimal(parcel_tonnes),
            "opening_stock_tonnes": order_opening_tonnes,
            "bcf_stock_tonnes": staging_closing,
            "bc_stock_tonnes": bc_closing,
            "stock_at_recorded_locations_tonnes": order_soh_tonnes,
            "order_location_soh_tonnes": order_soh_tonnes,
            "order_locations": len(
                {
                    str(row["location"])
                    for row in location_balances
                    if row["included_in_soh"]
                }
            ),
            "unresolved_soh_locations": sum(
                1
                for row in location_balances
                if row["balance_status"]
                == "Opening balance or missing receipts required"
            ),
            "allocations": len(rows),
            "movement_references": len(movements),
            "loaded_movements": loaded_movements,
            "offloaded_movements": offloaded_movements,
            "allocations_without_loading": len(allocation_gaps),
            "pending_offloads": sum(
                1
                for row in movements
                if row.get("loaded_tonnes") is not None
                and row.get("offloaded_tonnes") is None
            ),
            "in_transit": transit_movements,
            "in_transit_tonnes": transit_tonnes,
            "review_references": len(
                {
                    str(row.get("job_reference") or "")
                    for row in exceptions + allocation_gaps
                    if str(row.get("job_reference") or "")
                }
            ),
            "loaded_tonnes": loaded_total,
            "offloaded_tonnes": offloaded_total,
            "net_movement_difference_tonnes": offloaded_total - loaded_total,
            "completed_variance_tonnes": completed_variance,
            "delivery_pct": (
                (
                    completed_offloaded_total
                    / completed_loaded_total
                    * Decimal("100")
                ).quantize(Decimal("0.001"))
                if completed_loaded_total
                else None
            ),
            "unexpected_bay_movements": unexpected_bay_movements,
            "unlisted_route_movements": unlisted_route_movements,
            "weight_variance_exceptions": weight_variance_exceptions,
            "loading_exit_signed": sum(
                1 for row in loading_checklists if row["signed_job_id"]
            ),
            "offloading_exit_signed": sum(
                1 for row in offloading_checklists if row["signed_job_id"]
            ),
            "completed_checklist_chains": sum(
                1
                for loading, offloading in zip(
                    loading_checklists,
                    offloading_checklists,
                )
                if loading["signed_job_id"] and offloading["signed_job_id"]
            ),
            "mine_dispatch_tonnes": (
                route_totals.get("Mine to BCF", {}).get(
                    "loaded_tonnes", Decimal("0")
                )
                + route_totals.get("Mine to BC Direct", {}).get(
                    "loaded_tonnes", Decimal("0")
                )
            ),
            "bc_receipts_tonnes": (
                route_totals.get("BCF to BC", {}).get(
                    "offloaded_tonnes", Decimal("0")
                )
                + route_totals.get("Mine to BC Direct", {}).get(
                    "offloaded_tonnes", Decimal("0")
                )
            ),
            "bcf_opening_tonnes": staging_opening,
            "bcf_mine_receipts_tonnes": staging_receipts,
            "bcf_other_receipts_tonnes": staging_other_receipts,
            "bcf_dispatch_tonnes": staging_dispatch,
            "bcf_closing_tonnes": staging_closing,
        },
        "legs": leg_rows,
        "leg_lanes": leg_lane_rows,
        "routes": route_rows,
        "route_plan": route_plan_rows,
        "staging": staging_rows,
        "bc_areas": bc_area_rows,
        "bay_flows": flow_rows,
        "bay_chart": sorted(
            bay_chart.values(),
            key=lambda row: (
                -max(
                    row["delivered_out_tonnes"],
                    row["delivered_in_tonnes"],
                ),
                row["bay"],
            ),
        ),
        "location_balances": location_balances,
        "load_types": load_type_rows,
        "loading_checklists": loading_checklists,
        "offloading_checklists": offloading_checklists,
        "daily": [daily[key] for key in sorted(daily)],
        "movements": movements,
        "exceptions": exceptions,
        "allocation_gaps": allocation_gaps,
    }


class OperationsRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _password(self) -> str:
        if self.settings.db_password_env:
            return self.settings.db_password_env
        credential = read_windows_credential(self.settings.credential_target)
        if credential and credential.password:
            return credential.password
        raise RuntimeError(
            "The OPUS database password was not found in Windows Credential Manager "
            f"at {self.settings.credential_target!r} or in OPUS_DB_APP_PASSWORD."
        )

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        connection = psycopg.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            dbname=self.settings.db_name,
            user=self.settings.db_user,
            password=self._password(),
            connect_timeout=5,
            application_name="connect_logistics_opus_dashboard",
            row_factory=dict_row,
        )
        try:
            yield connection
        finally:
            connection.close()

    def ensure_partitions(self) -> None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT ingest.ensure_monthly_partitions(12, 6)")
            connection.commit()

    def detail_schema_ready(self) -> bool:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        to_regclass('ops.checklist_instances') IS NOT NULL
                        AND to_regclass('ops.checklist_answers') IS NOT NULL
                        AND to_regclass('ingest.extraction_errors') IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'ingest'
                              AND table_name = 'raw_records'
                              AND column_name = 'source_payload_sha256'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'ops'
                              AND table_name = 'allocations'
                              AND column_name = 'transport_allocation_created_at'
                        )
                        AND to_regprocedure(
                            'ops.refresh_operational_facts(bigint)'
                        ) IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1
                            FROM ops.checklist_definitions
                            WHERE active
                              AND (
                                  stage_code = 'bulk_import'
                                  OR lower(btrim(canonical_name))
                                     = 'bulk import for minerals transport allocation'
                              )
                        )
                        AND (
                            SELECT count(*) = 7
                            FROM (VALUES
                                ('transport_allocation', '4f7cd186-4bce-46eb-9d92-272e0ceded7e'::uuid),
                                ('vehicle_inspection', 'e3a5c4e9-fc33-44bf-8d5f-52cdbf56aa02'::uuid),
                                ('loading_exit', '652d5117-d609-47f3-b093-5ad4ebcb97bf'::uuid),
                                ('staging_arrival', 'aeca9758-c49b-486e-8f60-6a29d6c3dcb2'::uuid),
                                ('staging_exit', 'fe799df1-67ea-4ea9-93c6-913161422a8e'::uuid),
                                ('truck_arrival', '7aa017a8-cb07-4bd6-afe8-37ff55c47acc'::uuid),
                                ('offloading_exit', '246209a9-2f68-4ef5-b174-8b06e4c014fc'::uuid)
                            ) expected(stage_code, opus_checklist_id)
                            JOIN ops.checklist_definitions definition
                              ON definition.stage_code = expected.stage_code
                             AND definition.opus_checklist_id = expected.opus_checklist_id
                        )
                        AS ready
                    """
                )
                row = cursor.fetchone() or {}
        return bool(row.get("ready"))

    def load(self, period: str = "ytd") -> DashboardSnapshot:
        if period not in PERIOD_LABELS:
            period = "ytd"
        cutoff = _period_start(period)
        try:
            with self.connection() as connection:
                storage = self._storage(connection)
                if not (
                    storage.get("detail_schema_ready", 0)
                    and storage.get("stage_mapping_ready", 0)
                    and storage.get("source_hash_ready", 0)
                    and storage.get("root_baseline_ready", 0)
                ):
                    return DashboardSnapshot(
                        connected=True,
                        captured_at=datetime.now().astimezone(),
                        period=period,
                        storage=storage,
                        error=(
                            "Database migrations through 008 are required before "
                            "Transport Allocation-rooted detail can be extracted."
                        ),
                    )
                metrics = self._extraction_metrics(connection)
                checklist_summary = self._checklist_summary(connection)
                checklist_answers = self._checklist_answers(connection)
                order_workflows = self._order_workflows(connection)
                extraction_runs = self._extraction_runs(connection)
                extraction_errors = self._extraction_errors(connection)
            return DashboardSnapshot(
                connected=True,
                captured_at=datetime.now().astimezone(),
                period=period,
                metrics=metrics,
                extraction_runs=extraction_runs,
                extraction_errors=extraction_errors,
                checklist_summary=checklist_summary,
                checklist_answers=checklist_answers,
                order_workflows=order_workflows,
                storage=storage,
            )
        except Exception as exc:
            return DashboardSnapshot.disconnected(period, str(exc))

    def load_shell(self, period: str = "ytd") -> DashboardSnapshot:
        if period not in PERIOD_LABELS:
            period = "ytd"
        try:
            with self.connection() as connection:
                storage = self._storage(connection)
            return DashboardSnapshot(
                connected=True,
                captured_at=datetime.now().astimezone(),
                period=period,
                storage=storage,
            )
        except Exception as exc:
            return DashboardSnapshot.disconnected(period, str(exc))

    def load_checklist_detail(self, job_row_id: int) -> dict[str, Any]:
        if job_row_id <= 0:
            raise ValueError("A valid checklist job row ID is required.")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        j.id AS job_row_id,
                        j.job_reference,
                        a.order_reference AS order_number,
                        cd.canonical_name AS checklist_name,
                        cd.stage_code,
                        j.status AS job_status,
                        j.status_detail,
                        j.operator_name,
                        j.created_by_name,
                        j.created_from_operator_name,
                        j.source_created_at,
                        j.operator_started_at,
                        j.source_completed_at,
                        j.source_signed_off_at,
                        j.expected_start_at,
                        j.due_at,
                        j.last_updated_at,
                        ci.id AS checklist_instance_id,
                        ci.percentage_complete,
                        ci.score,
                        ci.total_score,
                        ci.possible_score,
                        ci.priority,
                        ci.duration,
                        ci.section_count,
                        ci.answer_count,
                        ci.image_count,
                        ci.item_count,
                        ci.table_count,
                        ci.detail_complete,
                        ci.source_updated_at
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    LEFT JOIN ops.checklist_instances ci ON ci.job_id = j.id
                    WHERE j.id = %s
                    ORDER BY ci.source_updated_at DESC NULLS LAST, ci.id DESC
                    LIMIT 1
                    """,
                    (job_row_id,),
                )
                summary = cursor.fetchone()
                if summary is None:
                    raise LookupError(
                        f"Checklist job row {job_row_id} was not found."
                    )
                cursor.execute(
                    """
                    SELECT
                        ca.id AS answer_row_id,
                        ca.section_sequence,
                        ca.section_name,
                        ca.subsection_name,
                        ca.question,
                        ca.question_report_full,
                        ca.question_report_short,
                        ca.question_summary,
                        ca.unformatted_question_text,
                        ca.action_text,
                        ca.optional_question,
                        ca.require_comment,
                        CASE
                            WHEN btrim(coalesce(
                                nullif(ca.text_value, ''),
                                nullif(ca.report_formatted_answer, ''),
                                ''
                            )) <> btrim(ca.question)
                            THEN coalesce(
                                nullif(ca.text_value, ''),
                                nullif(ca.report_formatted_answer, '')
                            )
                            ELSE NULL
                        END AS question_detail,
                        coalesce(
                            nullif(ca.answer_text, ''),
                            nullif(ca.unformatted_answer, ''),
                            CASE
                                WHEN ca.answer_items <> '[]'::jsonb
                                    THEN '[structured item]'
                                WHEN ca.answer_images <> '[]'::jsonb
                                    THEN '[image]'
                                WHEN ca.child_checklist_answers <> '[]'::jsonb
                                    THEN '[child checklist]'
                                WHEN ca.table_data <> '{}'::jsonb
                                    THEN '[table data]'
                                WHEN ca.answer_extra <> '{}'::jsonb
                                    THEN '[structured answer]'
                                ELSE ''
                            END
                        ) AS answer,
                        ca.question_type,
                        ca.comments,
                        ca.answer_text,
                        ca.text_value,
                        ca.unformatted_answer,
                        ca.report_formatted_answer,
                        CASE
                            WHEN jsonb_typeof(ca.answer_images) = 'array'
                            THEN jsonb_array_length(ca.answer_images)
                            ELSE 0
                        END AS images,
                        CASE
                            WHEN jsonb_typeof(ca.answer_items) = 'array'
                            THEN jsonb_array_length(ca.answer_items)
                            ELSE 0
                        END AS structured_items,
                        ca.table_data <> '{}'::jsonb AS has_table_data,
                        ca.answer_extra::text AS answer_extra,
                        ca.answer_images::text AS answer_images,
                        ca.answer_items::text AS answer_items,
                        ca.child_checklist_answers::text
                            AS child_checklist_answers,
                        ca.table_data::text AS table_data,
                        ca.table_columns::text AS table_columns
                    FROM ops.checklist_answers ca
                    WHERE ca.checklist_instance_id = %s
                    ORDER BY
                        ca.section_sequence,
                        ca.subsection_created_at NULLS LAST,
                        ca.id
                    """,
                    (summary.get("checklist_instance_id"),),
                )
                answers = cursor.fetchall()
        return {
            "summary": _clean_rows([summary])[0],
            "answers": _clean_rows(answers),
        }

    def analytics_schema_ready(self) -> bool:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        to_regclass('ops.checklist_operational_facts') IS NOT NULL
                        AND to_regclass('ops.order_master') IS NOT NULL
                        AND to_regclass('ops.stock_opening_balances') IS NOT NULL
                        AND to_regclass('ops.v_reference_workflow_state') IS NOT NULL
                        AND to_regclass('ops.v_transit_route_register') IS NOT NULL
                        AND to_regclass('ops.v_stock_reconciliation') IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'ops'
                              AND table_name = 'stock_opening_balances'
                              AND column_name = 'stock_role'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'ops'
                              AND table_name = 'v_reference_workflow_state'
                              AND column_name = 'transit_origin'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'ops'
                              AND table_name = 'v_stock_reconciliation'
                              AND column_name = 'loading_storage_display'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'ops'
                              AND table_name = 'v_stock_ledger'
                              AND column_name = 'stock_role'
                        )
                        AS ready
                    """
                )
                row = cursor.fetchone() or {}
        return bool(row.get("ready"))

    def data_filter_options(self) -> dict[str, list[str]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT canonical_name
                    FROM ops.checklist_definitions
                    WHERE stage_code <> 'bulk_import'
                    ORDER BY stage_order, canonical_name
                    """
                )
                checklists = [
                    str(row["canonical_name"]) for row in cursor.fetchall()
                ]
        return {
            "checklists": checklists,
            "statuses": [
                "Signed off",
                "Not started",
                "In progress",
                "Under review",
                "Closed",
                "Cancelled",
                "Other",
            ],
        }

    @staticmethod
    def _data_parameters(filters: DataFilters) -> dict[str, Any]:
        return {
            "date_from": filters.date_from,
            "date_to": filters.date_to,
            "job_reference": filters.job_reference.strip(),
            "checklist_name": filters.checklist_name.strip(),
            "status_group": filters.status_group.strip(),
        }

    @staticmethod
    def _data_where(
        *,
        include_checklist: bool = True,
        include_status: bool = True,
    ) -> str:
        clauses = [
            "cd.stage_code <> 'bulk_import'",
            (
                "(%(date_from)s::date IS NULL OR "
                "(a.transport_allocation_created_at AT TIME ZONE 'UTC')::date "
                ">= %(date_from)s::date)"
            ),
            (
                "(%(date_to)s::date IS NULL OR "
                "(a.transport_allocation_created_at AT TIME ZONE 'UTC')::date "
                "<= %(date_to)s::date)"
            ),
            (
                "(%(job_reference)s = '' OR "
                "j.job_reference ILIKE '%%' || %(job_reference)s || '%%')"
            ),
        ]
        if include_checklist:
            clauses.append(
                "(%(checklist_name)s = '' OR "
                "cd.canonical_name = %(checklist_name)s)"
            )
        if include_status:
            clauses.append(
                "(%(status_group)s = '' OR "
                "ops.normalized_job_status(j.status) = %(status_group)s)"
            )
        return "\n AND ".join(clauses)

    def load_data_page(
        self,
        filters: DataFilters,
        *,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 10), 250)
        parameters = {
            **self._data_parameters(filters),
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        table_where = self._data_where()
        status_where = self._data_where(include_status=False)
        checklist_where = self._data_where(include_checklist=False)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        ops.normalized_job_status(j.status) AS status_group,
                        count(*) AS checklist_jobs
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    WHERE {status_where}
                    GROUP BY ops.normalized_job_status(j.status)
                    ORDER BY status_group
                    """,
                    parameters,
                )
                statuses = {
                    str(row["status_group"]): int(row["checklist_jobs"])
                    for row in cursor.fetchall()
                }
                cursor.execute(
                    f"""
                    SELECT
                        cd.canonical_name AS checklist_name,
                        cd.stage_code,
                        cd.stage_order,
                        count(*) AS checklist_jobs,
                        count(DISTINCT j.job_reference) AS references,
                        count(*) FILTER (
                            WHERE ci.detail_complete
                        ) AS detailed_jobs
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    LEFT JOIN LATERAL (
                        SELECT detail_complete
                        FROM ops.checklist_instances instance
                        WHERE instance.job_id = j.id
                        ORDER BY instance.source_updated_at DESC NULLS LAST,
                                 instance.id DESC
                        LIMIT 1
                    ) ci ON true
                    WHERE {checklist_where}
                    GROUP BY cd.canonical_name, cd.stage_code, cd.stage_order
                    ORDER BY cd.stage_order, cd.canonical_name
                    """,
                    parameters,
                )
                checklist_totals = cursor.fetchall()
                cursor.execute(
                    f"""
                    SELECT count(*) AS total
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    WHERE {table_where}
                    """,
                    parameters,
                )
                total = int((cursor.fetchone() or {}).get("total") or 0)
                cursor.execute(
                    f"""
                    SELECT
                        j.id AS job_row_id,
                        j.job_reference,
                        a.order_reference AS order_number,
                        (
                            a.transport_allocation_created_at AT TIME ZONE 'UTC'
                        )::date AS root_date,
                        cd.canonical_name AS checklist_name,
                        cd.stage_code,
                        j.status AS opus_status,
                        ops.normalized_job_status(j.status) AS status_group,
                        j.operator_name,
                        j.source_created_at,
                        j.operator_started_at,
                        j.source_signed_off_at,
                        ci.percentage_complete,
                        ci.answer_count,
                        ci.detail_complete,
                        ci.source_updated_at
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    LEFT JOIN LATERAL (
                        SELECT
                            percentage_complete,
                            answer_count,
                            detail_complete,
                            source_updated_at
                        FROM ops.checklist_instances instance
                        WHERE instance.job_id = j.id
                        ORDER BY instance.source_updated_at DESC NULLS LAST,
                                 instance.id DESC
                        LIMIT 1
                    ) ci ON true
                    WHERE {table_where}
                    ORDER BY
                        a.transport_allocation_created_at DESC,
                        j.job_reference DESC,
                        cd.stage_order,
                        j.source_created_at,
                        j.id
                    LIMIT %(limit)s OFFSET %(offset)s
                    """,
                    parameters,
                )
                rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT
                        max(run.finished_at) FILTER (
                            WHERE run.status = 'succeeded'
                        ) AS extraction_freshness,
                        max(fact.refreshed_at) AS analytics_freshness
                    FROM ingest.extraction_runs run
                    FULL JOIN ops.checklist_operational_facts fact ON false
                    """
                )
                freshness = cursor.fetchone() or {}
        return {
            "statuses": statuses,
            "checklist_totals": _clean_rows(checklist_totals),
            "rows": _clean_rows(rows),
            "total": total,
            "page": page,
            "page_size": page_size,
            "freshness": _clean_rows([freshness])[0],
        }

    def load_reference_workflow(self, job_reference: str) -> list[dict[str, Any]]:
        reference = job_reference.strip()
        if not reference:
            return []
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        attempt.job_id AS job_row_id,
                        attempt.job_reference,
                        attempt.workflow_sequence,
                        attempt.attempt_sequence,
                        attempt.attempt_count,
                        attempt.checklist_name,
                        attempt.stage_code,
                        attempt.opus_status,
                        attempt.status_group,
                        attempt.status_detail,
                        attempt.operator_name,
                        attempt.source_created_at,
                        attempt.operator_started_at,
                        attempt.source_completed_at,
                        attempt.source_signed_off_at,
                        attempt.is_current_job,
                        attempt.has_later_job,
                        attempt.status_group IN ('Closed', 'Cancelled')
                            AND attempt.has_later_job AS superseded_terminal,
                        ci.percentage_complete,
                        ci.answer_count,
                        ci.detail_complete
                    FROM ops.v_workflow_attempts attempt
                    LEFT JOIN LATERAL (
                        SELECT
                            instance.percentage_complete,
                            instance.answer_count,
                            instance.detail_complete
                        FROM ops.checklist_instances instance
                        WHERE instance.job_id = attempt.job_id
                        ORDER BY instance.source_updated_at DESC NULLS LAST,
                                 instance.id DESC
                        LIMIT 1
                    ) ci ON true
                    WHERE attempt.job_reference = %s
                    ORDER BY attempt.workflow_sequence
                    """,
                    (reference,),
                )
                return _clean_rows(cursor.fetchall())

    @staticmethod
    def _ops_parameters(filters: OpsFilters) -> dict[str, Any]:
        return {
            "date_from": filters.date_from,
            "date_to": filters.date_to,
            "origin": filters.origin.strip(),
            "destination": filters.destination.strip(),
            "truck_type": filters.truck_type.strip(),
        }

    def load_ops_dashboard(self, filters: OpsFilters) -> dict[str, Any]:
        parameters = self._ops_parameters(filters)
        base_where = """
            (%(date_from)s::date IS NULL OR
             (transport_allocation_created_at AT TIME ZONE 'UTC')::date
             >= %(date_from)s::date)
            AND (%(date_to)s::date IS NULL OR
             (transport_allocation_created_at AT TIME ZONE 'UTC')::date
             <= %(date_to)s::date)
        """
        transit_where = """
            in_transit
            AND (%(origin)s = '' OR
                 coalesce(nullif(btrim(transit_origin), ''), 'Unknown')
                 = %(origin)s)
            AND (%(destination)s = '' OR
                 coalesce(nullif(btrim(transit_destination), ''), 'Unknown')
                 = %(destination)s)
            AND (%(truck_type)s = '' OR
                 coalesce(nullif(btrim(truck_type), ''), 'Unknown')
                 = %(truck_type)s)
        """
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    WITH base AS (
                        SELECT *
                        FROM ops.v_reference_workflow_state
                        WHERE {base_where}
                    ),
                    transit AS (
                        SELECT * FROM base WHERE {transit_where}
                    ),
                    duplicate_trucks AS (
                        SELECT
                            upper(btrim(truck_registration)) AS registration,
                            count(DISTINCT job_reference) AS reference_count
                        FROM transit
                        WHERE nullif(btrim(truck_registration), '') IS NOT NULL
                        GROUP BY upper(btrim(truck_registration))
                        HAVING count(DISTINCT job_reference) > 1
                    )
                    SELECT
                        (
                            SELECT count(DISTINCT upper(btrim(truck_registration)))
                            FROM transit
                            WHERE nullif(btrim(truck_registration), '') IS NOT NULL
                        ) AS distinct_trucks,
                        (SELECT count(*) FROM transit) AS qualifying_references,
                        (
                            SELECT coalesce(sum(loaded_tonnes), 0) FROM transit
                        ) AS transit_tonnes,
                        (
                            SELECT count(*) FROM transit
                            WHERE nullif(btrim(transit_origin), '') IS NULL
                        ) AS unknown_origin,
                        (
                            SELECT count(*) FROM transit
                            WHERE nullif(btrim(transit_destination), '') IS NULL
                        ) AS unknown_destination,
                        (
                            SELECT count(*) FROM transit
                            WHERE transit_route_fallback
                        ) AS route_fallbacks,
                        (
                            SELECT count(*) FROM transit
                            WHERE nullif(btrim(truck_registration), '') IS NULL
                        ) AS missing_registration,
                        (
                            SELECT count(*) FROM duplicate_trucks
                        ) AS duplicate_truck_registrations,
                        (
                            SELECT coalesce(sum(reference_count), 0)
                            FROM duplicate_trucks
                        ) AS duplicate_active_references,
                        (
                            SELECT count(*) FROM base
                            WHERE stopped_after_closure
                              AND loading_job_id IS NOT NULL
                              AND offloading_started_at IS NULL
                        ) AS stopped_closure_exceptions
                    """,
                    parameters,
                )
                metrics = cursor.fetchone() or {}
                cursor.execute(
                    f"""
                    WITH transit AS (
                        SELECT *
                        FROM ops.v_reference_workflow_state
                        WHERE {base_where} AND {transit_where}
                    )
                    SELECT
                        coalesce(nullif(btrim(transit_origin), ''), 'Unknown')
                            AS origin,
                        coalesce(nullif(btrim(transit_destination), ''), 'Unknown')
                            AS destination,
                        count(*) AS reference_count,
                        count(DISTINCT upper(btrim(truck_registration))) FILTER (
                            WHERE nullif(btrim(truck_registration), '') IS NOT NULL
                        ) AS trucks,
                        coalesce(sum(loaded_tonnes), 0) AS tonnes
                    FROM transit
                    GROUP BY 1, 2
                    ORDER BY reference_count DESC, origin, destination
                    """,
                    parameters,
                )
                routes = cursor.fetchall()
                cursor.execute(
                    f"""
                    WITH transit AS (
                        SELECT *
                        FROM ops.v_reference_workflow_state
                        WHERE {base_where} AND {transit_where}
                    )
                    SELECT
                        coalesce(nullif(btrim(transit_destination), ''), 'Unknown')
                            AS category,
                        count(*) AS reference_count,
                        count(DISTINCT upper(btrim(truck_registration))) FILTER (
                            WHERE nullif(btrim(truck_registration), '') IS NOT NULL
                        ) AS trucks,
                        coalesce(sum(loaded_tonnes), 0) AS tonnes
                    FROM transit
                    GROUP BY 1
                    ORDER BY reference_count DESC, category
                    """,
                    parameters,
                )
                destinations = cursor.fetchall()
                cursor.execute(
                    f"""
                    WITH transit AS (
                        SELECT *
                        FROM ops.v_reference_workflow_state
                        WHERE {base_where} AND {transit_where}
                    )
                    SELECT
                        coalesce(nullif(btrim(truck_type), ''), 'Unknown') AS category,
                        count(*) AS reference_count,
                        coalesce(sum(loaded_tonnes), 0) AS tonnes
                    FROM transit
                    GROUP BY 1
                    ORDER BY reference_count DESC, category
                    """,
                    parameters,
                )
                truck_types = cursor.fetchall()
                cursor.execute(
                    f"""
                    WITH transit AS (
                        SELECT *
                        FROM ops.v_reference_workflow_state
                        WHERE {base_where} AND {transit_where}
                    )
                    SELECT
                        coalesce(nullif(btrim(current_checklist), ''), 'Unknown')
                            AS category,
                        count(*) AS reference_count
                    FROM transit
                    GROUP BY 1
                    ORDER BY reference_count DESC, category
                    """,
                    parameters,
                )
                stages = cursor.fetchall()
                cursor.execute(
                    f"""
                    SELECT
                        job_reference,
                        order_reference,
                        client_name,
                        truck_registration,
                        truck_type,
                        transit_origin AS origin,
                        transit_destination AS destination,
                        transit_origin_source AS origin_source,
                        transit_destination_source AS destination_source,
                        transit_route_fallback AS route_fallback,
                        loaded_tonnes,
                        loading_signed_off_at AS departed_at,
                        current_checklist,
                        current_opus_status,
                        loading_signed_off_attempts,
                        loading_validation_errors,
                        count(*) OVER (
                            PARTITION BY upper(btrim(truck_registration))
                        ) > 1
                            AND nullif(btrim(truck_registration), '') IS NOT NULL
                            AS duplicate_active_truck
                    FROM ops.v_reference_workflow_state
                    WHERE {base_where} AND {transit_where}
                    ORDER BY loading_signed_off_at, job_reference
                    """,
                    parameters,
                )
                rows = cursor.fetchall()
        return {
            "metrics": {
                key: _clean_value(value or 0) for key, value in metrics.items()
            },
            "routes": _clean_rows(routes),
            "destinations": _clean_rows(destinations),
            "truck_types": _clean_rows(truck_types),
            "stages": _clean_rows(stages),
            "rows": _clean_rows(rows),
        }

    def ops_filter_options(self) -> dict[str, list[str]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT
                        coalesce(nullif(btrim(transit_origin), ''), 'Unknown')
                            AS value
                    FROM ops.v_reference_workflow_state
                    WHERE in_transit
                    ORDER BY value
                    """
                )
                origins = [str(row["value"]) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT DISTINCT
                        coalesce(nullif(btrim(transit_destination), ''), 'Unknown')
                            AS value
                    FROM ops.v_reference_workflow_state
                    WHERE in_transit
                    ORDER BY value
                    """
                )
                destinations = [str(row["value"]) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT DISTINCT
                        coalesce(nullif(btrim(truck_type), ''), 'Unknown') AS value
                    FROM ops.v_reference_workflow_state
                    WHERE in_transit
                    ORDER BY value
                    """
                )
                truck_types = [str(row["value"]) for row in cursor.fetchall()]
        return {
            "origins": origins,
            "destinations": destinations,
            "truck_types": truck_types,
        }

    def _stock_parameters(self, filters: StockFilters) -> dict[str, Any]:
        return {
            "movement_from": filters.movement_from or self.settings.opus_extract_from,
            "movement_to": filters.movement_to or date.today(),
            "as_of": filters.as_of or date.today(),
            "baseline_from": self.settings.opus_extract_from,
            "order_reference": filters.order_reference.strip(),
            "client_name": filters.client_name.strip(),
            "loading_point": filters.loading_point.strip(),
            "loading_storage": filters.loading_storage.strip(),
            "offloading_point": filters.offloading_point.strip(),
            "offloading_storage": filters.offloading_storage.strip(),
            "truck_type": filters.truck_type.strip(),
        }

    @staticmethod
    def _stock_dimension_where(alias: str = "r") -> str:
        return f"""
            (%(order_reference)s = '' OR
             coalesce({alias}.order_reference, '') ILIKE
             '%%' || %(order_reference)s || '%%')
            AND (%(client_name)s = '' OR
             {alias}.client_name = %(client_name)s)
            AND (%(loading_point)s = '' OR
             {alias}.loading_point = %(loading_point)s)
            AND (%(loading_storage)s = '' OR
             {alias}.loading_storage_display = %(loading_storage)s)
            AND (%(offloading_point)s = '' OR
             coalesce(
                 nullif(btrim({alias}.offloading_point), ''),
                 nullif(btrim({alias}.transit_destination), '')
             ) = %(offloading_point)s)
            AND (%(offloading_storage)s = '' OR
             {alias}.offloading_storage_display = %(offloading_storage)s)
            AND (%(truck_type)s = '' OR
             coalesce(nullif(btrim({alias}.truck_type), ''), 'Unknown')
             = %(truck_type)s)
        """

    def load_stock_dashboard(self, filters: StockFilters) -> dict[str, Any]:
        parameters = self._stock_parameters(filters)
        dimension_where = self._stock_dimension_where()
        range_where = """
            (
                (
                    loading_signed_off_at::date
                    BETWEEN %(movement_from)s::date AND %(movement_to)s::date
                )
                OR (
                    offloading_signed_off_at::date
                    BETWEEN %(movement_from)s::date AND %(movement_to)s::date
                )
            )
        """
        with self.connection() as connection:
            origin_positions = self._stock_positions(
                connection,
                parameters,
                "Origin",
            )
            destination_positions = self._stock_positions(
                connection,
                parameters,
                "Destination",
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    WITH base AS (
                        SELECT *
                        FROM ops.v_stock_reconciliation r
                        WHERE {dimension_where}
                    )
                    SELECT
                        coalesce(sum(loaded_tonnes) FILTER (
                            WHERE loading_signed_off_at::date
                              BETWEEN %(movement_from)s::date
                                  AND %(movement_to)s::date
                        ), 0) AS loaded_tonnes,
                        coalesce(sum(offloaded_tonnes) FILTER (
                            WHERE offloading_signed_off_at::date
                              BETWEEN %(movement_from)s::date
                                  AND %(movement_to)s::date
                        ), 0) AS offloaded_tonnes,
                        (
                            SELECT coalesce(sum(loaded_tonnes), 0)
                            FROM base
                            WHERE in_transit
                              AND loading_signed_off_at::date <= %(as_of)s::date
                        ) AS transit_tonnes,
                        coalesce(sum(variance_tonnes) FILTER (
                            WHERE offloaded_tonnes IS NOT NULL
                              AND offloading_signed_off_at::date
                                  BETWEEN %(movement_from)s::date
                                      AND %(movement_to)s::date
                        ), 0) AS variance_tonnes,
                        CASE
                            WHEN sum(loaded_tonnes) FILTER (
                                WHERE offloaded_tonnes IS NOT NULL
                                  AND offloading_signed_off_at::date
                                      BETWEEN %(movement_from)s::date
                                          AND %(movement_to)s::date
                            ) > 0
                            THEN round(
                                sum(offloaded_tonnes) FILTER (
                                    WHERE offloaded_tonnes IS NOT NULL
                                      AND offloading_signed_off_at::date
                                          BETWEEN %(movement_from)s::date
                                              AND %(movement_to)s::date
                                )
                                / sum(loaded_tonnes) FILTER (
                                    WHERE offloaded_tonnes IS NOT NULL
                                      AND offloading_signed_off_at::date
                                          BETWEEN %(movement_from)s::date
                                              AND %(movement_to)s::date
                                ) * 100.0,
                                3
                            )
                        END AS delivery_pct,
                        count(*) FILTER (
                            WHERE {range_where}
                              AND (
                                  duplicate_loading_attempts
                                  OR duplicate_offloading_attempts
                                  OR invalid_loading_slab
                                  OR (
                                      offloaded_tonnes IS NOT NULL
                                      AND invalid_offloading_slab
                                  )
                                  OR unmapped_client
                                  OR loading_validation_errors <> '[]'::jsonb
                                  OR (
                                      offloaded_tonnes IS NOT NULL
                                      AND offloading_validation_errors <> '[]'::jsonb
                                  )
                              )
                        ) AS exceptions
                    FROM base
                    """,
                    parameters,
                )
                movement_metrics = cursor.fetchone() or {}
                cursor.execute(
                    f"""
                    SELECT
                        job_reference,
                        order_reference,
                        client_name,
                        truck_registration,
                        truck_type,
                        loading_point,
                        loading_storage_display,
                        offloading_point,
                        offloading_storage_display,
                        loaded_tonnes,
                        offloaded_tonnes,
                        variance_tonnes,
                        delivery_pct,
                        minimum_delivery_pct,
                        variance_status,
                        in_transit,
                        duplicate_loading_attempts,
                        duplicate_offloading_attempts,
                        invalid_loading_slab,
                        invalid_offloading_slab,
                        unmapped_client,
                        loading_validation_errors,
                        offloading_validation_errors
                    FROM ops.v_stock_reconciliation r
                    WHERE {dimension_where}
                      AND {range_where}
                    ORDER BY
                        coalesce(offloading_signed_off_at, loading_signed_off_at)
                        DESC NULLS LAST,
                        job_reference
                    """,
                    parameters,
                )
                reconciliation = cursor.fetchall()
                charts = self._stock_charts(cursor, parameters, dimension_where)

        origin_points, origin_storage = _position_summaries(origin_positions)
        destination_points, destination_storage = _position_summaries(
            destination_positions
        )
        origin_opening = sum(
            Decimal(str(row.get("opening_tonnes") or 0))
            for row in origin_positions
        )
        origin_loaded = sum(
            Decimal(str(row.get("movement_tonnes") or 0))
            for row in origin_positions
        )
        origin_stock = sum(
            Decimal(str(row.get("stock_on_hand_tonnes") or 0))
            for row in origin_positions
        )
        destination_opening = sum(
            Decimal(str(row.get("opening_tonnes") or 0))
            for row in destination_positions
        )
        destination_offloaded = sum(
            Decimal(str(row.get("movement_tonnes") or 0))
            for row in destination_positions
        )
        destination_stock = sum(
            Decimal(str(row.get("stock_on_hand_tonnes") or 0))
            for row in destination_positions
        )
        metrics = {
            **movement_metrics,
            "origin_opening_tonnes": origin_opening,
            "origin_loaded_tonnes": origin_loaded,
            "origin_stock_on_hand_tonnes": origin_stock,
            "destination_opening_tonnes": destination_opening,
            "destination_offloaded_tonnes": destination_offloaded,
            "destination_stock_on_hand_tonnes": destination_stock,
            "incomplete_positions": sum(
                1
                for row in origin_positions + destination_positions
                if row.get("missing_opening_balance")
            ),
            "negative_positions": sum(
                1
                for row in origin_positions + destination_positions
                if row.get("negative_stock")
            ),
        }
        return {
            "metrics": {key: _clean_value(value or 0) for key, value in metrics.items()},
            "origin_positions": _clean_rows(origin_positions),
            "destination_positions": _clean_rows(destination_positions),
            "origin_point_positions": _clean_rows(origin_points),
            "destination_point_positions": _clean_rows(destination_points),
            "origin_storage_positions": _clean_rows(origin_storage),
            "destination_storage_positions": _clean_rows(destination_storage),
            "reconciliation": _clean_rows(reconciliation),
            **charts,
        }

    def _stock_positions(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        parameters: dict[str, Any],
        stock_role: str,
    ) -> list[dict[str, Any]]:
        if stock_role not in {"Origin", "Destination"}:
            raise ValueError(f"Unsupported stock role: {stock_role!r}")
        point_parameter = (
            "loading_point" if stock_role == "Origin" else "offloading_point"
        )
        storage_parameter = (
            "loading_storage" if stock_role == "Origin" else "offloading_storage"
        )
        scoped_parameters = {**parameters, "stock_role": stock_role}
        query = f"""
            WITH filtered_ledger AS (
                SELECT ledger.*
                FROM ops.v_stock_ledger ledger
                WHERE ledger.stock_role = %(stock_role)s
                  AND ledger.movement_at::date <= %(as_of)s::date
                  AND (%(order_reference)s = '' OR
                       coalesce(ledger.order_reference, '') ILIKE
                       '%%' || %(order_reference)s || '%%')
                  AND (%(client_name)s = '' OR
                       ledger.client_name = %(client_name)s)
                  AND (%({point_parameter})s = '' OR
                       ledger.location_name = %({point_parameter})s)
                  AND (%({storage_parameter})s = '' OR
                       ledger.storage_display = %({storage_parameter})s)
                  AND (%(truck_type)s = '' OR
                       coalesce(nullif(btrim(ledger.truck_type), ''), 'Unknown')
                       = %(truck_type)s)
            ),
            opening_ranked AS (
                SELECT
                    opening.*,
                    master.order_reference,
                    master.client_name,
                    row_number() OVER (
                        PARTITION BY
                            opening.stock_role,
                            opening.normalized_location,
                            opening.normalized_storage_identifier,
                            master.normalized_order_reference
                        ORDER BY opening.effective_date DESC, opening.id DESC
                    ) AS priority
                FROM ops.stock_opening_balances opening
                JOIN ops.order_master master ON master.id = opening.order_master_id
                WHERE opening.stock_role = %(stock_role)s
                  AND opening.effective_date <= %(as_of)s::date
                  AND (%(order_reference)s = '' OR
                       master.order_reference ILIKE
                       '%%' || %(order_reference)s || '%%')
                  AND (%(client_name)s = '' OR
                       master.client_name = %(client_name)s)
                  AND (%({point_parameter})s = '' OR
                       opening.location_name = %({point_parameter})s)
                  AND (
                      %({storage_parameter})s = ''
                      OR CASE
                          WHEN lower(btrim(opening.storage_identifier))
                               = lower(btrim(opening.location_name))
                              THEN 'Point-level / no slab'
                          ELSE opening.storage_identifier
                      END = %({storage_parameter})s
                  )
            ),
            latest_opening AS (
                SELECT * FROM opening_ranked WHERE priority = 1
            ),
            keys_raw AS (
                SELECT
                    lower(btrim(location_name)) AS location_key,
                    lower(btrim(storage_identifier)) AS storage_key,
                    upper(btrim(order_reference)) AS order_key,
                    max(location_name) AS location_name,
                    max(storage_identifier) AS storage_identifier,
                    max(storage_display) AS storage_display,
                    max(order_reference) AS order_reference,
                    max(client_name) AS client_name
                FROM filtered_ledger
                WHERE nullif(btrim(location_name), '') IS NOT NULL
                  AND nullif(btrim(storage_identifier), '') IS NOT NULL
                  AND nullif(btrim(order_reference), '') IS NOT NULL
                GROUP BY 1, 2, 3
                UNION ALL
                SELECT
                    normalized_location,
                    normalized_storage_identifier,
                    upper(btrim(order_reference)),
                    location_name,
                    storage_identifier,
                    CASE
                        WHEN normalized_storage_identifier = normalized_location
                            THEN 'Point-level / no slab'
                        ELSE storage_identifier
                    END,
                    order_reference,
                    client_name
                FROM latest_opening
            ),
            keys AS (
                SELECT
                    location_key,
                    storage_key,
                    order_key,
                    max(location_name) AS location_name,
                    max(storage_identifier) AS storage_identifier,
                    max(storage_display) AS storage_display,
                    max(order_reference) AS order_reference,
                    max(client_name) AS client_name
                FROM keys_raw
                GROUP BY location_key, storage_key, order_key
            )
            SELECT
                %(stock_role)s AS stock_role,
                keys.location_name,
                keys.storage_identifier,
                keys.storage_display,
                keys.order_reference,
                keys.client_name,
                opening.effective_date,
                coalesce(opening.opening_tonnes, 0) AS opening_tonnes,
                abs(coalesce(sum(ledger.quantity_tonnes) FILTER (
                    WHERE ledger.movement_at::date >= coalesce(
                        opening.effective_date,
                        %(baseline_from)s::date
                    )
                ), 0)) AS movement_tonnes,
                coalesce(sum(ledger.quantity_tonnes) FILTER (
                    WHERE ledger.movement_at::date >= coalesce(
                        opening.effective_date,
                        %(baseline_from)s::date
                    )
                ), 0) AS net_movement_tonnes,
                coalesce(opening.opening_tonnes, 0)
                  + coalesce(sum(ledger.quantity_tonnes) FILTER (
                        WHERE ledger.movement_at::date >= coalesce(
                            opening.effective_date,
                            %(baseline_from)s::date
                        )
                    ), 0) AS stock_on_hand_tonnes,
                opening.id IS NULL AS missing_opening_balance,
                (
                    coalesce(opening.opening_tonnes, 0)
                    + coalesce(sum(ledger.quantity_tonnes) FILTER (
                        WHERE ledger.movement_at::date >= coalesce(
                            opening.effective_date,
                            %(baseline_from)s::date
                        )
                    ), 0)
                ) < 0 AS negative_stock
            FROM keys
            LEFT JOIN latest_opening opening
              ON opening.stock_role = %(stock_role)s
             AND opening.normalized_location = keys.location_key
             AND opening.normalized_storage_identifier = keys.storage_key
             AND upper(btrim(opening.order_reference)) = keys.order_key
            LEFT JOIN filtered_ledger ledger
              ON lower(btrim(ledger.location_name)) = keys.location_key
             AND lower(btrim(ledger.storage_identifier)) = keys.storage_key
             AND upper(btrim(ledger.order_reference)) = keys.order_key
            GROUP BY
                keys.location_name,
                keys.storage_identifier,
                keys.storage_display,
                keys.order_reference,
                keys.client_name,
                opening.id,
                opening.effective_date,
                opening.opening_tonnes
            ORDER BY
                keys.location_name,
                keys.storage_identifier,
                keys.order_reference
        """
        with connection.cursor() as cursor:
            cursor.execute(query, scoped_parameters)
            return cursor.fetchall()

    @staticmethod
    def _stock_charts(
        cursor: psycopg.Cursor[dict[str, Any]],
        parameters: dict[str, Any],
        dimension_where: str,
    ) -> dict[str, list[dict[str, Any]]]:
        cursor.execute(
            f"""
            SELECT
                r.loading_point AS point,
                count(*) AS job_count,
                count(DISTINCT r.order_reference) AS order_count,
                coalesce(sum(r.loaded_tonnes), 0) AS loaded_tonnes
            FROM ops.v_stock_reconciliation r
            WHERE {dimension_where}
              AND r.loading_signed_off_at::date
                  BETWEEN %(movement_from)s::date AND %(movement_to)s::date
              AND nullif(btrim(r.loading_point), '') IS NOT NULL
            GROUP BY 1
            ORDER BY loaded_tonnes DESC, point
            """,
            parameters,
        )
        origin_points = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT
                r.loading_point AS point,
                r.loading_storage_display AS storage,
                r.loading_storage_scope AS storage_scope,
                count(*) AS job_count,
                count(DISTINCT r.order_reference) AS order_count,
                coalesce(sum(r.loaded_tonnes), 0) AS loaded_tonnes
            FROM ops.v_stock_reconciliation r
            WHERE {dimension_where}
              AND r.loading_signed_off_at::date
                  BETWEEN %(movement_from)s::date AND %(movement_to)s::date
              AND nullif(btrim(r.loading_point), '') IS NOT NULL
            GROUP BY 1, 2, 3
            ORDER BY point, loaded_tonnes DESC, storage
            """,
            parameters,
        )
        origin_storage = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT
                r.offloading_point AS point,
                count(*) AS job_count,
                count(DISTINCT r.order_reference) AS order_count,
                coalesce(sum(r.offloaded_tonnes), 0) AS offloaded_tonnes
            FROM ops.v_stock_reconciliation r
            WHERE {dimension_where}
              AND r.offloading_signed_off_at::date
                  BETWEEN %(movement_from)s::date AND %(movement_to)s::date
              AND nullif(btrim(r.offloading_point), '') IS NOT NULL
            GROUP BY 1
            ORDER BY offloaded_tonnes DESC, point
            """,
            parameters,
        )
        destination_points = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT
                r.offloading_point AS point,
                r.offloading_storage_display AS storage,
                r.offloading_storage_scope AS storage_scope,
                count(*) AS job_count,
                count(DISTINCT r.order_reference) AS order_count,
                coalesce(sum(r.offloaded_tonnes), 0) AS offloaded_tonnes
            FROM ops.v_stock_reconciliation r
            WHERE {dimension_where}
              AND r.offloading_signed_off_at::date
                  BETWEEN %(movement_from)s::date AND %(movement_to)s::date
              AND nullif(btrim(r.offloading_point), '') IS NOT NULL
            GROUP BY 1, 2, 3
            ORDER BY point, offloaded_tonnes DESC, storage
            """,
            parameters,
        )
        destination_storage = cursor.fetchall()
        cursor.execute(
            f"""
            WITH scoped AS (
                SELECT *
                FROM ops.v_stock_reconciliation r
                WHERE {dimension_where}
                  AND (
                      r.loading_signed_off_at::date
                          BETWEEN %(movement_from)s::date
                              AND %(movement_to)s::date
                      OR r.offloading_signed_off_at::date
                          BETWEEN %(movement_from)s::date
                              AND %(movement_to)s::date
                  )
            )
            SELECT
                coalesce(nullif(btrim(transit_origin), ''), 'Unknown') AS origin,
                coalesce(
                    nullif(btrim(offloading_point), ''),
                    nullif(btrim(transit_destination), ''),
                    'Unknown'
                )
                    AS destination,
                count(*) AS reference_count,
                count(DISTINCT upper(btrim(truck_registration))) FILTER (
                    WHERE nullif(btrim(truck_registration), '') IS NOT NULL
                ) AS trucks,
                coalesce(sum(loaded_tonnes) FILTER (
                    WHERE loading_signed_off_at::date
                        BETWEEN %(movement_from)s::date
                            AND %(movement_to)s::date
                ), 0) AS loaded_tonnes,
                coalesce(sum(offloaded_tonnes) FILTER (
                    WHERE offloading_signed_off_at::date
                        BETWEEN %(movement_from)s::date
                            AND %(movement_to)s::date
                ), 0) AS offloaded_tonnes,
                coalesce(sum(variance_tonnes) FILTER (
                    WHERE offloaded_tonnes IS NOT NULL
                      AND offloading_signed_off_at::date
                        BETWEEN %(movement_from)s::date
                            AND %(movement_to)s::date
                ), 0) AS variance_tonnes
            FROM scoped
            GROUP BY 1, 2
            ORDER BY loaded_tonnes DESC, origin, destination
            """,
            parameters,
        )
        lanes = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT
                coalesce(order_reference, 'Unmapped') AS category,
                max(client_name) AS client_name,
                coalesce(sum(loaded_tonnes) FILTER (
                    WHERE offloaded_tonnes IS NOT NULL
                ), 0) AS loaded_tonnes,
                coalesce(sum(offloaded_tonnes), 0) AS offloaded_tonnes,
                CASE
                    WHEN sum(loaded_tonnes) FILTER (
                        WHERE offloaded_tonnes IS NOT NULL
                    ) > 0
                    THEN round(
                        sum(offloaded_tonnes)
                        / sum(loaded_tonnes) FILTER (
                            WHERE offloaded_tonnes IS NOT NULL
                        ) * 100.0,
                        3
                    )
                END AS delivery_pct,
                max(minimum_delivery_pct) AS minimum_delivery_pct
            FROM ops.v_stock_reconciliation r
            WHERE {dimension_where}
              AND offloading_signed_off_at::date
                  BETWEEN %(movement_from)s::date AND %(movement_to)s::date
            GROUP BY order_reference
            ORDER BY loaded_tonnes DESC
            LIMIT 30
            """,
            parameters,
        )
        orders = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT
                ledger.movement_at::date AS activity_date,
                coalesce(sum(abs(ledger.quantity_tonnes)) FILTER (
                    WHERE ledger.movement_type = 'Loading'
                ), 0) AS loaded_tonnes,
                coalesce(sum(ledger.quantity_tonnes) FILTER (
                    WHERE ledger.movement_type = 'Offloading'
                ), 0) AS offloaded_tonnes
            FROM ops.v_stock_ledger ledger
            JOIN ops.v_stock_reconciliation r
              ON r.allocation_id = ledger.allocation_id
            WHERE ledger.movement_at::date
                  BETWEEN %(movement_from)s::date AND %(movement_to)s::date
              AND {dimension_where}
            GROUP BY 1
            ORDER BY 1
            """,
            parameters,
        )
        daily = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT
                coalesce(nullif(btrim(r.truck_type), ''), 'Unknown') AS category,
                coalesce(sum(r.loaded_tonnes) FILTER (
                    WHERE r.loading_signed_off_at::date
                      BETWEEN %(movement_from)s::date AND %(movement_to)s::date
                ), 0) AS loaded_tonnes,
                coalesce(sum(r.offloaded_tonnes) FILTER (
                    WHERE r.offloading_signed_off_at::date
                      BETWEEN %(movement_from)s::date AND %(movement_to)s::date
                ), 0) AS offloaded_tonnes
            FROM ops.v_stock_reconciliation r
            WHERE {dimension_where}
            GROUP BY 1
            ORDER BY loaded_tonnes DESC
            """,
            parameters,
        )
        truck_types = cursor.fetchall()
        return {
            "origin_points": _clean_rows(origin_points),
            "origin_storage": _clean_rows(origin_storage),
            "destination_points": _clean_rows(destination_points),
            "destination_storage": _clean_rows(destination_storage),
            "lanes": _clean_rows(lanes),
            "orders": _clean_rows(orders),
            "daily": _clean_rows(daily),
            "truck_types": _clean_rows(truck_types),
        }

    def stock_filter_options(self) -> dict[str, list[str]]:
        queries = {
            "clients": """
                SELECT DISTINCT client_name AS value
                FROM ops.order_master WHERE active ORDER BY value
            """,
            "loading_points": """
                SELECT DISTINCT loading_point AS value
                FROM ops.v_stock_reconciliation
                WHERE nullif(btrim(loading_point), '') IS NOT NULL ORDER BY value
            """,
            "offloading_points": """
                SELECT DISTINCT coalesce(
                    nullif(btrim(offloading_point), ''),
                    nullif(btrim(transit_destination), '')
                ) AS value
                FROM ops.v_stock_reconciliation
                WHERE coalesce(
                    nullif(btrim(offloading_point), ''),
                    nullif(btrim(transit_destination), '')
                ) IS NOT NULL
                ORDER BY value
            """,
            "loading_storage": """
                SELECT DISTINCT loading_storage_display AS value
                FROM ops.v_stock_reconciliation
                WHERE nullif(btrim(loading_storage_display), '') IS NOT NULL
                ORDER BY value
            """,
            "offloading_storage": """
                SELECT DISTINCT offloading_storage_display AS value
                FROM ops.v_stock_reconciliation
                WHERE nullif(btrim(offloading_storage_display), '') IS NOT NULL
                ORDER BY value
            """,
            "truck_types": """
                SELECT DISTINCT
                    coalesce(nullif(btrim(truck_type), ''), 'Unknown') AS value
                FROM ops.v_stock_reconciliation ORDER BY value
            """,
        }
        result: dict[str, list[str]] = {}
        with self.connection() as connection:
            with connection.cursor() as cursor:
                for key, query in queries.items():
                    cursor.execute(query)
                    result[key] = [str(row["value"]) for row in cursor.fetchall()]
        return result

    def order_investigation_options(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        btrim(order_reference) AS order_reference,
                        max(nullif(btrim(client_name), '')) AS client_name,
                        count(*) AS allocations,
                        min(
                            (
                                transport_allocation_created_at
                                AT TIME ZONE 'Africa/Johannesburg'
                            )::date
                        ) AS first_root_date,
                        max(
                            (
                                transport_allocation_created_at
                                AT TIME ZONE 'Africa/Johannesburg'
                            )::date
                        ) AS last_root_date
                    FROM ops.v_stock_reconciliation
                    WHERE nullif(btrim(order_reference), '') IS NOT NULL
                    GROUP BY btrim(order_reference)
                    ORDER BY last_root_date DESC, order_reference
                    """
                )
                return cursor.fetchall()

    def load_order_investigation(
        self,
        order_reference: str,
        date_from: date,
        date_to: date,
        expected_bays: tuple[str, ...],
        route_plan: RoutePlan = (),
        parcel_tonnes: Decimal | int | float = 0,
        event_cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        order_reference = order_reference.strip()
        if not order_reference:
            raise ValueError("Order reference is required.")
        if date_from > date_to:
            raise ValueError("Study start date cannot be after its end date.")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH selected AS (
                        SELECT
                            r.*,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.loading_job_id
                                ELSE historical_loading.job_id
                            END AS study_loading_job_id,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.loading_point
                                ELSE historical_loading.loading_point
                            END AS study_loading_point,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.loading_slab
                                ELSE historical_loading.loading_slab
                            END AS study_loading_slab,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.loaded_tonnes
                                ELSE historical_loading.nett_weight_tonnes
                            END AS study_loaded_tonnes,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.loading_signed_off_at
                                ELSE historical_loading.signed_off_at
                            END AS study_loading_signed_off_at,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.transit_destination
                                ELSE coalesce(
                                    nullif(
                                        btrim(historical_loading.offloading_point),
                                        ''
                                    ),
                                    r.allocation_offloading_point
                                )
                            END AS study_transit_destination,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.planned_offloading_slab
                                ELSE historical_loading.offloading_slab
                            END AS study_planned_offloading_slab,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.loading_signed_off_attempts
                                ELSE historical_loading.signed_off_attempt_count
                            END AS study_loading_signed_off_attempts,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.loading_validation_errors
                                ELSE historical_loading.validation_errors
                            END AS study_loading_validation_errors,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.offloading_job_id
                                ELSE historical_offloading.job_id
                            END AS study_offloading_job_id,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.offloading_point
                                ELSE historical_offloading.offloading_point
                            END AS study_offloading_point,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.offloading_slab
                                ELSE historical_offloading.offloading_slab
                            END AS study_offloading_slab,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL THEN r.offloaded_tonnes
                                ELSE historical_offloading.nett_weight_tonnes
                            END AS study_offloaded_tonnes,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.offloading_signed_off_at
                                ELSE historical_offloading.signed_off_at
                            END AS study_offloading_signed_off_at,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.offloading_signed_off_attempts
                                ELSE historical_offloading.signed_off_attempt_count
                            END AS study_offloading_signed_off_attempts,
                            CASE
                                WHEN %(event_cutoff)s::timestamptz IS NULL
                                    THEN r.offloading_validation_errors
                                ELSE historical_offloading.validation_errors
                            END AS study_offloading_validation_errors
                        FROM ops.v_stock_reconciliation r
                        LEFT JOIN LATERAL (
                            SELECT
                                attempt.job_id,
                                coalesce(
                                    attempt.source_signed_off_at,
                                    attempt.source_completed_at
                                ) AS signed_off_at,
                                fact.loading_point,
                                fact.offloading_point,
                                fact.loading_slab,
                                fact.offloading_slab,
                                fact.nett_weight_tonnes,
                                fact.validation_errors,
                                count(*) OVER () AS signed_off_attempt_count
                            FROM ops.v_workflow_attempts attempt
                            JOIN ops.checklist_operational_facts fact
                              ON fact.job_id = attempt.job_id
                            WHERE %(event_cutoff)s::timestamptz IS NOT NULL
                              AND attempt.allocation_id = r.allocation_id
                              AND attempt.stage_code = 'loading_exit'
                              AND attempt.status_group = 'Signed off'
                              AND coalesce(
                                    attempt.source_signed_off_at,
                                    attempt.source_completed_at
                                  ) <= %(event_cutoff)s::timestamptz
                            ORDER BY
                                coalesce(
                                    attempt.source_signed_off_at,
                                    attempt.source_completed_at
                                ) DESC NULLS LAST,
                                attempt.chronology_at DESC,
                                attempt.job_id DESC
                            LIMIT 1
                        ) historical_loading ON true
                        LEFT JOIN LATERAL (
                            SELECT
                                attempt.job_id,
                                coalesce(
                                    attempt.source_signed_off_at,
                                    attempt.source_completed_at
                                ) AS signed_off_at,
                                fact.offloading_point,
                                fact.offloading_slab,
                                fact.nett_weight_tonnes,
                                fact.validation_errors,
                                count(*) OVER () AS signed_off_attempt_count
                            FROM ops.v_workflow_attempts attempt
                            JOIN ops.checklist_operational_facts fact
                              ON fact.job_id = attempt.job_id
                            WHERE %(event_cutoff)s::timestamptz IS NOT NULL
                              AND attempt.allocation_id = r.allocation_id
                              AND attempt.stage_code = 'offloading_exit'
                              AND attempt.status_group = 'Signed off'
                              AND coalesce(
                                    attempt.source_signed_off_at,
                                    attempt.source_completed_at
                                  ) <= %(event_cutoff)s::timestamptz
                            ORDER BY
                                coalesce(
                                    attempt.source_signed_off_at,
                                    attempt.source_completed_at
                                ) DESC NULLS LAST,
                                attempt.chronology_at DESC,
                                attempt.job_id DESC
                            LIMIT 1
                        ) historical_offloading ON true
                    )
                    SELECT
                        r.job_reference,
                        r.order_reference,
                        r.client_name,
                        r.transport_allocation_created_at,
                        r.truck_registration,
                        r.truck_type,
                        r.driver_name,
                        r.transporter_name,
                        r.allocation_loading_point,
                        r.allocation_offloading_point,
                        r.study_loading_job_id AS loading_job_id,
                        r.study_loading_point AS loading_point,
                        r.study_loading_slab AS loading_slab,
                        r.study_loaded_tonnes AS loaded_tonnes,
                        r.study_loading_signed_off_at AS loading_signed_off_at,
                        loading_job.operator_name AS loading_operator,
                        latest_loading.job_id AS latest_loading_job_id,
                        latest_loading.status_group AS latest_loading_status,
                        latest_loading.opus_status AS latest_loading_opus_status,
                        r.study_transit_destination AS transit_destination,
                        r.study_planned_offloading_slab AS planned_offloading_slab,
                        r.study_offloading_job_id AS offloading_job_id,
                        r.study_offloading_point AS offloading_point,
                        r.study_offloading_slab AS offloading_slab,
                        r.study_offloaded_tonnes AS offloaded_tonnes,
                        r.study_offloading_signed_off_at
                            AS offloading_signed_off_at,
                        offloading_job.operator_name AS offloading_operator,
                        latest_offloading.job_id AS latest_offloading_job_id,
                        latest_offloading.status_group AS latest_offloading_status,
                        latest_offloading.opus_status AS latest_offloading_opus_status,
                        CASE
                            WHEN r.study_loaded_tonnes IS NOT NULL
                             AND r.study_offloaded_tonnes IS NOT NULL
                            THEN round(
                                r.study_offloaded_tonnes - r.study_loaded_tonnes,
                                3
                            )
                        END AS variance_tonnes,
                        CASE
                            WHEN r.study_loaded_tonnes > 0
                             AND r.study_offloaded_tonnes IS NOT NULL
                            THEN round(
                                r.study_offloaded_tonnes
                                / r.study_loaded_tonnes
                                * 100.0,
                                3
                            )
                        END AS delivery_pct,
                        r.minimum_delivery_pct,
                        CASE
                            WHEN r.study_loaded_tonnes IS NULL
                                THEN 'Missing loading weight'
                            WHEN r.study_offloaded_tonnes IS NULL
                                THEN 'Pending / in transit'
                            WHEN (
                                r.study_offloaded_tonnes
                                / nullif(r.study_loaded_tonnes, 0)
                            ) * 100.0 >= r.minimum_delivery_pct
                                THEN 'Within tolerance'
                            ELSE 'Below tolerance'
                        END AS variance_status,
                        r.in_transit,
                        r.current_checklist,
                        r.current_opus_status,
                        r.current_status_group,
                        r.transit_exclusion_reason,
                        r.study_loading_signed_off_attempts
                            AS loading_signed_off_attempts,
                        r.study_offloading_signed_off_attempts
                            AS offloading_signed_off_attempts,
                        coalesce(r.study_loading_signed_off_attempts, 0) > 1
                            AS duplicate_loading_attempts,
                        coalesce(r.study_offloading_signed_off_attempts, 0) > 1
                            AS duplicate_offloading_attempts,
                        (
                            nullif(btrim(r.study_loading_slab), '') IS NULL
                            OR btrim(r.study_loading_slab) = '0'
                        ) AS invalid_loading_slab,
                        (
                            nullif(btrim(r.study_offloading_slab), '') IS NULL
                            OR btrim(r.study_offloading_slab) = '0'
                        ) AS invalid_offloading_slab,
                        r.study_loading_validation_errors
                            AS loading_validation_errors,
                        r.study_offloading_validation_errors
                            AS offloading_validation_errors
                    FROM selected r
                    LEFT JOIN ops.jobs loading_job
                      ON loading_job.id = r.study_loading_job_id
                    LEFT JOIN ops.jobs offloading_job
                      ON offloading_job.id = r.study_offloading_job_id
                    LEFT JOIN LATERAL (
                        SELECT
                            attempt.job_id,
                            attempt.status_group,
                            attempt.opus_status
                        FROM ops.v_workflow_attempts attempt
                        WHERE attempt.allocation_id = r.allocation_id
                          AND attempt.stage_code = 'loading_exit'
                          AND (
                              %(event_cutoff)s::timestamptz IS NULL
                              OR coalesce(
                                  attempt.source_signed_off_at,
                                  attempt.source_completed_at,
                                  attempt.chronology_at
                              ) <= %(event_cutoff)s::timestamptz
                          )
                        ORDER BY attempt.chronology_at DESC, attempt.job_id DESC
                        LIMIT 1
                    ) latest_loading ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            attempt.job_id,
                            attempt.status_group,
                            attempt.opus_status
                        FROM ops.v_workflow_attempts attempt
                        WHERE attempt.allocation_id = r.allocation_id
                          AND attempt.stage_code = 'offloading_exit'
                          AND (
                              %(event_cutoff)s::timestamptz IS NULL
                              OR coalesce(
                                  attempt.source_signed_off_at,
                                  attempt.source_completed_at,
                                  attempt.chronology_at
                              ) <= %(event_cutoff)s::timestamptz
                          )
                        ORDER BY attempt.chronology_at DESC, attempt.job_id DESC
                        LIMIT 1
                    ) latest_offloading ON true
                    WHERE upper(btrim(coalesce(r.order_reference, '')))
                              = upper(btrim(%(order_reference)s))
                      AND (
                          r.transport_allocation_created_at
                          AT TIME ZONE 'Africa/Johannesburg'
                      )::date BETWEEN %(date_from)s::date AND %(date_to)s::date
                    ORDER BY
                        r.transport_allocation_created_at,
                        r.job_reference
                    """,
                    {
                        "order_reference": order_reference,
                        "date_from": date_from,
                        "date_to": date_to,
                        "event_cutoff": event_cutoff,
                    },
                )
                rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT DISTINCT ON (
                        b.stock_role,
                        b.normalized_location,
                        b.normalized_storage_identifier
                    )
                        b.effective_date,
                        b.stock_role,
                        b.location_name,
                        b.storage_identifier,
                        b.opening_tonnes
                    FROM ops.stock_opening_balances b
                    JOIN ops.order_master o ON o.id = b.order_master_id
                    WHERE o.normalized_order_reference
                              = upper(btrim(%(order_reference)s))
                      AND b.effective_date <= %(date_from)s::date
                    ORDER BY
                        b.stock_role,
                        b.normalized_location,
                        b.normalized_storage_identifier,
                        b.effective_date DESC,
                        b.id DESC
                    """,
                    {
                        "order_reference": order_reference,
                        "date_from": date_from,
                    },
                )
                opening_balances = cursor.fetchall()
        study = _order_study(
            rows,
            order_reference=order_reference,
            date_from=date_from,
            date_to=date_to,
            expected_bays=expected_bays,
            opening_balances=opening_balances,
            route_plan=route_plan,
            parcel_tonnes=parcel_tonnes,
            event_cutoff=event_cutoff,
        )
        return {
            **study,
            "date_from": _clean_value(study["date_from"]),
            "date_to": _clean_value(study["date_to"]),
            "first_root_date": _clean_value(study["first_root_date"]),
            "last_root_date": _clean_value(study["last_root_date"]),
            "event_cutoff": _clean_value(study["event_cutoff"]),
            "metrics": {
                key: _clean_value(value)
                for key, value in study["metrics"].items()
            },
            "routes": _clean_rows(study["routes"]),
            "legs": _clean_rows(study["legs"]),
            "leg_lanes": _clean_rows(study["leg_lanes"]),
            "route_plan": _clean_rows(study["route_plan"]),
            "staging": _clean_rows(study["staging"]),
            "bc_areas": _clean_rows(study["bc_areas"]),
            "bay_flows": _clean_rows(study["bay_flows"]),
            "bay_chart": _clean_rows(study["bay_chart"]),
            "location_balances": _clean_rows(study["location_balances"]),
            "load_types": _clean_rows(study["load_types"]),
            "loading_checklists": _clean_rows(study["loading_checklists"]),
            "offloading_checklists": _clean_rows(study["offloading_checklists"]),
            "daily": _clean_rows(study["daily"]),
            "movements": _clean_rows(study["movements"]),
            "exceptions": _clean_rows(study["exceptions"]),
            "allocation_gaps": _clean_rows(study["allocation_gaps"]),
        }

    def export_order_investigation_workbook(
        self,
        path: Path,
        order_reference: str,
        date_from: date,
        date_to: date,
        expected_bays: tuple[str, ...],
        route_plan: RoutePlan = (),
        parcel_tonnes: Decimal | int | float = 0,
    ) -> dict[str, int]:
        study = self.load_order_investigation(
            order_reference,
            date_from,
            date_to,
            expected_bays,
            route_plan,
            parcel_tonnes,
        )
        writer = XlsxStreamWriter()
        metric_labels = {
            "parcel_tonnes": "Parcel allocation tonnes",
            "opening_stock_tonnes": "Opening stock tonnes",
            "order_location_soh_tonnes": "Known order-location SOH tonnes",
            "order_locations": "Locations included in known SOH",
            "unresolved_soh_locations": "Unresolved intermediate location balances",
            "in_transit_tonnes": "Strict in-transit tonnes shown separately",
            "loading_exit_signed": "Signed Loading and Exit checklists",
            "offloading_exit_signed": "Signed Offloading and Exit checklists",
            "completed_checklist_chains": "Completed loading-to-offloading chains",
            "review_references": "Unique references requiring review",
            "allocations": "Transport Allocation references",
            "movement_references": "References with movement",
            "allocations_without_loading": "Allocations without signed-off loading",
            "pending_offloads": "Loaded without signed-off offloading",
            "unexpected_bay_movements": "Movements using unexpected bays",
            "unlisted_route_movements": "Movements outside configured route/bay plan",
            "weight_variance_exceptions": "Movements outside 0.250% weight variance",
        }
        if route_plan:
            metric_labels.update(
                {
                    "bcf_stock_tonnes": "Current governed BCF stock tonnes",
                    "bc_stock_tonnes": "Current governed BC stock tonnes",
                }
            )
        summary_metric_keys = tuple(metric_labels)
        summary_rows: list[tuple[Any, ...]] = [
            ("Study", "Order", order_reference),
            ("Study", "Root date from", study["date_from"]),
            ("Study", "Root date to", study["date_to"]),
            (
                "Method",
                "Order-specific checklist reconciliation",
                (
                    "Signed Loading and Exit dispatches and linked signed Offloading "
                    "and Exit receipts grouped by the selected order's actual points "
                    "and bays"
                ),
            ),
            ("Coverage", "First stored root", study["first_root_date"]),
            ("Coverage", "Last stored root", study["last_root_date"]),
            ("Coverage", "Warning", study["coverage_warning"]),
            ("Coverage", "Note", study["coverage_note"]),
        ]
        summary_rows.extend(
            ("Expected bay", f"Bay {index}", bay)
            for index, bay in enumerate(expected_bays, start=1)
        )
        summary_rows.extend(
            ("KPI", metric_labels[key], value)
            for key in summary_metric_keys
            if (value := study["metrics"].get(key)) is not None
        )
        writer.add_sheet(
            "Summary",
            ("Category", "Metric", "Value"),
            summary_rows,
        )

        def add_rows(
            title: str,
            rows: list[dict[str, Any]],
            columns: tuple[tuple[str, str], ...],
        ) -> None:
            writer.add_sheet(
                title,
                tuple(label for _key, label in columns),
                (
                    tuple(row.get(key) for key, _label in columns)
                    for row in rows
                ),
            )

        add_rows(
            "Physical Legs",
            study["legs"],
            (
                ("route_name", "Physical Leg"),
                ("movement_references", "Movement References"),
                ("loaded_movements", "Dispatched Movements"),
                ("offloaded_movements", "Received Movements"),
                ("loaded_tonnes", "Dispatched Tonnes"),
                ("offloaded_tonnes", "Received Tonnes"),
                ("pending_references", "Awaiting Receipt References"),
                ("pending_loaded_tonnes", "Awaiting Receipt Tonnes"),
                ("movement_difference_tonnes", "Received Less Dispatched Tonnes"),
                ("completed_variance_tonnes", "Completed Movement Variance Tonnes"),
                ("delivery_pct", "Completed Delivery %"),
            ),
        )
        add_rows(
            "Leg Bay Detail",
            study["leg_lanes"],
            (
                ("route_name", "Physical Leg"),
                ("origin", "Origin"),
                ("from_bay", "From Bay / Area"),
                ("destination", "Destination"),
                ("to_bay", "To Bay / Area"),
                ("movement_references", "Movement References"),
                ("loaded_tonnes", "Dispatched Tonnes"),
                ("offloaded_tonnes", "Received Tonnes"),
                ("pending_references", "Awaiting Receipt References"),
                ("pending_loaded_tonnes", "Awaiting Receipt Tonnes"),
                ("movement_difference_tonnes", "Received Less Dispatched Tonnes"),
            ),
        )
        add_rows(
            "Route Plan",
            study["route_plan"],
            (
                ("plan_status", "Route Classification"),
                ("route_name", "Physical Leg"),
                ("origin", "Origin"),
                ("from_bay", "From Bay"),
                ("destination", "Destination"),
                ("to_bay", "To Bay"),
                ("movement_references", "Movement References"),
                ("loaded_movements", "Loaded Movements"),
                ("offloaded_movements", "Offloaded Movements"),
                ("loaded_tonnes", "Loaded Tonnes"),
                ("offloaded_tonnes", "Offloaded Tonnes"),
            ),
        )
        add_rows(
            "Routes",
            study["routes"],
            (
                ("route_name", "Supply Chain Leg"),
                ("origin", "Origin"),
                ("destination", "Destination"),
                ("movement_references", "Movement References"),
                ("loaded_movements", "Loaded Movements"),
                ("offloaded_movements", "Offloaded Movements"),
                ("loaded_tonnes", "Loaded Tonnes"),
                ("offloaded_tonnes", "Offloaded Tonnes"),
                ("net_difference_tonnes", "Offloaded Less All Loaded Tonnes"),
                ("completed_variance_tonnes", "Completed Variance Tonnes"),
                ("delivery_pct", "Completed Delivery %"),
            ),
        )
        add_rows(
            "Load Type Split",
            study["load_types"],
            (
                ("truck_type", "Truck Type"),
                ("load_count", "Signed Loads"),
                ("load_pct", "Load Split %"),
                ("loaded_tonnes", "Loaded Nett Tonnes"),
            ),
        )
        add_rows(
            "Location SOH",
            study["location_balances"],
            (
                ("location_role", "Location Role"),
                ("location", "Order Location"),
                ("bay", "Bay / Storage"),
                ("opening_status", "Opening Source"),
                ("opening_tonnes", "Opening Tonnes"),
                ("received_tonnes", "Signed Offloading Receipts Tonnes"),
                ("dispatched_tonnes", "Signed Loading Dispatches Tonnes"),
                ("movement_balance_tonnes", "Movement Balance Tonnes"),
                ("soh_tonnes", "Known Order SOH Tonnes"),
                ("balance_status", "Balance Status"),
            ),
        )
        add_rows(
            "Loading and Exit",
            study["loading_checklists"],
            (
                ("job_reference", "Job Reference"),
                ("allocation_loading_point", "Allocated Loading Point"),
                ("allocation_offloading_point", "Allocated Offloading Point"),
                ("latest_status", "Latest Checklist Status"),
                ("latest_opus_status", "Latest OPUS Status"),
                ("evidence_status", "Signed Evidence"),
                ("loading_point", "Checklist Loading Point"),
                ("loading_bay", "Loading Bay"),
                ("nett_weight_tonnes", "Loaded Nett Tonnes"),
                ("operator_name", "Loading Operator"),
                ("signed_off_at", "Signed Off At"),
                ("signed_off_attempts", "Signed Off Attempts"),
                ("validation_errors", "Validation Errors"),
            ),
        )
        add_rows(
            "Offloading and Exit",
            study["offloading_checklists"],
            (
                ("job_reference", "Job Reference"),
                ("planned_offloading_point", "Planned Offloading Point"),
                ("planned_offloading_bay", "Planned Offloading Bay"),
                ("latest_status", "Latest Checklist Status"),
                ("latest_opus_status", "Latest OPUS Status"),
                ("evidence_status", "Signed Evidence"),
                ("offloading_point", "Checklist Offloading Point"),
                ("offloading_bay", "Offloading Bay"),
                ("nett_weight_tonnes", "Offloaded Nett Tonnes"),
                ("operator_name", "Offloading Operator"),
                ("signed_off_at", "Signed Off At"),
                ("signed_off_attempts", "Signed Off Attempts"),
                ("validation_errors", "Validation Errors"),
            ),
        )
        if route_plan:
            add_rows(
                "BCF Staging",
                study["staging"],
                (
                    ("bay", "BCF Bay / Area"),
                    ("effective_date", "Opening Effective Date"),
                    ("opening_status", "Opening Source"),
                    ("opening_tonnes", "Opening Tonnes"),
                    ("mine_receipts_tonnes", "Mine Receipts Tonnes"),
                    ("other_receipts_tonnes", "Other Receipts Tonnes"),
                    ("bcf_dispatch_tonnes", "Loaded To BC Tonnes"),
                    ("movement_delta_tonnes", "Net Movement Tonnes"),
                    ("closing_tonnes", "Current SOH Tonnes"),
                ),
            )
            add_rows(
                "BC Areas",
                study["bc_areas"],
                (
                    ("area", "BC Bay / Area"),
                    ("effective_date", "Opening Effective Date"),
                    ("opening_status", "Opening Source"),
                    ("opening_tonnes", "Opening Tonnes"),
                    ("received_from_bcf_tonnes", "Received From BCF Tonnes"),
                    (
                        "received_direct_from_mine_tonnes",
                        "Received Direct From Mine Tonnes",
                    ),
                    ("other_receipts_tonnes", "Other Receipts Tonnes"),
                    ("total_received_tonnes", "Total Received Tonnes"),
                    ("closing_tonnes", "Current SOH Tonnes"),
                ),
            )
        add_rows(
            "Bay Flow",
            study["bay_flows"],
            (
                ("direction", "Direction"),
                ("point", "Location"),
                ("bay", "Bay / Location Fallback"),
                ("storage_source", "Storage Source"),
                ("expected_status", "Expected Status"),
                ("movement_count", "Movement Count"),
                ("tonnes", "Tonnes"),
            ),
        )
        add_rows(
            "Daily Totals",
            study["daily"],
            (
                ("activity_date", "Activity Date"),
                ("loaded_movements", "Loaded Movements"),
                ("offloaded_movements", "Offloaded Movements"),
                ("loaded_tonnes", "Loaded Tonnes"),
                ("offloaded_tonnes", "Offloaded Tonnes"),
            ),
        )
        movement_columns = (
            ("job_reference", "Job Reference"),
            ("root_date", "Transport Allocation Date"),
            ("route_name", "Supply Chain Leg"),
            ("truck_registration", "Truck Registration"),
            ("truck_type", "Truck Type"),
            ("origin_display", "Origin"),
            ("origin_bay", "Loading Bay / Origin Fallback"),
            ("loaded_tonnes", "Loaded Nett Weight Tonnes"),
            ("loading_operator", "Loading Operator"),
            ("loading_signed_off_at", "Loading Signed Off"),
            ("destination_display", "Destination"),
            ("destination_bay", "Offloading Bay / Destination Fallback"),
            ("offloaded_tonnes", "Offloaded Nett Weight Tonnes"),
            ("offloading_operator", "Offloading Operator"),
            ("offloading_signed_off_at", "Offloading Signed Off"),
            ("variance_tonnes", "Offloaded Less Loaded Tonnes"),
            ("delivery_pct", "Delivery %"),
            ("in_transit", "In Transit"),
            ("current_checklist", "Current Checklist"),
            ("current_opus_status", "Current OPUS Status"),
            ("audit_status", "Audit Status"),
            ("audit_reasons", "Audit Reasons"),
            ("loading_signed_off_attempts", "Loading Signed-off Attempts"),
            ("offloading_signed_off_attempts", "Offloading Signed-off Attempts"),
            ("loading_validation_errors", "Loading Validation Errors"),
            ("offloading_validation_errors", "Offloading Validation Errors"),
        )
        add_rows("Movements", study["movements"], movement_columns)
        add_rows("Exceptions", study["exceptions"], movement_columns)
        add_rows(
            "Allocation Gaps",
            study["allocation_gaps"],
            (
                ("job_reference", "Job Reference"),
                ("root_date", "Transport Allocation Date"),
                ("truck_registration", "Truck Registration"),
                ("allocation_loading_point", "Allocated Loading Point"),
                ("allocation_offloading_point", "Allocated Offloading Point"),
                ("current_checklist", "Current Checklist"),
                ("current_opus_status", "Current OPUS Status"),
                ("current_status_group", "Current Status Group"),
                ("transit_exclusion_reason", "Transit Exclusion Reason"),
            ),
        )
        writer.save(path)
        return writer.row_counts

    def export_order_investigation_pdf(
        self,
        path: Path,
        order_reference: str,
        date_from: date,
        date_to: date,
        expected_bays: tuple[str, ...],
        route_plan: RoutePlan = (),
        parcel_tonnes: Decimal | int | float = 0,
    ) -> dict[str, int]:
        study = self.load_order_investigation(
            order_reference,
            date_from,
            date_to,
            expected_bays,
            route_plan,
            parcel_tonnes,
        )
        return create_order_study_pdf(path, study)

    def apply_control_workbook(self, preview: WorkbookPreview) -> int:
        if not preview.valid:
            raise ValueError("The workbook contains validation errors.")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                if preview.import_id is None:
                    preview.import_id = self._insert_control_workbook_audit(
                        cursor,
                        preview,
                    )
                import_id = preview.import_id
                cursor.execute(
                    """
                    SELECT status
                    FROM ingest.control_workbook_imports
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (import_id,),
                )
                audit = cursor.fetchone()
                if audit is None:
                    raise LookupError(
                        f"Workbook import audit {import_id} was not found."
                    )
                if audit["status"] != "validated":
                    raise ValueError(
                        f"Workbook import {import_id} is already "
                        f"{audit['status']}."
                    )
                replaced_orders = 0
                order_ids: dict[str, int] = {}
                for row in preview.orders:
                    cursor.execute(
                        """
                        SELECT id
                        FROM ops.order_master
                        WHERE normalized_order_reference = upper(btrim(%s))
                        """,
                        (row["order_reference"],),
                    )
                    existing = cursor.fetchone()
                    action = "replaced" if existing else "inserted"
                    replaced_orders += int(existing is not None)
                    cursor.execute(
                        """
                        INSERT INTO ops.order_master (
                            order_reference,
                            client_name,
                            minimum_delivery_pct,
                            import_id,
                            active
                        )
                        VALUES (%s, %s, %s, %s, true)
                        ON CONFLICT (normalized_order_reference) DO UPDATE
                        SET order_reference = EXCLUDED.order_reference,
                            client_name = EXCLUDED.client_name,
                            minimum_delivery_pct = EXCLUDED.minimum_delivery_pct,
                            import_id = EXCLUDED.import_id,
                            active = true
                        RETURNING id
                        """,
                        (
                            row["order_reference"],
                            row["client_name"],
                            row["minimum_delivery_pct"],
                            import_id,
                        ),
                    )
                    order_id = int(cursor.fetchone()["id"])
                    order_ids[str(row["order_reference"]).casefold()] = order_id
                    cursor.execute(
                        """
                        INSERT INTO ingest.control_workbook_rows (
                            import_id, sheet_name, row_number, row_key, action, payload
                        )
                        VALUES (%s, 'Order Master', %s, %s, %s, %s::jsonb)
                        """,
                        (
                            import_id,
                            row["row_number"],
                            row["order_reference"],
                            action,
                            _json_payload(row),
                        ),
                    )

                replaced_balances = 0
                for row in preview.opening_balances:
                    order_id = order_ids[str(row["order_reference"]).casefold()]
                    cursor.execute(
                        """
                        SELECT id
                        FROM ops.stock_opening_balances
                        WHERE effective_date = %s
                          AND stock_role = %s
                          AND normalized_location = lower(btrim(%s))
                          AND normalized_storage_identifier = lower(btrim(%s))
                          AND order_master_id = %s
                        """,
                        (
                            row["effective_date"],
                            row["stock_role"],
                            row["location_name"],
                            row["storage_identifier"],
                            order_id,
                        ),
                    )
                    existing = cursor.fetchone()
                    action = "replaced" if existing else "inserted"
                    replaced_balances += int(existing is not None)
                    cursor.execute(
                        """
                        INSERT INTO ops.stock_opening_balances (
                            effective_date,
                            stock_role,
                            location_name,
                            storage_identifier,
                            order_master_id,
                            opening_tonnes,
                            import_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (
                            effective_date,
                            stock_role,
                            normalized_location,
                            normalized_storage_identifier,
                            order_master_id
                        ) DO UPDATE
                        SET stock_role = EXCLUDED.stock_role,
                            location_name = EXCLUDED.location_name,
                            storage_identifier = EXCLUDED.storage_identifier,
                            opening_tonnes = EXCLUDED.opening_tonnes,
                            import_id = EXCLUDED.import_id
                        """,
                        (
                            row["effective_date"],
                            row["stock_role"],
                            row["location_name"],
                            row["storage_identifier"],
                            order_id,
                            row["opening_tonnes"],
                            import_id,
                        ),
                    )
                    row_key = "|".join(
                        (
                            row["effective_date"].isoformat(),
                            row["stock_role"],
                            row["location_name"],
                            row["storage_identifier"],
                            row["order_reference"],
                        )
                    )
                    cursor.execute(
                        """
                        INSERT INTO ingest.control_workbook_rows (
                            import_id, sheet_name, row_number, row_key, action, payload
                        )
                        VALUES (
                            %s, 'Opening Balances', %s, %s, %s, %s::jsonb
                        )
                        """,
                        (
                            import_id,
                            row["row_number"],
                            row_key,
                            action,
                            _json_payload(row),
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE ingest.control_workbook_imports
                    SET status = 'applied',
                        replaced_order_rows = %s,
                        replaced_balance_rows = %s,
                        applied_at = clock_timestamp()
                    WHERE id = %s
                    """,
                    (replaced_orders, replaced_balances, import_id),
                )
            connection.commit()
        return import_id

    def record_control_workbook_preview(
        self,
        preview: WorkbookPreview,
    ) -> int:
        if preview.import_id is not None:
            return preview.import_id
        with self.connection() as connection:
            with connection.cursor() as cursor:
                preview.import_id = self._insert_control_workbook_audit(
                    cursor,
                    preview,
                )
            connection.commit()
        return preview.import_id

    @staticmethod
    def _insert_control_workbook_audit(
        cursor: psycopg.Cursor[dict[str, Any]],
        preview: WorkbookPreview,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO ingest.control_workbook_imports (
                original_filename,
                file_sha256,
                status,
                order_rows,
                opening_balance_rows,
                error_count,
                imported_by,
                metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, current_user, %s::jsonb)
            RETURNING id
            """,
            (
                preview.filename,
                preview.file_sha256,
                "validated" if preview.valid else "rejected",
                len(preview.orders),
                len(preview.opening_balances),
                len(preview.errors),
                _json_payload({"validation_errors": preview.errors}),
            ),
        )
        return int(cursor.fetchone()["id"])

    def control_workbook_history(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id AS import_id,
                        original_filename,
                        status,
                        order_rows,
                        opening_balance_rows,
                        replaced_order_rows,
                        replaced_balance_rows,
                        error_count,
                        imported_by,
                        created_at,
                        applied_at
                    FROM ingest.control_workbook_imports
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
                return _clean_rows(cursor.fetchall())

    def export_data_workbook(
        self,
        path: Path,
        filters: DataFilters,
    ) -> dict[str, int]:
        parameters = self._data_parameters(filters)
        where = self._data_where()
        writer = XlsxStreamWriter()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT 'Status' AS "Category",
                           ops.normalized_job_status(j.status) AS "Metric",
                           count(*) AS "Value"
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    WHERE {where}
                    GROUP BY ops.normalized_job_status(j.status)
                    UNION ALL
                    SELECT 'Checklist', cd.canonical_name, count(*)
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    WHERE {where}
                    GROUP BY cd.canonical_name
                    ORDER BY "Category", "Metric"
                    """,
                    parameters,
                )
                summary_rows = cursor.fetchall()
            writer.add_sheet(
                "Summary",
                ("Category", "Metric", "Value"),
                summary_rows,
            )

            workflow_headers = (
                "Job Reference",
                "Order Number",
                "Root Date",
                "Workflow Sequence",
                "Attempt Sequence",
                "Checklist",
                "OPUS Status",
                "Status Group",
                "Operator",
                "Created",
                "Started",
                "Completed",
                "Signed Off",
                "Current Job",
                "Superseded Terminal",
            )
            with connection.cursor(name="workflow_export") as cursor:
                cursor.itersize = 2000
                cursor.execute(
                    f"""
                    SELECT
                        j.job_reference AS "Job Reference",
                        a.order_reference AS "Order Number",
                        (
                            a.transport_allocation_created_at AT TIME ZONE 'UTC'
                        )::date AS "Root Date",
                        attempt.workflow_sequence AS "Workflow Sequence",
                        attempt.attempt_sequence AS "Attempt Sequence",
                        cd.canonical_name AS "Checklist",
                        j.status AS "OPUS Status",
                        attempt.status_group AS "Status Group",
                        j.operator_name AS "Operator",
                        j.source_created_at AS "Created",
                        j.operator_started_at AS "Started",
                        j.source_completed_at AS "Completed",
                        j.source_signed_off_at AS "Signed Off",
                        attempt.is_current_job AS "Current Job",
                        (
                            attempt.status_group IN ('Closed', 'Cancelled')
                            AND attempt.has_later_job
                        ) AS "Superseded Terminal"
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    JOIN ops.v_workflow_attempts attempt ON attempt.job_id = j.id
                    WHERE {where}
                    ORDER BY j.job_reference, attempt.workflow_sequence
                    """,
                    parameters,
                )
                writer.add_sheet("Workflow", workflow_headers, cursor)

            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT DISTINCT cd.canonical_name
                    FROM ops.jobs j
                    JOIN ops.allocations a ON a.id = j.allocation_id
                    JOIN ops.checklist_definitions cd
                      ON cd.id = j.checklist_definition_id
                    WHERE {where}
                    ORDER BY cd.canonical_name
                    """,
                    parameters,
                )
                checklist_names = [
                    str(row["canonical_name"]) for row in cursor.fetchall()
                ]

            answer_headers = (
                "Job Reference",
                "Order Number",
                "Root Date",
                "Job Row ID",
                "OPUS Status",
                "Status Group",
                "Operator",
                "Job Created",
                "Job Started",
                "Job Signed Off",
                "Section",
                "Subsection",
                "Question Number",
                "Question",
                "Question Detail",
                "Answer",
                "Unformatted Answer",
                "Formatted Answer",
                "Comments",
                "Question Type",
                "Question Unit",
                "Question Function",
                "Optional",
                "Requires Comment",
                "Images",
                "Structured Items",
                "Child Checklists",
                "Table Data",
                "Table Columns",
                "Answer Extra",
                "Source Updated",
            )
            for index, checklist_name in enumerate(checklist_names, start=1):
                answer_parameters = {
                    **parameters,
                    "export_checklist": checklist_name,
                }
                cursor_name = f"answers_export_{index}"
                with connection.cursor(name=cursor_name) as cursor:
                    cursor.itersize = 2000
                    cursor.execute(
                        f"""
                        SELECT
                            j.job_reference AS "Job Reference",
                            a.order_reference AS "Order Number",
                            (
                                a.transport_allocation_created_at
                                AT TIME ZONE 'UTC'
                            )::date AS "Root Date",
                            j.id AS "Job Row ID",
                            j.status AS "OPUS Status",
                            ops.normalized_job_status(j.status) AS "Status Group",
                            j.operator_name AS "Operator",
                            j.source_created_at AS "Job Created",
                            j.operator_started_at AS "Job Started",
                            j.source_signed_off_at AS "Job Signed Off",
                            ca.section_name AS "Section",
                            ca.subsection_name AS "Subsection",
                            ca.question_number AS "Question Number",
                            ca.question AS "Question",
                            coalesce(
                                nullif(ca.question_report_full, ''),
                                nullif(ca.question_summary, ''),
                                nullif(ca.unformatted_question_text, ''),
                                nullif(ca.action_text, '')
                            ) AS "Question Detail",
                            ca.answer_text AS "Answer",
                            ca.unformatted_answer AS "Unformatted Answer",
                            ca.report_formatted_answer AS "Formatted Answer",
                            ca.comments AS "Comments",
                            ca.question_type AS "Question Type",
                            ca.question_unit AS "Question Unit",
                            ca.question_function AS "Question Function",
                            ca.optional_question AS "Optional",
                            ca.require_comment AS "Requires Comment",
                            ca.answer_images AS "Images",
                            ca.answer_items AS "Structured Items",
                            ca.child_checklist_answers AS "Child Checklists",
                            ca.table_data AS "Table Data",
                            ca.table_columns AS "Table Columns",
                            ca.answer_extra AS "Answer Extra",
                            ci.source_updated_at AS "Source Updated"
                        FROM ops.jobs j
                        JOIN ops.allocations a ON a.id = j.allocation_id
                        JOIN ops.checklist_definitions cd
                          ON cd.id = j.checklist_definition_id
                        JOIN ops.checklist_instances ci ON ci.job_id = j.id
                        JOIN ops.checklist_answers ca
                          ON ca.checklist_instance_id = ci.id
                        WHERE {where}
                          AND cd.canonical_name = %(export_checklist)s
                        ORDER BY
                            j.job_reference,
                            j.source_created_at,
                            ca.section_sequence,
                            ca.subsection_created_at,
                            ca.id
                        """,
                        answer_parameters,
                    )
                    writer.add_sheet(checklist_name, answer_headers, cursor)
        writer.save(path)
        return writer.row_counts

    def _extraction_metrics(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> dict[str, int]:
        query = """
            WITH scoped_jobs AS (
                SELECT job.*
                FROM ops.jobs job
                JOIN ops.allocations allocation
                  ON allocation.id = job.allocation_id
                WHERE (
                    allocation.transport_allocation_created_at
                    AT TIME ZONE 'UTC'
                )::date BETWEEN %(extract_from)s::date AND %(extract_to)s::date
            ),
            scoped_allocations AS (
                SELECT DISTINCT allocation_id AS id
                FROM scoped_jobs
            )
            SELECT
                (SELECT count(*) FROM scoped_allocations) AS allocations,
                (SELECT count(*) FROM scoped_jobs) AS checklist_jobs,
                (
                    SELECT count(*)
                    FROM ops.checklist_instances ci
                    JOIN scoped_jobs j ON j.id = ci.job_id
                ) AS checklist_instances,
                (
                    SELECT count(*)
                    FROM ops.checklist_answers ca
                    JOIN ops.checklist_instances ci
                      ON ci.id = ca.checklist_instance_id
                    JOIN scoped_jobs j ON j.id = ci.job_id
                ) AS checklist_answers,
                (
                    SELECT count(*)
                    FROM ops.checklist_instances ci
                    JOIN scoped_jobs j ON j.id = ci.job_id
                    WHERE NOT ci.detail_complete
                ) AS incomplete_details,
                (
                    SELECT count(*)
                    FROM ingest.extraction_errors error
                    JOIN ingest.extraction_runs run
                      ON run.id = error.extraction_run_id
                    WHERE run.filter_payload ->> 'date_from'
                            = %(extract_from_text)s
                      AND run.filter_payload ->> 'date_to'
                            = %(extract_to_text)s
                ) AS extraction_errors
        """
        with connection.cursor() as cursor:
            cursor.execute(query, self._scope_parameters())
            row = cursor.fetchone() or {}
        return {key: int(value or 0) for key, value in row.items()}

    def _checklist_summary(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                ci.name AS checklist_name,
                coalesce(cd.stage_code, 'discovered') AS stage_code,
                count(*) AS checklist_instances,
                count(DISTINCT ci.job_reference) AS job_references,
                sum(ci.section_count) AS sections,
                sum(ci.answer_count) AS answers,
                sum(ci.image_count) AS images,
                sum(ci.item_count) AS structured_items,
                count(*) FILTER (WHERE NOT ci.detail_complete) AS incomplete,
                max(ci.source_updated_at) AS latest_source_update
            FROM ops.checklist_instances ci
            LEFT JOIN ops.checklist_definitions cd
              ON cd.id = ci.checklist_definition_id
            JOIN ops.jobs j ON j.id = ci.job_id
            JOIN ops.allocations a ON a.id = ci.allocation_id
            WHERE (
                a.transport_allocation_created_at AT TIME ZONE 'UTC'
            )::date BETWEEN %(extract_from)s::date AND %(extract_to)s::date
            GROUP BY ci.name, cd.stage_code, cd.stage_order
            ORDER BY cd.stage_order NULLS LAST, ci.name
        """
        with connection.cursor() as cursor:
            cursor.execute(query, self._scope_parameters())
            return _clean_rows(cursor.fetchall())

    def _checklist_answers(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                ca.id AS answer_row_id,
                ci.job_reference,
                ci.name AS checklist_name,
                j.status AS job_status,
                ci.source_updated_at,
                ca.section_name,
                ca.subsection_name,
                ca.question,
                ca.question_report_full,
                ca.question_report_short,
                ca.question_summary,
                ca.unformatted_question_text,
                ca.action_text,
                ca.optional_question,
                ca.require_comment,
                coalesce(
                    nullif(ca.answer_text, ''),
                    nullif(ca.unformatted_answer, ''),
                    CASE
                        WHEN ca.answer_items <> '[]'::jsonb THEN '[structured item]'
                        WHEN ca.answer_images <> '[]'::jsonb THEN '[image]'
                        WHEN ca.child_checklist_answers <> '[]'::jsonb
                            THEN '[child checklist]'
                        WHEN ca.table_data <> '{}'::jsonb THEN '[table data]'
                        WHEN ca.answer_extra <> '{}'::jsonb
                            THEN '[structured answer]'
                        ELSE ''
                    END
                ) AS answer,
                CASE
                    WHEN btrim(coalesce(
                        nullif(ca.text_value, ''),
                        nullif(ca.report_formatted_answer, ''),
                        ''
                    )) <> btrim(ca.question)
                    THEN coalesce(
                        nullif(ca.text_value, ''),
                        nullif(ca.report_formatted_answer, '')
                    )
                    ELSE NULL
                END AS question_detail,
                ca.question_type,
                ca.comments,
                CASE
                    WHEN jsonb_typeof(ca.answer_images) = 'array'
                    THEN jsonb_array_length(ca.answer_images)
                    ELSE 0
                END AS images,
                CASE
                    WHEN jsonb_typeof(ca.answer_items) = 'array'
                    THEN jsonb_array_length(ca.answer_items)
                    ELSE 0
                END AS structured_items,
                ca.table_data <> '{}'::jsonb AS has_table_data
            FROM ops.checklist_answers ca
            JOIN ops.checklist_instances ci
              ON ci.id = ca.checklist_instance_id
            JOIN ops.jobs j ON j.id = ci.job_id
            JOIN ops.allocations a ON a.id = ci.allocation_id
            WHERE (
                a.transport_allocation_created_at AT TIME ZONE 'UTC'
            )::date BETWEEN %(extract_from)s::date AND %(extract_to)s::date
            ORDER BY
                ci.source_updated_at DESC NULLS LAST,
                ci.job_reference,
                ci.name,
                ca.section_sequence,
                ca.id
            LIMIT 1000
        """
        with connection.cursor() as cursor:
            cursor.execute(query, self._scope_parameters())
            return _clean_rows(cursor.fetchall())

    def _extraction_errors(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                error.id AS extraction_error_id,
                error.occurred_at,
                run.run_key::text,
                error.job_reference,
                error.phase,
                error.error_type,
                error.error_message
            FROM ingest.extraction_errors error
            JOIN ingest.extraction_runs run
              ON run.id = error.extraction_run_id
                        WHERE run.filter_payload ->> 'date_from' = %(extract_from_text)s
                            AND run.filter_payload ->> 'date_to' = %(extract_to_text)s
            ORDER BY error.occurred_at DESC
            LIMIT 100
        """
        with connection.cursor() as cursor:
            cursor.execute(query, self._scope_parameters())
            return _clean_rows(cursor.fetchall())

    def _order_workflows(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            WITH scoped AS (
                SELECT
                    a.order_reference AS order_number,
                    j.id AS job_row_id,
                    j.opus_job_id::text,
                    j.job_reference,
                    cd.canonical_name AS checklist_name,
                    cd.stage_code,
                    cd.stage_order,
                    j.status,
                    j.status_detail,
                    CASE
                        WHEN j.source_signed_off_at IS NOT NULL
                             OR coalesce(j.status, '') ~* '(signed off|completed)'
                            THEN 'Completed'
                        WHEN coalesce(j.status, '') ~* '(in progress|review)'
                            THEN 'In progress'
                        WHEN coalesce(j.status, '') ~* 'not started'
                            THEN 'Not started'
                        WHEN coalesce(j.status, '') ~* 'closed'
                             AND j.operator_started_at IS NULL
                            THEN 'Closed - not started'
                        WHEN coalesce(j.status, '') ~* 'closed'
                            THEN 'Closed'
                        ELSE coalesce(j.status, 'Unknown')
                    END AS workflow_status,
                    j.operator_name,
                    j.created_by_name,
                    j.operator_created,
                    j.created_from_operator_name,
                    j.source_created_at,
                    j.operator_started_at,
                    j.source_completed_at,
                    j.source_signed_off_at,
                    coalesce(
                        j.source_signed_off_at,
                        j.source_completed_at
                    ) AS end_at,
                    coalesce(
                        j.source_signed_off_at,
                        j.source_completed_at,
                        j.operator_started_at,
                        j.source_created_at,
                        j.last_updated_at,
                        j.first_seen_at
                    ) AS activity_at,
                    j.expected_start_at,
                    j.due_at,
                    j.last_updated_at,
                    j.workflow_parent_job_id::text,
                    ci.opus_checklist_instance_id::text,
                    ci.percentage_complete,
                    ci.section_count,
                    ci.answer_count,
                    ci.image_count,
                    ci.item_count,
                    ci.table_count,
                    ci.detail_complete,
                    ci.source_updated_at AS checklist_source_updated_at
                FROM ops.jobs j
                JOIN ops.allocations a ON a.id = j.allocation_id
                JOIN ops.checklist_definitions cd
                  ON cd.id = j.checklist_definition_id
                LEFT JOIN LATERAL (
                    SELECT
                        instance.opus_checklist_instance_id,
                        instance.percentage_complete,
                        instance.section_count,
                        instance.answer_count,
                        instance.image_count,
                        instance.item_count,
                        instance.table_count,
                        instance.detail_complete,
                        instance.source_updated_at
                    FROM ops.checklist_instances instance
                    WHERE instance.job_id = j.id
                    ORDER BY
                        instance.source_updated_at DESC NULLS LAST,
                        instance.id DESC
                    LIMIT 1
                ) ci ON true
                WHERE (
                      a.transport_allocation_created_at
                      AT TIME ZONE 'UTC'
                  )::date BETWEEN %(extract_from)s::date AND %(extract_to)s::date
            ),
            ordered AS (
                SELECT
                    scoped.*,
                    row_number() OVER (
                        PARTITION BY job_reference
                        ORDER BY
                            source_created_at NULLS LAST,
                            end_at NULLS LAST,
                            stage_order NULLS LAST,
                            job_row_id
                    ) AS workflow_sequence,
                    row_number() OVER (
                        PARTITION BY job_reference
                        ORDER BY
                            activity_at DESC NULLS LAST,
                            source_created_at DESC NULLS LAST,
                            stage_order DESC NULLS LAST,
                            job_row_id DESC
                    ) = 1 AS is_current_stage
                FROM scoped
            )
            SELECT
                order_number,
                job_row_id,
                opus_job_id,
                job_reference,
                checklist_name,
                stage_code,
                stage_order,
                workflow_sequence,
                is_current_stage,
                status,
                status_detail,
                workflow_status,
                operator_name,
                created_by_name,
                operator_created,
                created_from_operator_name,
                source_created_at,
                operator_started_at,
                source_completed_at,
                source_signed_off_at,
                end_at,
                expected_start_at,
                due_at,
                last_updated_at,
                workflow_parent_job_id,
                opus_checklist_instance_id,
                percentage_complete,
                section_count,
                answer_count,
                image_count,
                item_count,
                table_count,
                detail_complete,
                checklist_source_updated_at
            FROM ordered
            ORDER BY
                job_reference,
                workflow_sequence,
                source_created_at,
                end_at
        """
        with connection.cursor() as cursor:
            cursor.execute(query, self._scope_parameters())
            return _clean_rows(cursor.fetchall())

    @staticmethod
    def _metrics(
        connection: psycopg.Connection[dict[str, Any]],
        cutoff: datetime | None,
    ) -> dict[str, int]:
        query = """
            WITH scoped_allocations AS (
                SELECT *
                FROM ops.allocations
                WHERE (
                    %(cutoff)s::timestamptz IS NULL
                    OR coalesce(booked_at, first_seen_at) >= %(cutoff)s::timestamptz
                )
            )
            SELECT
                (SELECT count(*) FROM scoped_allocations) AS allocations,
                (
                    SELECT count(*)
                    FROM ops.jobs j
                    JOIN scoped_allocations a ON a.id = j.allocation_id
                ) AS checklist_jobs,
                (
                    SELECT count(*)
                    FROM scoped_allocations
                    WHERE coalesce(current_status, '') ~* '(complete|completed|offload|closed|exit)'
                ) AS completed,
                (
                    SELECT count(*)
                    FROM ops.v_current_transit t
                    WHERE (
                        %(cutoff)s::timestamptz IS NULL
                        OR coalesce(t.date_booked, t.captured_at) >= %(cutoff)s::timestamptz
                    )
                ) AS in_transit,
                (
                    SELECT count(DISTINCT vehicle_id)
                    FROM scoped_allocations
                    WHERE vehicle_id IS NOT NULL
                ) AS active_trucks
        """
        with connection.cursor() as cursor:
            cursor.execute(query, {"cutoff": cutoff})
            row = cursor.fetchone() or {}
        return {key: int(value or 0) for key, value in row.items()}

    @staticmethod
    def _stage_coverage(
        connection: psycopg.Connection[dict[str, Any]],
        cutoff: datetime | None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                cd.stage_code,
                cd.canonical_name,
                cd.stage_order,
                cd.workflow_role,
                count(j.id) FILTER (
                    WHERE (
                        %(cutoff)s::timestamptz IS NULL
                        OR coalesce(a.booked_at, a.first_seen_at) >= %(cutoff)s::timestamptz
                    )
                ) AS job_rows,
                count(DISTINCT j.allocation_id) FILTER (
                    WHERE (
                        %(cutoff)s::timestamptz IS NULL
                        OR coalesce(a.booked_at, a.first_seen_at) >= %(cutoff)s::timestamptz
                    )
                ) AS allocation_count
            FROM ops.checklist_definitions cd
            LEFT JOIN ops.jobs j ON j.checklist_definition_id = cd.id
            LEFT JOIN ops.allocations a ON a.id = j.allocation_id
            WHERE cd.active
            GROUP BY cd.id
            ORDER BY cd.stage_order, cd.canonical_name
        """
        with connection.cursor() as cursor:
            cursor.execute(query, {"cutoff": cutoff})
            return _clean_rows(cursor.fetchall())

    @staticmethod
    def _activity(
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            WITH days AS (
                SELECT generate_series(
                    current_date - interval '29 days',
                    current_date,
                    interval '1 day'
                )::date AS activity_date
            ),
            allocation_counts AS (
                SELECT coalesce(booked_at, first_seen_at)::date AS activity_date, count(*) AS rows
                FROM ops.allocations
                WHERE coalesce(booked_at, first_seen_at) >= current_date - interval '29 days'
                GROUP BY 1
            ),
            job_counts AS (
                SELECT coalesce(last_updated_at, first_seen_at)::date AS activity_date, count(*) AS rows
                FROM ops.jobs
                WHERE coalesce(last_updated_at, first_seen_at) >= current_date - interval '29 days'
                GROUP BY 1
            )
            SELECT
                d.activity_date,
                coalesce(a.rows, 0) AS allocations,
                coalesce(j.rows, 0) AS checklist_jobs
            FROM days d
            LEFT JOIN allocation_counts a USING (activity_date)
            LEFT JOIN job_counts j USING (activity_date)
            ORDER BY d.activity_date
        """
        with connection.cursor() as cursor:
            cursor.execute(query)
            return _clean_rows(cursor.fetchall())

    @staticmethod
    def _allocations(
        connection: psycopg.Connection[dict[str, Any]],
        cutoff: datetime | None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                job_reference,
                booked_at,
                loading_point,
                offloading_point,
                transporter,
                truck_registration,
                driver_name,
                current_status,
                current_stage_code,
                checklist_job_count,
                last_seen_at
            FROM ops.v_allocation_progress
            WHERE (
                %(cutoff)s::timestamptz IS NULL
                OR coalesce(booked_at, last_seen_at) >= %(cutoff)s::timestamptz
            )
            ORDER BY coalesce(booked_at, last_seen_at) DESC
            LIMIT 250
        """
        with connection.cursor() as cursor:
            cursor.execute(query, {"cutoff": cutoff})
            return _clean_rows(cursor.fetchall())

    @staticmethod
    def _transit(
        connection: psycopg.Connection[dict[str, Any]],
        cutoff: datetime | None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                job_reference,
                date_booked,
                loading_point,
                offloading_point,
                transporter_name,
                truck_registration,
                truck_type,
                driver_name,
                loading_exit_at,
                staging_arrival_at,
                staging_exit_at,
                truck_arrival_at,
                offloading_exit_at,
                captured_at
            FROM ops.v_current_transit
            WHERE (
                %(cutoff)s::timestamptz IS NULL
                OR coalesce(date_booked, captured_at) >= %(cutoff)s::timestamptz
            )
            ORDER BY captured_at DESC
            LIMIT 250
        """
        with connection.cursor() as cursor:
            cursor.execute(query, {"cutoff": cutoff})
            return _clean_rows(cursor.fetchall())

    def _extraction_runs(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                run_key::text,
                source_name,
                extraction_scope,
                started_at,
                finished_at,
                status,
                rows_seen,
                rows_inserted,
                rows_updated,
                rows_rejected,
                coalesce((metadata ->> 'bundles_queued')::bigint, 0)
                    AS bundles_queued,
                coalesce((metadata ->> 'bundles_deferred')::bigint, 0)
                    AS bundles_deferred,
                coalesce((metadata ->> 'bundles_completed')::bigint, 0)
                    AS bundles_completed,
                coalesce((metadata ->> 'checklist_instances')::bigint, 0)
                    AS checklist_instances,
                coalesce((metadata ->> 'checklist_sections')::bigint, 0)
                    AS checklist_sections,
                coalesce((metadata ->> 'checklist_answers')::bigint, 0)
                    AS checklist_answers,
                coalesce((metadata ->> 'request_retries')::bigint, 0)
                    AS request_retries,
                coalesce((metadata ->> 'jobs_scanned')::bigint, 0)
                    AS jobs_scanned,
                coalesce((metadata ->> 'recent_jobs_discovered')::bigint, 0)
                    AS recent_jobs_discovered,
                coalesce((metadata ->> 'active_jobs_discovered')::bigint, 0)
                    AS active_jobs_discovered,
                coalesce((metadata ->> 'backlog_jobs_discovered')::bigint, 0)
                    AS backlog_jobs_discovered,
                coalesce((metadata ->> 'active_audit_jobs')::bigint, 0)
                    AS active_audit_jobs,
                coalesce((metadata ->> 'historical_audit_jobs')::bigint, 0)
                    AS historical_audit_jobs,
                coalesce((metadata ->> 'references_scanned')::bigint, 0)
                    AS references_scanned,
                coalesce((metadata ->> 'eligible_references')::bigint, 0)
                    AS eligible_references,
                coalesce((
                    metadata ->> 'excluded_missing_root_references'
                )::bigint, 0) AS excluded_missing_roots,
                coalesce((
                    metadata ->> 'excluded_out_of_window_root_references'
                )::bigint, 0) AS excluded_old_roots,
                coalesce((metadata ->> 'bulk_import_jobs_excluded')::bigint, 0)
                    AS excluded_bulk_jobs,
                error_message
            FROM ingest.extraction_runs
            WHERE filter_payload ->> 'date_from' = %(extract_from_text)s
              AND filter_payload ->> 'date_to' = %(extract_to_text)s
            ORDER BY started_at DESC
            LIMIT 25
        """
        with connection.cursor() as cursor:
            cursor.execute(query, self._scope_parameters())
            return _clean_rows(cursor.fetchall())

    def _scope_parameters(self) -> dict[str, Any]:
        extract_to = self.settings.effective_extract_to()
        return {
            "extract_from": self.settings.opus_extract_from,
            "extract_to": extract_to,
            "extract_from_text": self.settings.opus_extract_from.isoformat(),
            "extract_to_text": extract_to.isoformat(),
        }

    @staticmethod
    def _workflow_edges(
        connection: psycopg.Connection[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                source.canonical_name AS source_checklist,
                target.canonical_name AS target_checklist,
                edge.sequence_no,
                edge.condition_expression,
                edge.assigned_role,
                edge.start_timing,
                edge.due_rule,
                edge.published
            FROM ops.workflow_edges edge
            JOIN ops.checklist_definitions source ON source.id = edge.source_checklist_id
            JOIN ops.checklist_definitions target ON target.id = edge.target_checklist_id
            WHERE edge.active
            ORDER BY edge.sequence_no, source.stage_order, target.stage_order
        """
        with connection.cursor() as cursor:
            cursor.execute(query)
            return _clean_rows(cursor.fetchall())

    @staticmethod
    def _storage(
        connection: psycopg.Connection[dict[str, Any]],
    ) -> dict[str, int]:
        query = """
            SELECT
                (
                    SELECT count(*)
                    FROM ops.checklist_definitions
                    WHERE active
                ) AS checklist_definitions,
                (
                    SELECT count(*)
                    FROM pg_inherits i
                    JOIN pg_class child ON child.oid = i.inhrelid
                    JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                    WHERE namespace.nspname IN ('ops', 'ingest')
                      AND child.relkind = 'r'
                ) AS partitions,
                (
                    SELECT count(*)
                    FROM information_schema.tables
                    WHERE table_schema IN ('ops', 'ingest')
                      AND table_type = 'BASE TABLE'
                ) AS accessible_tables,
                (
                    has_table_privilege(
                        current_user,
                        'ops.checklist_definitions',
                        'INSERT'
                    )
                    AND has_table_privilege(
                        current_user,
                        'ops.checklist_definitions',
                        'UPDATE'
                    )
                ) AS connector_writable
                ,(
                    to_regclass('ops.checklist_instances') IS NOT NULL
                    AND to_regclass('ops.checklist_answers') IS NOT NULL
                    AND to_regclass('ingest.extraction_errors') IS NOT NULL
                ) AS detail_schema_ready
                ,(
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'ingest'
                          AND table_name = 'raw_records'
                          AND column_name = 'source_payload_sha256'
                    )
                ) AS source_hash_ready
                ,(
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'ops'
                          AND table_name = 'allocations'
                          AND column_name = 'transport_allocation_created_at'
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM ops.checklist_definitions
                        WHERE active
                          AND (
                              stage_code = 'bulk_import'
                              OR lower(btrim(canonical_name))
                                 = 'bulk import for minerals transport allocation'
                          )
                    )
                ) AS root_baseline_ready
                ,(
                    SELECT count(*) = 7
                    FROM (VALUES
                        ('transport_allocation', '4f7cd186-4bce-46eb-9d92-272e0ceded7e'::uuid),
                        ('vehicle_inspection', 'e3a5c4e9-fc33-44bf-8d5f-52cdbf56aa02'::uuid),
                        ('loading_exit', '652d5117-d609-47f3-b093-5ad4ebcb97bf'::uuid),
                        ('staging_arrival', 'aeca9758-c49b-486e-8f60-6a29d6c3dcb2'::uuid),
                        ('staging_exit', 'fe799df1-67ea-4ea9-93c6-913161422a8e'::uuid),
                        ('truck_arrival', '7aa017a8-cb07-4bd6-afe8-37ff55c47acc'::uuid),
                        ('offloading_exit', '246209a9-2f68-4ef5-b174-8b06e4c014fc'::uuid)
                    ) expected(stage_code, opus_checklist_id)
                    JOIN ops.checklist_definitions definition
                      ON definition.stage_code = expected.stage_code
                     AND definition.opus_checklist_id = expected.opus_checklist_id
                ) AS stage_mapping_ready
                ,(
                    to_regclass('ops.checklist_operational_facts') IS NOT NULL
                    AND to_regclass('ops.order_master') IS NOT NULL
                    AND to_regclass('ops.stock_opening_balances') IS NOT NULL
                    AND to_regclass('ops.v_reference_workflow_state') IS NOT NULL
                    AND to_regclass('ops.v_transit_route_register') IS NOT NULL
                    AND to_regclass('ops.v_stock_reconciliation') IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'ops'
                          AND table_name = 'stock_opening_balances'
                          AND column_name = 'stock_role'
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'ops'
                          AND table_name = 'v_reference_workflow_state'
                          AND column_name = 'transit_origin'
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'ops'
                          AND table_name = 'v_stock_reconciliation'
                          AND column_name = 'loading_storage_display'
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'ops'
                          AND table_name = 'v_stock_ledger'
                          AND column_name = 'stock_role'
                    )
                ) AS analytics_schema_ready
        """
        with connection.cursor() as cursor:
            cursor.execute(query)
            row = cursor.fetchone() or {}
        return {key: int(value or 0) for key, value in row.items()}
