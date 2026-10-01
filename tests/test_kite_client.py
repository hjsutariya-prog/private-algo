"""Mock HTTP only: no real API keys, login, quotes, or broker network calls."""

import gzip
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_client import (  # noqa: E402
    KiteAuthError, KiteClient, KiteDataError, KiteError, KiteNetworkError,
    KiteRateLimitError, IST, _NoRedirect,
)


KEY, TOKEN, SECRET, REQUEST_TOKEN = "TEST_API_KEY", "TEST_ACCESS_TOKEN", "TEST_SECRET", "TEST_REQUEST_TOKEN"
CSV_HEADER = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
OPTION_ROW = "123456,123,BANKNIFTY26OCT55000CE,BANKNIFTY,0,2026-10-27,55000,0.05,30,CE,NFO-OPT,NFO\n"
INDEX_ROW = "260105,1016,NIFTY BANK,NIFTY BANK,0,,0,0,0,EQ,INDICES,NSE\n"


def success(data):
    return json.dumps({"status": "success", "data": data}).encode("utf-8")


def spot(**changes):
    quote = {"instrument_token": 260105, "last_price": 54123.45, "timestamp": "2026-09-29 15:12:34",
             "ohlc": {"open": 54000, "high": 54300, "low": 53900, "close": 53500}}
    quote.update(changes)
    return quote


class FakeResponse:
    def __init__(self, body, url, headers=None, code=200):
        self.stream = io.BytesIO(body)
        self.url, self.headers, self.code = url, headers or {}, code

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def geturl(self):
        return self.url

    def getcode(self):
        return self.code

    def read(self, limit):
        return self.stream.read(limit)


class FakeOpener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected mock HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return FakeResponse(response[0], request.full_url, response[1])
        return FakeResponse(response, request.full_url)


class KiteLoginTests(unittest.TestCase):
    def test_login_url_contains_public_key_and_encoded_return_state_only(self):
        opener = FakeOpener()
        client = KiteClient(KEY, opener=opener)
        parsed = urlsplit(client.login_url({"state": "ONE-TIME&STATE"}))
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "kite.zerodha.com")
        query = parse_qs(parsed.query)
        self.assertEqual(query["api_key"], [KEY])
        self.assertEqual(query["v"], ["3"])
        self.assertEqual(parse_qs(query["redirect_params"][0]), {"state": ["ONE-TIME&STATE"]})
        self.assertEqual(opener.requests, [])

    def test_manual_token_exchange_sends_checksum_not_secret_and_sanitizes_response(self):
        opener = FakeOpener(success({"access_token": TOKEN, "user_id": "TEST123", "api_key": KEY,
                                     "broker": "ZERODHA", "user_shortname": "Test", "login_time": "2026-09-29 10:00:00",
                                     "email": "private@example.invalid", "enctoken": "PRIVATE", "public_token": "PRIVATE"}))
        client = KiteClient(KEY, opener=opener)
        session = client.exchange_request_token(REQUEST_TOKEN, SECRET)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.kite.trade/session/token")
        form = parse_qs(request.data.decode())
        self.assertEqual(form["request_token"], [REQUEST_TOKEN])
        self.assertEqual(form["checksum"], [hashlib.sha256((KEY + REQUEST_TOKEN + SECRET).encode()).hexdigest()])
        self.assertNotIn(SECRET, request.data.decode())
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(set(session), {"access_token", "user_id", "user_shortname", "login_time"})
        self.assertEqual(session["access_token"], TOKEN)
        self.assertNotIn(SECRET, repr(vars(client)))
        self.assertNotIn(REQUEST_TOKEN, repr(vars(client)))

    def test_client_repr_does_not_expose_credentials(self):
        client = KiteClient(KEY, TOKEN, opener=FakeOpener())
        for secret in (KEY, TOKEN):
            self.assertNotIn(secret, repr(client))

    def test_controls_and_invalid_credentials_are_rejected(self):
        for bad in ("", None, 123, "bad\r\nheader", "bad token", "key:token"):
            with self.subTest(value=bad), self.assertRaises(KiteError):
                KiteClient(bad, opener=FakeOpener())

    def test_failed_session_shape_is_not_accepted(self):
        for payload in ({"access_token": TOKEN}, {"user_id": "TEST123"},
                        {"access_token": TOKEN, "user_id": "TEST123", "api_key": "OTHER_KEY"}):
            with self.subTest(payload=payload):
                with self.assertRaises(KiteDataError):
                    KiteClient(KEY, opener=FakeOpener(success(payload))).exchange_request_token(REQUEST_TOKEN, SECRET)

    def test_profile_returns_minimal_identity_without_personal_extra_fields(self):
        opener = FakeOpener(success({"user_id": "TEST123", "user_shortname": "Test", "broker": "ZERODHA", "email": "private@example.invalid"}))
        result = KiteClient(KEY, TOKEN, opener=opener).profile()
        self.assertEqual(result, {"user_id": "TEST123", "user_shortname": "Test", "broker": "ZERODHA"})
        self.assertEqual(opener.requests[0].get_header("Authorization"), f"token {KEY}:{TOKEN}")

    def test_no_login_means_no_network_request(self):
        opener = FakeOpener()
        with self.assertRaises(KiteAuthError):
            KiteClient(KEY, opener=opener).banknifty_spot()
        self.assertEqual(opener.requests, [])


