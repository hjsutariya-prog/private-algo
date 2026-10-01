"""Local Zerodha setup tests with a fake client; no network or real credentials."""
from datetime import date, datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from app import KiteState, LocalServer
from kite_client import KiteAuthError, KiteClient, KiteNetworkError
from store import Store
from market_sessions import session_minutes


class FakeKiteClient:
    def __init__(self, api_key, access_token=None, *, opener=None):
        self.api_key = api_key
        self.access_token = access_token

    def login_url(self, redirect_params=None):
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}&redirect_params={quote(urlencode(redirect_params or {}))}"

    def exchange_request_token(self, request_token, api_secret):
        if request_token != "example_request_token" or api_secret != "examplesecret123":
            raise KiteAuthError("Zerodha sign-in did not succeed.")
        self.access_token = "test-session-only"
        return {"access_token": self.access_token}

    def instruments(self, exchange="NFO"):
        assert exchange == "NFO"
        expiry = (date.today() + timedelta(days=40)).isoformat()
        return [{"exchange": "NFO", "name": "BANKNIFTY", "tradingsymbol": "BANKNIFTYEXAMPLECE",
                 "instrument_type": "CE", "expiry": expiry, "lot_size": 30},
                {"exchange": "NFO", "name": "BANKNIFTY", "tradingsymbol": "BANKNIFTYEXAMPLEPE",
                 "instrument_type": "PE", "expiry": expiry, "lot_size": 30},
                {"exchange": "NFO", "name": "NIFTY", "tradingsymbol": "NIFTYEXAMPLECE",
                 "instrument_type": "CE", "expiry": expiry, "lot_size": 75}]

    def banknifty_spot(self):
        return {"instrument_token": 260105, "last_price": 50123.45, "timestamp_ist": "2026-09-29T15:25:00+05:30"}

    def historical_minutes(self, instrument_token, from_datetime, to_datetime):
        assert instrument_token == 260105
        rows = []
        chosen = from_datetime.date()
        while chosen <= to_datetime.date():
            rows.extend({"timestamp_ist": minute.isoformat(), "open": 53000.0, "high": 53000.0,
                         "low": 53000.0, "close": 53000.0, "volume": 0}
                        for minute in session_minutes(chosen) if from_datetime <= minute <= to_datetime)
            chosen += timedelta(days=1)
        return rows


class KiteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "test.sqlite3"
        cls.server = LocalServer(("127.0.0.1", 0), Store(cls.path), FakeKiteClient)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.temp.cleanup()

    def setUp(self):
        self.server.kite = KiteState(FakeKiteClient)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        supplied = {"Content-Type": "application/json", "X-Local-Request": "PrivateAlgoDesk"}
        supplied.update(headers or {})
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, supplied)
            response = connection.getresponse()
            raw = response.read()
            payload = json.loads(raw) if raw else None
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def configure(self):
        return self.request("POST", "/api/kite/config", {"api_key": "examplekey123", "api_secret": "examplesecret123"})

    def login(self):
        self.configure()
        status, response, _ = self.request("GET", "/api/kite/login-url")
        self.assertEqual(status, 200)
        parsed = urlsplit(response["login_url"])
        self.assertEqual(parsed.netloc, "kite.zerodha.com")
        params = parse_qs(parsed.query)
        state = parse_qs(params["redirect_params"][0])["state"][0]
        callback = f"/api/kite/callback?status=success&request_token=example_request_token&state={state}"
        return self.request("GET", callback)

    def test_status_has_exact_local_redirect_and_no_credentials(self):
        status, response, _ = self.request("GET", "/api/kite/status")
        self.assertEqual(status, 200)
        self.assertFalse(response["status"]["connected"])
        self.assertEqual(response["status"]["redirect_url"], f"http://127.0.0.1:{self.port}/api/kite/callback")
        self.assertNotIn("api_secret", response["status"])

    def test_real_client_builds_one_use_login_state_without_network(self):
        kite = KiteState(KiteClient)
        kite.configure({"api_key": "examplekey123", "api_secret": "examplesecret123"}, self.port)
        parsed = urlsplit(kite.login_url())
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "kite.zerodha.com")
        state = parse_qs(parse_qs(parsed.query)["redirect_params"][0])["state"][0]
        self.assertEqual(state, kite.nonce)
        self.assertNotIn("examplesecret123", parsed.geturl())

    def test_login_network_failure_is_not_called_bad_credentials(self):
        class OfflineClient(FakeKiteClient):
            def exchange_request_token(self, request_token, api_secret):
                raise KiteNetworkError("Kite could not be reached.")
        kite = KiteState(OfflineClient)
        kite.configure({"api_key": "examplekey123", "api_secret": "examplesecret123"}, self.port)
        kite.login_url()
        with self.assertRaises(KiteNetworkError):
            kite.finish_login(f"status=success&request_token=example_request_token&state={kite.nonce}")
        self.assertIn("could not reach Zerodha", kite.status(self.port)["message"])

    def test_config_is_memory_only_and_status_masks_key(self):
        status, response, _ = self.configure()
        self.assertEqual(status, 200)
        self.assertEqual(response["status"]["masked_api_key"], "••••y123")
        self.assertNotIn("examplesecret123", json.dumps(response))
        self.assertNotIn(b"examplesecret123", self.path.read_bytes())

    def test_invalid_credentials_are_rejected_without_echo(self):
        status, response, _ = self.request("POST", "/api/kite/config", {"api_key": "bad key!", "api_secret": "examplesecret123"})
        self.assertEqual(status, 400)
        self.assertNotIn("examplesecret123", json.dumps(response))
        self.assertFalse(self.server.kite.status(self.port)["configured"])

    def test_login_requires_config(self):
        status, _, _ = self.request("GET", "/api/kite/login-url")
        self.assertEqual(status, 400)

    def test_callback_requires_matching_one_use_state(self):
        self.configure()
        self.request("GET", "/api/kite/login-url")
        status, _, headers = self.request("GET", "/api/kite/callback?status=success&request_token=example_request_token&state=wrong")
        self.assertEqual(status, 303)
        self.assertIn("kite=failed", headers["Location"])
        self.assertFalse(self.server.kite.status(self.port)["connected"])

    def test_successful_login_then_only_market_data_reads(self):
        status, _, headers = self.login()
        self.assertEqual(status, 303)
        self.assertIn("kite=connected", headers["Location"])
        self.assertTrue(headers["Location"].endswith("#kite-connection"))
        status, result, _ = self.request("GET", "/api/kite/status")
        self.assertTrue(result["status"]["connected"])
        self.assertNotIn("test-session-only", json.dumps(result))
        status, spot, _ = self.request("GET", "/api/kite/spot")
        self.assertEqual(status, 200)
        self.assertEqual(spot["quote"]["instrument"], "NSE:NIFTY BANK")
        self.assertEqual(spot["quote"]["last_price"], 50123.45)
        status, instruments, _ = self.request("POST", "/api/kite/instruments/sync", {})
        self.assertEqual(status, 200)
        self.assertEqual(instruments["status"]["banknifty_contract_count"], 2)
        self.assertEqual(instruments["status"]["lot_sizes"], [30])
        self.assertEqual(instruments["status"]["instrument_count"], 3)
        status, state, _ = self.request("GET", "/api/state")
        self.assertEqual(state["mode"], "offline")
        self.assertFalse(state["live_enabled"])
        self.assertEqual(self.request("POST", "/api/orders", {"side": "BUY"})[0], 404)

    def test_market_data_requires_login(self):
        self.assertEqual(self.request("GET", "/api/kite/spot")[0], 400)
        self.assertEqual(self.request("POST", "/api/kite/instruments/sync", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-28"})[0], 400)
        self.assertEqual(self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-01"})[0], 400)

    def test_spot_range_from_chosen_date_is_read_only(self):
        self.login()
        before = self.server.store.recent_runs()
        status, response, _ = self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-01"})
        self.assertEqual(status, 200)
        checked = response["range"]
        self.assertEqual(checked["start_date"], "2026-09-01")
        self.assertEqual(checked["end_date"], checked["latest_completed_date"])
        self.assertEqual(checked["days"][0]["date"], "2026-09-01")
        self.assertEqual(checked["days"][0]["status"], "complete")
        self.assertEqual(checked["days"][0]["signal_count"], 0)
        holiday = next(day for day in checked["days"] if day["date"] == "2026-09-14")
        self.assertEqual(holiday["status"], "holiday")
        self.assertEqual(holiday["verified_closed_date"], "Ganesh Chaturthi")
        self.assertEqual(response["spot_proxy"]["status"], "complete")
        self.assertEqual(response["spot_proxy"]["starting_state"], "flat")
        self.assertEqual(response["spot_proxy"]["positions"], [])
        self.assertEqual(response["spot_proxy"]["closed_spot_points_proxy"], 0)
        self.assertEqual(response["option_model"]["status"], "complete")
        self.assertEqual(response["option_model"]["label"], "Modelled option premiums")
        self.assertEqual(response["option_model"]["summary"]["selling"]["closed_count"], 0)
        self.assertGreater(len(response["option_model"]["ledgers"]["ce_hedge"]), 0)
        self.assertIn("NSE:NIFTY BANK", checked["source"])
        self.assertEqual(self.server.store.recent_runs(), before)
        self.assertNotIn("api_secret", json.dumps(response))
        self.assertNotIn("access_token", json.dumps(response))
        self.assertEqual(self.request("POST", "/api/orders", {})[0], 404)

    def test_spot_range_invalid_start_rejected(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/replay/spot-range", {"start_date": "not-a-date"})[0], 400)
        self.assertEqual(self.request("POST", "/api/replay/spot-range", {"start_date": "2099-01-01"})[0], 400)
        self.assertEqual(self.request("POST", "/api/replay/spot-range", {"date": "2026-09-01"})[0], 400)

    def test_option_quantity_changes_flow_through_read_only_api(self):
        self.login()
        config = {"sell_lots": 1, "ce_hedge_lots": 0, "pe_hedge_lots": 0, "units_per_lot": 15}
        status, response, _ = self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-28", "model_config": config, "return_base": 500000})
        self.assertEqual(status, 200)
        self.assertEqual(response["option_model"]["config"]["units_per_lot"], 15)
        self.assertEqual(response["option_model"]["reporting"]["return_base"], 500000)
        self.assertEqual(response["option_model"]["orders"], [])
        for invalid in ({"sell_lots": True}, {"units_per_lot": 0}, {"sell_lots": 1.5}, {"instrument": "NIFTY"}):
            self.assertEqual(self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-28", "model_config": invalid})[0], 400)
        for invalid_return_base in (True, 0, 1.5, 1000000001):
            self.assertEqual(self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-28", "return_base": invalid_return_base})[0], 400)

    def test_background_replay_routes_start_report_and_restore_result(self):
        self.login()
        self.server.kite.client.historical_minutes = lambda *args: []
        status, response, _ = self.request("POST", "/api/replay/spot-range/jobs", {"start_date": "2026-09-01"})
        self.assertEqual(status, 202)
        identity = response["job"]["id"]
        for _ in range(100):
            status, response, _ = self.request("GET", f"/api/replay/spot-range/jobs/{identity}")
            if response["job"]["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(status, 200)
        self.assertEqual(response["job"]["status"], "complete")
        self.assertEqual(response["job"]["result"]["range"]["start_date"], "2026-09-01")
        _, current, _ = self.request("GET", "/api/replay/spot-range/jobs/current")
        self.assertEqual(current["job"]["id"], identity)
        self.assertEqual(self.request("POST", f"/api/replay/spot-range/jobs/{identity}/cancel", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/replay/spot-range/jobs/missing")[0], 404)

    def test_spot_range_gap_is_not_mislabeled_as_no_signal(self):
        self.login()
        original = self.server.kite.client.historical_minutes
        start = datetime.fromisoformat("2026-09-01T09:15:00+05:30")
        end = datetime.fromisoformat("2026-09-29T15:29:00+05:30")
        full = original(260105, start, end)
        missing_at = "2026-09-01T10:17:00+05:30"
        self.server.kite.client.historical_minutes = lambda token, request_start, request_end: (
            [row for row in full if row["timestamp_ist"] != missing_at]
            if request_start.hour == 9 else [])
        status, response, _ = self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-01"})
        self.assertEqual(status, 200)
        first = response["range"]["days"][0]
        self.assertEqual(first["status"], "partial_hours")
        self.assertEqual(first["missing_minutes"], 1)
        self.assertEqual(first["missing_times"], [missing_at])
        self.assertEqual(first["completed_hour_count"], 5)
        self.assertEqual(first["signals"], [])
        self.assertEqual(response["spot_proxy"]["status"], "complete")
        self.assertEqual(response["spot_proxy"]["skipped_minute_count"], 1)
        self.assertTrue(response["option_model"]["approximate_due_to_missing_minutes"])

    def test_spot_range_retries_one_missing_minute_once(self):
        self.login()
        original = self.server.kite.client.historical_minutes
        start = datetime.fromisoformat("2026-09-01T09:15:00+05:30")
        end = datetime.fromisoformat("2026-09-29T15:29:00+05:30")
        full = original(260105, start, end)
        missing_at = "2026-09-01T10:17:00+05:30"
        missing = next(row for row in full if row["timestamp_ist"] == missing_at)
        calls = []

        def fetch(token, request_start, request_end):
            calls.append((request_start, request_end))
            return [row for row in full if row is not missing] if len(calls) == 1 else [missing]

        self.server.kite.client.historical_minutes = fetch
        status, response, _ = self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-01"})
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(response["range"]["recovered_minutes"], [missing_at])
        self.assertEqual(response["range"]["days"][0]["status"], "complete")

    def test_spot_range_final_fragment_gap_is_skipped_not_a_cutoff(self):
        self.login()
        original = self.server.kite.client.historical_minutes
        start = datetime.fromisoformat("2026-09-01T09:15:00+05:30")
        end = datetime.fromisoformat("2026-09-29T15:29:00+05:30")
        full = original(260105, start, end)
        missing_at = "2026-09-29T15:29:00+05:30"
        self.server.kite.client.historical_minutes = lambda token, request_start, request_end: (
            [row for row in full if row["timestamp_ist"] != missing_at]
            if request_start.hour == 9 else [])
        status, response, _ = self.request("POST", "/api/replay/spot-range", {"start_date": "2026-09-01"})
        self.assertEqual(status, 200)
        final_day = next(day for day in response["range"]["days"] if day["date"] == "2026-09-29")
        self.assertEqual(final_day["status"], "partial_hours")
        self.assertEqual(final_day["completed_hour_count"], 6)
        proxy = response["spot_proxy"]
        self.assertEqual(proxy["status"], "complete")
        self.assertEqual(proxy["last_session"], "2026-09-29")
        self.assertIsNone(proxy["blocked_from"])
        self.assertEqual(proxy["data_gaps"][0]["missing_times"], [missing_at])
        self.assertEqual(response["option_model"]["status"], "complete")

    def test_older_start_keeps_entire_range_to_latest_completed_date(self):
        self.login()
        status, response, _ = self.request("POST", "/api/replay/spot-range", {"start_date": "2026-08-01"})
        self.assertEqual(status, 200)
        self.assertEqual(response["range"]["start_date"], "2026-08-01")
        self.assertEqual(response["range"]["end_date"], response["range"]["latest_completed_date"])
        self.assertGreater(response["range"]["end_date"], "2026-08-31")
        self.assertGreaterEqual(response["range"]["download_batches"], 2)

    def test_past_spot_check_is_read_only_and_uses_complete_hourly_bars(self):
        self.login()
        before = self.server.store.recent_runs()
        status, response, _ = self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-28"})
        self.assertEqual(status, 200)
        check = response["check"]
        self.assertEqual(check["date"], "2026-09-28")
        self.assertEqual(check["minute_count"], 360)
        self.assertEqual(check["completed_hour_count"], 6)
        self.assertEqual(check["signals"], [])
        self.assertEqual(check["bars"][0]["bar_start"], "2026-09-28T09:15:00+05:30")
        self.assertEqual(check["bars"][-1]["scheduled_check_at"], "2026-09-28T15:15:20+05:30")
        self.assertIn("NSE:NIFTY BANK", check["source"])
        self.assertNotIn("access_token", json.dumps(response))
        self.assertNotIn("api_secret", json.dumps(response))
        self.assertEqual(self.server.store.recent_runs(), before)
        self.assertEqual(self.request("POST", "/api/orders", {})[0], 404)

    def test_incomplete_past_day_is_not_reported_as_no_signal(self):
        self.login()
        self.server.kite.client.historical_minutes = lambda *args: []
        status, response, _ = self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-28"})
        self.assertEqual(status, 422)
        self.assertEqual(response["quality"]["missing_minutes"], 360)
        self.assertEqual(len(response["quality"]["missing_times"]), 10)
        self.assertNotIn("check", response)

    def test_missing_minute_in_unused_final_fragment_does_not_block_signals(self):
        self.login()
        original = self.server.kite.client.historical_minutes
        self.server.kite.client.historical_minutes = lambda *args: original(*args)[:-1]
        status, response, _ = self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-28"})
        self.assertEqual(status, 200)
        self.assertEqual(response["check"]["minute_count"], 360)
        self.assertEqual(response["check"]["ignored_candles"]["partial_final_segment"], 14)
        self.assertIsNone(response["check"]["recovered_minute_at"])

    def test_one_missing_used_minute_is_refetched_only_from_broker(self):
        self.login()
        original = self.server.kite.client.historical_minutes
        full = original(260105, datetime.fromisoformat("2026-09-28T09:15:00+05:30"),
                        datetime.fromisoformat("2026-09-28T15:29:00+05:30"))
        missing = full[62]
        calls = []

        def fetch(token, start, end):
            calls.append((token, start.isoformat(), end.isoformat()))
            if len(calls) == 1:
                return [candle for candle in full if candle is not missing]
            return [missing]

        self.server.kite.client.historical_minutes = fetch
        status, response, _ = self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-28"})
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], (260105, "2026-09-28T10:17:00+05:30", "2026-09-28T10:17:59+05:30"))
        self.assertEqual(response["check"]["recovered_minute_at"], missing["timestamp_ist"])
        self.assertEqual(response["check"]["minute_count"], 360)

    def test_persistent_missing_used_minute_shows_exact_time_not_no_signal(self):
        self.login()
        original = self.server.kite.client.historical_minutes
        full = original(260105, datetime.fromisoformat("2026-09-28T09:15:00+05:30"),
                        datetime.fromisoformat("2026-09-28T15:29:00+05:30"))
        self.server.kite.client.historical_minutes = lambda token, start, end: (
            [candle for candle in full if candle["timestamp_ist"] != "2026-09-28T10:17:00+05:30"]
            if start.hour == 9 else [])
        status, response, _ = self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-28"})
        self.assertEqual(status, 422)
        self.assertEqual(response["quality"]["missing_minutes"], 1)
        self.assertEqual(response["quality"]["missing_times"], ["2026-09-28T10:17:00+05:30"])
        self.assertNotIn("check", response)

    def test_replay_rejects_future_and_weekend_dates_before_broker_request(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/replay/spot-signals", {"date": "2099-01-01"})[0], 422)
        self.assertEqual(self.request("POST", "/api/replay/spot-signals", {"date": "2026-09-27"})[0], 422)
        self.assertEqual(self.request("POST", "/api/replay/spot-signals", {"date": "not-a-date"})[0], 400)

    def test_forget_connection_clears_secret_token_and_cache(self):
        self.login()
        self.request("POST", "/api/kite/instruments/sync", {})
        status, reply, _ = self.request("POST", "/api/kite/disconnect", {})
        self.assertEqual(status, 200)
        self.assertFalse(reply["status"]["configured"])
        self.assertFalse(reply["status"]["connected"])
        self.assertIsNone(self.server.kite.api_secret)
        self.assertIsNone(self.server.kite.client)
        self.assertEqual(self.server.kite.instruments, [])
        self.assertEqual(self.request("GET", "/api/kite/spot")[0], 400)

    def test_expired_session_no_longer_counts_as_connected(self):
        self.login()
        self.server.kite.session_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        status, payload, _ = self.request("GET", "/api/kite/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["status"]["connected"])
        self.assertEqual(self.request("GET", "/api/kite/spot")[0], 400)

    def test_late_old_session_auth_error_does_not_clear_new_session(self):
        kite = KiteState(FakeKiteClient)
        old = FakeKiteClient("examplekey123")
        new = FakeKiteClient("examplekey123")
        kite.client = new
        kite.session_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        kite._auth_failed(old)
        self.assertIs(kite.client, new)
        self.assertTrue(kite.status(self.port)["connected"])

    def test_quote_in_flight_cannot_return_after_connection_is_forgotten(self):
        kite = KiteState(FakeKiteClient)
        old = FakeKiteClient("examplekey123")
        def delayed_quote():
            kite.clear(self.port)
            return {"last_price": 50123.45, "timestamp_ist": "2026-09-29T15:25:00+05:30"}
        old.banknifty_spot = delayed_quote
        kite.client = old
        kite.session_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, "session changed"):
            kite.spot()

    def test_untrusted_origins_cannot_set_credentials(self):
        status, _, _ = self.request("POST", "/api/kite/config", {"api_key": "examplekey123", "api_secret": "examplesecret123"}, {"Origin": "https://example.org"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
