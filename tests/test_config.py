from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from opus_dashboard.config import Settings, _date_setting, _validate_date_window


class ConfigTests(unittest.TestCase):
    def test_default_baseline_starts_on_order_study_date(self) -> None:
        settings = Settings()
        self.assertEqual(settings.opus_extract_from, date(2026, 6, 29))
        self.assertEqual(settings.opus_scan_chunk_days, 1)
        self.assertEqual(settings.opus_sync_minutes, 2)
        self.assertEqual(settings.opus_live_lookback_days, 1)
        self.assertEqual(settings.opus_live_detail_batch_size, 10)
        self.assertEqual(settings.opus_backlog_lookback_days, 7)
        self.assertEqual(settings.opus_active_audit_batch_size, 25)
        self.assertEqual(settings.opus_audit_batch_size, 25)
        self.assertEqual(settings.opus_active_sweep_minutes, 4)
        self.assertEqual(settings.opus_audit_minutes, 60)

    def test_extract_from_date_uses_iso_format(self) -> None:
        with patch.dict("os.environ", {"OPUS_EXTRACT_FROM": "2026-07-20"}):
            self.assertEqual(
                _date_setting("OPUS_EXTRACT_FROM", date(2026, 1, 1)),
                date(2026, 7, 20),
            )

    def test_extract_from_date_rejects_ambiguous_format(self) -> None:
        with patch.dict("os.environ", {"OPUS_EXTRACT_FROM": "01/07/2026"}):
            with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                _date_setting("OPUS_EXTRACT_FROM", date(2026, 1, 1))

    def test_extract_window_is_inclusive_and_ordered(self) -> None:
        _validate_date_window(date(2026, 7, 20), date(2026, 7, 26))
        with self.assertRaisesRegex(ValueError, "on or after"):
            _validate_date_window(date(2026, 7, 26), date(2026, 7, 20))

    def test_effective_extract_to_uses_today_when_env_not_set(self) -> None:
        settings = Settings(opus_extract_to=date(2026, 7, 26))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(settings.effective_extract_to(), date.today())

    def test_effective_extract_to_honours_explicit_env_override(self) -> None:
        settings = Settings(opus_extract_to=date(2026, 7, 26))
        with patch.dict("os.environ", {"OPUS_EXTRACT_TO": "2026-07-26"}):
            self.assertEqual(settings.effective_extract_to(), date(2026, 7, 26))


if __name__ == "__main__":
    unittest.main()