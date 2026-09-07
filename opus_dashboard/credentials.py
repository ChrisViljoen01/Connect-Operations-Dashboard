from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


@dataclass(frozen=True, slots=True)
class StoredCredential:
    username: str
    password: str


class _CredentialAttributeW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CredentialAttributeW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def read_windows_credential(target: str) -> StoredCredential | None:
    """Read a generic Windows credential without writing it to disk or logs."""
    if sys.platform != "win32":
        return None

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    credential_pointer = ctypes.POINTER(_CredentialW)()
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    cred_read.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]

    if not cred_read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
        error_code = ctypes.get_last_error()
        if error_code == 1168:  # ERROR_NOT_FOUND
            return None
        raise OSError(error_code, f"Windows Credential Manager could not read {target!r}.")

    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(
            credential.CredentialBlob,
            credential.CredentialBlobSize,
        )
        try:
            password = blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            password = blob.decode("utf-8").rstrip("\x00")
        return StoredCredential(
            username=credential.UserName or "",
            password=password,
        )
    finally:
        advapi32.CredFree(credential_pointer)


def write_windows_credential(target: str, username: str, password: str) -> None:
    """Save a generic credential in the current Windows user's vault."""
    if sys.platform != "win32":
        raise RuntimeError("Windows Credential Manager is only available on Windows.")
    if not target.strip() or not username.strip() or not password:
        raise ValueError("Credential target, username, and password are required.")

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL

    blob = password.encode("utf-16-le")
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _CredentialW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        blob_buffer,
        ctypes.POINTER(ctypes.c_ubyte),
    )
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = username

    if not cred_write(ctypes.byref(credential), 0):
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            f"Windows Credential Manager could not save {target!r}.",
        )


def delete_windows_credential(target: str) -> bool:
    """Delete a generic Windows credential, returning False when absent."""
    if sys.platform != "win32":
        raise RuntimeError("Windows Credential Manager is only available on Windows.")
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_delete = advapi32.CredDeleteW
    cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    cred_delete.restype = wintypes.BOOL
    if cred_delete(target, CRED_TYPE_GENERIC, 0):
        return True
    error_code = ctypes.get_last_error()
    if error_code == 1168:
        return False
    raise OSError(
        error_code,
        f"Windows Credential Manager could not delete {target!r}.",
    )