class KiteInstrumentTests(unittest.TestCase):
    def test_default_nfo_catalogue_has_typed_fields_without_invented_lot_size(self):
        opener = FakeOpener((CSV_HEADER + OPTION_ROW).encode())
        result = KiteClient(KEY, TOKEN, opener=opener).instruments()
        self.assertEqual(opener.requests[0].full_url, "https://api.kite.trade/instruments/NFO")
        self.assertEqual(result[0]["lot_size"], 30)
        self.assertEqual(result[0]["expiry"], "2026-10-27")
        self.assertEqual(result[0]["strike"], 55000.0)
        self.assertEqual(result[0]["last_price"], 0.0)

    def test_index_can_have_zero_lot_and_tick_without_becoming_a_derivative(self):
        client = KiteClient(KEY, TOKEN, opener=FakeOpener((CSV_HEADER + INDEX_ROW).encode()))
        index = client.instruments("NSE")[0]
        self.assertEqual(index["tradingsymbol"], "NIFTY BANK")
        self.assertEqual(index["segment"], "INDICES")
        self.assertIsNone(index["expiry"])
        self.assertEqual(index["lot_size"], 0)

    def test_gzip_catalogue_is_supported(self):
        body = gzip.compress((CSV_HEADER + OPTION_ROW).encode())
        client = KiteClient(KEY, TOKEN, opener=FakeOpener((body, {"Content-Encoding": "gzip"})))
        self.assertEqual(client.instruments()[0]["lot_size"], 30)

    def test_duplicate_contract_references_are_not_silently_overwritten(self):
        client = KiteClient(KEY, TOKEN, opener=FakeOpener((CSV_HEADER + OPTION_ROW + OPTION_ROW).encode()))
        with self.assertRaises(KiteDataError):
            client.instruments()

    def test_missing_or_invalid_catalogue_values_are_rejected(self):
        for body in (
            CSV_HEADER, "wrong,columns\n1,2\n", CSV_HEADER + OPTION_ROW.replace(",30,CE,", ",0,CE,"),
            CSV_HEADER + OPTION_ROW.replace("2026-10-27", "2026-02-31"),
            CSV_HEADER + OPTION_ROW.replace(",55000,", ",NaN,"),
            CSV_HEADER + OPTION_ROW.replace(",NFO\n", ",NSE\n"),
        ):
            with self.subTest(body=body), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(body.encode())).instruments()

    def test_unknown_exchange_or_path_injection_is_rejected_without_http(self):
        opener = FakeOpener()
        for exchange in ("../orders", "NFO?secret=x", "MCX"):
            with self.subTest(exchange=exchange), self.assertRaises(KiteError):
                KiteClient(KEY, TOKEN, opener=opener).instruments(exchange)
        self.assertEqual(opener.requests, [])


