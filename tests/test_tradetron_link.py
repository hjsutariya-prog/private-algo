"""Tests for the undeployed Tradetron paper-executor link.

All links and tokens in this file are invented test fixtures.  The production
module deliberately has no network client or outbound delivery operation.
"""
import json
from pathlib import Path
import tempfile
import unittest

from credential_store import CredentialStoreError
from tradetron_link import TradetronLinkState, TradetronLinkStore, parse_api_link
from tradetron_protocol import parse_portal_signal, runtime_pairs, template_contract


TOKEN = "11111111-1111-4111-8111-111111111111"
API_LINK = "https://api.tradetron.tech/api?auth-token=" + TOKEN + "&key=api_buy&value=1"


class FixtureProtection:
    """Reversible test fixture only; production uses Windows account protection."""
    available = True

    def protect(self, value):
        return b"test-only:" + value[::-1]

    def unprotect(self, value):
        if not value.startswith(b"test-only:"):
            raise ValueError()
        return value[10:][::-1]


class ProfilelessProtection:
    available = True

    def protect(self, value):
        raise CredentialStoreError("DPAPI profile unavailable")

    def unprotect(self, value):
        raise CredentialStoreError("DPAPI profile unavailable")


class FixtureCredentialManager:
    available = True

    def __init__(self):
        self.payload = None

    def save(self, payload):
        self.payload = bytes(payload)

    def load(self):
        return self.payload

    def forget(self):
        self.payload = None


class TradetronLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tradetron.dpapi"
        self.store = TradetronLinkStore(self.path, FixtureProtection())
        self.state = TradetronLinkState(self.store)

    def test_api_link_parser_accepts_only_expected_generated_link_shape(self):
        self.assertEqual(parse_api_link(API_LINK), {
            "auth_token": TOKEN, "signal_key": "api_buy", "signal_value": "1",
        })
        for invalid in (
            "http://api.tradetron.tech/api?auth-token=" + TOKEN + "&key=api_buy&value=1",
            "https://api.tradetron.tech/other?auth-token=" + TOKEN + "&key=api_buy&value=1",
            "https://other.example/api?auth-token=" + TOKEN + "&key=api_buy&value=1",
            "https://api.tradetron.tech/api?auth-token=" + TOKEN + "&key=api_buy",
            API_LINK + "&value=2",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_api_link(invalid)

    def test_status_never_exposes_api_token_or_signal_values(self):
        status = self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        visible = json.dumps(status)
        self.assertTrue(status["linked"])
        self.assertEqual(status["strategy_id"], "999056202")
        self.assertEqual(status["delivery"], "unverified")
        self.assertEqual(status["mode"], "paper_only")
        self.assertTrue(status["signal_contract"]["signals_locked"])
        self.assertNotIn(TOKEN, visible)
        self.assertNotIn("api_buy", visible)
        self.assertNotIn("auth_token", visible)

    def test_accepted_delivery_is_visible_without_exposing_the_link(self):
        self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        self.state.mark_delivery_verified()
        status = self.state.status()
        self.assertEqual(status["delivery"], "verified")
        self.assertIn("accepted", status["message"])
        self.assertNotIn(TOKEN, json.dumps(status))

    def test_remote_rejection_requires_a_new_link_without_exposing_its_value(self):
        self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        self.state.mark_delivery_rejected()
        status = self.state.status()
        self.assertEqual(status["delivery"], "needs_relink")
        self.assertIn("rejected", status["message"])
        self.assertNotIn(TOKEN, json.dumps(status))

    def test_saved_file_is_protected_and_restart_only_restores_status(self):
        self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        encrypted = self.path.read_bytes()
        self.assertNotIn(TOKEN.encode(), encrypted)
        self.assertNotIn(b"api_buy", encrypted)
        fresh = TradetronLinkState(self.store)
        self.assertTrue(fresh.status()["linked"])
        self.assertEqual(fresh.status()["strategy_id"], "999056202")
        self.assertNotIn(TOKEN, json.dumps(fresh.status()))

    def test_link_requires_protected_storage_and_forget_removes_it(self):
        with self.assertRaises(ValueError):
            self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": False})
        self.store.protection.available = False
        with self.assertRaises(CredentialStoreError):
            self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        self.store.protection.available = True
        self.state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        cleared = self.state.clear()
        self.assertFalse(cleared["linked"])
        self.assertFalse(self.path.exists())

    def test_profileless_dpapi_uses_credential_manager_without_writing_a_link_file(self):
        manager = FixtureCredentialManager()
        store = TradetronLinkStore(self.path, ProfilelessProtection(), manager)
        state = TradetronLinkState(store)
        state.configure({"strategy_id": "999056202", "api_link": API_LINK, "remember": True})
        self.assertFalse(self.path.exists())
        self.assertIsNotNone(manager.payload)
        restored = TradetronLinkState(store)
        self.assertTrue(restored.status()["linked"])
        restored.clear()
        self.assertIsNone(manager.payload)

    def test_corrupt_saved_link_is_visible_but_not_treated_as_linked(self):
        self.path.write_bytes(b"not a protected link")
        recovered = TradetronLinkState(self.store).status()
        self.assertFalse(recovered["linked"])
        self.assertIn("could not be unlocked", recovered["credential_storage_warning"])

    def test_entry_contract_uses_directional_runtime_names_and_original_spot_risk(self):
        signal = parse_portal_signal({
            "event_id": "entry-ce-20261001-001", "event_type": "entry", "direction": "CE", "sequence": 1,
            "expiry": "2026-10-27", "expiry_offset": 0, "quantity": 30, "short_symbol": "BANKNIFTY26OCT54000CE", "short_strike": 54000,
            "hedge_symbol": "BANKNIFTY26OCT55000CE", "hedge_strike": 55000,
            "signal_hour_close": 54500, "stop_spot": 55000, "target_spot": 53500,
        })
        self.assertEqual(runtime_pairs(signal), {
            "pa_ce_event_id": "entry-ce-20261001-001", "pa_ce_expiry": "2026-10-27", "pa_ce_expiry_offset": "0", "pa_ce_qty": "30",
            "pa_ce_entry_seq": "1", "pa_ce_short_strike": "54000", "pa_ce_hedge_strike": "55000",
            "pa_ce_short_symbol": "BANKNIFTY26OCT54000CE", "pa_ce_hedge_symbol": "BANKNIFTY26OCT55000CE",
            "pa_ce_signal_close": "54500", "pa_ce_stop_spot": "55000", "pa_ce_target_spot": "53500",
        })

    def test_contract_rejects_wrong_strike_shape_risk_or_hedge_direction(self):
        base = {"event_id": "entry-pe-20261001-001", "event_type": "entry", "direction": "PE", "sequence": 1,
                "expiry": "2026-10-27", "expiry_offset": 0, "quantity": 30, "short_symbol": "BANKNIFTY26OCT54500PE", "short_strike": 54500,
                "hedge_symbol": "BANKNIFTY26OCT53500PE", "hedge_strike": 53500,
                "signal_hour_close": 54000, "stop_spot": 53500, "target_spot": 55000}
        for changed in ({"quantity": 20}, {"hedge_strike": 53750}, {"target_spot": 54999}, {"hedge_strike": 55000}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_portal_signal({**base, **changed})

    def test_exit_and_replacement_remain_isolated_per_direction(self):
        exit_signal = parse_portal_signal({"event_id": "exit-pe-20261001-002", "event_type": "exit", "direction": "PE",
                                            "sequence": 2, "expiry": "2026-10-27", "expiry_offset": 0, "quantity": 30})
        replacement = parse_portal_signal({
            "event_id": "hedge-ce-20261001-003", "event_type": "hedge_replacement", "direction": "CE", "sequence": 3,
            "expiry": "2026-10-27", "expiry_offset": 0, "quantity": 30, "previous_hedge_symbol": "BANKNIFTY26OCT55000CE",
            "previous_hedge_strike": 55000, "hedge_symbol": "BANKNIFTY26OCT56000CE", "hedge_strike": 56000,
            "previous_expiry": "2026-10-27", "previous_expiry_offset": 0,
        })
        self.assertEqual(runtime_pairs(exit_signal)["pa_pe_exit_seq"], "2")
        self.assertEqual(runtime_pairs(replacement)["pa_ce_hedge_seq"], "3")
        self.assertNotIn("pa_ce_short_strike", runtime_pairs(replacement))

    def test_visible_contract_never_unlocks_delivery(self):
        contract = template_contract()
        self.assertEqual(contract["mode"], "paper_only")
        self.assertTrue(contract["signals_locked"])
        self.assertEqual(contract["directions"]["CE"]["entry"][0], "pa_ce_entry_seq")

    def test_month_offsets_are_explicit_and_replacement_keeps_both_months(self):
        base = {
            "event_id": "hedge-pe-20261001-004", "event_type": "hedge_replacement", "direction": "PE",
            "sequence": 4, "expiry": "2026-11-24", "expiry_offset": 1, "quantity": 30,
            "previous_hedge_symbol": "BANKNIFTY26OCT53500PE", "previous_hedge_strike": 53500,
            "hedge_symbol": "BANKNIFTY26NOV52500PE", "hedge_strike": 52500,
            "previous_expiry": "2026-10-27", "previous_expiry_offset": 0,
        }
        parsed = parse_portal_signal(base)
        pairs = runtime_pairs(parsed)
        self.assertEqual(pairs["pa_pe_expiry_offset"], "1")
        self.assertEqual(pairs["pa_pe_previous_expiry_offset"], "0")
        for change in ({"expiry_offset": 2}, {"previous_expiry_offset": 2}, {"previous_expiry": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_portal_signal({**base, **change})


if __name__ == "__main__":
    unittest.main()
