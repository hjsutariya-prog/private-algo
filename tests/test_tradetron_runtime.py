"""Tests for the durable, deliberately disarmed paper signal outbox."""
import json
from pathlib import Path
import tempfile
import unittest

from tradetron_delivery import TradetronDeliveryError
from tradetron_runtime import ARMING_CONFIRMATION, TradetronSignalRuntime, TradetronSignalStore


TOKEN = "11111111-1111-4111-8111-111111111111"


class LinkedPaperExecutor:
    details = {"strategy_id": "999056854", "auth_token": TOKEN,
               "signal_key": "not-visible", "signal_value": "1"}


class RecordingDelivery:
    calls = []
    def __init__(self, details):
        self.details = details
    def signal(self, signal):
        self.calls.append(signal.event_id)
        return {"runtime_variables": ["pa_" + signal.direction.lower() + "_entry_seq"], "http_status": 200}


class FailingDelivery:
    def __init__(self, details):
        self.details = details
    def signal(self, signal):
        raise TradetronDeliveryError("No usable acknowledgement")


def entry(event_id="entry-ce-20261001-001", sequence=1):
    return {
        "event_id": event_id, "event_type": "entry", "direction": "CE", "sequence": sequence,
        "expiry": "2026-10-27", "expiry_offset": 0, "quantity": 30,
        "short_symbol": "BANKNIFTY26OCT54000CE", "short_strike": 54000,
        "hedge_symbol": "BANKNIFTY26OCT55000CE", "hedge_strike": 55000,
        "signal_hour_close": 54500, "stop_spot": 55000, "target_spot": 53500,
    }


class TradetronRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "outbox.sqlite3"
        RecordingDelivery.calls = []

    def runtime(self, delivery=RecordingDelivery):
        return TradetronSignalRuntime(LinkedPaperExecutor(), self.path, delivery)

    def test_queue_is_durable_but_disarmed_runtime_sends_nothing(self):
        runtime = self.runtime()
        queued = runtime.queue(entry())
        self.assertEqual(queued["status"], "queued")
        with self.assertRaisesRegex(ValueError, "disarmed"):
            runtime.dispatch_next()
        self.assertEqual(RecordingDelivery.calls, [])
        self.assertEqual(runtime.status()["pending_events"], 1)

    def test_exact_event_is_idempotent_but_same_id_with_changed_trade_is_rejected(self):
        runtime = self.runtime()
        first = runtime.queue(entry())
        second = runtime.queue(entry())
        self.assertEqual(first["event_id"], second["event_id"])
        changed = entry(); changed["short_strike"] = 53500; changed["short_symbol"] = "BANKNIFTY26OCT53500CE"
        with self.assertRaisesRegex(ValueError, "different portal decision"):
            runtime.queue(changed)

    def test_accepted_api_response_is_not_called_a_fill_and_never_replays(self):
        runtime = self.runtime()
        runtime.queue(entry())
        runtime.arm(ARMING_CONFIRMATION)
        sent = runtime.dispatch_next()
        self.assertEqual(sent["status"], "accepted_unconfirmed")
        self.assertIn("not a broker-execution", sent["note"])
        self.assertEqual(RecordingDelivery.calls, ["entry-ce-20261001-001"])
        self.assertIsNone(runtime.dispatch_next())
        confirmed = runtime.confirm_after_operator_review(sent["event_id"])
        self.assertEqual(confirmed["status"], "operator_confirmed")

    def test_unknown_outbound_outcome_blocks_automatic_retry(self):
        runtime = self.runtime(FailingDelivery)
        runtime.queue(entry())
        runtime.arm(ARMING_CONFIRMATION)
        uncertain = runtime.dispatch_next()
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertTrue(runtime.status()["blocked"])
        self.assertIn("not retried", uncertain["note"])
        with self.assertRaisesRegex(ValueError, "blocked"):
            runtime.dispatch_next()

    def test_restart_marks_interrupted_send_uncertain_instead_of_replaying_it(self):
        store = TradetronSignalStore(self.path)
        parsed, queued, _ = store.queue(entry())
        store.update(queued["event_id"], "sending")
        restarted = self.runtime()
        visible = restarted.store.event(parsed.event_id)[1]
        self.assertEqual(visible["status"], "uncertain")
        self.assertTrue(restarted.status()["blocked"])

    def test_visible_status_and_records_do_not_leak_protected_credentials(self):
        runtime = self.runtime()
        runtime.queue(entry())
        visible = json.dumps(runtime.status())
        self.assertNotIn(TOKEN, visible)
        self.assertNotIn("auth_token", visible)


if __name__ == "__main__":
    unittest.main()
