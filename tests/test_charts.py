from __future__ import annotations

import unittest

from opus_dashboard.charts import grouped_bar_options, movement_trend_options


class ChartOptionTests(unittest.TestCase):
    def test_grouped_bar_can_show_tonne_labels_and_units(self) -> None:
        options = grouped_bar_options(
            [{"bay": "BCF Bay 1", "opening": 100}],
            category_key="bay",
            series=[("Opening", "opening", "#1c2545")],
            horizontal=True,
            show_labels=True,
            value_suffix="t",
        )

        self.assertEqual(options["xAxis"]["name"], "Tonnes")
        self.assertTrue(options["series"][0]["label"]["show"])
        self.assertEqual(options["series"][0]["label"]["formatter"], "{c} t")

    def test_movement_trend_can_show_tonne_labels_and_units(self) -> None:
        options = movement_trend_options(
            [
                {
                    "activity_date": "05 Aug 2026",
                    "loaded_tonnes": 10,
                    "offloaded_tonnes": 9,
                }
            ],
            show_labels=True,
            value_suffix="t",
        )

        self.assertEqual(options["yAxis"]["name"], "Tonnes")
        self.assertTrue(options["series"][0]["label"]["show"])
        self.assertTrue(options["series"][1]["label"]["show"])


if __name__ == "__main__":
    unittest.main()
