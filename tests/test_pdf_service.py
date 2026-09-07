from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opus_dashboard.pdf_service import create_order_study_pdf


class PdfServiceTests(unittest.TestCase):
    def test_order_study_pdf_contains_all_management_sections(self) -> None:
        study = {
            "order_reference": "KFTS26-11MG",
            "client_name": "Pelagic",
            "date_from": "29 Jun 2026",
            "date_to": "05 Aug 2026",
            "coverage_warning": "",
            "coverage_note": "First recorded activity is 30 Jun 2026.",
            "metrics": {
                "parcel_tonnes": 100,
                "opening_stock_tonnes": 0,
                "bcf_stock_tonnes": -10,
                "bc_stock_tonnes": 58,
                "stock_at_recorded_locations_tonnes": 48,
                "in_transit_tonnes": 2,
                "review_references": 4,
                "loaded_tonnes": 100,
                "offloaded_tonnes": 98,
                "net_movement_difference_tonnes": -2,
                "completed_variance_tonnes": -1,
                "delivery_pct": 99,
                "movement_references": 3,
                "pending_offloads": 1,
                "allocations_without_loading": 2,
                "unexpected_bay_movements": 1,
                "unlisted_route_movements": 1,
                "weight_variance_exceptions": 1,
                "mine_dispatch_tonnes": 60,
                "bcf_opening_tonnes": 0,
                "bcf_mine_receipts_tonnes": 40,
                "bcf_dispatch_tonnes": 50,
                "bcf_closing_tonnes": -10,
                "bc_receipts_tonnes": 58,
            },
            "exceptions": [{"job_reference": "ORDBULK-1"}],
            "route_plan": [
                {
                    "plan_status": "Planned route observed",
                    "route_name": "Mine to BCF",
                    "origin": "Kookfontein",
                    "from_bay": "From Mine",
                    "destination": "BCF",
                    "to_bay": "BCF Bay 5",
                    "movement_references": 1,
                    "loaded_tonnes": 40,
                    "offloaded_tonnes": 39,
                },
                {
                    "plan_status": (
                        "Observed in OPUS but not in route plan - review"
                    ),
                    "route_name": "BCF to BC",
                    "origin": "BCF",
                    "from_bay": "BCF Bay 2",
                    "destination": "BC",
                    "to_bay": "Island View N",
                    "movement_references": 1,
                    "loaded_tonnes": 50,
                    "offloaded_tonnes": 49,
                },
            ],
            "legs": [
                {
                    "route_name": "Mine to BCF",
                    "loaded_tonnes": 40,
                    "offloaded_tonnes": 39,
                    "movement_difference_tonnes": -1,
                    "pending_references": 0,
                    "pending_loaded_tonnes": 0,
                },
                {
                    "route_name": "BCF to BC",
                    "loaded_tonnes": 50,
                    "offloaded_tonnes": 49,
                    "movement_difference_tonnes": -1,
                    "pending_references": 0,
                    "pending_loaded_tonnes": 0,
                },
                {
                    "route_name": "Mine to BC Direct",
                    "loaded_tonnes": 10,
                    "offloaded_tonnes": 10,
                    "movement_difference_tonnes": 0,
                    "pending_references": 0,
                    "pending_loaded_tonnes": 0,
                },
            ],
            "staging": [
                {
                    "bay": "BCF Bay 5",
                    "effective_date": "29 Jun 2026",
                    "opening_status": "Supplied",
                    "opening_tonnes": 0,
                    "mine_receipts_tonnes": 40,
                    "other_receipts_tonnes": 0,
                    "bcf_dispatch_tonnes": 50,
                    "closing_tonnes": -10,
                }
            ],
            "bay_flows": [
                {
                    "direction": "Delivered in",
                    "point": "OP-Base Chrome Fields - Base Chrome Fields",
                    "bay": "BCF Bay 5",
                    "storage_source": "Checklist bay",
                    "expected_status": "Expected",
                    "movement_count": 1,
                    "tonnes": 39,
                }
            ],
            "bc_areas": [
                {
                    "area": "Island View N",
                    "opening_status": "No opening recorded - treated as zero",
                    "opening_tonnes": 0,
                    "received_from_bcf_tonnes": 49,
                    "received_direct_from_mine_tonnes": 9,
                    "total_received_tonnes": 58,
                    "closing_tonnes": 58,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            result = create_order_study_pdf(path, study)
            payload = path.read_bytes()

        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["leg_rows"], 3)
        self.assertEqual(result["route_rows"], 2)
        self.assertEqual(result["staging_rows"], 1)
        self.assertEqual(result["bc_rows"], 1)
        self.assertEqual(result["bay_rows"], 2)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertGreater(len(payload), 5_000)


if __name__ == "__main__":
    unittest.main()
