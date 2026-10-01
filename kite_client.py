"""Narrow, read-only Kite Connect adapter; stdlib only and no order methods.

The only POST allowed is the manual-login session-token exchange. Credentials
stay in memory. Do not serialize session results, request objects, or this
client's private attributes into browser responses or logs.

Official contracts: https://kite.trade/docs/connect/v3/user/ and
https://kite.trade/docs/connect/v3/market-quotes/ and
https://kite.trade/docs/connect/v3/historical/ . Snapshots are NOT hourly
candles: quote OHLC close is the previous trading day's close. CSV last_price
is a daily catalogue value, never a live quote or a proposed execution price.
"""

import csv
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
from http.client import HTTPException
import io
import json
import math
import re
import threading
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ROOT = "https://api.kite.trade"
LOGIN_ROOT = "https://kite.zerodha.com/connect/login"
IST = timezone(timedelta(hours=5, minutes=30))
QUOTE_INTERVAL_SECONDS = 1.0
HISTORY_INTERVAL_SECONDS = 0.4  # Below Kite's three historical requests/second.
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_MINUTE_HISTORY_SPAN = timedelta(days=60)
_HISTORICAL_MINUTE_PATH = re.compile(r"/instruments/historical/[1-9][0-9]{0,18}/minute\Z")
_CANDLE_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:?[0-9]{2}\Z")


class KiteError(Exception):
    """Safe message for the local UI; never includes broker bodies or secrets."""


class KiteAuthError(KiteError):
    """A new manual login is required; the caller should clear its token too."""


class KiteRateLimitError(KiteError):
    pass


class KiteDataError(KiteError):
    pass


