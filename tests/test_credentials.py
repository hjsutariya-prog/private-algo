"""Invented app details only. Never accesses the user's saved credentials."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import KiteState, LocalServer
from credential_store import CredentialStore, CredentialStoreError
from store import Store


DETAILS = {"api_key": "examplekey123", "api_secret": "examplesecret123"}


class FixtureProtection:
    """Reversible TEST FIXTURE, deliberately not production encryption."""
    available = True

    def protect(self, value):
        return b"test-only:" + value[::-1]

    def unprotect(self, value):
        if not value.startswith(b"test-only:"):
            raise ValueError()
        return value[10:][::-1]


class ProfilelessProtection:
    """Models a local runner whose Windows profile cannot serve DPAPI."""
    available = True

    def protect(self, value):
        raise CredentialStoreError("DPAPI profile unavailable")

    def unprotect(self, value):
        raise CredentialStoreError("DPAPI profile unavailable")


class FixtureCredentialManager:
    """In-memory stand-in for Windows Credential Manager; never uses user data."""
    available = True

    def __init__(self):
        self.payload = None

    def save(self, payload):
        self.payload = bytes(payload)

    def load(self):
        return self.payload

    def forget(self):
        self.payload = None


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test-kite.dpapi"
        self.store = CredentialStore(self.path, FixtureProtection())
        self.state = KiteState(credential_store=self.store)

    def test_default_is_memory_only(self):
        result = self.state.configure(DETAILS, 8765)
        self.assertTrue(result["configured"])
        self.assertFalse(result["credentials_saved"])
        self.assertFalse(self.path.exists())
        self.assertFalse(KiteState(credential_store=self.store).status(8765)["configured"])

    def test_remember_survives_new_state_without_saving_login(self):
        result = self.state.configure({**DETAILS, "remember": True}, 8765)
        self.state.client = object()
        self.state.session_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertTrue(result["credentials_saved"])
        for value in DETAILS.values():
            self.assertNotIn(value, json.dumps(result))
            self.assertNotIn(value.encode(), self.path.read_bytes())
        fresh = KiteState(credential_store=self.store)
        self.assertEqual(fresh.api_secret, DETAILS["api_secret"])
        self.assertTrue(fresh.status(8765)["credentials_saved"])
        self.assertFalse(fresh.status(8765)["connected"])
        self.assertIsNone(fresh.nonce)
        self.assertIsNone(fresh.session_expires_at)
        self.assertEqual(self.store.load(), DETAILS)

    def test_forget_clears_disk_memory_and_login(self):
        self.state.configure({**DETAILS, "remember": True}, 8765)
        self.state.client = object()
        result = self.state.clear(8765)
        self.assertFalse(result["configured"])
        self.assertFalse(result["credentials_saved"])
        self.assertIsNone(self.state.client)
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.store.load())

    def test_session_only_replacement_removes_previous_saved_key(self):
        self.state.configure({**DETAILS, "remember": True}, 8765)
        self.state.configure({**DETAILS, "api_key": "changedkey123", "remember": False}, 8765)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.state.api_key, "changedkey123")
        self.assertFalse(KiteState(credential_store=self.store).status(8765)["configured"])

    def test_expiry_does_not_delete_saved_app_details(self):
        self.state.configure({**DETAILS, "remember": True}, 8765)
        self.state.client = object()
        self.state.session_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertFalse(self.state.status(8765)["connected"])
        self.assertTrue(self.state.status(8765)["credentials_saved"])

    def test_non_boolean_remember_is_rejected_without_mutation(self):
        for value in (None, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.state.configure({**DETAILS, "remember": value}, 8765)
        self.assertIsNone(self.state.api_key)
        self.assertFalse(self.path.exists())

    def test_unavailable_protection_never_falls_back_to_plaintext(self):
        self.store.protection.available = False
        with self.assertRaises(CredentialStoreError):
            self.state.configure({**DETAILS, "remember": True}, 8765)
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.state.api_key)
        self.state.configure(DETAILS, 8765)

    def test_profileless_dpapi_uses_protected_windows_credential_manager_not_a_file(self):
        manager = FixtureCredentialManager()
        store = CredentialStore(self.path, ProfilelessProtection(), manager)
        state = KiteState(credential_store=store)
        result = state.configure({**DETAILS, "remember": True}, 8765)
        self.assertTrue(result["credentials_saved"])
        self.assertFalse(self.path.exists())
        self.assertIsNotNone(manager.payload)
        restored = KiteState(credential_store=store)
        self.assertEqual(restored.api_key, DETAILS["api_key"])
        restored.clear(8765)
        self.assertIsNone(manager.payload)

    def test_encryption_failure_leaves_old_credentials_and_file_unchanged(self):
        self.state.configure({**DETAILS, "remember": True}, 8765)
        original = self.path.read_bytes()
        with patch.object(self.store.protection, "protect", side_effect=OSError("private OS detail")):
            with self.assertRaises(CredentialStoreError) as raised:
                self.state.configure({**DETAILS, "api_secret": "newsecret123", "remember": True}, 8765)
        self.assertNotIn("private OS detail", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.state.api_secret, DETAILS["api_secret"])

    def test_atomic_write_failure_keeps_previous_file_and_cleans_temporary(self):
        self.store.save(DETAILS["api_key"], DETAILS["api_secret"])
        original = self.path.read_bytes()
        with patch("credential_store.os.replace", side_effect=PermissionError()):
            with self.assertRaises(CredentialStoreError):
                self.store.save("newkey123", "newsecret123")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_cannot_claim_forgotten_when_delete_fails(self):
        self.state.configure({**DETAILS, "remember": True}, 8765)
        with patch.object(Path, "unlink", side_effect=PermissionError()):
            with self.assertRaises(CredentialStoreError):
                self.state.clear(8765)
        self.assertTrue(self.state.status(8765)["credentials_saved"])
        self.assertTrue(self.path.exists())

    def test_corrupt_file_is_visible_and_can_be_forgotten(self):
        self.path.write_bytes(b"invalid file")
        fresh = KiteState(credential_store=self.store)
        status = fresh.status(8765)
        self.assertFalse(status["configured"])
        self.assertIn("could not be unlocked", status["credential_storage_warning"])
        self.assertTrue(self.path.exists())
        fresh.clear(8765)
        self.assertFalse(self.path.exists())

    def test_invalid_saved_payloads_are_not_loaded(self):
        for payload in ({**DETAILS, "access_token": "never-save"}, {**DETAILS, "api_key": "bad!"}, []):
            self.path.write_bytes(self.store.HEADER + self.store.protection.protect(json.dumps(payload).encode()))
            self.assertFalse(KiteState(credential_store=self.store).status(8765)["configured"])

    @unittest.skipUnless(os.name == "nt", "Windows account protection test")
    def test_real_windows_protection_round_trip_and_tamper_rejection(self):
        store = CredentialStore(self.path)
        try:
            store.save(DETAILS["api_key"], DETAILS["api_secret"])
            self.assertEqual(CredentialStore(self.path).load(), DETAILS)
            if self.path.exists():
                encrypted = self.path.read_bytes()
                self.assertNotIn(DETAILS["api_secret"].encode(), encrypted)
                self.assertNotIn(DETAILS["api_key"].encode(), encrypted)
                tampered = bytearray(encrypted)
                tampered[-1] ^= 1
                self.path.write_bytes(tampered)
                with self.assertRaises(CredentialStoreError):
                    store.load()
        finally:
            store.forget()

    @unittest.skipUnless(os.name == "nt", "Windows server restart test")
    def test_server_restart_loads_protected_app_details_only(self):
        database = Path(self.temp.name) / "server.sqlite3"
        first = LocalServer(("127.0.0.1", 0), Store(database))
        try:
            first.kite.configure({**DETAILS, "remember": True}, first.server_port)
        finally:
            first.server_close()
        second = LocalServer(("127.0.0.1", 0), Store(database))
        try:
            status = second.kite.status(second.server_port)
            self.assertTrue(status["configured"])
            self.assertTrue(status["credentials_saved"])
            self.assertFalse(status["connected"])
            second.kite.clear(second.server_port)
        finally:
            second.server_close()


if __name__ == "__main__":
    unittest.main()
