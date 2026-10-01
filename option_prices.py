"""Shared recorded-first option pricing, with explicit model fallback.

An observation is never a broker fill. Contract identity and observation time
must match; no interpolation, token-only matching or future-price selection.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager, closing
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from backtest_config import decimal
from engine import IST
from market_sessions import session_windows

POLICY = "recorded_first_estimate_fallback_v1"


def at_time(value):
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("Option-price times need a timezone.")
    return at.astimezone(IST)


def contract_key(contract):
    if not isinstance(contract, dict):
        raise ValueError("An exact option contract is required.")
    instrument, kind = contract.get("instrument"), contract.get("option_type")
    expiry, strike = date.fromisoformat(contract["expiry"]), decimal(contract["strike"])
    if not instrument or kind not in ("CE", "PE") or strike <= 0:
        raise ValueError("Invalid option contract.")
    return instrument, expiry.isoformat(), kind, float(strike)


def resolve_premium(estimated, observation=None, reason="No matching recorded option price"):
    """One common resolver for historical and forward paper valuations."""
    if observation is not None:
        price = decimal(observation["premium"])
        if price <= 0:
            raise ValueError("Recorded option prices must be positive.")
        return {"premium": float(price), "basis": "recorded", "source": dict(observation),
                "fallback_reason": None, "is_broker_fill": False}
    price = decimal(estimated)
    if price < 0:
        raise ValueError("Estimated option prices cannot be negative.")
    return {"premium": float(price), "basis": "estimated", "source": None,
            "fallback_reason": reason, "is_broker_fill": False}


def combined_basis(*prices):
    bases = {p.get("basis", "estimated") for p in prices}
    return next(iter(bases)) if len(bases) == 1 else "mixed"


def fresh_depth_price(quote, side, now, *, symbol=None, token=None):
    """A fresh, identity-matched bid/ask mark; not a guaranteed depth fill."""
    now = at_time(now)
    if not isinstance(quote, dict) or quote.get("source") not in ("zerodha_stream", "zerodha_snapshot", "synthetic_test"):
        return None
    if symbol is not None and quote.get("symbol") != symbol or token is not None and quote.get("token") != token:
        return None
    try:
        if any(not 0 <= (now-at_time(quote[k])).total_seconds() <= 5 for k in ("timestamp", "received_at")):
            return None
        if decimal(quote["last_price"]) <= 0:
            return None
        bids, asks = quote.get("bids", []), quote.get("asks", [])
        for levels in (bids, asks):
            if any(decimal(p["price"]) <= 0 or type(p["quantity"]) is not int or p["quantity"] <= 0 for p in levels):
                return None
        if bids != sorted(bids, key=lambda p: -p["price"]) or asks != sorted(asks, key=lambda p: p["price"]) or bids and asks and bids[0]["price"] > asks[0]["price"]:
            return None
        levels = asks if side == "BUY" else bids
        if not levels:
            return None
        return {"premium": levels[0]["price"], "kind": "ask" if side == "BUY" else "bid",
                "selection_premium": float(decimal(quote["last_price"])),
                "observed_at": at_time(quote["timestamp"]).isoformat(), "source": quote["source"],
                "symbol": quote["symbol"], "available_units": levels[0]["quantity"],
                "limitation": "Top-of-book valuation, not guaranteed execution or full-quantity liquidity."}
    except (ValueError, TypeError, KeyError):
        return None


class OptionPriceBook:
    """Immutable-for-a-run observations indexed by full contract identity."""
    def __init__(self):
        self.contracts, self.candles, self.quotes = {}, {}, defaultdict(list)
        self.conflicts = set()
        self.notes = []
        self.rejected = 0
        self._quote_times = {}

    def add_candle(self, contract, row, source_id, source="kite"):
        key, at = contract_key(contract), at_time(row[0])
        prices = tuple(float(decimal(v)) for v in row[1:5])
        volume = decimal(row[5]) if len(row) > 5 else 0
        if len(prices) != 4 or at.second or at.microsecond or at.date().isoformat() > key[1]:
            raise ValueError("Invalid option candle time.")
        o, h, l, c = prices
        if min(prices) <= 0 or not l <= min(o,c) <= max(o,c) <= h or volume <= 0:
            raise ValueError("Option candle has invalid prices or no traded volume.")
        if not any(a <= at < b for a,b in session_windows(at.date())):
            raise ValueError("Option candle is outside its exchange session.")
        identity = key, at
        existing = self.candles.get(identity)
        if existing and existing["prices"] != prices:
            self.conflicts.add(identity)  # Keep ambiguity visible; do not pick a favourable revision.
        self.contracts[key] = dict(contract)
        self.candles.setdefault(identity, {"prices": prices, "source_id": source_id, "source": source})

    def add_quote(self, contract, quote):
        key = contract_key(contract)
        if quote.get("symbol") != "NFO:" + contract["symbol"]:
            raise ValueError("Archived quote contract does not match.")
        at = at_time(quote["timestamp"])
        if at.date().isoformat() > key[1]:
            raise ValueError("Expired option quote.")
        self.contracts[key] = dict(contract)
        self.quotes[key].append((at, dict(quote)))
        self._quote_times.pop(key, None)

    def lookup(self, contract, at, *, field="open", side="BUY", candle_at=None):
        if contract is None:
            return None, "Contract was not identifiable at entry; a later contract cannot be substituted"
        key, at = contract_key(contract), at_time(at)
        if at.date().isoformat() > key[1]:
            return None, "Contract has expired"
        quotes = self.quotes.get(key, [])
        if quotes:
            if key not in self._quote_times:
                quotes.sort(key=lambda pair: pair[0])
                self._quote_times[key] = [pair[0] for pair in quotes]
            index = bisect_right(self._quote_times[key], at)-1
            while index >= 0 and (at-quotes[index][0]).total_seconds() <= 5:
                quote = quotes[index][1]
                observed = fresh_depth_price(quote, side, at, symbol="NFO:"+contract["symbol"])
                if observed:
                    return observed, None
                index -= 1
        minute = at_time(candle_at) if candle_at is not None else at if field == "open" else at-timedelta(minutes=1)
        observed_at = minute if field == "open" else minute+timedelta(minutes=1)
        if observed_at != at:
            return None, "Exact trigger-second quote unavailable; a later candle close would use future information"
        identity = key, minute
        if identity in self.conflicts:
            return None, "Conflicting recorded option candle revisions"
        record = self.candles.get(identity)
        if not record:
            return None, "No matching recorded option price at this time"
        return {"premium": record["prices"][0 if field == "open" else 3],
                "kind": "minute_"+field, "observed_at": at.isoformat(), "candle_at": minute.isoformat(),
                "source": record["source"], "source_id": record["source_id"], "symbol": contract.get("symbol"),
                "limitation": "Recorded candle price proxy; spread, exact trigger second and fill availability are unknown."}, None

    def select_short(self, instrument, expiry, kind, at, spot, target=1000):
        candidates = []
        for key, contract in self.contracts.items():
            if key[:3] != (instrument, str(expiry), kind) or key[3] % 500:
                continue
            if not (key[3] <= spot if kind == "CE" else key[3] >= spot):
                continue
            price, _ = self.lookup(contract, at, side="SELL")
            if price:
                candidates.append((abs(price.get("selection_premium",price["premium"])-target), abs(key[3]-spot), key[3], contract, price))
        if not candidates:
            return None, None
        selected = min(candidates, key=lambda item: item[:3])
        return dict(selected[3]), selected[4]

    def describe(self):
        return {"policy": POLICY, "contracts": len(self.contracts), "candle_count": len(self.candles),
                "quote_count": sum(map(len,self.quotes.values())), "conflicting_minutes": len(self.conflicts),
                "rejected_rows": self.rejected, "notes": list(self.notes)}


class OptionPriceArchive:
    """Local durable observations; optional old cache is always read-only."""
    def __init__(self, path, legacy_path=None):
        self.path, self.legacy_path = Path(path), Path(legacy_path) if legacy_path else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS option_candles(id TEXT PRIMARY KEY, day TEXT NOT NULL, contract TEXT NOT NULL, candle TEXT NOT NULL, source TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS option_candle_day ON option_candles(day);
                CREATE TABLE IF NOT EXISTS option_quotes(id TEXT PRIMARY KEY, day TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS option_quote_day ON option_quotes(day);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record_quotes(self, items):
        records = []
        for contract, quote in items:
            contract_key(contract)
            if quote.get("source") != "zerodha_stream" or quote.get("symbol") != "NFO:"+contract["symbol"]:
                continue
            payload = json.dumps(quote, sort_keys=True, allow_nan=False)
            contract_json = json.dumps(contract, sort_keys=True)
            identity = hashlib.sha256((contract_json+payload).encode()).hexdigest()
            records.append((identity, at_time(quote["timestamp"]).date().isoformat(), contract_json, payload))
        if records:
            with self.db() as db:
                db.executemany("INSERT OR IGNORE INTO option_quotes VALUES (?,?,?,?)", records)

    def record_candles(self, contract, rows, source="kite"):
        book, records = OptionPriceBook(), []
        for row in rows:
            book.add_candle(contract,row,"validation",source)
            contract_json, payload = json.dumps(contract,sort_keys=True), json.dumps(row,allow_nan=False)
            identity = hashlib.sha256((contract_json+payload+source).encode()).hexdigest()
            records.append((identity,at_time(row[0]).date().isoformat(),contract_json,payload,source))
        with self.db() as db:
            db.executemany("INSERT OR IGNORE INTO option_candles VALUES (?,?,?,?,?)",records)

    def snapshot(self, first, last, check_active=lambda: None):
        first, last, book = str(first), str(last), OptionPriceBook()
        try:
            with self.db() as db:
                for identity, contract, row, source in db.execute("SELECT id,contract,candle,source FROM option_candles WHERE day BETWEEN ? AND ?",(first,last)):
                    check_active()
                    try: book.add_candle(json.loads(contract),json.loads(row),identity,source)
                    except (ValueError,TypeError,KeyError): book.rejected += 1
                for contract,quote in db.execute("SELECT contract,quote FROM option_quotes WHERE day BETWEEN ? AND ?",(first,last)):
                    try: book.add_quote(json.loads(contract),json.loads(quote))
                    except (ValueError,TypeError,KeyError): book.rejected += 1
        except (OSError,sqlite3.Error):
            book.notes.append("Local option archive could not be read; missing prices use estimates.")
        if self.legacy_path:
            try:
                with closing(sqlite3.connect(self.legacy_path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
                    batches = db.execute("SELECT id,identity,payload FROM downloads WHERE json_extract(identity,'$.underlying')='BANKNIFTY' AND json_extract(identity,'$.interval')='minute' AND json_extract(identity,'$.kind') IN ('CE','PE') AND json_extract(identity,'$.from')<=? AND json_extract(identity,'$.to')>=?",(last,first))
                    for identity, spec, payload in batches:
                        check_active()
                        spec = json.loads(spec)
                        if spec.get("source") != "kite":
                            continue
                        contract = {"instrument": spec["underlying"], "expiry": spec["expiry"], "option_type": spec["kind"],
                                    "strike": float(decimal(spec["strike_cents"])/100), "symbol": spec["symbol"]}
                        raw = json.loads(payload)
                        raw = raw["data"]["candles"] if isinstance(raw,dict) else raw
                        for row in raw:
                            try:
                                if first <= at_time(row[0]).date().isoformat() <= last:
                                    book.add_candle(contract,row,identity,"kite_cached_history")
                            except (ValueError,TypeError,KeyError,IndexError): book.rejected += 1
                book.notes.append("Includes the existing engine's saved Zerodha history, opened read-only.")
            except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
                book.notes.append("Existing-engine option cache unavailable or invalid; missing prices use estimates.")
        return book