class KiteNetworkError(KiteError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Never forward authenticated headers or token forms to another URL.
        return None


def _credential(value, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or not value.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or ":" in value):
        raise KiteError(f"Enter a valid {label}; it must not contain spaces or control characters.")
    return value


def _number(value, *, positive=False, csv_value=False):
    if isinstance(value, bool) or (not csv_value and not isinstance(value, (int, float))):
        raise KiteDataError("Kite returned an invalid numeric value. No market value was accepted.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise KiteDataError("Kite returned an invalid numeric value. No market value was accepted.") from None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise KiteDataError("Kite returned an unusable price or quantity. Refresh before relying on it.")
    return result


def _integer(value, *, positive=False, csv_value=False):
    if csv_value:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise KiteDataError("Kite returned an invalid instrument identifier or lot size.")
        result = int(value)
    elif type(value) is int:
        result = value
    else:
        raise KiteDataError("Kite returned an invalid instrument identifier or lot size.")
    if result < 0 or (positive and result == 0):
        raise KiteDataError("Kite returned an invalid instrument identifier or lot size.")
    return result


def _safe_text(value, *, required=False):
    if value is None and not required:
        return None
    if (not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or (required and not value.strip())):
        raise KiteDataError("Kite returned an invalid text field.")
    return value


class KiteClient:
    """Read-only data client. ``opener`` injection is for offline HTTP tests.

    Keep one shared instance per local account to share the one-quote-per-second
    guard. The guard is not a cross-process rate limiter. There are no automatic
    retries, no saved passwords, no automatic login, and no order endpoints.
    """

    def __init__(self, api_key, access_token=None, *, opener=None):
        self._api_key = _credential(api_key, "API key")
        self._access_token = None if access_token is None else _credential(access_token, "access token")
        self._opener = opener if opener is not None else build_opener(_NoRedirect())
        self._lock = threading.RLock()
        self._last_quote_at = None
        self._last_history_at = None

    def __repr__(self):
        return f"<KiteClient read_only=True authenticated={self._access_token is not None}>"

    def clear_access_token(self):
        with self._lock:
            self._access_token = None

    def login_url(self, redirect_params=None):
        """Public login URL. User signs in at Zerodha, never inside this adapter.

        Optional redirect_params is a string-to-string dict; caller should put a
        one-time state value here and validate it at its registered callback.
        """
        query = {"v": "3", "api_key": self._api_key}
        if redirect_params is not None:
            if (not isinstance(redirect_params, dict) or len(redirect_params) > 10
                    or not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 100 and len(v) <= 1024
                               for k, v in redirect_params.items())):
                raise KiteError("Invalid login return parameters.")
            query["redirect_params"] = urlencode(redirect_params)
        return LOGIN_ROOT + "?" + urlencode(query)

    def exchange_request_token(self, request_token, api_secret):
        """Manual-login exchange; returned access_token is SERVER-ONLY.

        request_token lasts only minutes. Access tokens normally expire at 06:00
        the next day and may be invalidated sooner. No api_secret is retained.
        """
        request_token = _credential(request_token, "request token")
        api_secret = _credential(api_secret, "API secret")
        checksum = hashlib.sha256((self._api_key + request_token + api_secret).encode("utf-8")).hexdigest()
        data = self._json("POST", "/session/token", form={
            "api_key": self._api_key, "request_token": request_token, "checksum": checksum,
        }, authenticated=False)
        try:
            token = _credential(data.get("access_token"), "access token")
            user_id = _safe_text(data.get("user_id"), required=True)
            shortname = _safe_text(data.get("user_shortname"))
            login_time = _safe_text(data.get("login_time"))
        except KiteError:
            raise KiteDataError("Kite did not return a complete, valid login session. Sign in again.") from None
        if data.get("api_key", self._api_key) != self._api_key or data.get("broker", "ZERODHA") != "ZERODHA":
            raise KiteDataError("The login response did not match this Kite app.")
        with self._lock:
            self._access_token = token
        return {"access_token": token, "user_id": user_id,
                "user_shortname": shortname, "login_time": login_time}

    def profile(self):
        """Minimal account identity only; no email, funds, positions or tokens."""
        data = self._json("GET", "/user/profile")
        broker = _safe_text(data.get("broker"), required=True)
        if broker != "ZERODHA":
            raise KiteDataError("The returned account is not a Zerodha account.")
        return {"user_id": _safe_text(data.get("user_id"), required=True),
                "user_shortname": _safe_text(data.get("user_shortname")), "broker": broker}

    def instruments(self, exchange="NFO"):
        """Validated daily catalogue; expiry ISO string/None, numeric fields typed.

        Refresh at most daily unless explicitly needed. Use exchange + symbol as
        identity: numeric derivative tokens can be reused after expiry. This
        method does not label contracts monthly, choose expiry, or promise that
        Zerodha permits buying every listed strike.
        """
        if exchange not in ("NSE", "NFO"):
            raise KiteError("This read-only adapter accepts only NSE or NFO instruments.")
        raw = self._request("GET", "/instruments/" + exchange, csv_response=True)
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise KiteDataError("The Kite instrument file was unreadable.") from None
        reader = csv.DictReader(io.StringIO(source), strict=True)
        required = {"instrument_token", "exchange_token", "tradingsymbol", "name", "last_price", "expiry",
                    "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange"}
        if not reader.fieldnames or set(reader.fieldnames) != required or len(reader.fieldnames) != len(required):
            raise KiteDataError("The Kite instrument file has unexpected columns; no catalogue was accepted.")
        instruments, seen = [], set()
        try:
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise KiteDataError("The Kite instrument file contains an incomplete row.")
                item = {key: _safe_text(row[key], required=key != "name")
                        for key in ("tradingsymbol", "name", "instrument_type", "segment", "exchange")}
                if item["exchange"] != exchange:
                    raise KiteDataError("The instrument file contains an unexpected exchange.")
                identity = (item["exchange"], item["tradingsymbol"])
                if identity in seen:
                    raise KiteDataError("The instrument file contains a duplicate contract reference.")
                seen.add(identity)
                item.update(instrument_token=_integer(row["instrument_token"], positive=True, csv_value=True),
                            exchange_token=_integer(row["exchange_token"], csv_value=True),
                            lot_size=_integer(row["lot_size"], csv_value=True),
                            tick_size=_number(row["tick_size"], csv_value=True),
                            last_price=_number(row["last_price"], csv_value=True),
                            strike=_number(row["strike"], csv_value=True) if row["strike"] else None)
                try:
                    item["expiry"] = date.fromisoformat(row["expiry"]).isoformat() if row["expiry"] else None
                except ValueError:
                    raise KiteDataError("The instrument file contains an invalid expiry date.") from None
                if item["instrument_type"] in ("CE", "PE", "FUT"):
                    if not item["expiry"] or item["lot_size"] <= 0:
                        raise KiteDataError("A derivative contract is missing its expiry or lot size.")
                    if item["instrument_type"] in ("CE", "PE") and (item["strike"] is None or item["strike"] <= 0):
                        raise KiteDataError("An option contract is missing its strike.")
                instruments.append(item)
        except csv.Error:
            raise KiteDataError("The Kite instrument file contains invalid CSV data.") from None
        if not instruments:
            raise KiteDataError("Kite returned an empty instrument catalogue.")
        return instruments

    def quotes(self, symbols):
        """Full snapshot map; missing keys/time/positive price fail closed.

        timestamp is the broker string, timestamp_ist is normalized ISO time.
        Daily OHLC may be absent. Index quotes do not require trade-only fields
        such as volume, last_trade_time or depth. Receipt is not proof of fresh
        data: the caller must show/check the exchange timestamp independently.
        """
        if (not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= 500
                or not all(isinstance(symbol, str) for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise KiteError("Request 1 to 500 distinct exchange:symbol references.")
        for symbol in symbols:
            if (not isinstance(symbol, str) or len(symbol) > 120 or symbol.count(":") != 1
                    or symbol.split(":", 1)[0] not in ("NSE", "NFO") or not symbol.split(":", 1)[1].strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in symbol)):
                raise KiteError("Use an exact NSE:SYMBOL or NFO:SYMBOL quote reference.")
        data = self._json("GET", "/quote", query=[("i", symbol) for symbol in symbols])
        result = {}
        for symbol in symbols:
            row = data.get(symbol)
            if not isinstance(row, dict):
                raise KiteDataError("Kite did not return every requested quote; no missing price was substituted.")
            stamp = _safe_text(row.get("timestamp"), required=True)
            try:
                if len(stamp) < 19 or stamp[10] not in (" ", "T"):
                    raise ValueError("Missing time component")
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=IST)
                normalized = parsed.astimezone(IST).isoformat()
            except (ValueError, OverflowError):
                raise KiteDataError("Kite returned an invalid quote timestamp.") from None
            item = {"instrument_token": _integer(row.get("instrument_token"), positive=True),
                    "last_price": _number(row.get("last_price"), positive=True),
                    "timestamp": stamp, "timestamp_ist": normalized}
            if "ohlc" in row:
                daily = row["ohlc"]
                if not isinstance(daily, dict) or not {"open", "high", "low", "close"} <= daily.keys():
                    raise KiteDataError("Kite returned incomplete daily OHLC values.")
                item["ohlc"] = {key: _number(daily[key]) for key in ("open", "high", "low", "close")}
            result[symbol] = item
        return result

    def banknifty_spot(self):
        """NSE index snapshot, NOT an NFO futures/option approximation."""
        return self.quotes(["NSE:NIFTY BANK"])["NSE:NIFTY BANK"]

    def open_stream(self):
        """Server-only read-only socket; never return/log the authenticated URL."""
        import socket as socket_module
        import ssl
        import sys
        from pathlib import Path
        project_root = Path(__file__).resolve().parent
        # .deps is an older optional install location. Prefer the project-owned
        # replacement path so a damaged or unreadable old folder cannot prevent
        # read-only price streaming.
        clean_path = project_root / ".stream-deps"
        legacy_path = project_root / ".deps"
        chosen_path = clean_path if clean_path.is_dir() else legacy_path
        dependency_path = str(chosen_path)
        if chosen_path.is_dir() and dependency_path not in sys.path:
            sys.path.insert(0, dependency_path)
        try:
            from websocket import create_connection
        except ImportError:
            raise KiteError("Streaming dependency is not installed. See requirements-stream.txt.") from None
        with self._lock:
            if not self._access_token:
                raise KiteAuthError("Sign in to Zerodha before starting the stream.")
            query = urlencode({"api_key": self._api_key, "access_token": self._access_token})
        try:
            socket = create_connection("wss://ws.kite.trade?" + query, timeout=8,
                                       redirect_limit=0, enable_multithread=True)
            # websocket-client returns an unconnected socket when redirect_limit=0
            # and a redirect occurs. Never allow subsequent credential forwarding.
            if not socket.connected or socket.getstatus() != 101:
                socket.close()
                raise ValueError("Handshake incomplete")
            return socket
        except Exception as error:
            # Do not copy the socket exception into the UI: it can contain the
            # authenticated URL.  These categories are deliberately small and
            # safe enough to display to the person running the local desk.
            status = getattr(error, "status_code", None)
            if status in (401, 403):
                message = "Zerodha declined the price stream. Sign in again and check that this app has streaming access."
            elif status == 429:
                message = "Zerodha is limiting price-stream connections. Wait a few minutes before reconnecting."
            elif status in (301, 302, 303, 307, 308):
                message = "Zerodha redirected the price stream. The app blocked it to keep your sign-in details private."
            elif isinstance(error, (TimeoutError, socket_module.timeout)):
                message = "The price stream timed out before Zerodha replied. Check your network or try again shortly."
            elif isinstance(error, socket_module.gaierror):
                message = "This computer could not find Zerodha's price-stream address. Check DNS, VPN, or network settings."
            elif isinstance(error, ssl.SSLError):
                message = "The secure connection to Zerodha's price stream failed. Check the computer clock or network security software."
            elif isinstance(error, ConnectionRefusedError):
                message = "The network refused Zerodha's price-stream connection. Check firewall, proxy, or VPN settings."
            else:
                message = "Zerodha's price stream could not connect. Check network access and streaming entitlement."
            raise KiteNetworkError(message) from None

    def historical_minutes(self, instrument_token, from_datetime, to_datetime):
        """Past, one-minute OHLCV candles for an exact instrument token.

        Both bounds must be timezone-aware and are sent in India Standard Time.
        The end must precede the currently forming minute. A completed minute
        still may be unavailable or revised by the exchange; callers doing a
        full-day replay must separately require a completed trading session.
        This method cannot retrieve expired option contracts without their
        previously saved tokens; it does not synthesize continuous option data.
        """
        if type(instrument_token) is not int or not 0 < instrument_token < 2**63:
            raise KiteError("Select a valid positive instrument token.")
        if (not isinstance(from_datetime, datetime) or not isinstance(to_datetime, datetime)
                or from_datetime.tzinfo is None or to_datetime.tzinfo is None
                or from_datetime.utcoffset() is None or to_datetime.utcoffset() is None):
            raise KiteError("Choose a timezone-aware start and end time for historical data.")
        start = from_datetime.astimezone(IST)
        end = to_datetime.astimezone(IST)
        if (start >= end or end - start >= MAX_MINUTE_HISTORY_SPAN
                or end >= datetime.now(IST).replace(second=0, microsecond=0)):
            raise KiteError("Choose a past time range shorter than 60 days, ending before the current minute.")
        path = f"/instruments/historical/{instrument_token}/minute"
        data = self._json("GET", path, query={
            "from": start.strftime("%Y-%m-%d %H:%M:%S"),
            "to": end.strftime("%Y-%m-%d %H:%M:%S"),
            "continuous": "0", "oi": "0",
        })
        rows = data.get("candles")
        if not isinstance(rows, list):
            raise KiteDataError("Kite returned an invalid historical candle list.")
        result = []
        previous = None
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                raise KiteDataError("Kite returned a malformed one-minute candle.")
            stamp = row[0]
            if not isinstance(stamp, str) or not _CANDLE_TIMESTAMP.fullmatch(stamp):
                raise KiteDataError("Kite returned an invalid one-minute candle time.")
            try:
                at = datetime.fromisoformat(stamp).astimezone(IST)
            except (ValueError, OverflowError):
                raise KiteDataError("Kite returned an invalid one-minute candle time.") from None
            if (at.second != 0 or at.microsecond != 0 or not start <= at <= end
                    or (previous is not None and at <= previous)):
                raise KiteDataError("Kite returned a candle outside the requested range or out of time order.")
            opening, high, low, closing = (_number(value, positive=True) for value in row[1:5])
            if low > min(opening, closing) or high < max(opening, closing) or low > high:
                raise KiteDataError("Kite returned inconsistent one-minute OHLC prices.")
            result.append({"timestamp_ist": at.isoformat(), "open": opening, "high": high,
                           "low": low, "close": closing, "volume": _integer(row[5])})
            previous = at
        return result

    def _json(self, method, path, **kwargs):
        raw = self._request(method, path, **kwargs)
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeDecodeError, ValueError):
            raise KiteDataError("Kite returned an unreadable response. No market value was accepted.") from None
        if not isinstance(payload, dict):
            raise KiteDataError("Kite returned an unexpected response shape.")
        if payload.get("status") != "success":
            self._raise_api_error(None, payload.get("error_type"))
        if not isinstance(payload.get("data"), dict):
            raise KiteDataError("Kite returned an unexpected data shape.")
        return payload["data"]

    def _raise_api_error(self, code, error_type=None):
        if code in (401, 403) or error_type == "TokenException":
            self.clear_access_token()
            raise KiteAuthError("Kite login has expired or is invalid. Sign in again manually.") from None
        if code == 429:
            raise KiteRateLimitError("Kite is limiting requests. Wait before refreshing again.") from None
        if code in (301, 302, 303, 307, 308):
            raise KiteNetworkError("Kite redirected an API request. The redirect was blocked for credential safety.") from None
        raise KiteError("Kite could not complete this read-only request. Check the connection and try again.") from None

    def _request(self, method, path, *, query=None, form=None, authenticated=True, csv_response=False):
        allowed = {("POST", "/session/token"), ("GET", "/user/profile"),
                   ("GET", "/instruments/NSE"), ("GET", "/instruments/NFO"), ("GET", "/quote")}
        if ((method, path) not in allowed and not (method == "GET" and _HISTORICAL_MINUTE_PATH.fullmatch(path))
                or (method == "GET" and form is not None)):
            raise KiteError("This adapter allows only login and specified read-only data requests.")
        with self._lock:
            if authenticated and self._access_token is None:
                raise KiteAuthError("Sign in to Kite before requesting account or market data.")
            if path == "/quote":
                current = time.monotonic()
                if self._last_quote_at is not None and current - self._last_quote_at < QUOTE_INTERVAL_SECONDS:
                    raise KiteRateLimitError("Wait at least one second between quote refreshes.")
                self._last_quote_at = current
            elif _HISTORICAL_MINUTE_PATH.fullmatch(path):
                current = time.monotonic()
                if self._last_history_at is not None:
                    delay = HISTORY_INTERVAL_SECONDS - (current - self._last_history_at)
                    if delay > 0:
                        time.sleep(delay)
                self._last_history_at = time.monotonic()
            url = API_ROOT + path + ("?" + urlencode(query) if query else "")
            headers = {"X-Kite-Version": "3", "Accept": "text/csv" if csv_response else "application/json",
                       "Accept-Encoding": "gzip", "User-Agent": "PrivateAlgoDesk-ReadOnly/0.1"}
            if authenticated:
                headers["Authorization"] = f"token {self._api_key}:{self._access_token}"
            body = None
            if form is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = urlencode(form).encode("utf-8")
            request = Request(url, data=body, headers=headers, method=method)
            maximum = MAX_CSV_BYTES if csv_response else MAX_JSON_BYTES
            try:
                with self._opener.open(request, timeout=12) as response:
                    if response.geturl() != url:
                        raise KiteNetworkError("An unexpected API redirect was blocked.")
                    code = response.getcode()
                    if code != 200:
                        self._raise_api_error(code)
                    raw = response.read(maximum + 1)
                    encoding = response.headers.get("Content-Encoding", "").strip().lower()
                if len(raw) > maximum:
                    raise KiteDataError("The Kite response exceeded the safe size limit.")
                if encoding == "gzip" or raw.startswith(b"\x1f\x8b"):
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                        raw = compressed.read(maximum + 1)
                    if len(raw) > maximum:
                        raise KiteDataError("The decoded Kite response exceeded the safe size limit.")
                elif encoding not in ("", "identity"):
                    raise KiteDataError("Kite returned an unsupported response encoding.")
                return raw
            except HTTPError as error:
                error.close()
                self._raise_api_error(error.code)
            except URLError as error:
                # Windows error 10013 means local security software or a
                # firewall policy refused the outgoing socket.  It is not a
                # failed Zerodha login, so give the person a useful next step
                # without exposing an OS exception, endpoint, or credentials.
                reason = getattr(error, "reason", None)
                if isinstance(reason, PermissionError) and getattr(reason, "winerror", None) == 10013:
                    raise KiteNetworkError(
                        "Windows blocked the local app from reaching Zerodha. Allow Private Algo Desk's Python "
                        "app to make outbound HTTPS connections, then sign in again.") from None
                raise KiteNetworkError("Kite could not be reached or returned incomplete data. No order was sent.") from None
            except PermissionError as error:
                if getattr(error, "winerror", None) == 10013:
                    raise KiteNetworkError(
                        "Windows blocked the local app from reaching Zerodha. Allow Private Algo Desk's Python "
                        "app to make outbound HTTPS connections, then sign in again.") from None
                raise KiteNetworkError("Kite could not be reached or returned incomplete data. No order was sent.") from None
            except (TimeoutError, OSError, EOFError, HTTPException, zlib.error):
                raise KiteNetworkError("Kite could not be reached or returned incomplete data. No order was sent.") from None
