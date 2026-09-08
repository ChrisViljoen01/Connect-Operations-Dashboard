"""Encrypted file credential store for hosts without a Windows vault.

Windows deployments keep using Windows Credential Manager (credentials.py).
Linux hosts and containers have no equivalent OS vault available to this
application, so the OPUS password is encrypted at rest with a key derived
from a secret.

Scope of the protection, stated plainly:

* The password is never stored in clear text and never written to the
  project directory, the database, or the logs.
* The file is written with owner-only permissions (0600).
* When OPUS_APP_STORAGE_SECRET is set, the key is derived from that secret
  and never touches disk. This is the recommended production setting.
* When it is not set, a random key file is generated beside the credential
  file. That still protects backups, image layers and stray copies of the
  credential file alone, but anyone who can read both files as that user can
  recover the password. It is not equivalent to an OS keyring.

Only the standard library is used, so this adds no dependency: scrypt for key
derivation and HMAC-SHA256 in counter mode as the stream cipher, with a
separate HMAC-SHA256 authentication tag verified before decryption.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from opus_dashboard.credentials import StoredCredential


FORMAT_VERSION = 1
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_NONCE_BYTES = 16
_KEY_BYTES = 32


def _derive_keys(secret: str, salt: bytes) -> tuple[bytes, bytes]:
    """Return (encryption key, authentication key) derived from the secret."""
    material = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES * 2,
        maxmem=64 * 1024 * 1024,
    )
    return material[:_KEY_BYTES], material[_KEY_BYTES:]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 in counter mode, used as a keystream."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key,
            nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _tag(key: bytes, salt: bytes, nonce: bytes, username: str, blob: bytes) -> bytes:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(FORMAT_VERSION.to_bytes(4, "big"))
    mac.update(salt)
    mac.update(nonce)
    encoded_user = username.encode("utf-8")
    mac.update(len(encoded_user).to_bytes(4, "big"))
    mac.update(encoded_user)
    mac.update(blob)
    return mac.digest()


def default_credential_path() -> Path:
    """Location of the credential file when none is configured.

    Kept under the account's home directory so the login is never written
    into the project directory, a bind-mounted source tree, or an image
    layer. In the container this is /home/appuser, which docker-compose.yml
    backs with a named volume so the login survives a restart.
    """
    return Path.home() / ".connect-ops" / "opus_credentials.json"


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        # Permission bits are best effort on filesystems that do not carry
        # them (bind mounts from Windows hosts, for example).
        pass
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class FileCredentialStore:
    """Encrypted credential file used when no Windows vault is available."""

    path: Path
    secret: str = ""

    @property
    def key_file(self) -> Path:
        return self.path.with_name(self.path.name + ".key")

    def _secret(self) -> str:
        """Return the configured secret, or a persisted random key file."""
        if self.secret.strip():
            return self.secret
        if self.key_file.exists():
            existing = self.key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = secrets.token_urlsafe(48)
        try:
            _write_private(self.key_file, generated)
        except OSError as exc:
            raise RuntimeError(
                f"The credential encryption key could not be written to "
                f"{self.key_file}: {exc}. Make that directory writable by the "
                "account running the dashboard, or set OPUS_APP_STORAGE_SECRET "
                "so that no key file is needed."
            ) from exc
        return generated

    def read(self) -> StoredCredential | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"The saved OPUS login at {self.path} could not be read: {exc}"
            ) from exc
        if int(payload.get("version", 0)) != FORMAT_VERSION:
            raise RuntimeError(
                f"The saved OPUS login at {self.path} uses an unsupported format. "
                "Remove the file and save the login again."
            )
        try:
            salt = base64.b64decode(payload["salt"])
            nonce = base64.b64decode(payload["nonce"])
            blob = base64.b64decode(payload["password"])
            stored_tag = base64.b64decode(payload["tag"])
            username = str(payload["username"])
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"The saved OPUS login at {self.path} is incomplete: {exc}"
            ) from exc

        encryption_key, mac_key = _derive_keys(self._secret(), salt)
        expected = _tag(mac_key, salt, nonce, username, blob)
        if not hmac.compare_digest(expected, stored_tag):
            raise RuntimeError(
                "The saved OPUS login could not be decrypted. This usually means "
                "OPUS_APP_STORAGE_SECRET changed since it was saved. Save the "
                "OPUS login again to replace it."
            )
        password = bytes(
            a ^ b for a, b in zip(blob, _keystream(encryption_key, nonce, len(blob)))
        ).decode("utf-8")
        return StoredCredential(username=username, password=password)

    def save(self, username: str, password: str) -> None:
        if not username.strip() or not password:
            raise ValueError("Credential username and password are required.")
        salt = secrets.token_bytes(_SALT_BYTES)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        encryption_key, mac_key = _derive_keys(self._secret(), salt)
        clear = password.encode("utf-8")
        blob = bytes(
            a ^ b for a, b in zip(clear, _keystream(encryption_key, nonce, len(clear)))
        )
        payload = {
            "version": FORMAT_VERSION,
            "kdf": "scrypt",
            "username": username.strip(),
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "password": base64.b64encode(blob).decode("ascii"),
            "tag": base64.b64encode(
                _tag(mac_key, salt, nonce, username.strip(), blob)
            ).decode("ascii"),
        }
        try:
            _write_private(self.path, json.dumps(payload, indent=2))
        except OSError as exc:
            raise RuntimeError(
                f"The OPUS login could not be saved to {self.path}: {exc}. "
                "Set OPUS_CREDENTIAL_FILE to a writable location, or mount a "
                "writable volume for it."
            ) from exc

    def delete(self) -> bool:
        removed = False
        if self.path.exists():
            self.path.unlink()
            removed = True
        # The key protects nothing once the credential is gone, and leaving it
        # behind would silently re-key the next saved credential.
        self.key_file.unlink(missing_ok=True)
        return removed
