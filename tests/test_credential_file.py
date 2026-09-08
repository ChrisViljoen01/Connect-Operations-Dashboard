from __future__ import annotations

import json
import sys

import pytest

from opus_dashboard.credential_file import FileCredentialStore
from opus_dashboard.sync import OpusCredentialStore


@pytest.fixture()
def store(tmp_path):
    return FileCredentialStore(
        path=tmp_path / "opus_credentials.json",
        secret="unit-test-storage-secret",
    )


def test_missing_file_reads_as_no_credential(store):
    assert store.read() is None


def test_saved_credential_round_trips(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    stored = store.read()
    assert stored is not None
    assert stored.username == "ops@example.com"
    assert stored.password == "s3cret-p@ssw0rd"


def test_password_is_not_written_in_clear_text(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    raw = store.path.read_text(encoding="utf-8")
    assert "s3cret-p@ssw0rd" not in raw
    payload = json.loads(raw)
    assert payload["username"] == "ops@example.com"
    assert payload["password"]


def test_each_save_uses_fresh_salt_and_nonce(store):
    store.save("ops@example.com", "same-password")
    first = json.loads(store.path.read_text(encoding="utf-8"))
    store.save("ops@example.com", "same-password")
    second = json.loads(store.path.read_text(encoding="utf-8"))
    assert first["salt"] != second["salt"]
    assert first["nonce"] != second["nonce"]
    # Identical passwords must not produce identical ciphertext.
    assert first["password"] != second["password"]


def test_unicode_password_round_trips(store):
    store.save("ops@example.com", "pässwörd-\u4e2d\u6587-\U0001f512")
    stored = store.read()
    assert stored is not None
    assert stored.password == "pässwörd-\u4e2d\u6587-\U0001f512"


def test_wrong_secret_is_rejected_rather_than_returning_junk(store, tmp_path):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    other = FileCredentialStore(path=store.path, secret="a-different-secret")
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        other.read()


def test_tampered_ciphertext_is_rejected(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["password"] = "AAAA" + payload["password"][4:]
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        store.read()


def test_tampered_username_is_rejected(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["username"] = "attacker@example.com"
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        store.read()


def test_unsupported_format_version_is_reported(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["version"] = 99
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported format"):
        store.read()


def test_delete_removes_credential_and_reports_absence(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    assert store.delete() is True
    assert store.read() is None
    assert store.delete() is False


def test_generated_key_is_reused_across_instances(tmp_path):
    path = tmp_path / "opus_credentials.json"
    first = FileCredentialStore(path=path)
    first.save("ops@example.com", "s3cret-p@ssw0rd")
    assert first.key_file.exists()
    # A fresh instance with no configured secret must still decrypt.
    second = FileCredentialStore(path=path)
    stored = second.read()
    assert stored is not None
    assert stored.password == "s3cret-p@ssw0rd"


def test_delete_also_removes_the_generated_key(tmp_path):
    path = tmp_path / "opus_credentials.json"
    store = FileCredentialStore(path=path)
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    store.delete()
    assert not store.key_file.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_credential_file_is_owner_readable_only(store):
    store.save("ops@example.com", "s3cret-p@ssw0rd")
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_unwritable_location_explains_how_to_fix(tmp_path):
    # A path whose parent cannot be created (a file stands where a directory
    # would have to be) is the realistic container misconfiguration.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store = FileCredentialStore(path=blocker / "nested" / "creds.json", secret="s")
    with pytest.raises((RuntimeError, OSError)):
        store.save("ops@example.com", "s3cret-p@ssw0rd")


class TestOpusCredentialStore:
    """The store must not require Windows on a non-Windows host."""

    def test_save_and_read_without_windows_vault(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        store = OpusCredentialStore(
            "irrelevant/target",
            credential_file=str(tmp_path / "creds.json"),
            storage_secret="unit-test-secret",
        )
        store.save("ops@example.com", "s3cret-p@ssw0rd")
        stored = store.read()
        assert stored is not None
        assert stored.username == "ops@example.com"
        assert stored.password == "s3cret-p@ssw0rd"
        assert store.delete() is True
        assert store.read() is None

    def test_saved_credential_wins_over_environment_fallback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "platform", "linux")
        store = OpusCredentialStore(
            "irrelevant/target",
            fallback_email="env@example.com",
            fallback_password="env-password",
            credential_file=str(tmp_path / "creds.json"),
            storage_secret="unit-test-secret",
        )
        assert store.read().username == "env@example.com"
        store.save("ops@example.com", "s3cret-p@ssw0rd")
        assert store.read().username == "ops@example.com"
        # Removing the saved login falls back to the environment again.
        store.delete()
        assert store.read().username == "env@example.com"

    def test_backend_description_matches_platform(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        store = OpusCredentialStore(
            "irrelevant/target",
            credential_file=str(tmp_path / "creds.json"),
        )
        assert "encrypted file" in store.backend
        assert "Windows Credential Manager" not in store.backend
