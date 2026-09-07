from __future__ import annotations

import unittest

from opus_dashboard.vessel_reconciliation import (
    build_vessel_reconciliation,
    load_pelagic_vessel_snapshot,
)


class VesselReconciliationTests(unittest.TestCase):
    def test_vessel_snapshot_reconciles_order_and_combined_totals(self) -> None:
        snapshot = load_pelagic_vessel_snapshot()

        self.assertAlmostEqual(
            snapshot["combined"]["grounded_at_bc_tonnes"],
            39786.6,
        )
        self.assertAlmostEqual(
            snapshot["combined"]["vessel_draft_tonnes"],
            38893.5,
        )
        self.assertAlmostEqual(
            snapshot["combined"]["reported_variance_tonnes"],
            -893.1,
        )

    def test_vessel_comparison_keeps_inbound_and_vessel_variances_separate(
        self,
    ) -> None:
        result = build_vessel_reconciliation(
            {
                "KFTS26-11MG": {
                    "metrics": {"bc_stock_tonnes": 29698.9}
                },
                "KFTS26-10M": {
                    "metrics": {"bc_stock_tonnes": 10009.76}
                },
            }
        )

        summary = result["summary"]
        self.assertAlmostEqual(summary["opus_bc_receipts_tonnes"], 39708.66)
        self.assertAlmostEqual(summary["pelagic_soh_bc_tonnes"], 39736.0)
        self.assertAlmostEqual(summary["pelagic_grounded_tonnes"], 39786.6)
        self.assertAlmostEqual(summary["vessel_draft_tonnes"], 38893.5)
        self.assertAlmostEqual(summary["opus_vs_grounded_tonnes"], -77.94)
        self.assertAlmostEqual(summary["grounded_to_draft_tonnes"], -893.1)
        self.assertAlmostEqual(summary["vessel_shortage_pct"], 2.245)

        ten_m = next(
            row
            for row in result["orders"]
            if row["order_reference"] == "KFTS26-10M"
        )
        self.assertAlmostEqual(ten_m["grounded_to_draft_tonnes"], -52.14)
        self.assertAlmostEqual(ten_m["vessel_shortage_pct"], 0.523)
        self.assertAlmostEqual(
            ten_m["manual_reported_variance_tonnes"],
            -52.92,
        )
        self.assertAlmostEqual(ten_m["share_of_vessel_variance_pct"], 5.838)
        self.assertTrue(
            any(
                "dispatch baseline" in row["finding"]
                for row in result["evidence"]
            )
        )
        inbound = next(
            row
            for row in result["evidence"]
            if row["finding"]
            == "OPUS and Pelagic inbound differences offset by order"
        )
        self.assertIn("126.160 t below", inbound["evidence"])
        self.assertIn("48.220 t above", inbound["evidence"])


if __name__ == "__main__":
    unittest.main()