class KiteQuoteTests(unittest.TestCase):
    def test_banknifty_is_exact_nse_spot_and_timestamp_is_preserved(self):
        opener = FakeOpener(success({"NSE:NIFTY BANK": spot()}))
        result = KiteClient(KEY, TOKEN, opener=opener).banknifty_spot()
        request = opener.requests[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"i": ["NSE:NIFTY BANK"]})
        self.assertEqual(result["last_price"], 54123.45)
        self.assertEqual(result["timestamp"], "2026-09-29 15:12:34")
        self.assertEqual(result["timestamp_ist"], "2026-09-29T15:12:34+05:30")
        # Previous close need not lie within today's high/low. It is not a candle.
        self.assertEqual(result["ohlc"]["close"], 53500)
        self.assertNotIn("volume", result)
        self.assertNotIn("depth", result)

    def test_multiple_symbols_use_repeated_query_parameters(self):
        symbols = ["NSE:NIFTY BANK", "NFO:BANKNIFTY26OCT55000CE"]
        opener = FakeOpener(success({symbol: spot() for symbol in symbols}))
        result = KiteClient(KEY, TOKEN, opener=opener).quotes(symbols)
        self.assertEqual(parse_qs(urlsplit(opener.requests[0].full_url).query)["i"], symbols)
        self.assertEqual(set(result), set(symbols))

    def test_missing_symbol_is_an_error_not_zero_or_previous_price(self):
        with self.assertRaises(KiteDataError):
            KiteClient(KEY, TOKEN, opener=FakeOpener(success({}))).banknifty_spot()

    def test_missing_invalid_or_dateless_timestamp_is_rejected(self):
        for timestamp in (None, "yesterday", "2026-09-29", "2026-02-31 12:00:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(success({"NSE:NIFTY BANK": spot(timestamp=timestamp)}))).banknifty_spot()

    def test_zero_negative_nonfinite_boolean_or_text_prices_are_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True, "54123.45"):
            with self.subTest(value=value), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(success({"NSE:NIFTY BANK": spot(last_price=value)}))).banknifty_spot()

    def test_unusable_instrument_tokens_are_rejected(self):
        for value in (0, -1, True, "260105", None):
            with self.subTest(value=value), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(success({"NSE:NIFTY BANK": spot(instrument_token=value)}))).banknifty_spot()

    def test_rate_guard_blocks_rapid_repeat_without_sleeping_or_http(self):
        opener = FakeOpener(success({"NSE:NIFTY BANK": spot()}), success({"NSE:NIFTY BANK": spot()}))
        client = KiteClient(KEY, TOKEN, opener=opener)
        with patch("kite_client.time.monotonic", side_effect=[10.0, 10.9, 11.0]):
            client.banknifty_spot()
            with self.assertRaises(KiteRateLimitError):
                client.banknifty_spot()
            client.banknifty_spot()
        self.assertEqual(len(opener.requests), 2)

    def test_invalid_or_excessive_symbol_lists_fail_without_http(self):
        for symbols in ([], "NSE:NIFTY BANK", [None], [[]], ["NSE:NIFTY BANK"] * 2,
                        ["NFO:"], ["NSE:bad\nheader"], [f"NSE:S{i}" for i in range(501)]):
            opener = FakeOpener()
            with self.subTest(symbols=symbols), self.assertRaises(KiteError):
                KiteClient(KEY, TOKEN, opener=opener).quotes(symbols)
            self.assertEqual(opener.requests, [])


