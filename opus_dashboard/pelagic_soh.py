from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from opus_dashboard.config import PROJECT_ROOT


SNAPSHOT_PATH = (
    PROJECT_ROOT / "assets" / "pelagic_soh_2026-08-05.json"
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def load_pelagic_soh_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    generated_at = datetime.fromisoformat(snapshot["report_generated_at"])
    if generated_at.tzinfo is None:
        raise ValueError("Pelagic SOH report timestamp must include a timezone.")
    for order in snapshot.get("orders") or []:
        location_total = sum(
            (_decimal(row.get("soh_tonnes")) for row in order.get("locations") or []),
            Decimal("0"),
        )
        reported_total = _decimal(order.get("total_soh_tonnes"))
        if location_total != reported_total:
            raise ValueError(
                f"{order.get('order_reference')} Pelagic location values "
                f"sum to {location_total}, not {reported_total}."
            )
    return snapshot


def compare_pelagic_soh(
    opus_studies: dict[str, dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or load_pelagic_soh_snapshot()
    tolerance = _decimal(snapshot.get("comparison_tolerance_tonnes"))
    order_rows: list[dict[str, Any]] = []
    bay_rows: list[dict[str, Any]] = []
    leg_rows: list[dict[str, Any]] = []

    for manual_order in snapshot.get("orders") or []:
        order_reference = str(manual_order["order_reference"])
        study = opus_studies.get(order_reference)
        if study is None:
            continue
        leg_rows.extend(
            {
                "order_reference": order_reference,
                "route_name": row.get("route_name"),
                "dispatched_tonnes": _decimal(row.get("loaded_tonnes")),
                "received_tonnes": _decimal(row.get("offloaded_tonnes")),
                "movement_difference_tonnes": _decimal(
                    row.get("movement_difference_tonnes")
                ),
                "pending_references": int(row.get("pending_references") or 0),
                "pending_loaded_tonnes": _decimal(
                    row.get("pending_loaded_tonnes")
                ),
            }
            for row in study.get("legs") or []
        )

        opus_locations: dict[tuple[str, str], Decimal] = {
            ("BCF", str(row.get("bay") or "Unresolved OPUS location")): _decimal(
                row.get("closing_tonnes")
            )
            for row in study.get("staging") or []
        }
        opus_locations.update(
            {
                ("BC", str(row.get("area") or "Unresolved OPUS location")): _decimal(
                    row.get("closing_tonnes")
                )
                for row in study.get("bc_areas") or []
            }
        )
        manual_locations = {
            (str(row["site"]), str(row["bay"])): _decimal(row.get("soh_tonnes"))
            for row in manual_order.get("locations") or []
        }

        for site, bay in sorted(
            set(opus_locations) | set(manual_locations),
            key=lambda key: (key[0], key[1]),
        ):
            opus_tonnes = opus_locations.get((site, bay), Decimal("0"))
            pelagic_tonnes = manual_locations.get((site, bay), Decimal("0"))
            difference = opus_tonnes - pelagic_tonnes
            bay_rows.append(
                {
                    "order_reference": order_reference,
                    "site": site,
                    "bay": bay,
                    "opus_soh_tonnes": opus_tonnes,
                    "pelagic_soh_tonnes": pelagic_tonnes,
                    "variance_tonnes": difference,
                    "variance_pct": (
                        (difference / pelagic_tonnes * Decimal("100")).quantize(
                            Decimal("0.001")
                        )
                        if pelagic_tonnes
                        else None
                    ),
                    "comparison_status": (
                        "Matches within 0.01 t"
                        if abs(difference) <= tolerance
                        else "Difference identified"
                    ),
                }
            )

        metrics = study.get("metrics") or {}
        opus_bcf = _decimal(metrics.get("bcf_stock_tonnes"))
        opus_bc = _decimal(metrics.get("bc_stock_tonnes"))
        opus_total = _decimal(metrics.get("stock_at_recorded_locations_tonnes"))
        pelagic_bcf = sum(
            (
                _decimal(row.get("soh_tonnes"))
                for row in manual_order.get("locations") or []
                if str(row.get("site")) == "BCF"
            ),
            Decimal("0"),
        )
        pelagic_bc = sum(
            (
                _decimal(row.get("soh_tonnes"))
                for row in manual_order.get("locations") or []
                if str(row.get("site")) == "BC"
            ),
            Decimal("0"),
        )
        pelagic_total = _decimal(manual_order.get("total_soh_tonnes"))
        difference = opus_total - pelagic_total
        order_rows.append(
            {
                "order_reference": order_reference,
                "allocation_tonnes": _decimal(
                    manual_order.get("allocation_tonnes")
                ),
                "opus_bcf_soh_tonnes": opus_bcf,
                "pelagic_bcf_soh_tonnes": pelagic_bcf,
                "bcf_variance_tonnes": opus_bcf - pelagic_bcf,
                "opus_bc_soh_tonnes": opus_bc,
                "pelagic_bc_soh_tonnes": pelagic_bc,
                "bc_variance_tonnes": opus_bc - pelagic_bc,
                "opus_total_soh_tonnes": opus_total,
                "pelagic_total_soh_tonnes": pelagic_total,
                "total_variance_tonnes": difference,
                "total_variance_pct": (
                    (difference / pelagic_total * Decimal("100")).quantize(
                        Decimal("0.001")
                    )
                    if pelagic_total
                    else None
                ),
                "comparison_status": (
                    "Matches within 0.01 t"
                    if abs(difference) <= tolerance
                    else "Difference identified"
                ),
            }
        )

    def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                key: float(value) if isinstance(value, Decimal) else value
                for key, value in row.items()
            }
            for row in rows
        ]

    return {
        "source": snapshot["source"],
        "report_generated_at": snapshot["report_generated_at"],
        "comparison_tolerance_tonnes": float(tolerance),
        "orders": clean_rows(order_rows),
        "bays": clean_rows(bay_rows),
        "legs": clean_rows(leg_rows),
        "excluded_manual_fields": snapshot.get("excluded_manual_fields") or [],
        "exclusion_reason": snapshot.get("exclusion_reason") or "",
    }
