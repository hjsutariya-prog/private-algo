"""Optional, Windows-user-protected Kite app details. Never stores login tokens."""
import ctypes
from ctypes import wintypes
import hashlib
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


class WindowsCredentialManager:
    """User-scoped Windows Credential Manager bytes, never a plaintext file.

    Some local runners do not load a DPAPI profile even though they run under
    the signed-in Windows account. Credential Manager remains available in
    that situation, so it is a narrow fallback for the same local user.
    """
    _TYPE_GENERIC = 1
    _PERSIST_SESSION = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168
    _ERROR_NO_SUCH_LOGON_SESSION = 1312

    class _Credential(ctypes.Structure):
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
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def __init__(self, path, purpose):
        digest = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:32]
        self.target = f"PrivateAlgoDesk/{purpose}/{digest}"
        self.available = os.name == "nt"

    def _api(self):
        if not self.available:
            raise CredentialStoreError("Windows Credential Manager is unavailable.")
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        write = advapi.CredWriteW
        write.argtypes = [ctypes.POINTER(self._Credential), wintypes.DWORD]
        write.restype = wintypes.BOOL
        read = advapi.CredReadW
        read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         ctypes.POINTER(ctypes.POINTER(self._Credential))]
        read.restype = wintypes.BOOL
        delete = advapi.CredDeleteW
        delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        delete.restype = wintypes.BOOL
        free = advapi.CredFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        return write, read, delete, free

    def save(self, payload):
        if not isinstance(payload, bytes) or not payload or len(payload) > 512:
            raise CredentialStoreError("App details could not be saved securely.")
        try:
            write, _read, _delete, _free = self._api()
            buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            credential = self._Credential(
                0, self._TYPE_GENERIC, self.target, None, wintypes.FILETIME(), len(payload), buffer,
                self._PERSIST_LOCAL_MACHINE, 0, None, None, "PrivateAlgoDesk",
            )
            ctypes.set_last_error(0)
            if not write(ctypes.byref(credential), 0):
                # The desktop app's local server can run without a loaded
                # DPAPI profile.  Its Windows logon session is still private,
                # so retain the protected credential there until sign-out.
                if ctypes.get_last_error() != self._ERROR_NO_SUCH_LOGON_SESSION:
                    raise CredentialStoreError("Windows Credential Manager could not save the protected app details.")
                credential.Persist = self._PERSIST_SESSION
                ctypes.set_last_error(0)
                if not write(ctypes.byref(credential), 0):
                    raise CredentialStoreError("Windows Credential Manager could not save the protected app details.")
        except (OSError, ValueError):
            raise CredentialStoreError("Windows Credential Manager could not save the protected app details.") from None
        finally:
            if "buffer" in locals():
                ctypes.memset(buffer, 0, len(payload))

    def load(self):
        credential = ctypes.POINTER(self._Credential)()
        try:
            _write, read, _delete, free = self._api()
            if not read(self.target, self._TYPE_GENERIC, 0, ctypes.byref(credential)):
                if ctypes.get_last_error() == self._ERROR_NOT_FOUND:
                    return None
                raise CredentialStoreError("Windows Credential Manager could not unlock the saved app details.")
            item = credential.contents
            if not item.CredentialBlob or not 0 < item.CredentialBlobSize <= 512:
                raise CredentialStoreError("Saved app details could not be unlocked.")
            return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        except (OSError, ValueError):
            raise CredentialStoreError("Windows Credential Manager could not unlock the saved app details.") from None
        finally:
            if credential:
                try:
                    free(ctypes.cast(credential, ctypes.c_void_p))
                except (OSError, UnboundLocalError):
                    pass

    def forget(self):
        try:
            _write, _read, delete, _free = self._api()
            if not delete(self.target, self._TYPE_GENERIC, 0) and ctypes.get_last_error() != self._ERROR_NOT_FOUND:
                raise CredentialStoreError("Windows Credential Manager could not remove the saved app details.")
        except (OSError, ValueError):
            raise CredentialStoreError("Windows Credential Manager could not remove the saved app details.") from None


class CredentialStore:
    HEADER = b"PrivateAlgoDesk-DPAPI-v1\n"

    def __init__(self, path, protection=None, credential_manager=None):
        self.path = Path(path)
        self.protection = protection or WindowsProtection()
        # Fixtures deliberately supply their own protection and never touch
        # the desktop Credential Manager. Production gets this safe fallback.
        self.credential_manager = credential_manager if credential_manager is not None else (
            WindowsCredentialManager(self.path, "Kite") if isinstance(self.protection, WindowsProtection) else None)

    @property
    def available(self):
        return self.protection.available or bool(self.credential_manager and self.credential_manager.available)

    @staticmethod
    def _decode(payload):
        details = json.loads(payload)
        if not valid_details(details):
            raise ValueError()
        return details

    def _manager_load(self):
        if not self.credential_manager:
            return None
        payload = self.credential_manager.load()
        return None if payload is None else self._decode(payload)

    def load(self):
        try:
            with self.path.open("rb") as saved:
                encrypted = saved.read(16385)
            if len(encrypted) > 16384 or not encrypted.startswith(self.HEADER):
                raise ValueError()
            return self._decode(self.protection.unprotect(encrypted[len(self.HEADER):]))
        except FileNotFoundError:
            try:
                return self._manager_load()
            except (OSError, ValueError):
                raise CredentialStoreError("Saved app details could not be unlocked. Enter them again or choose Forget connection details.") from None
        except (OSError, ValueError):
            try:
                fallback = self._manager_load()
                if fallback is not None:
                    return fallback
            except (OSError, ValueError):
                pass
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
            if self.credential_manager:
                try:
                    self.credential_manager.save(json.dumps(details, separators=(",", ":")).encode("utf-8"))
                    try:
                        self.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return
                except (OSError, ValueError):
                    pass
            raise CredentialStoreError("App details were not saved. Windows protection or file access failed. Try again, or turn off Remember to use this session only.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Any leftover temporary file contains only protected bytes.

    def forget(self):
        manager_error = None
        if self.credential_manager:
            try:
                self.credential_manager.forget()
            except CredentialStoreError as exc:
                manager_error = exc
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise CredentialStoreError("Saved app details could not be removed. They have not been forgotten; check file access and try again.") from None
        if manager_error:
            raise manager_error