class KiteHistoricalTests(unittest.TestCase):
    START = datetime(2020, 1, 2, 9, 15, tzinfo=IST)
    END = datetime(2020, 1, 2, 9, 17, tzinfo=IST)

    @staticmethod
    def candle(stamp="2020-01-02T09:15:00+0530", opening=100, high=103, low=99,
               closing=102, volume=0):
        return [stamp, opening, high, low, closing, volume]

    def test_exact_minute_endpoint_and_normalized_chronological_candles(self):
        rows = [self.candle(), self.candle("2020-01-02T09:16:00+0530", 102, 104, 101, 103, 42)]
        opener = FakeOpener(success({"candles": rows}))
        result = KiteClient(KEY, TOKEN, opener=opener).historical_minutes(260105, self.START, self.END)
        request = opener.requests[0]
        self.assertEqual(urlsplit(request.full_url).path, "/instruments/historical/260105/minute")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            "from": ["2020-01-02 09:15:00"], "to": ["2020-01-02 09:17:00"],
            "continuous": ["0"], "oi": ["0"],
        })
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(result, [
            {"timestamp_ist": "2020-01-02T09:15:00+05:30", "open": 100.0, "high": 103.0,
             "low": 99.0, "close": 102.0, "volume": 0},
            {"timestamp_ist": "2020-01-02T09:16:00+05:30", "open": 102.0, "high": 104.0,
             "low": 101.0, "close": 103.0, "volume": 42},
        ])

    def test_historical_batches_are_paced_below_three_requests_per_second(self):
        opener = FakeOpener(success({"candles": []}), success({"candles": []}))
        client = KiteClient(KEY, TOKEN, opener=opener)
        with patch("kite_client.time.monotonic", side_effect=[10.0, 10.0, 10.1, 10.4]), patch("kite_client.time.sleep") as pause:
            client.historical_minutes(260105, self.START, self.END)
            client.historical_minutes(260105, self.START, self.END)
        self.assertEqual(len(opener.requests), 2)
        pause.assert_called_once()
        self.assertAlmostEqual(pause.call_args.args[0], 0.3)

    def test_aware_utc_bounds_are_converted_to_exchange_time(self):
        opener = FakeOpener(success({"candles": [self.candle()]}))
        start = self.START.astimezone(timezone.utc)
        end = self.END.astimezone(timezone.utc)
        result = KiteClient(KEY, TOKEN, opener=opener).historical_minutes(260105, start, end)
        self.assertEqual(result[0]["timestamp_ist"], "2020-01-02T09:15:00+05:30")
        self.assertEqual(parse_qs(urlsplit(opener.requests[0].full_url).query)["from"], ["2020-01-02 09:15:00"])

    def test_empty_candle_list_is_an_explicit_no_data_result(self):
        self.assertEqual(KiteClient(KEY, TOKEN, opener=FakeOpener(success({"candles": []})))
                         .historical_minutes(260105, self.START, self.END), [])

    def test_invalid_token_bounds_or_incomplete_current_minute_never_use_http(self):
        current_minute = datetime.now(IST).replace(second=0, microsecond=0)
        cases = [
            (True, self.START, self.END), (0, self.START, self.END),
            ("260105", self.START, self.END), (2**63, self.START, self.END),
            (260105, self.START.replace(tzinfo=None), self.END),
            (260105, self.START, self.END.replace(tzinfo=None)),
            (260105, self.END, self.START),
            (260105, self.START, self.START + timedelta(days=60)),
            (260105, current_minute - timedelta(minutes=1), current_minute),
            (260105, current_minute + timedelta(minutes=1), current_minute + timedelta(minutes=2)),
        ]
        for token, start, end in cases:
            opener = FakeOpener()
            with self.subTest(token=token, start=start, end=end), self.assertRaises(KiteError):
                KiteClient(KEY, TOKEN, opener=opener).historical_minutes(token, start, end)
            self.assertEqual(opener.requests, [])

    def test_completed_today_minute_is_allowed_but_not_assumed_to_have_data(self):
        current_minute = datetime.now(IST).replace(second=0, microsecond=0)
        opener = FakeOpener(success({"candles": []}))
        result = KiteClient(KEY, TOKEN, opener=opener).historical_minutes(
            260105, current_minute - timedelta(minutes=2), current_minute - timedelta(minutes=1))
        self.assertEqual(result, [])
        self.assertEqual(len(opener.requests), 1)

    def test_malformed_response_rows_fail_closed(self):
        rows = [
            None, {}, self.candle()[:-1], self.candle() + [7],
            self.candle(stamp="2020-01-02 09:15:00"),
            self.candle(stamp="2020-01-02T09:15:30+0530"),
            self.candle(stamp="2020-01-02T09:14:00+0530"),
            self.candle(stamp="2020-01-02T09:18:00+0530"),
            self.candle(opening=0), self.candle(high=98), self.candle(low=103),
            self.candle(closing="102"), self.candle(closing=float("nan")),
            self.candle(high=float("inf")), self.candle(volume=-1),
            self.candle(volume=0.5), self.candle(volume=True),
        ]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(success({"candles": [row]}))).historical_minutes(
                    260105, self.START, self.END)
        for payload in ({}, {"candles": {}}, {"candles": None}):
            with self.subTest(payload=payload), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(success(payload))).historical_minutes(
                    260105, self.START, self.END)

    def test_duplicate_or_out_of_order_candles_fail_closed(self):
        first = self.candle()
        second = self.candle("2020-01-02T09:16:00+0530")
        for rows in ([first, first], [second, first]):
            with self.subTest(rows=rows), self.assertRaises(KiteDataError):
                KiteClient(KEY, TOKEN, opener=FakeOpener(success({"candles": rows}))).historical_minutes(
                    260105, self.START, self.END)

    def test_only_minute_historical_path_can_be_sent(self):
        client = KiteClient(KEY, TOKEN, opener=FakeOpener())
        for path in ("/instruments/historical/260105/day", "/instruments/historical/260105/60minute",
                     "/instruments/historical/../orders/minute", "/instruments/historical/260105/minute?x=1"):
            with self.subTest(path=path), self.assertRaises(KiteError):
                client._request("GET", path)

    def test_historical_requires_manual_login_and_does_not_send_http_without_it(self):
        opener = FakeOpener()
        with self.assertRaises(KiteAuthError):
            KiteClient(KEY, opener=opener).historical_minutes(260105, self.START, self.END)
        self.assertEqual(opener.requests, [])


