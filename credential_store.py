"""Optional, Windows-user-protected Kite app details. Never stores login tokens."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import tempfile


class CredentialStoreError(ValueError):
    """Safe to show to the user; never includes credentials or OS error text."""


def valid_details(details):
    return (isinstance(details, dict) and set(details) == {"api_key", "api_secret"}
            and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{6,128}", value)
                    for value in details.values()))


class WindowsProtection:
    available = os.name == "nt"

    def _convert(self, data, *, decrypt=False):
        if not self.available:
            raise CredentialStoreError("Remembering app details requires Windows account protection. Use this session only.")

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                             ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source, result = Blob(len(data), buffer), Blob()
        try:
            # UI_FORBIDDEN only: never LOCAL_MACHINE (which would allow other users).
            if not function(ctypes.byref(source), None if decrypt else "Private Algo Desk Kite app details",
                            None, None, None, 1, ctypes.byref(result)):
                raise CredentialStoreError("Windows could not protect or unlock the saved app details.")
            return ctypes.string_at(result.data, result.size)
        finally:
            ctypes.memset(buffer, 0, len(data))
            if result.data:
                ctypes.memset(result.data, 0, result.size)
                kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))

    def protect(self, data):
        return self._convert(data)

    def unprotect(self, data):
        return self._convert(data, decrypt=True)


class CredentialStore:
    HEADER = b"PrivateAlgoDesk-DPAPI-v1\n"

    def __init__(self, path, protection=None):
        self.path = Path(path)
        self.protection = protection or WindowsProtection()

    @property
    def available(self):
        return self.protection.available

    def load(self):
        try:
            with self.path.open("rb") as saved:
                encrypted = saved.read(16385)
            if len(encrypted) > 16384 or not encrypted.startswith(self.HEADER):
                raise ValueError()
            details = json.loads(self.protection.unprotect(encrypted[len(self.HEADER):]))
            if not valid_details(details):
                raise ValueError()
            return details
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise CredentialStoreError("Saved app details could not be unlocked. Enter them again or choose Forget connection details.") from None

    def save(self, key, secret):
        details = {"api_key": key, "api_secret": secret}
        if not valid_details(details):
            raise CredentialStoreError("The app details could not be saved. Check their format.")
        temporary = None
        try:
            # Encrypt before opening any output file; no plaintext fallback.
            encrypted = self.HEADER + self.protection.protect(json.dumps(details).encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".kite-", delete=False) as saved:
                temporary = Path(saved.name)
                saved.write(encrypted)
                saved.flush()
                os.fsync(saved.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, ValueError):
            raise CredentialStoreError("App details were not saved. Windows protection or file access failed. Try again, or turn off Remember to use this session only.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Any leftover temporary file contains only protected bytes.

    def forget(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise CredentialStoreError("Saved app details could not be removed. They have not been forgotten; check file access and try again.") from None
