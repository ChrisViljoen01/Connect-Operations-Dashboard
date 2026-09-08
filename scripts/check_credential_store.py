"""Container smoke check for the non-Windows credential store.

Runs inside the image with no test dependencies, as the unprivileged user
the app actually runs as, against the real default path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from opus_dashboard.credential_file import default_credential_path
from opus_dashboard.sync import OpusCredentialStore

EMAIL = "container-check@example.com"
SECRET = "container-check-password-\u00e4\u4e2d\u6587"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {detail}")


print(f"platform: {sys.platform}")
print(f"user home: {Path.home()}")
print(f"default credential path: {default_credential_path()}")

store = OpusCredentialStore("ConnectLogisticsOps/OPUS/app.opus4business.com")
print(f"backend: {store.backend}")

check("no Windows requirement in backend", "Windows" not in store.backend)
check("starts with no saved login", store.read() is None)

# This is the exact call that failed for the user with
# "Windows Credential Manager is only available on Windows."
store.save(EMAIL, SECRET)
print("  ok    save() did not raise")

stored = store.read()
check("read returns the saved login", stored is not None)
check("username round trips", stored is not None and stored.username == EMAIL)
check("password round trips", stored is not None and stored.password == SECRET)

path = default_credential_path()
check("file written to the persisted path", path.exists(), str(path))
raw = path.read_text(encoding="utf-8")
check("password absent in clear text", SECRET not in raw)
check("file is owner-only (0600)", path.stat().st_mode & 0o777 == 0o600,
      oct(path.stat().st_mode & 0o777))
payload = json.loads(raw)
check("stored under a known format", payload.get("version") == 1)

# A second store instance is what a container restart looks like.
reopened = OpusCredentialStore("ConnectLogisticsOps/OPUS/app.opus4business.com")
again = reopened.read()
check("login survives a restart", again is not None and again.password == SECRET)

check("delete reports removal", store.delete() is True)
check("login is gone after delete", store.read() is None)
check("delete is idempotent", store.delete() is False)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All container credential checks passed.")
