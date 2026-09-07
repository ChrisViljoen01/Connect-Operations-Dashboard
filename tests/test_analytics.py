from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
import unittest

from opus_dashboard.analytics import (
    delivery_metrics,
    normalize_status,
    parse_nett_weight_tonnes,
    resolve_transit,
    storage_identifier,
    WorkflowAttempt,
)


class AnalyticsRuleTests(unittest.TestCase):
    def test_status_normalization_prioritizes_terminal_states(self) -> None:
        self.assertEqual(normalize_status("Operator Signed Off"), "Signed off")
        self.assertEqual(normalize_status("Closed - Not Started"), "Closed")
        self.assertEqual(normalize_status("Job Closed"), "Closed")
        self.assertEqual(normalize_status("Pending Start"), "Not started")
        self.assertEqual(
            normalize_status("Awaiting Supervisor Review"),
            "Under review",
        )

    def test_nett_weight_converts_current_opus_kilograms_to_tonnes(self) -> None:
        self.assertEqual(parse_nett_weight_tonnes("36750"), Decimal("36.750"))
        self.assertEqual(parse_nett_weight_tonnes("36.750"), Decimal("36.750"))
        self.assertIsNone(parse_nett_weight_tonnes("not-a-number"))
        self.assertIsNone(parse_nett_weight_tonnes(""))
        self.assertIsNone(parse_nett_weight_tonnes("-2000"))
        self.assertIsNone(parse_nett_weight_tonnes("999"))

    def test_storage_fallback_requires_applicable_explicit_sentinel(self) -> None:
        self.assertEqual(
            storage_identifier(
                "No Loading Slab",
                "Montrose",
                loading=True,
            ),
            ("Montrose", None),
        )
        self.assertEqual(
            storage_identifier(
                "No Loading Slab",
                "Durban",
                loading=False,
            ),
            ("No Loading Slab", None),
        )
        self.assertEqual(
            storage_identifier("", "Montrose", loading=True),
            (None, "missing_or_invalid_slab"),
        )

    def test_delivery_metrics_exclude_pending_and_apply_threshold(self) -> None:
        self.assertEqual(
            delivery_metrics(Decimal("40"), None),
            (None, None, "Pending / in transit"),
        )
        self.assertEqual(
            delivery_metrics(Decimal("40"), Decimal("39.9")),
            (
                Decimal("-0.100"),
                Decimal("99.750"),
                "Within tolerance",
            ),
        )
        self.assertEqual(
            delivery_metrics(Decimal("40"), Decimal("39.8")),
            (
                Decimal("-0.200"),
                Decimal("99.500"),
                "Below tolerance",
            ),
        )

    def test_intermediate_closure_is_superseded_by_later_workflow(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Signed Off",
                    start - timedelta(hours=1),
                    source_signed_off_at=start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start,
                    source_signed_off_at=start + timedelta(hours=1),
                ),
                WorkflowAttempt(
                    "vehicle_inspection",
                    "Job Closed",
                    start + timedelta(hours=2),
                ),
                WorkflowAttempt(
                    "staging_arrival",
                    "Operator Not Started",
                    start + timedelta(hours=3),
                ),
            ]
        )
        self.assertTrue(decision.in_transit)
        self.assertFalse(decision.stopped_after_closure)

    def test_final_closure_stops_transit(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Signed Off",
                    start - timedelta(hours=1),
                    source_signed_off_at=start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start,
                    source_signed_off_at=start + timedelta(hours=1),
                ),
                WorkflowAttempt(
                    "staging_arrival",
                    "Job Closed",
                    start + timedelta(hours=2),
                ),
            ]
        )
        self.assertFalse(decision.in_transit)
        self.assertTrue(decision.stopped_after_closure)

    def test_latest_offloading_clone_controls_transit_boundary(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Signed Off",
                    start - timedelta(hours=1),
                    source_signed_off_at=start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start,
                    source_signed_off_at=start + timedelta(hours=1),
                ),
                WorkflowAttempt(
                    "offloading_exit",
                    "Job Closed",
                    start + timedelta(hours=2),
                    operator_started_at=start + timedelta(hours=2),
                ),
                WorkflowAttempt(
                    "offloading_exit",
                    "Operator Not Started",
                    start + timedelta(hours=3),
                ),
            ]
        )
        self.assertTrue(decision.in_transit)
        self.assertIsNone(decision.offloading_attempt.operator_started_at)

    def test_latest_loading_clone_must_be_signed_off(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Signed Off",
                    start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start + timedelta(hours=1),
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Not Started",
                    start + timedelta(hours=2),
                ),
            ]
        )
        self.assertFalse(decision.in_transit)
        self.assertEqual(
            decision.exclusion_reason,
            "Latest Loading and Exit is not signed off",
        )

    def test_signed_off_offloading_never_remains_in_transit(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Signed Off",
                    start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start + timedelta(hours=2),
                    source_signed_off_at=start + timedelta(hours=3),
                ),
                WorkflowAttempt(
                    "offloading_exit",
                    "Operator Signed Off",
                    start + timedelta(hours=4),
                    operator_started_at=start + timedelta(hours=1),
                    source_signed_off_at=start + timedelta(hours=5),
                ),
            ]
        )
        self.assertFalse(decision.in_transit)
        self.assertEqual(
            decision.exclusion_reason,
            "Latest Offloading and Exit has started",
        )

    def test_transport_allocation_must_be_signed_off(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Not Started",
                    start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start + timedelta(hours=1),
                ),
            ]
        )
        self.assertFalse(decision.in_transit)
        self.assertEqual(
            decision.exclusion_reason,
            "Latest Transport Allocation is not signed off",
        )

    def test_contradictory_not_started_offload_is_not_in_transit(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        decision = resolve_transit(
            [
                WorkflowAttempt(
                    "transport_allocation",
                    "Operator Signed Off",
                    start,
                ),
                WorkflowAttempt(
                    "loading_exit",
                    "Operator Signed Off",
                    start + timedelta(hours=1),
                ),
                WorkflowAttempt(
                    "offloading_exit",
                    "Operator Not Started",
                    start + timedelta(hours=2),
                    source_completed_at=start + timedelta(hours=3),
                ),
            ]
        )
        self.assertFalse(decision.in_transit)
        self.assertEqual(
            decision.exclusion_reason,
            "Latest Offloading and Exit has started",
        )
