from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator
from unittest.mock import patch

from opus_dashboard.app import (
    _checklist_instance_groups,
    _checklist_page_rows,
    _filename_token,
    _job_references,
    _progress_fraction,
    _is_stock_exception,
)
from opus_dashboard.config import Settings
from opus_dashboard.models import DashboardSnapshot
from opus_dashboard.repository import (
    OperationsRepository,
    PERIOD_LABELS,
    _flow_route_name,
    _order_location_balances,
    _order_study,
    _period_start,
    _position_summaries,
    _route_bay_reconciliation,
    _study_bay,
    _study_site_name,
)


class FakeCursor:
    def __init__(
        self,
        summary: dict[str, Any] | None,
        answers: list[dict[str, Any]] | None = None,
    ) -> None:
        self.summary = summary
        self.answers = answers or []
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[Any, ...]) -> None:
        self.executions.append((query, parameters))

    def fetchone(self) -> dict[str, Any] | None:
        return self.summary

    def fetchall(self) -> list[dict[str, Any]]:
        return self.answers


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor


@contextmanager
def fake_connection(cursor: FakeCursor) -> Iterator[FakeConnection]:
    yield FakeConnection(cursor)


class RepositoryHelpersTests(unittest.TestCase):
    def test_order_export_filename_token_removes_path_separators(self) -> None:
        self.assertEqual(
            _filename_token("RANCHA463 / SC3450"),
            "RANCHA463-SC3450",
        )

    def test_order_locations_are_normalized_without_bcf_assumptions(self) -> None:
        self.assertEqual(
            _study_site_name(
                "LP-RIETVLEI PLANT - RUSTENBURG - "
                "RIETVLEI PLANT - RUSTENBURG"
            ),
            "RIETVLEI PLANT - RUSTENBURG",
        )
        balances = _order_location_balances(
            [
                {
                    "origin_display": "Mine A",
                    "origin_bay": "Stockpile A",
                    "loaded_tonnes": Decimal("100"),
                    "destination_display": "Staging B",
                    "destination_bay": "Bay 1",
                    "offloaded_tonnes": Decimal("100"),
                },
                {
                    "origin_display": "Staging B",
                    "origin_bay": "Bay 1",
                    "loaded_tonnes": Decimal("60"),
                    "destination_display": "Port C",
                    "destination_bay": "Bay 9",
                    "offloaded_tonnes": Decimal("60"),
                },
            ],
            [],
        )

        by_location = {row["location"]: row for row in balances}
        self.assertEqual(by_location["Mine A"]["location_role"], "Origin")
        self.assertIsNone(by_location["Mine A"]["soh_tonnes"])
        self.assertEqual(by_location["Staging B"]["location_role"], "Intermediate")
        self.assertEqual(by_location["Staging B"]["soh_tonnes"], Decimal("40"))
        self.assertEqual(by_location["Port C"]["location_role"], "Destination")
        self.assertEqual(by_location["Port C"]["soh_tonnes"], Decimal("60"))

    def test_year_to_date_begins_on_first_day_of_current_year(self) -> None:
        cutoff = _period_start("ytd")
        self.assertIsNotNone(cutoff)
        assert cutoff is not None
        self.assertEqual(cutoff.month, 1)
        self.assertEqual(cutoff.day, 1)
        self.assertEqual(cutoff.tzinfo, timezone.utc)

    def test_all_data_has_no_cutoff(self) -> None:
        self.assertIsNone(_period_start("all"))

    def test_disconnected_snapshot_preserves_diagnostic(self) -> None:
        snapshot = DashboardSnapshot.disconnected("ytd", "test diagnostic")
        self.assertFalse(snapshot.connected)
        self.assertEqual(snapshot.period, "ytd")
        self.assertEqual(snapshot.error, "test diagnostic")
        self.assertIsInstance(snapshot.captured_at, datetime)

    def test_supported_periods_include_year_to_date(self) -> None:
        self.assertEqual(PERIOD_LABELS["ytd"], "Year to date")

    def test_job_references_sort_by_numeric_suffix(self) -> None:
        snapshot = DashboardSnapshot(
            connected=True,
            captured_at=datetime.now(timezone.utc),
            period="all",
            order_workflows=[
                {"job_reference": "ORDBULK-100"},
                {"job_reference": "ORDBULK-9"},
                {"job_reference": "ORDBULK-12"},
                {"job_reference": "ORDBULK-9"},
                {"job_reference": ""},
            ],
        )

        self.assertEqual(
            _job_references(snapshot),
            ["ORDBULK-9", "ORDBULK-12", "ORDBULK-100"],
        )

    def test_progress_fraction_is_clamped_and_requires_total(self) -> None:
        self.assertIsNone(_progress_fraction(0, 0))
        self.assertEqual(_progress_fraction(25, 100), 0.25)
        self.assertEqual(_progress_fraction(120, 100), 1.0)

    def test_position_summaries_aggregate_orders_without_collapsing_storage(self) -> None:
        points, storage = _position_summaries(
            [
                {
                    "location_name": "Origin A",
                    "storage_display": "Bay 1",
                    "order_reference": "ORDER-1",
                    "opening_tonnes": Decimal("100"),
                    "movement_tonnes": Decimal("25"),
                    "stock_on_hand_tonnes": Decimal("75"),
                },
                {
                    "location_name": "Origin A",
                    "storage_display": "Bay 1",
                    "order_reference": "ORDER-2",
                    "opening_tonnes": Decimal("50"),
                    "movement_tonnes": Decimal("10"),
                    "stock_on_hand_tonnes": Decimal("40"),
                },
                {
                    "location_name": "Origin A",
                    "storage_display": "Point-level / no slab",
                    "order_reference": "ORDER-1",
                    "opening_tonnes": Decimal("20"),
                    "movement_tonnes": Decimal("5"),
                    "stock_on_hand_tonnes": Decimal("15"),
                },
            ]
        )

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["opening_tonnes"], Decimal("170"))
        self.assertEqual(points[0]["movement_tonnes"], Decimal("40"))
        self.assertEqual(points[0]["storage_count"], 2)
        self.assertEqual(points[0]["order_count"], 2)
        self.assertEqual(len(storage), 2)
        bay = next(row for row in storage if row["storage"] == "Bay 1")
        self.assertEqual(bay["stock_on_hand_tonnes"], Decimal("115"))

    def test_stock_exception_matches_completed_offload_scope(self) -> None:
        self.assertFalse(
            _is_stock_exception(
                {
                    "offloaded_tonnes": None,
                    "invalid_offloading_slab": True,
                    "offloading_validation_errors": ["pending"],
                }
            )
        )
        self.assertTrue(
            _is_stock_exception(
                {
                    "offloaded_tonnes": Decimal("10"),
                    "invalid_offloading_slab": True,
                }
            )
        )
        self.assertTrue(
            _is_stock_exception(
                {
                    "offloaded_tonnes": None,
                    "loading_validation_errors": ["invalid weight"],
                }
            )
        )

    def test_study_bay_uses_bcf_prefix_and_location_fallback(self) -> None:
        self.assertEqual(
            _study_bay("LP-Base Chrome Fields - Base Chrome Fields", "Bay 5"),
            ("BCF Bay 5", False),
        )
        self.assertEqual(
            _study_bay("OP-Bulk Connection - Bulk Connection", "Bay 5"),
            ("Invalid / unresolved (OPUS: Bay 5)", False),
        )
        self.assertEqual(
            _study_bay("LP-Kookfontein - Kookfontein", "No Loading Slab"),
            ("Kookfontein point-level / no bay", True),
        )
        self.assertEqual(
            _flow_route_name("LP-Kookfontein", "OP-Base Chrome Fields"),
            "Mine to BCF",
        )
        self.assertEqual(
            _flow_route_name("LP-Base Chrome Fields", "OP-Bulk Connection"),
            "BCF to BC",
        )

    def test_order_study_keeps_completed_and_all_flow_variances_separate(
        self,
    ) -> None:
        rows = [
            {
                "job_reference": "ORDBULK-1",
                "transport_allocation_created_at": datetime(
                    2026, 6, 29, 8, tzinfo=timezone.utc
                ),
                "loading_point": "LP-Base Chrome Fields",
                "loading_slab": "Bay 5",
                "loaded_tonnes": Decimal("40"),
                "loading_signed_off_at": datetime(
                    2026, 6, 29, 9, tzinfo=timezone.utc
                ),
                "offloading_point": "OP-Bulk Connection",
                "offloading_slab": "Island View N",
                "offloaded_tonnes": Decimal("39.9"),
                "offloading_signed_off_at": datetime(
                    2026, 6, 29, 11, tzinfo=timezone.utc
                ),
            },
            {
                "job_reference": "ORDBULK-2",
                "transport_allocation_created_at": datetime(
                    2026, 6, 29, 10, tzinfo=timezone.utc
                ),
                "loading_point": "LP-Kookfontein",
                "loading_slab": "No Loading Slab",
                "loaded_tonnes": Decimal("35"),
                "loading_signed_off_at": datetime(
                    2026, 6, 29, 12, tzinfo=timezone.utc
                ),
                "transit_destination": "OP-Bulk Connection",
                "planned_offloading_slab": "Bay 5",
                "offloaded_tonnes": None,
                "in_transit": True,
            },
            {
                "job_reference": "ORDBULK-3",
                "transport_allocation_created_at": datetime(
                    2026, 6, 29, 13, tzinfo=timezone.utc
                ),
                "loaded_tonnes": None,
                "offloaded_tonnes": None,
            },
            {
                "job_reference": "ORDBULK-4",
                "transport_allocation_created_at": datetime(
                    2026, 6, 29, 14, tzinfo=timezone.utc
                ),
                "offloading_point": "OP-Bulk Connection",
                "offloading_slab": "Island View N",
                "loaded_tonnes": None,
                "offloaded_tonnes": Decimal("10"),
                "offloading_signed_off_at": datetime(
                    2026, 6, 29, 16, tzinfo=timezone.utc
                ),
            },
        ]

        study = _order_study(
            rows,
            order_reference="KFTS26-11MG",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 7, 1),
            expected_bays=("BCF Bay 5", "Island View N", "Island View S"),
        )

        metrics = study["metrics"]
        self.assertEqual(metrics["loaded_tonnes"], Decimal("75"))
        self.assertEqual(metrics["offloaded_tonnes"], Decimal("49.9"))
        self.assertEqual(
            metrics["net_movement_difference_tonnes"],
            Decimal("-25.1"),
        )
        self.assertEqual(metrics["completed_variance_tonnes"], Decimal("-0.1"))
        self.assertEqual(metrics["pending_offloads"], 1)
        self.assertEqual(metrics["unexpected_bay_movements"], 1)
        self.assertEqual(metrics["allocations_without_loading"], 1)
        self.assertEqual(len(study["routes"]), 3)
        pending = next(
            row
            for row in study["movements"]
            if row["job_reference"] == "ORDBULK-2"
        )
        self.assertEqual(
            pending["destination_bay"],
            "Invalid / unresolved (OPUS: Bay 5)",
        )
        self.assertIn("Unexpected planned offloading bay", pending["audit_reasons"])
        self.assertEqual(
            next(
                row
                for row in study["bay_flows"]
                if row["direction"] == "Delivered out"
                and row["point"] == "LP-Kookfontein"
            )["expected_status"],
            "Location fallback",
        )

    def test_order_study_flags_zero_loaded_weight(self) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-ZERO",
                    "transport_allocation_created_at": datetime(
                        2026, 6, 29, 8, tzinfo=timezone.utc
                    ),
                    "loading_point": "LP-Base Chrome Fields",
                    "loading_slab": "Bay 5",
                    "loaded_tonnes": Decimal("0"),
                    "offloading_point": "OP-Bulk Connection",
                    "offloading_slab": "Island View N",
                    "offloaded_tonnes": Decimal("50"),
                }
            ],
            order_reference="KFTS26-11MG",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 7, 1),
            expected_bays=("BCF Bay 5", "Island View N", "Island View S"),
        )

        self.assertEqual(study["metrics"]["weight_variance_exceptions"], 1)
        self.assertEqual(study["movements"][0]["audit_status"], "Review")
        self.assertIn(
            "zero or negative",
            study["movements"][0]["audit_reasons"],
        )
        self.assertEqual(len(study["exceptions"]), 1)

    def test_order_study_derives_unconfigured_routes_and_bays_from_opus(
        self,
    ) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-DYNAMIC",
                    "transport_allocation_created_at": datetime(
                        2026, 8, 18, 8, tzinfo=timezone.utc
                    ),
                    "loading_point": "LP-Other Mine",
                    "loading_slab": "Stockpile A",
                    "loaded_tonnes": Decimal("35"),
                    "offloading_point": "OP-Other Destination",
                    "offloading_slab": "Bay Z",
                    "offloaded_tonnes": Decimal("35"),
                }
            ],
            order_reference="DYNAMIC-ORDER",
            date_from=date(2026, 8, 18),
            date_to=date(2026, 8, 18),
            expected_bays=(),
        )

        self.assertEqual(study["metrics"]["unexpected_bay_movements"], 0)
        self.assertEqual(study["movements"][0]["audit_status"], "Reconciled")
        self.assertEqual(
            {row["expected_status"] for row in study["bay_flows"]},
            {"Recorded in OPUS"},
        )
        other_leg = next(
            row for row in study["legs"] if row["route_name"] == "Other / review"
        )
        self.assertEqual(other_leg["movement_references"], 1)
        self.assertEqual(study["leg_lanes"][0]["from_bay"], "Stockpile A")
        self.assertEqual(study["leg_lanes"][0]["to_bay"], "Bay Z")

    def test_order_study_groups_signed_loads_by_truck_type(self) -> None:
        rows = []
        for index, truck_type in enumerate(("Tipper", "Tautliner", "Tipper")):
            rows.append(
                {
                    "job_reference": f"ORDBULK-TYPE-{index}",
                    "transport_allocation_created_at": datetime(
                        2026, 8, 18, 8 + index, tzinfo=timezone.utc
                    ),
                    "truck_type": truck_type,
                    "loading_point": "LP-Test Mine",
                    "loading_slab": "No Loading Slab",
                    "loaded_tonnes": Decimal("30"),
                    "loading_signed_off_at": datetime(
                        2026, 8, 18, 9 + index, tzinfo=timezone.utc
                    ),
                }
            )

        study = _order_study(
            rows,
            order_reference="LOAD-TYPES",
            date_from=date(2026, 8, 18),
            date_to=date(2026, 8, 18),
            expected_bays=(),
        )

        self.assertEqual(
            study["load_types"],
            [
                {
                    "truck_type": "Tipper",
                    "load_count": 2,
                    "loaded_tonnes": Decimal("60"),
                    "load_pct": Decimal("66.7"),
                },
                {
                    "truck_type": "Tautliner",
                    "load_count": 1,
                    "loaded_tonnes": Decimal("30"),
                    "load_pct": Decimal("33.3"),
                },
            ],
        )

    def test_order_study_reconciles_bcf_staging_opening_and_flow(self) -> None:
        common = {
            "transport_allocation_created_at": datetime(
                2026, 6, 29, 8, tzinfo=timezone.utc
            )
        }
        study = _order_study(
            [
                {
                    **common,
                    "job_reference": "ORDBULK-MINE-BCF",
                    "loading_point": "LP-Kookfontein",
                    "loading_slab": "No Loading Slab",
                    "loaded_tonnes": Decimal("50"),
                    "offloading_point": "OP-Base Chrome Fields",
                    "offloading_slab": "Bay 1",
                    "offloaded_tonnes": Decimal("49"),
                },
                {
                    **common,
                    "job_reference": "ORDBULK-BCF-BC",
                    "loading_point": "LP-Base Chrome Fields",
                    "loading_slab": "Bay 1",
                    "loaded_tonnes": Decimal("40"),
                    "offloading_point": "OP-Bulk Connection",
                    "offloading_slab": "Island View R",
                    "offloaded_tonnes": Decimal("40"),
                },
                {
                    **common,
                    "job_reference": "ORDBULK-DIRECT",
                    "loading_point": "LP-Kookfontein",
                    "loading_slab": "No Loading Slab",
                    "loaded_tonnes": Decimal("10"),
                    "offloading_point": "OP-Bulk Connection",
                    "offloading_slab": "Island View R",
                    "offloaded_tonnes": Decimal("10"),
                },
            ],
            order_reference="KFTS26-10M",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 7, 1),
            expected_bays=("BCF Bay 1", "Island View R"),
            opening_balances=[
                {
                    "effective_date": date(2026, 6, 29),
                    "location_name": "LP-Base Chrome Fields",
                    "storage_identifier": "Bay 1",
                    "opening_tonnes": Decimal("100"),
                }
            ],
        )

        metrics = study["metrics"]
        self.assertEqual(metrics["mine_dispatch_tonnes"], Decimal("60"))
        self.assertEqual(metrics["bcf_mine_receipts_tonnes"], Decimal("49"))
        self.assertEqual(metrics["bcf_dispatch_tonnes"], Decimal("40"))
        self.assertEqual(metrics["bcf_closing_tonnes"], Decimal("109"))
        self.assertEqual(metrics["bc_receipts_tonnes"], Decimal("50"))
        self.assertEqual(metrics["bcf_stock_tonnes"], Decimal("109"))
        self.assertEqual(metrics["bc_stock_tonnes"], Decimal("50"))
        self.assertEqual(
            metrics["stock_at_recorded_locations_tonnes"],
            Decimal("159"),
        )
        self.assertEqual(study["staging"][0]["bay"], "BCF Bay 1")
        self.assertEqual(study["staging"][0]["closing_tonnes"], Decimal("109"))
        self.assertEqual(study["bc_areas"][0]["area"], "Island View R")
        self.assertEqual(study["bc_areas"][0]["closing_tonnes"], Decimal("50"))
        self.assertEqual(
            [row["route_name"] for row in study["legs"]],
            ["Mine to BCF", "BCF to BC", "Mine to BC Direct"],
        )
        self.assertEqual(study["legs"][0]["loaded_tonnes"], Decimal("50"))
        self.assertEqual(study["legs"][1]["offloaded_tonnes"], Decimal("40"))
        self.assertEqual(study["legs"][2]["offloaded_tonnes"], Decimal("10"))

    def test_order_study_uses_sast_business_dates(self) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-MIDNIGHT",
                    "transport_allocation_created_at": datetime(
                        2026, 6, 28, 22, 30, tzinfo=timezone.utc
                    ),
                    "loading_point": "LP-Kookfontein",
                    "loading_slab": "No Loading Slab",
                    "loaded_tonnes": Decimal("40"),
                    "loading_signed_off_at": datetime(
                        2026, 6, 28, 23, 30, tzinfo=timezone.utc
                    ),
                    "offloading_point": "OP-Bulk Connection",
                    "offloading_slab": "Island View N",
                    "offloaded_tonnes": Decimal("40"),
                    "offloading_signed_off_at": datetime(
                        2026, 6, 29, 0, 30, tzinfo=timezone.utc
                    ),
                }
            ],
            order_reference="KFTS26-11MG",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 7, 1),
            expected_bays=("Island View N",),
        )

        self.assertEqual(study["first_root_date"], date(2026, 6, 29))
        self.assertEqual(study["movements"][0]["root_date"], date(2026, 6, 29))
        self.assertEqual(
            [row["activity_date"] for row in study["daily"]],
            [date(2026, 6, 29)],
        )

    def test_order_study_does_not_treat_first_inactive_day_as_missing_data(
        self,
    ) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-FIRST-ACTIVITY",
                    "transport_allocation_created_at": datetime(
                        2026, 6, 30, 8, tzinfo=timezone.utc
                    ),
                }
            ],
            order_reference="KFTS26-11MG",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 7, 1),
            expected_bays=("Island View N",),
        )

        self.assertEqual(study["coverage_warning"], "")
        self.assertEqual(
            study["coverage_note"],
            (
                "No Transport Allocation roots are recorded from 29 Jun 2026 "
                "through 29 Jun 2026; the first recorded order activity is "
                "30 Jun 2026."
            ),
        )

    def test_route_bay_plan_keeps_unlisted_observed_combinations_visible(
        self,
    ) -> None:
        rows, unlisted = _route_bay_reconciliation(
            [
                {
                    "route_name": "Mine to BCF",
                    "origin": "LP-Kookfontein - Kookfontein",
                    "origin_bay": "LP-Kookfontein - Kookfontein",
                    "destination": "OP-Base Chrome Fields - Base Chrome Fields",
                    "destination_bay": "BCF Bay 5",
                    "loaded_tonnes": Decimal("40"),
                    "offloaded_tonnes": Decimal("39"),
                },
                {
                    "route_name": "BCF to BC",
                    "origin": "LP-Base Chrome Fields - Base Chrome Fields",
                    "origin_bay": "BCF Bay 2",
                    "destination": "OP-Bulk Connection - Bulk Connection",
                    "destination_bay": "Island View N",
                    "loaded_tonnes": Decimal("38"),
                    "offloaded_tonnes": Decimal("38"),
                },
            ],
            (
                (
                    "Mine to BCF",
                    "Kookfontein",
                    "BCF",
                    "From Mine",
                    "BCF Bay 5",
                ),
                (
                    "BCF to BC",
                    "BCF",
                    "BC",
                    "BCF Bay 5",
                    "Island View N",
                ),
            ),
        )

        self.assertEqual(unlisted, 1)
        self.assertEqual(
            [row["plan_status"] for row in rows],
            [
                "Planned route observed",
                "Planned route - no OPUS movement",
                "Observed in OPUS but not in route plan - review",
            ],
        )
        self.assertEqual(rows[0]["from_bay"], "From Mine")
        self.assertEqual(rows[2]["from_bay"], "BCF Bay 2")

    def test_order_study_applies_event_cutoff_before_stock_calculation(self) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-CUTOFF",
                    "transport_allocation_created_at": datetime(
                        2026, 6, 29, 8, tzinfo=timezone.utc
                    ),
                    "loading_point": "LP-Base Chrome Fields",
                    "loading_slab": "Bay 1",
                    "loaded_tonnes": Decimal("40"),
                    "loading_signed_off_at": datetime(
                        2026, 8, 5, 10, tzinfo=timezone.utc
                    ),
                    "offloading_point": "OP-Bulk Connection",
                    "offloading_slab": "Island View R",
                    "offloaded_tonnes": Decimal("39"),
                    "offloading_signed_off_at": datetime(
                        2026, 8, 6, 10, tzinfo=timezone.utc
                    ),
                }
            ],
            order_reference="KFTS26-10M",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 8, 5),
            expected_bays=("BCF Bay 1", "Island View R"),
            opening_balances=[
                {
                    "effective_date": date(2026, 6, 29),
                    "stock_role": "Origin",
                    "location_name": "LP-Base Chrome Fields",
                    "storage_identifier": "Bay 1",
                    "opening_tonnes": Decimal("100"),
                }
            ],
            route_plan=(
                ("BCF to BC", "BCF", "BC", "BCF Bay 1", "Island View R"),
            ),
            parcel_tonnes=100,
            event_cutoff=datetime(2026, 8, 5, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(study["metrics"]["bcf_stock_tonnes"], Decimal("60"))
        self.assertEqual(study["metrics"]["bc_stock_tonnes"], Decimal("0"))
        self.assertEqual(
            study["metrics"]["stock_at_recorded_locations_tonnes"],
            Decimal("60"),
        )
        self.assertEqual(study["metrics"]["in_transit_tonnes"], Decimal("40"))
        self.assertEqual(study["legs"][1]["pending_references"], 1)
        self.assertEqual(study["bc_areas"][0]["opening_tonnes"], Decimal("0"))
        self.assertEqual(
            study["bc_areas"][0]["opening_status"],
            "No opening recorded - treated as zero",
        )

    def test_point_level_opening_and_movement_share_one_balance_row(self) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-POINT",
                    "transport_allocation_created_at": datetime(
                        2026, 6, 29, 8, tzinfo=timezone.utc
                    ),
                    "loading_point": "LP-Kookfontein",
                    "loading_slab": "No Loading Slab",
                    "loaded_tonnes": Decimal("10"),
                    "offloading_point": "OP-Bulk Connection",
                    "offloading_slab": "No Offloading Slab",
                    "offloaded_tonnes": Decimal("10"),
                }
            ],
            order_reference="KFTS26-11MG",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 8, 5),
            expected_bays=(),
            opening_balances=[
                {
                    "effective_date": date(2026, 6, 29),
                    "stock_role": "Destination",
                    "location_name": "OP-Bulk Connection",
                    "storage_identifier": "OP-Bulk Connection",
                    "opening_tonnes": Decimal("100"),
                }
            ],
        )

        self.assertEqual(len(study["bc_areas"]), 1)
        self.assertEqual(
            study["bc_areas"][0]["area"],
            "BC point-level / no bay",
        )
        self.assertEqual(study["bc_areas"][0]["opening_tonnes"], Decimal("100"))
        self.assertEqual(study["bc_areas"][0]["closing_tonnes"], Decimal("110"))

    def test_unclassified_bcf_dispatch_still_reduces_bcf_stock(self) -> None:
        study = _order_study(
            [
                {
                    "job_reference": "ORDBULK-OTHER",
                    "transport_allocation_created_at": datetime(
                        2026, 6, 29, 8, tzinfo=timezone.utc
                    ),
                    "loading_point": "LP-Base Chrome Fields",
                    "loading_slab": "Bay 1",
                    "loaded_tonnes": Decimal("10"),
                    "offloading_point": "Unexpected destination",
                    "offloading_slab": "Area 1",
                    "offloaded_tonnes": Decimal("10"),
                }
            ],
            order_reference="KFTS26-10M",
            date_from=date(2026, 6, 29),
            date_to=date(2026, 8, 5),
            expected_bays=("BCF Bay 1",),
            opening_balances=[
                {
                    "effective_date": date(2026, 6, 29),
                    "stock_role": "Origin",
                    "location_name": "LP-Base Chrome Fields",
                    "storage_identifier": "Bay 1",
                    "opening_tonnes": Decimal("100"),
                }
            ],
        )

        self.assertEqual(study["movements"][0]["route_name"], "Other / review")
        self.assertEqual(
            study["staging"][0]["bcf_dispatch_tonnes"],
            Decimal("10"),
        )
        self.assertEqual(study["metrics"]["bcf_stock_tonnes"], Decimal("90"))

    def test_checklist_instance_groups_exclude_bulk_and_follow_stage_order(
        self,
    ) -> None:
        snapshot = DashboardSnapshot(
            connected=True,
            captured_at=datetime.now(timezone.utc),
            period="all",
            order_workflows=[
                {
                    "checklist_name": "Vehicle Inspection",
                    "stage_order": 2.1,
                    "job_row_id": 2,
                },
                {
                    "checklist_name": "Transport Allocation",
                    "stage_order": 1,
                    "job_row_id": 1,
                },
                {
                    "checklist_name": (
                        "Bulk Import for Minerals Transport Allocation"
                    ),
                    "stage_order": 0,
                    "job_row_id": 0,
                },
            ],
        )

        self.assertEqual(
            [name for name, _ in _checklist_instance_groups(snapshot)],
            ["Transport Allocation", "Vehicle Inspection"],
        )

    def test_checklist_page_rows_sorts_and_returns_only_requested_page(self) -> None:
        rows = [
            {"job_row_id": index, "job_reference": f"ORDBULK-{index}"}
            for index in range(1, 21)
        ]

        page = _checklist_page_rows(
            rows,
            {
                "page": 2,
                "rowsPerPage": 5,
                "sortBy": "job_reference",
                "descending": True,
            },
        )

        self.assertEqual(
            [row["job_reference"] for row in page],
            [
                "ORDBULK-15",
                "ORDBULK-14",
                "ORDBULK-13",
                "ORDBULK-12",
                "ORDBULK-11",
            ],
        )

    def test_load_checklist_detail_returns_summary_and_answers(self) -> None:
        cursor = FakeCursor(
            {
                "job_row_id": 51,
                "job_reference": "ORDBULK-59695",
                "checklist_name": "Transport Allocation",
                "checklist_instance_id": 71,
                "source_created_at": datetime(2026, 7, 20, 8, 15, tzinfo=timezone.utc),
            },
            [
                {
                    "answer_row_id": 7,
                    "question": "Client",
                    "answer": "Example client",
                }
            ],
        )
        repository = OperationsRepository(Settings())
        with patch.object(
            repository,
            "connection",
            return_value=fake_connection(cursor),
        ):
            detail = repository.load_checklist_detail(51)

        self.assertEqual(detail["summary"]["job_reference"], "ORDBULK-59695")
        self.assertIn("20 Jul 2026", detail["summary"]["source_created_at"])
        self.assertEqual(detail["answers"][0]["question"], "Client")
        self.assertEqual(
            [parameters for _, parameters in cursor.executions],
            [(51,), (71,)],
        )

    def test_load_checklist_detail_rejects_missing_job(self) -> None:
        cursor = FakeCursor(None)
        repository = OperationsRepository(Settings())
        with patch.object(
            repository,
            "connection",
            return_value=fake_connection(cursor),
        ):
            with self.assertRaisesRegex(LookupError, "999 was not found"):
                repository.load_checklist_detail(999)


if __name__ == "__main__":
    unittest.main()
