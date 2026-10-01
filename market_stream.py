"""Read-only Kite full-mode stream. No broker order messages are supported."""
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import math
import struct
from threading import Event, RLock, Thread

from engine import IST
from kite_client import KiteError


class StreamRefusedError(Exception):
    """A broker text frame rejected the subscription; raw body stays private."""


def decode_frame(frame, subscriptions, received):
    if not isinstance(frame, bytes) or len(frame) > 1024 * 1024:
        raise ValueError("Invalid market-data frame.")
    if len(frame) == 1:
        return []  # heartbeat, never a quote
    if len(frame) < 2:
        raise ValueError("Truncated frame.")
    count = struct.unpack_from(">H", frame)[0]
    offset, quotes = 2, []
    if count > 3000:
        raise ValueError("Too many packets.")
    for _ in range(count):
        if offset + 2 > len(frame):
            raise ValueError("Truncated packet size.")
        size = struct.unpack_from(">H", frame, offset)[0]
        offset += 2
        packet = frame[offset:offset + size]
        offset += size
        if len(packet) != size or size not in (32, 184):
            raise ValueError("Only complete full-mode packets are accepted.")
        number = lambda start: struct.unpack_from(">I", packet, start)[0]
        token = number(0)
        spec = subscriptions.get(token)
        if not spec or (size == 32) != (spec["symbol"] == "NSE:NIFTY BANK"):
            raise ValueError("Unexpected instrument or packet type.")
        at = datetime.fromtimestamp(number(28 if size == 32 else 60), timezone.utc)
        if not -1 <= (received - at).total_seconds() <= 5:
            raise ValueError("Stale or future exchange timestamp.")
        price = number(4) / 100
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid last price.")
        quote = {"token": token, "symbol": spec["symbol"], "last_price": price,
                 "timestamp": at.isoformat(), "received_at": received.isoformat(), "source": "zerodha_stream"}
        if size == 184:
            for name, start in (("bids", 64), ("asks", 124)):
                quote[name] = []
                for pos in range(start, start + 60, 12):
                    quantity, value = number(pos), number(pos + 4)
                    if quantity and value:
                        quote[name].append({"quantity": quantity, "price": value / 100})
                    elif quantity or value:
                        raise ValueError("Incomplete depth level.")
            bids, asks = quote["bids"], quote["asks"]
            if bids != sorted(bids, key=lambda r: -r["price"]) or asks != sorted(asks, key=lambda r: r["price"]) or (bids and asks and bids[0]["price"] > asks[0]["price"]):
                raise ValueError("Crossed or unordered market depth.")
        quotes.append(quote)
    if offset != len(frame):
        raise ValueError("Unexpected trailing packet bytes.")
    return quotes


class ObservedCandles:
    """Observed ticks only. Disconnects and gaps invalidate an hour for entry."""
    def __init__(self, calendar):
        self.calendar = calendar
        self.minutes = {}
        self.invalid = set()
        self.last_at = None
        self.connected_at = None

    def connection(self, at):
        self.connected_at = at
        self.invalid.add(at.replace(second=0, microsecond=0))

    def disconnect(self, at):
        self.invalid.add(at.replace(second=0, microsecond=0))
        self.last_at = None

    def add(self, quote):
        at = datetime.fromisoformat(quote["timestamp"]).astimezone(IST)
        if not self.calendar.is_open(at):
            return
        if self.last_at and at < self.last_at:
            raise ValueError("Out-of-order spot tick.")
        minute = at.replace(second=0, microsecond=0)
        if self.last_at and at - self.last_at > timedelta(seconds=5):
            cursor = self.last_at.replace(second=0, microsecond=0)
            while cursor <= minute:
                self.invalid.add(cursor)
                cursor += timedelta(minutes=1)
        price = quote["last_price"]
        row = self.minutes.setdefault(minute, {"open": price, "high": price, "low": price, "close": price})
        row.update(high=max(row["high"], price), low=min(row["low"], price), close=price)
        self.last_at = at
        cutoff = at - timedelta(days=2)
        self.minutes = {t: v for t, v in self.minutes.items() if t >= cutoff}
        self.invalid = {t for t in self.invalid if t >= cutoff}

    def hour(self, start, now):
        start, now = start.astimezone(IST), now.astimezone(IST)
        end = start + timedelta(hours=1)
        windows = self.calendar.windows(start.date())
        if not any(a <= start and end <= b and (start - a).total_seconds() % 3600 == 0 for a, b in windows):
            raise ValueError("Not a scheduled full-hour candle.")
        if now < end or not self.last_at or self.last_at < end:
            raise ValueError("The hour is not closed and observed yet.")
        keys = [start + timedelta(minutes=i) for i in range(60)]
        if any(k not in self.minutes or k in self.invalid for k in keys):
            raise ValueError("Incomplete observed hour; no new entry is permitted.")
        rows = [self.minutes[k] for k in keys]
        return {"start": start.isoformat(), "end": end.isoformat(), "open": rows[0]["open"],
                "high": max(r["high"] for r in rows), "low": min(r["low"] for r in rows),
                "close": rows[-1]["close"], "source": "observed_stream_ticks", "exchange_ohlc_verified": False}


