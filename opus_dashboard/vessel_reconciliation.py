from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from opus_dashboard.config import PROJECT_ROOT
from opus_dashboard.pelagic_soh import load_pelagic_soh_snapshot


VESSEL_SNAPSHOT_PATH = (
    PROJECT_ROOT / "assets" / "pelagic_vessel_recon_2026-08-06.json"
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if not denominator:
        return None
    return (numerator / denominator * Decimal("100")).quantize(
        Decimal("0.001")
    )


def load_pelagic_vessel_snapshot(
    path: Path = VESSEL_SNAPSHOT_PATH,
) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    order_grounded = sum(
        (_decimal(row.get("grounded_at_bc_tonnes")) for row in snapshot["orders"]),
        Decimal("0"),
    )
    order_draft = sum(
        (_decimal(row.get("vessel_draft_tonnes")) for row in snapshot["orders"]),
        Decimal("0"),
    )
    combined = snapshot["combined"]
    if order_grounded != _decimal(combined.get("grounded_at_bc_tonnes")):
        raise ValueError("Pelagic order grounded tonnes do not match the combined total.")
    if order_draft != _decimal(combined.get("vessel_draft_tonnes")):
        raise ValueError("Pelagic order draft tonnes do not match the combined total.")
    if order_draft - order_grounded != _decimal(
        combined.get("reported_variance_tonnes")
    ):
        raise ValueError("Pelagic combined draft variance does not reconcile.")
    return snapshot


def build_vessel_reconciliation(
    opus_studies: dict[str, dict[str, Any]],
    *,
    soh_snapshot: dict[str, Any] | None = None,
    vessel_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    soh_snapshot = soh_snapshot or load_pelagic_soh_snapshot()
    vessel_snapshot = vessel_snapshot or load_pelagic_vessel_snapshot()
    soh_bc = {
        str(order["order_reference"]): sum(
            (
                _decimal(row.get("soh_tonnes"))
                for row in order.get("locations") or []
                if str(row.get("site")) == "BC"
            ),
            Decimal("0"),
        )
        for order in soh_snapshot.get("orders") or []
    }

    order_rows: list[dict[str, Any]] = []
    flow_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    for vessel_order in vessel_snapshot.get("orders") or []:
        order_reference = str(vessel_order["order_reference"])
        study = opus_studies.get(order_reference)
        if study is None:
            raise ValueError(f"OPUS study is missing for {order_reference}.")
        opus_bc = _decimal(
            (study.get("metrics") or {}).get("bc_stock_tonnes")
        )
        pelagic_soh_bc = soh_bc.get(order_reference, Decimal("0"))
        grounded = _decimal(vessel_order.get("grounded_at_bc_tonnes"))
        draft = _decimal(vessel_order.get("vessel_draft_tonnes"))
        grounded_to_draft = draft - grounded
        order_rows.append(
            {
                "order_reference": order_reference,
                "opus_bc_receipts_tonnes": opus_bc,
                "pelagic_soh_bc_tonnes": pelagic_soh_bc,
                "pelagic_grounded_tonnes": grounded,
                "vessel_draft_tonnes": draft,
                "opus_vs_soh_tonnes": opus_bc - pelagic_soh_bc,
                "opus_vs_grounded_tonnes": opus_bc - grounded,
                "soh_to_grounded_tonnes": grounded - pelagic_soh_bc,
                "grounded_to_draft_tonnes": grounded_to_draft,
                "vessel_shortage_pct": _pct(abs(grounded_to_draft), grounded),
                "draft_recovery_pct": _pct(draft, grounded),
                "opus_to_draft_tonnes": draft - opus_bc,
                "manual_reported_variance_tonnes": _decimal(
                    vessel_order.get("manual_reported_variance_tonnes")
                ),
                "dg_tonnes": (
                    _decimal(vessel_order["dg_tonnes"])
                    if vessel_order.get("dg_tonnes") is not None
                    else None
                ),
                "shore_scale_tonnes": (
                    _decimal(vessel_order["shore_scale_tonnes"])
                    if vessel_order.get("shore_scale_tonnes") is not None
                    else None
                ),
            }
        )
        for flow in vessel_order.get("flows") or []:
            dispatched = _decimal(flow.get("dispatched_tonnes"))
            received = _decimal(flow.get("received_tonnes"))
            flow_rows.append(
                {
                    "order_reference": order_reference,
                    "physical_leg": flow.get("physical_leg"),
                    "dispatched_tonnes": dispatched,
                    "received_tonnes": received,
                    "transit_variance_tonnes": received - dispatched,
                    "transit_variance_pct": _pct(
                        received - dispatched,
                        dispatched,
                    ),
                }
            )

    combined_opus = sum(
        (row["opus_bc_receipts_tonnes"] for row in order_rows),
        Decimal("0"),
    )
    combined_soh = sum(
        (row["pelagic_soh_bc_tonnes"] for row in order_rows),
        Decimal("0"),
    )
    combined_grounded = sum(
        (row["pelagic_grounded_tonnes"] for row in order_rows),
        Decimal("0"),
    )
    combined_draft = sum(
        (row["vessel_draft_tonnes"] for row in order_rows),
        Decimal("0"),
    )
    combined_variance = combined_draft - combined_grounded
    for row in order_rows:
        row["share_of_vessel_variance_pct"] = _pct(
            abs(row["grounded_to_draft_tonnes"]),
            abs(combined_variance),
        )

    ten_m = next(
        row for row in order_rows if row["order_reference"] == "KFTS26-10M"
    )
    eleven_mg = next(
        row for row in order_rows if row["order_reference"] == "KFTS26-11MG"
    )
    ten_m_reported = ten_m["manual_reported_variance_tonnes"]
    ten_m_comparable = ten_m["grounded_to_draft_tonnes"]
    dg = eleven_mg["dg_tonnes"]
    shore = eleven_mg["shore_scale_tonnes"]

    def relative_position(value: Decimal) -> str:
        direction = "above" if value >= 0 else "below"
        return f"{abs(value):,.3f} t {direction}"

    evidence_rows.extend(
        (
            {
                "priority": "High",
                "finding": "Most vessel variance sits in KFTS26-11MG",
                "evidence": (
                    f"{abs(eleven_mg['grounded_to_draft_tonnes']):,.3f} t of "
                    f"{abs(combined_variance):,.3f} t "
                    f"({eleven_mg['share_of_vessel_variance_pct']:.3f}%)."
                ),
                "potential_causes": (
                    "Order allocation to the vessel, draft-survey basis, moisture, "
                    "trim/density assumptions, residual cargo, or handling loss."
                ),
            },
            {
                "priority": "High",
                "finding": "Two measurement families disagree for KFTS26-11MG",
                "evidence": (
                    f"Draft {eleven_mg['vessel_draft_tonnes']:,.3f} t and DG "
                    f"{dg:,.3f} t differ by "
                    f"{abs(eleven_mg['vessel_draft_tonnes'] - dg):,.3f} t; "
                    f"shore scale {shore:,.3f} t is "
                    f"{abs(shore - eleven_mg['vessel_draft_tonnes']):,.3f} t "
                    "above draft."
                ),
                "potential_causes": (
                    "Different survey timestamps or measurement bases, shore-scale "
                    "calibration, draft constants, water density, moisture, or cargo "
                    "remaining in the transfer system."
                ),
            },
            {
                "priority": "Review",
                "finding": "The prior Pelagic SOH snapshot predates final grounded totals",
                "evidence": (
                    f"Grounded tonnes exceed the dated Pelagic BC SOH by "
                    f"{combined_grounded - combined_soh:,.3f} t, entirely on "
                    "KFTS26-11MG."
                ),
                "potential_causes": (
                    "Additional receipts after the 5 Aug 20:23 SOH snapshot or a "
                    "later controller adjustment."
                ),
            },
            {
                "priority": "Review",
                "finding": "OPUS and Pelagic inbound differences offset by order",
                "evidence": (
                    f"OPUS is "
                    f"{relative_position(eleven_mg['opus_vs_grounded_tonnes'])} "
                    f"Pelagic grounded for KFTS26-11MG and "
                    f"{relative_position(ten_m['opus_vs_grounded_tonnes'])} "
                    "for KFTS26-10M."
                ),
                "potential_causes": (
                    "Reference-to-order allocation, reporting cutoff, missing or "
                    "duplicated truck offloads, or manual receipt adjustments."
                ),
            },
            {
                "priority": "Confirmed",
                "finding": "KFTS26-10M workbook variance uses the dispatch baseline",
                "evidence": (
                    f"The workbook reports {ten_m_reported:,.3f} t using vessel "
                    f"draft less BCF dispatch. Draft less grounded-at-BC is "
                    f"{ten_m_comparable:,.3f} t; the "
                    f"{abs(ten_m_reported - ten_m_comparable):,.3f} t difference "
                    "equals the recorded BCF-to-BC transit variance."
                ),
                "potential_causes": (
                    "Formula-basis inconsistency; use grounded tonnes for the "
                    "combined vessel comparison and keep transit variance separate."
                ),
            },
            {
                "priority": "Limitation",
                "finding": "OPUS has no vessel load-out event in this study",
                "evidence": (
                    "OPUS supplies signed-off truck receipts into BC bays; the "
                    "vessel draft, DG, and shore-scale values come from Pelagic."
                ),
                "potential_causes": (
                    "A bay-to-vessel load list or governed vessel-loading control is "
                    "required to prove residual stock and vessel tonnes by bay."
                ),
            },
        )
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
        "source": vessel_snapshot["source"],
        "received_date": vessel_snapshot["received_date"],
        "summary": {
            "opus_bc_receipts_tonnes": float(combined_opus),
            "pelagic_soh_bc_tonnes": float(combined_soh),
            "pelagic_grounded_tonnes": float(combined_grounded),
            "vessel_draft_tonnes": float(combined_draft),
            "opus_vs_soh_tonnes": float(combined_opus - combined_soh),
            "opus_vs_grounded_tonnes": float(combined_opus - combined_grounded),
            "soh_to_grounded_tonnes": float(combined_grounded - combined_soh),
            "grounded_to_draft_tonnes": float(combined_variance),
            "vessel_shortage_pct": float(
                _pct(abs(combined_variance), combined_grounded) or 0
            ),
            "draft_recovery_pct": float(
                _pct(combined_draft, combined_grounded) or 0
            ),
            "opus_to_draft_tonnes": float(combined_draft - combined_opus),
        },
        "orders": clean_rows(order_rows),
        "flows": clean_rows(flow_rows),
        "evidence": evidence_rows,
    }
