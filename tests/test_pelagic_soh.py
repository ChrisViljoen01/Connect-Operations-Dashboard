from __future__ import annotations

import unittest

from opus_dashboard.pelagic_soh import (
    compare_pelagic_soh,
    load_pelagic_soh_snapshot,
)


class PelagicSohTests(unittest.TestCase):
    def test_snapshot_reproduces_reported_order_and_bay_totals(self) -> None:
        snapshot = load_pelagic_soh_snapshot()
        orders = {
            row["order_reference"]: row for row in snapshot["orders"]
        }

        self.assertEqual(orders["KFTS26-10M"]["total_soh_tonnes"], 10000.0)
        self.assertEqual(orders["KFTS26-11MG"]["total_soh_tonnes"], 29968.48)
        self.assertEqual(
            [
                row["soh_tonnes"]
                for row in orders["KFTS26-11MG"]["locations"]
            ],
            [194.02, 20313.88, 9460.58],
        )

    def test_comparison_uses_stock_locations_and_keeps_variances_visible(self) -> None:
        comparison = compare_pelagic_soh(
            {
                "KFTS26-10M": {
                    "metrics": {
                        "bcf_stock_tonnes": 40,
                        "bc_stock_tonnes": 9960,
                        "stock_at_recorded_locations_tonnes": 10000,
                    },
                    "staging": [
                        {"bay": "BCF Bay 1", "closing_tonnes": 40}
                    ],
                    "bc_areas": [
                        {"area": "Island View R", "closing_tonnes": 9960}
                    ],
                    "legs": [],
                }
            }
        )

        self.assertEqual(len(comparison["orders"]), 1)
        self.assertEqual(
            comparison["orders"][0]["comparison_status"],
            "Matches within 0.01 t",
        )
        bay = next(
            row for row in comparison["bays"] if row["bay"] == "BCF Bay 1"
        )
        self.assertEqual(bay["variance_tonnes"], 1.54)
        self.assertEqual(bay["comparison_status"], "Difference identified")


if __name__ == "__main__":
    unittest.main()
