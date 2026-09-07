from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = PROJECT_ROOT / ".logos"
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _int_setting(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number, not {raw!r}.") from exc


def _bool_setting(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, not {raw!r}.")


def _date_setting(name: str, default: date) -> date:
    raw = os.getenv(name, default.isoformat()).strip()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD format, not {raw!r}.") from exc


def _validate_date_window(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise ValueError("OPUS_EXTRACT_TO must be on or after OPUS_EXTRACT_FROM.")


@dataclass(frozen=True, slots=True)
class Settings:
    db_host: str = os.getenv("OPUS_DB_HOST", "localhost").strip()
    db_port: int = _int_setting("OPUS_DB_PORT", 5432)
    db_name: str = os.getenv("OPUS_DB_NAME", "connect_logistics_ops").strip()
    db_user: str = os.getenv("OPUS_DB_USER", "connect_ops_app").strip()
    db_password_env: str = os.getenv("OPUS_DB_APP_PASSWORD", "")
    credential_target: str = os.getenv(
        "OPUS_DB_CREDENTIAL_TARGET",
        "ConnectLogisticsOps/PostgreSQL/connect_ops_app",
    ).strip()
    opus_api_url: str = os.getenv(
        "OPUS_API_URL",
        "https://appsvc.opus4business.com/api/",
    ).strip()
    opus_credential_target: str = os.getenv(
        "OPUS_APP_CREDENTIAL_TARGET",
        "ConnectLogisticsOps/OPUS/app.opus4business.com",
    ).strip()
    opus_sync_minutes: int = _int_setting("OPUS_SYNC_MINUTES", 2)
    opus_full_sync_hours: int = _int_setting("OPUS_FULL_SYNC_HOURS", 24)
    opus_request_timeout: int = _int_setting("OPUS_REQUEST_TIMEOUT", 45)
    opus_read_attempts: int = _int_setting("OPUS_READ_ATTEMPTS", 3)
    opus_retry_backoff_seconds: int = _int_setting(
        "OPUS_RETRY_BACKOFF_SECONDS",
        2,
    )
    opus_detail_workers: int = _int_setting("OPUS_DETAIL_WORKERS", 8)
    opus_section_workers: int = _int_setting("OPUS_SECTION_WORKERS", 4)
    opus_scan_chunk_days: int = _int_setting("OPUS_SCAN_CHUNK_DAYS", 1)
    opus_live_lookback_days: int = _int_setting("OPUS_LIVE_LOOKBACK_DAYS", 1)
    opus_live_detail_batch_size: int = _int_setting(
        "OPUS_LIVE_DETAIL_BATCH_SIZE",
        10,
    )
    opus_backlog_lookback_days: int = _int_setting(
        "OPUS_BACKLOG_LOOKBACK_DAYS",
        7,
    )
    opus_active_audit_batch_size: int = _int_setting(
        "OPUS_ACTIVE_AUDIT_BATCH_SIZE",
        25,
    )
    opus_audit_batch_size: int = _int_setting("OPUS_AUDIT_BATCH_SIZE", 25)
    opus_active_sweep_minutes: int = _int_setting(
        "OPUS_ACTIVE_SWEEP_MINUTES",
        4,
    )
    opus_audit_minutes: int = _int_setting("OPUS_AUDIT_MINUTES", 60)
    opus_extract_from: date = _date_setting(
        "OPUS_EXTRACT_FROM",
        date(2026, 6, 29),
    )
    opus_extract_to: date = _date_setting(
        "OPUS_EXTRACT_TO",
        date.today(),
    )
    opus_auto_sync: bool = _bool_setting("OPUS_AUTO_SYNC", True)
    app_host: str = os.getenv("OPUS_APP_HOST", "127.0.0.1").strip()
    app_port: int = _int_setting("OPUS_APP_PORT", 8091)
    app_title: str = "Connect Logistics Operations Dashboard"

    def __post_init__(self) -> None:
        _validate_date_window(self.opus_extract_from, self.opus_extract_to)

    def effective_extract_to(self) -> date:
        """
        Returns the effective extraction end date for a sync run.

        When OPUS_EXTRACT_TO is not explicitly set in the environment, this
        always returns today so that long-running processes never stop at the
        date the process started.  An explicit env override acts as a hard cap
        (useful in testing or to limit the window deliberately).
        """
        if os.getenv("OPUS_EXTRACT_TO", "").strip():
            return self.opus_extract_to
        return date.today()


settings = Settings()