class KiteTransportSafetyTests(unittest.TestCase):
    def test_auth_errors_clear_local_token_without_exposing_provider_body(self):
        for code in (401, 403):
            error = HTTPError("https://api.kite.trade/quote", code, SECRET, {}, io.BytesIO(TOKEN.encode()))
            opener = FakeOpener(error)
            client = KiteClient(KEY, TOKEN, opener=opener)
            with self.subTest(code=code), self.assertRaises(KiteAuthError) as context:
                client.profile()
            self.assertNotIn(SECRET, str(context.exception))
            self.assertNotIn(TOKEN, str(context.exception))
            with self.assertRaises(KiteAuthError):
                client.profile()
            self.assertEqual(len(opener.requests), 1)

    def test_json_token_exception_requires_relogin(self):
        body = json.dumps({"status": "error", "error_type": "TokenException", "message": SECRET}).encode()
        with self.assertRaises(KiteAuthError) as context:
            KiteClient(KEY, TOKEN, opener=FakeOpener(body)).profile()
        self.assertNotIn(SECRET, str(context.exception))

    def test_rate_error_does_not_clear_valid_token(self):
        opener = FakeOpener(HTTPError("https://api.kite.trade/quote", 429, SECRET, {}, None),
                            success({"user_id": "TEST123", "broker": "ZERODHA"}))
        client = KiteClient(KEY, TOKEN, opener=opener)
        with self.assertRaises(KiteRateLimitError):
            client.profile()
        self.assertEqual(client.profile()["user_id"], "TEST123")

    def test_network_errors_are_sanitized_and_not_retried(self):
        opener = FakeOpener(URLError(f"Internal details {TOKEN} {SECRET}"))
        with self.assertRaises(KiteNetworkError) as context:
            KiteClient(KEY, TOKEN, opener=opener).profile()
        self.assertNotIn(TOKEN, str(context.exception))
        self.assertNotIn(SECRET, str(context.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_windows_outbound_block_has_an_actionable_safe_message(self):
        blocked = PermissionError(10013, "access denied")
        blocked.winerror = 10013
        opener = FakeOpener(URLError(blocked))
        with self.assertRaises(KiteNetworkError) as context:
            KiteClient(KEY, TOKEN, opener=opener).profile()
        self.assertIn("Windows blocked the local app", str(context.exception))
        self.assertIn("outbound HTTPS", str(context.exception))
        self.assertNotIn("access denied", str(context.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_redirect_handler_never_forwards_request(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://another.invalid/"))

    def test_redirect_response_is_a_safe_failure(self):
        error = HTTPError("https://api.kite.trade/quote", 302, SECRET, {}, None)
        with self.assertRaises(KiteNetworkError):
            KiteClient(KEY, TOKEN, opener=FakeOpener(error)).profile()

    def test_malformed_json_or_success_shape_is_rejected(self):
        for body in (b"not json", b"[]", b'{"status":"success","data":[]}', b'{"status":"error","message":"PRIVATE"}'):
            with self.subTest(body=body), self.assertRaises(KiteError) as context:
                KiteClient(KEY, TOKEN, opener=FakeOpener(body)).profile()
            self.assertNotIn("PRIVATE", str(context.exception))

    def test_response_and_decompressed_size_are_bounded(self):
        for response in (b"x" * 65, (gzip.compress(b"x" * 65), {"Content-Encoding": "gzip"})):
            with self.subTest(compressed=isinstance(response, tuple)), patch("kite_client.MAX_JSON_BYTES", 64):
                with self.assertRaises(KiteDataError):
                    KiteClient(KEY, TOKEN, opener=FakeOpener(response)).profile()

    def test_adapter_cannot_call_order_or_account_mutation_endpoints(self):
        client = KiteClient(KEY, TOKEN, opener=FakeOpener())
        for method, path in (("POST", "/orders/regular"), ("DELETE", "/session/token"),
                             ("GET", "/orders"), ("GET", "https://other.invalid/quote")):
            with self.subTest(method=method, path=path), self.assertRaises(KiteError):
                client._request(method, path)
        self.assertFalse(hasattr(client, "place_order"))


if __name__ == "__main__":
    unittest.main()

