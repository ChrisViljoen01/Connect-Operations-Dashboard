from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opus_dashboard.credentials import read_windows_credential  # noqa: E402


ADMIN_TARGET = os.getenv(
    "OPUS_PG_ADMIN_CREDENTIAL_TARGET",
    "ConnectLogisticsOps/PostgreSQL/postgres",
)


def _execute_file(connection: psycopg.Connection[object], path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        cursor.execute(sql)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply owner migrations and reset OPUS operational data.",
    )
    parser.add_argument(
        "action",
        choices=("migrate", "migrate-and-reset"),
    )
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="Required with migrate-and-reset.",
    )
    args = parser.parse_args()
    if args.action == "migrate-and-reset" and not args.confirm_reset:
        parser.error("migrate-and-reset requires --confirm-reset")

    credential = read_windows_credential(ADMIN_TARGET)
    if credential is None or not credential.password:
        raise RuntimeError(
            "PostgreSQL owner credential is missing from Windows Credential "
            f"Manager target {ADMIN_TARGET!r}. Run save_database_admin_credential.py."
        )

    connection = psycopg.connect(
        host=os.getenv("OPUS_DB_HOST", "localhost"),
        port=int(os.getenv("OPUS_DB_PORT", "5432")),
        dbname=os.getenv("OPUS_DB_NAME", "connect_logistics_ops"),
        user=credential.username or "postgres",
        password=credential.password,
        autocommit=True,
        connect_timeout=5,
        application_name="connect_logistics_opus_admin",
    )
    try:
        for version, migration_name in (
            ("006", "06_raw_record_source_hash.sql"),
            ("007", "07_transport_root_baseline.sql"),
            ("008", "08_operational_analytics.sql"),
            ("009", "09_stock_transit_dimensions.sql"),
        ):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM audit.schema_migrations
                        WHERE version = %s
                    )
                    """,
                    (version,),
                )
                applied = bool(cursor.fetchone()[0])
            if applied:
                print(f"Skipped {migration_name}; version {version} is applied")
                continue
            _execute_file(connection, PROJECT_ROOT / "db" / migration_name)
            print(f"Applied {migration_name}")
        if args.action == "migrate-and-reset":
            _execute_file(
                connection,
                PROJECT_ROOT / "db" / "reset_operational_data.sql",
            )
            print("Operational and ingestion data reset completed")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
