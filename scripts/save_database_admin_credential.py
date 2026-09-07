from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opus_dashboard.credentials import write_windows_credential  # noqa: E402


def main() -> None:
    target = os.getenv(
        "OPUS_PG_ADMIN_CREDENTIAL_TARGET",
        "ConnectLogisticsOps/PostgreSQL/postgres",
    )
    username = input("PostgreSQL administrator [postgres]: ").strip() or "postgres"
    password = getpass.getpass("PostgreSQL administrator password: ")
    if not password:
        raise ValueError("A PostgreSQL administrator password is required.")
    write_windows_credential(target, username, password)
    print(f"Saved PostgreSQL administrator credential to {target}.")


if __name__ == "__main__":
    main()
