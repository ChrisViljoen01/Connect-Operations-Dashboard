from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opus_dashboard.config import settings  # noqa: E402
from opus_dashboard.repository import OperationsRepository  # noqa: E402


def main() -> None:
    repository = OperationsRepository(settings)
    with repository.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT ops.refresh_operational_facts(NULL) AS refreshed")
            refreshed = int((cursor.fetchone() or {}).get("refreshed") or 0)
        connection.commit()
    print(f"Rebuilt {refreshed:,} operational checklist facts.")


if __name__ == "__main__":
    main()