class MarketStream:
    def __init__(self, calendar, *, now=None, on_quotes=None):
        self.calendar = calendar
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.on_quotes = on_quotes
        self.archive_error = None
        self.lock, self.stop_event = RLock(), Event()
        self.thread = self.socket = None
        self.subscriptions, self.quotes = {}, {}
        self.state, self.message = "stopped", "Start the read-only stream during a verified session."
        self.retries = self.accepted = self.rejected = 0
        self.last_packet = None
        self.connected_since = None
        self.last_failure = None
        self.candles = ObservedCandles(calendar)

    def start(self, client, subscriptions, session_valid):
        if not subscriptions or len(subscriptions) > 3000:
            raise ValueError("Choose 1 to 3000 exact subscriptions.")
        if not self.calendar.is_open(self.now()):
            raise ValueError("Start the stream during a verified open exchange session.")
        self.stop()
        with self.lock:
            self.subscriptions = dict(subscriptions)
            self.quotes = {}
            self.retries = self.accepted = self.rejected = 0
            self.last_failure = None
            self.state, self.message = "connecting", "Connecting to Zerodha prices…"
            self.candles = ObservedCandles(self.calendar)
            self.stop_event = Event()
            self.thread = Thread(target=self._run, args=(client, session_valid), daemon=True)
            self.thread.start()
        return self.status()

    def _run(self, client, session_valid):
        # Initial connection plus at most three retries; never silently loop forever.
        for attempt in range(4):
            if self.stop_event.is_set():
                return
            phase = "opening"
            try:
                if not session_valid() or not self.calendar.is_open(self.now()):
                    raise ValueError("Session unavailable")
                sock = client.open_stream()
                with self.lock:
                    self.socket = sock
                tokens = list(self.subscriptions)
                phase = "subscribing"
                sock.send(json.dumps({"a": "subscribe", "v": tokens}))
                sock.send(json.dumps({"a": "mode", "v": ["full", tokens]}))
                with self.lock:
                    self.state, self.message = "connected", "Connected; waiting for fresh prices."
                    self.connected_since = self.now().isoformat()
                    self.candles.connection(self.now().astimezone(IST))
                phase = "receiving"
                while not self.stop_event.is_set():
                    if not session_valid() or not self.calendar.is_open(self.now()):
                        raise ValueError("Session unavailable")
                    frame = sock.recv()  # socket timeout also catches a dead connection
                    if not frame:
                        raise ValueError("Disconnected")
                    if isinstance(frame, str):
                        # No broker order updates are consumed as paper fills.
                        message = json.loads(frame)
                        if not isinstance(message, dict) or message.get("type") == "error":
                            raise StreamRefusedError()
                        continue
                    try:
                        batch = decode_frame(frame, self.subscriptions, self.now())
                        with self.lock:
                            self.last_packet = self.now().isoformat()
                            for quote in batch:
                                previous = self.quotes.get(quote["token"])
                                if previous and quote["timestamp"] < previous["timestamp"]:
                                    raise ValueError("Out-of-order stream update")
                                if quote["symbol"] == "NSE:NIFTY BANK":
                                    self.candles.add(quote)
                                self.quotes[quote["token"]] = quote
                                self.accepted += 1
                            self.message = "Receiving exchange prices. Paper fills remain estimates."
                        if self.on_quotes:
                            try:
                                self.on_quotes(batch)
                                self.archive_error = None
                            except Exception:
                                # Persistence failures must remain visible, without exposing credentials.
                                self.archive_error = "Option quote saving failed; future history may have gaps."
                    except (ValueError, OverflowError, KeyError, struct.error):
                        with self.lock:
                            self.rejected += 1
                            self.quotes.clear()
                            self.candles.disconnect(self.now().astimezone(IST))
                            self.message = "Invalid or stale update blocked; waiting for fresh prices."
            except KiteError as error:
                # KiteError messages are constructed by the adapter and never
                # contain a socket URL, access token, or broker response body.
                safe_failure = str(error)
                with self.lock:
                    self.last_failure = safe_failure
                    self.state, self.message = "reconnecting", safe_failure
                    self.quotes.clear()
                    self.candles.disconnect(self.now().astimezone(IST))
            except StreamRefusedError:
                safe_failure = "Zerodha refused the price subscription. Check that this Kite Connect app has active WebSocket market-data access."
                with self.lock:
                    self.last_failure = safe_failure
                    self.state, self.message = "reconnecting", safe_failure
                    self.quotes.clear()
                    self.candles.disconnect(self.now().astimezone(IST))
            except Exception:
                # Never expose arbitrary socket errors: handshake URLs can
                # contain credentials.  Keep the generic message for errors
                # raised after the safe broker adapter boundary too.
                if phase == "subscribing":
                    safe_failure = "The price stream closed while BANK NIFTY subscriptions were being sent. Check WebSocket access, firewall, proxy, or VPN settings."
                elif phase == "receiving":
                    safe_failure = "The price stream closed before any usable prices arrived. Check WebSocket access, firewall, proxy, or VPN settings."
                else:
                    safe_failure = "The price stream connection was interrupted. Fresh prices are blocked until it reconnects."
                with self.lock:
                    self.last_failure = safe_failure
                    self.state, self.message = "reconnecting", safe_failure
                    self.quotes.clear()
                    self.candles.disconnect(self.now().astimezone(IST))
            finally:
                with self.lock:
                    sock, self.socket = self.socket, None
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if not session_valid():
                break
            if attempt < 3:
                with self.lock:
                    self.retries = attempt + 1
                if self.stop_event.wait(min(2 ** attempt, 4)):
                    return
        with self.lock:
            if not self.stop_event.is_set():
                detail = self.last_failure or "The price stream connection could not be completed."
                self.state, self.message = "blocked", f"{detail} Reconnect manually."

    def stop(self):
        self.stop_event.set()
        with self.lock:
            sock, thread = self.socket, self.thread
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if thread:
            thread.join(timeout=12)
            if thread.is_alive():
                raise ValueError("Previous stream is still stopping. Try again shortly.")
        with self.lock:
            self.thread = None
            self.quotes.clear()
            self.state, self.message = "stopped", "Read-only stream stopped."

    def quote(self, token):
        with self.lock:
            if self.state != "connected" or token not in self.quotes:
                raise ValueError("A connected stream and fresh quote are required.")
            quote = dict(self.quotes[token])
            if any(not -1 <= (self.now() - datetime.fromisoformat(quote[k])).total_seconds() <= 5 for k in ("timestamp", "received_at")):
                raise ValueError("Quote is stale; no paper fill is permitted.")
            return quote

    def status(self):
        with self.lock:
            fresh = []
            for token in self.quotes:
                try:
                    fresh.append(self.quote(token))
                except ValueError:
                    pass
            return {"state": self.state, "message": self.message, "subscribed": len(self.subscriptions),
                    "fresh_count": len(fresh), "quotes": fresh, "accepted_ticks": self.accepted,
                    "rejected_packets": self.rejected, "retries": self.retries, "last_packet": self.last_packet,
                    "connected_since": self.connected_since, "mode": "read_only", "archive_error": self.archive_error,
                    "last_failure": self.last_failure}
