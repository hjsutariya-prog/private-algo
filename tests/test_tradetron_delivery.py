"""Tests for the explicit, paper-only Tradetron runtime-variable sender."""
import json
import unittest
from datetime import date
from urllib.error import HTTPError

from tradetron_delivery import RuntimeUpdate, TradetronDeliveryError, TradetronPaperDelivery, bootstrap_entry_updates, dispatch_snapshot_entries, entry_updates, snapshot_entries


DETAILS = {"strategy_id": "999056854", "auth_token": "11111111-1111-4111-8111-111111111111",
           "signal_key": "ignored_setup_key", "signal_value": "1"}


class Response:
    def __init__(self, status=200): self.status = status
    def getcode(self): return self.status
    def __enter__(self): return self
    def __exit__(self, *args): return False


class CaptureOpener:
    def __init__(self):
        self.requests = []
        self.request_objects = []
    def __call__(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        self.request_objects.append(request)
        return Response()


class RejectingOpener:
    def __call__(self, request, timeout):
        raise HTTPError(request.full_url, 401, "Unauthorized", None, None)


def snapshot():
    return {"kind": "historical_paper_replay", "as_of": "2026-10-01T09:15:00+05:30", "legs": [
        {"status":"open","ledger":"selling","option_type":"CE","direction":"SELL","expiry":"2026-10-27","quantity":120,"strike":54000,"risk_reference_spot":54642.55,"stop_spot":55142.55,"target_spot":53642.55,"contract":{"symbol":"BANKNIFTY26OCT54000CE"}},
        {"status":"open","ledger":"ce_hedge","option_type":"CE","direction":"BUY","expiry":"2026-10-27","quantity":120,"strike":56000,"contract":{"symbol":"BANKNIFTY26OCT56000CE"}},
        {"status":"open","ledger":"selling","option_type":"PE","direction":"SELL","expiry":"2026-10-27","quantity":120,"strike":54500,"risk_reference_spot":54151.55,"stop_spot":53651.55,"target_spot":55151.55,"contract":{"symbol":"BANKNIFTY26OCT54500PE"}},
        {"status":"open","ledger":"pe_hedge","option_type":"PE","direction":"BUY","expiry":"2026-10-27","quantity":120,"strike":53000,"contract":{"symbol":"BANKNIFTY26OCT53000PE"}},
    ]}


class DeliveryTests(unittest.TestCase):
    def test_snapshot_reconstructs_exact_ce_and_pe_pairs(self):
        ce, pe = snapshot_entries(snapshot(), current_date=date(2026, 10, 1))
        self.assertEqual((ce.short_symbol, ce.hedge_symbol, ce.quantity), ("BANKNIFTY26OCT54000CE", "BANKNIFTY26OCT56000CE", 120))
        self.assertEqual((pe.short_symbol, pe.hedge_symbol, pe.quantity), ("BANKNIFTY26OCT54500PE", "BANKNIFTY26OCT53000PE", 120))

    def test_entry_values_arrive_before_directional_sequence_trigger(self):
        ce, _ = snapshot_entries(snapshot(), current_date=date(2026, 10, 1))
        self.assertEqual([item.name for item in entry_updates(ce)], ["pa_ce_expiry_offset", "pa_ce_short_strike", "pa_ce_hedge_strike", "pa_ce_entry_seq"])

    def test_bootstrap_rearms_only_the_directional_test_acknowledgement(self):
        ce, _ = snapshot_entries(snapshot(), sequence=2, current_date=date(2026, 10, 1))
        updates = bootstrap_entry_updates(ce)
        self.assertEqual([item.name for item in updates], ["ce_entry_seen", "pa_ce_expiry_offset", "pa_ce_short_strike", "pa_ce_hedge_strike", "pa_ce_entry_seq"])
        self.assertEqual([item.value for item in updates], ["0", "0", "54000", "56000", "2"])

    def test_delivery_posts_one_ordered_json_signal_and_redacts_secret_from_result(self):
        opener = CaptureOpener()
        ce, _ = snapshot_entries(snapshot(), current_date=date(2026, 10, 1))
        result = TradetronPaperDelivery(DETAILS, opener=opener).entry(ce)
        self.assertEqual(result, {"runtime_variables": ["pa_ce_expiry_offset", "pa_ce_short_strike", "pa_ce_hedge_strike", "pa_ce_entry_seq"], "http_status": 200})
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(json.loads(opener.request_objects[-1].data), {
            "auth-token": DETAILS["auth_token"],
            "key": "pa_ce_expiry_offset", "value": "0",
            "key1": "pa_ce_short_strike", "value1": "54000",
            "key2": "pa_ce_hedge_strike", "value2": "56000",
            "key3": "pa_ce_entry_seq", "value3": "1",
        })
        self.assertEqual(opener.request_objects[-1].get_method(), "POST")
        self.assertEqual(opener.request_objects[-1].get_header("Content-type"), "application/json")
        self.assertEqual(opener.request_objects[-1].get_header("User-agent"), "PrivateAlgoDesk/0.1")
        self.assertNotIn(DETAILS["auth_token"], str(result))

    def test_bootstrap_posts_two_rearmed_directional_pairs_at_sequence_two(self):
        opener = CaptureOpener()
        result = dispatch_snapshot_entries(DETAILS, snapshot(), opener=opener)
        self.assertEqual([entry["direction"] for entry in result["entries"]], ["CE", "PE"])
        self.assertEqual(len(opener.request_objects), 2)
        ce_payload = json.loads(opener.request_objects[0].data)
        pe_payload = json.loads(opener.request_objects[1].data)
        self.assertEqual((ce_payload["key"], ce_payload["value"], ce_payload["key4"], ce_payload["value4"]), ("ce_entry_seen", "0", "pa_ce_entry_seq", "2"))
        self.assertEqual((pe_payload["key"], pe_payload["value"], pe_payload["key4"], pe_payload["value4"]), ("pe_entry_seen", "0", "pa_pe_entry_seq", "2"))

    def test_snapshot_rejects_stale_expiry_and_non_four_leg_state(self):
        stale = snapshot(); stale["as_of"] = "2026-11-01T09:15:00+05:30"
        with self.assertRaises(ValueError): snapshot_entries(stale, current_date=date(2026, 11, 1))
        incomplete = snapshot(); incomplete["legs"] = incomplete["legs"][:-1]
        with self.assertRaises(ValueError): snapshot_entries(incomplete)

    def test_json_delivery_rejects_unsafe_name(self):
        with self.assertRaises(ValueError):
            TradetronPaperDelivery(DETAILS, opener=CaptureOpener()).deliver((RuntimeUpdate("bad name", "1"),))

    def test_http_rejection_is_not_misreported_as_an_unknown_connection_outcome(self):
        with self.assertRaises(TradetronDeliveryError) as raised:
            TradetronPaperDelivery(DETAILS, opener=RejectingOpener()).deliver((RuntimeUpdate("pa_ce_entry_seq", "1"),))
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn(DETAILS["auth_token"], str(raised.exception))


if __name__ == "__main__":
    unittest.main()
