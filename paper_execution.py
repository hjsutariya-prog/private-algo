"""Persistent, paper-only execution. No broker client, credentials or network calls.

Market depth is consumed once per quote/side across all local strategies.
This estimates liquidity; it does not promise that exchange orders would fill.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from engine import IST

ACTIVE = {"OPEN", "PARTIAL", "WAITING", "UNKNOWN"}
TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED"}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def stamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required.")
    return result


class PaperExecution:
    max_units = 30000  # Software safety ceiling, NOT broker margin.

    def __init__(self, path, *, now=None, calendar=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calendar = calendar
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_orders(id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0, value REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, reason TEXT);
                CREATE TABLE IF NOT EXISTS paper_fills(id TEXT PRIMARY KEY, order_id TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL, at TEXT NOT NULL, quote_id TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_events(id INTEGER PRIMARY KEY, at TEXT NOT NULL, order_id TEXT, event TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_depth(quote_id TEXT NOT NULL, side TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(quote_id,side));
                CREATE TABLE IF NOT EXISTS paper_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_checks(id TEXT PRIMARY KEY, created TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_fill_context(fill_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)
        # After restart, orders must not resume on old quotes or silently re-submit.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT COUNT(*) FROM paper_orders WHERE status IN ('OPEN','PARTIAL','WAITING','UNKNOWN')").fetchone()[0]
            open_positions = sum(p["quantity"] != 0 for p in self._positions(db))
            if pending or open_positions:
                db.execute("UPDATE paper_orders SET status='UNKNOWN',reason='Restart reconciliation required' WHERE status IN ('OPEN','PARTIAL','WAITING')")
                self._meta(db, "paused", "true")
                self._event(db, None, "RESTART_PAUSE", {"pending_orders": pending, "open_positions": open_positions})
            db.execute("INSERT OR IGNORE INTO paper_meta VALUES ('paused','false')")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _event(self, db, order, event, detail):
        db.execute("INSERT INTO paper_events(at,order_id,event,detail) VALUES (?,?,?,?)", (self.now().isoformat(), order, event, encoded(detail)))

    @staticmethod
    def _meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO paper_meta VALUES (?,?)", (key, value))

    @staticmethod
    def _paused(db):
        row = db.execute("SELECT value FROM paper_meta WHERE key='paused'").fetchone()
        return bool(row and row[0] == "true")

    @staticmethod
    def _view(row):
        if row is None:
            raise ValueError("Unknown paper order.")
        result = dict(row)
        result["intent"] = json.loads(result["intent"])
        result["average_price"] = round(result["value"] / result["filled"], 4) if result["filled"] else None
        result["mode"] = "paper"
        return result

    def order(self, identity):
        with self.db() as db:
            return self._view(db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone())

    def submit(self, key, intent):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key):
            raise ValueError("A stable, unique paper-order key is required.")
        required = {"strategy", "symbol", "side", "quantity", "lot_size", "token", "expiry", "close_only", "depends_on"}
        if not isinstance(intent, dict) or set(intent) != required:
            raise ValueError("Incomplete paper-order intent.")
        if not isinstance(intent["strategy"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", intent["strategy"]):
            raise ValueError("Invalid strategy reference.")
        if not isinstance(intent["symbol"], str) or not re.fullmatch(r"NFO:BANKNIFTY[A-Z0-9]+", intent["symbol"]):
            raise ValueError("Only exact BANKNIFTY option contracts are supported.")
        if intent["side"] not in {"BUY", "SELL"} or type(intent["close_only"]) is not bool:
            raise ValueError("Invalid paper-order side or close setting.")
        if any(type(intent[k]) is not int or intent[k] <= 0 for k in ("quantity", "lot_size", "token")):
            raise ValueError("Quantity, lot size and token must be positive integers.")
        if (not intent["close_only"] and intent["quantity"] % intent["lot_size"]) or intent["quantity"] > self.max_units:
            raise ValueError("Quantity must be whole lots and within the account-wide software limit.")
        from datetime import date
        expiry = date.fromisoformat(intent["expiry"])
        if expiry < self.now().astimezone(IST).date():
            raise ValueError("Cannot submit an expired contract.")
        if intent["depends_on"] is not None and not isinstance(intent["depends_on"], str):
            raise ValueError("Invalid dependency reference.")
        payload = encoded(intent)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM paper_orders WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["intent"] != payload:
                    raise ValueError("Idempotency key already belongs to a different order.")
                return self._view(existing)
            if self._paused(db):
                raise ValueError("Paper execution is paused; reconcile before resuming.")
            if self.calendar and not self.calendar.is_open(self.now()):
                raise ValueError("The verified exchange session is closed.")
            dependency = intent["depends_on"]
            if dependency:
                parent = db.execute("SELECT * FROM paper_orders WHERE id=?", (dependency,)).fetchone()
                if not parent:
                    raise ValueError("Unknown replacement buy; retain the old hedge.")
                parent_intent = json.loads(parent["intent"])
                if (parent_intent["strategy"] != intent["strategy"] or parent_intent["side"] != "BUY"
                        or intent["side"] != "SELL" or not intent["close_only"] or parent_intent["close_only"]
                        or parent_intent["symbol"] == intent["symbol"] or parent_intent["quantity"] < intent["quantity"]):
                    raise ValueError("Replacement dependency must be a matching full hedge buy.")
            positions = self._positions(db)
            outstanding = [self._view(r) for r in db.execute("SELECT * FROM paper_orders WHERE status IN ('OPEN','PARTIAL','WAITING','UNKNOWN')")]
            if not intent["close_only"] and sum(abs(p["quantity"]) for p in positions) + sum(o["intent"]["quantity"] - o["filled"] for o in outstanding) + intent["quantity"] > self.max_units:
                raise ValueError("Account-wide paper quantity limit reached.")
            if intent["close_only"]:
                held = sum(p["quantity"] for p in positions if p["strategy"] == intent["strategy"] and p["symbol"] == intent["symbol"])
                reserved = sum(o["intent"]["quantity"] - o["filled"] for o in outstanding if o["intent"]["strategy"] == intent["strategy"] and o["intent"]["symbol"] == intent["symbol"] and o["intent"]["close_only"] and o["intent"]["side"] == intent["side"])
                if (held if intent["side"] == "SELL" else -held) - reserved < intent["quantity"]:
                    raise ValueError("Insufficient unreserved paper position to close.")
            identity = uuid4().hex
            db.execute("INSERT INTO paper_orders(id,key,intent,status,created) VALUES (?,?,?,?,?)", (identity, key, payload, "WAITING" if dependency else "OPEN", self.now().isoformat()))
            self._event(db, identity, "SUBMITTED", {"mode": "paper", "key": key})
            return self._view(db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone())

    def match(self, identity, quote, *, healthy, valuation_context=None):
        now = self.now()
        if valuation_context is not None:
            if (set(valuation_context) != {"option_type", "entry_spot", "daily_decay"}
                    or valuation_context["option_type"] not in ("CE", "PE")
                    or not positive(valuation_context["entry_spot"]) or not positive(valuation_context["daily_decay"])):
                raise ValueError("Invalid paper fill valuation reference.")
        if not healthy or (self.calendar and not self.calendar.is_open(now)):
            raise ValueError("Fresh connected market data in an open verified session is required.")
        if not isinstance(quote, dict) or quote.get("source") not in {"zerodha_stream", "zerodha_snapshot", "synthetic_test"}:
            raise ValueError("A validated, source-labelled quote is required.")
        for key in ("timestamp", "received_at"):
            age = (now - stamp(quote[key])).total_seconds()
            if not -1 <= age <= 5:
                raise ValueError("Stale or future quote blocked; no paper fill created.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self._view(db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone())
            if order["status"] in TERMINAL:
                return order
            if self._paused(db) or order["status"] == "UNKNOWN":
                raise ValueError("Paper execution is paused or needs reconciliation.")
            intent = order["intent"]
            if stamp(order["created"]).astimezone(IST).date() != now.astimezone(IST).date():
                db.execute("UPDATE paper_orders SET status='CANCELLED',reason='Paper day order expired' WHERE id=?", (identity,))
                self._event(db, identity, "EXPIRED", {"filled_quantity_kept": order["filled"]})
                return self._view(db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone())
            if quote.get("symbol") != intent["symbol"] or quote.get("token") != intent["token"]:
                raise ValueError("Quote does not match the exact requested instrument.")
            if stamp(quote["received_at"]) < stamp(order["created"]):
                raise ValueError("A new quote received after submission is required.")
            if intent["depends_on"]:
                dependency = db.execute("SELECT status FROM paper_orders WHERE id=?", (intent["depends_on"],)).fetchone()
                if not dependency or dependency[0] != "COMPLETE":
                    return order  # Never sell the old hedge before confirmed full paper buy.
            bids, asks = quote.get("bids", []), quote.get("asks", [])
            for side in (bids, asks):
                if not isinstance(side, list) or len(side) > 5 or any(not isinstance(level, dict) or not positive(level.get("price")) or type(level.get("quantity")) is not int or level["quantity"] <= 0 for level in side):
                    raise ValueError("Invalid order-book depth.")
            if bids != sorted(bids, key=lambda x: -x["price"]) or asks != sorted(asks, key=lambda x: x["price"]) or (bids and asks and bids[0]["price"] > asks[0]["price"]):
                raise ValueError("Crossed or unordered market depth blocked.")
            levels = asks if intent["side"] == "BUY" else bids
            quote_id = hashlib.sha256(encoded({k: quote.get(k) for k in ("symbol", "token", "timestamp", "bids", "asks", "source")}).encode()).hexdigest()
            self._event(db, identity, "QUOTE_EVIDENCE", dict(quote, quote_id=quote_id))
            budget = db.execute("SELECT used FROM paper_depth WHERE quote_id=? AND side=?", (quote_id, intent["side"])).fetchone()
            used = budget[0] if budget else 0
            remaining = intent["quantity"] - order["filled"]
            consumed, value, skipped = 0, 0.0, used
            for level in levels:
                available = max(0, level["quantity"] - skipped)
                skipped = max(0, skipped - level["quantity"])
                amount = min(remaining - consumed, available)
                if amount:
                    price = level["price"]
                    fill_id = uuid4().hex
                    db.execute("INSERT INTO paper_fills VALUES (?,?,?,?,?,?,?,?,?,?)", (fill_id, identity, intent["strategy"], intent["symbol"], intent["side"], amount, price, now.isoformat(), quote_id, quote["source"]))
                    if valuation_context is not None:
                        db.execute("INSERT INTO paper_fill_context VALUES (?,?)",(fill_id,encoded(valuation_context)))
                    consumed += amount
                    value += amount * price
                if consumed == remaining:
                    break
            if not consumed:
                return order  # Repeated/no-depth snapshots are not fills or submission retries.
            filled = order["filled"] + consumed
            status = "COMPLETE" if filled == intent["quantity"] else "PARTIAL"
            db.execute("INSERT OR REPLACE INTO paper_depth VALUES (?,?,?)", (quote_id, intent["side"], used + consumed))
            db.execute("UPDATE paper_orders SET status=?,filled=?,value=value+?,attempts=attempts+1,reason=NULL WHERE id=?", (status, filled, value, identity))
            self._event(db, identity, status, {"quantity": consumed, "quote_id": quote_id, "source": quote["source"]})
            return self._view(db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone())

    def cancel(self, identity):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone()
            order = self._view(row)
            if order["status"] == "UNKNOWN":
                raise ValueError("Reconcile unknown orders before cancelling.")
            if order["status"] not in TERMINAL:
                db.execute("UPDATE paper_orders SET status='CANCELLED',reason='Unfilled remainder cancelled' WHERE id=?", (identity,))
                self._event(db, identity, "CANCELLED", {"filled_quantity_kept": order["filled"]})
        return self.order(identity)

    def pause(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._meta(db, "paused", "true")
            self._event(db, None, "PAUSED", {"positions_preserved": True})

    def reject(self, identity, reason="Paper test rejected"):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self._view(db.execute("SELECT * FROM paper_orders WHERE id=?", (identity,)).fetchone())
            if order["filled"] or order["status"] == "UNKNOWN":
                raise ValueError("Filled or uncertain orders cannot be rejected; cancel or reconcile instead.")
            if order["status"] not in TERMINAL:
                db.execute("UPDATE paper_orders SET status='REJECTED',reason=? WHERE id=?", (reason[:200], identity))
                self._event(db, identity, "REJECTED", {"reason": reason[:200]})
        return self.order(identity)

    def reconcile_and_resume(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM paper_orders").fetchall():
                total = db.execute("SELECT COALESCE(SUM(quantity),0),COALESCE(SUM(quantity*price),0) FROM paper_fills WHERE order_id=?", (row["id"],)).fetchone()
                if total[0] != row["filled"] or abs(total[1] - row["value"]) > .0001:
                    raise ValueError("Ledger mismatch. Remain paused; manual repair is required.")
            # Paper-only ledger is the authority. Never retry an interrupted remainder.
            db.execute("UPDATE paper_orders SET status='CANCELLED',reason='Restart: unfilled remainder cancelled' WHERE status='UNKNOWN'")
            self._meta(db, "paused", "false")
            self._event(db, None, "RECONCILED", {"broker_orders": 0})

    @staticmethod
    def _positions(db):
        return [dict(row) for row in db.execute("SELECT strategy,symbol,SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS quantity,SUM(CASE WHEN side='SELL' THEN quantity*price ELSE -quantity*price END) AS cashflow FROM paper_fills GROUP BY strategy,symbol")]

    def evidence(self, strategy, *, quotes=None, spot_quote=None):
        from backtest_fees import FeeSchedule
        with self.db() as db:
            orders = [self._view(row) for row in db.execute("SELECT * FROM paper_orders ORDER BY rowid") if json.loads(row["intent"])["strategy"] == strategy]
            fees = []
            for order in orders:
                if order["filled"]:
                    fees.append(FeeSchedule().calculate(stamp(order["created"]).astimezone(IST), order["intent"]["side"], order["value"] / order["filled"], order["filled"]))
            ids = {o["id"] for o in orders}
            events = [dict(row, detail=json.loads(row["detail"])) for row in db.execute("SELECT * FROM paper_events ORDER BY id") if row["order_id"] in ids]
            positions = [p for p in self._positions(db) if p["strategy"] == strategy]
            gross = sum(p["cashflow"] for p in positions) if all(p["quantity"] == 0 for p in positions) else None
            charges = round(sum(f["total"] for f in fees), 2)
            return {"orders": orders, "positions": positions, "events": events, "estimated_fees": fees,
                    "estimated_charges": charges, "closed_gross": gross, "closed_net_estimate": round(gross - charges, 2) if gross is not None else None,
                    "open_position_estimate": None, "combined_result": None,
                    "valuation_note": "Legacy totals require every leg flat; the separately labelled valuation splits closed profit from open marks.",
                    "valuation": self.valuation(quotes=quotes,spot_quote=spot_quote,strategy=strategy)}

    def valuation(self, *, quotes=None, spot_quote=None, strategy=None):
        from paper_valuation import value_paper_fills
        with self.db() as db:
            orders = [self._view(r) for r in db.execute("SELECT * FROM paper_orders ORDER BY rowid")]
            fills = [dict(r) for r in db.execute("SELECT * FROM paper_fills ORDER BY rowid") if strategy is None or r["strategy"]==strategy]
            contexts = {r["fill_id"]:json.loads(r["payload"]) for r in db.execute("SELECT * FROM paper_fill_context")}
        return value_paper_fills(fills,orders,contexts,quotes or {},spot_quote,self.now())

    def record_check(self, result):
        result = dict(result, id=uuid4().hex, created_at=self.now().isoformat(), mode="paper", actual_broker_orders=0)
        with self.db() as db:
            db.execute("INSERT INTO paper_checks VALUES (?,?,?)", (result["id"], result["created_at"], encoded(result)))
        return result

    def status(self):
        with self.db() as db:
            orders = [self._view(row) for row in db.execute("SELECT * FROM paper_orders ORDER BY rowid DESC LIMIT 30")]
            checks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM paper_checks ORDER BY rowid DESC LIMIT 10")]
            positions = self._positions(db)
            return {"mode": "paper", "live_enabled": False, "paused": self._paused(db), "orders": orders,
                    "positions": positions, "checks": checks, "fill_count": db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
                    "limitation": "Simulated fills at available bid/ask depth. No queue priority, latency or real margin guarantee. Partial fills may be smaller than a lot."}

    def desk_records(self):
        """Read-only evidence. Never infer broker positions or submit recovery orders.

        Retain every outstanding order and every order for open groups, even if
        older than the recent-history window. Read one SQLite snapshot.
        """
        with self.db() as db:
            db.execute("BEGIN")
            positions = self._positions(db)
            active = {p["strategy"] for p in positions if p["quantity"]}
            all_orders = [self._view(r) for r in db.execute("SELECT * FROM paper_orders ORDER BY rowid DESC")]
            recent = {o["id"] for o in all_orders[:30]}
            orders = [o for o in all_orders if o["id"] in recent or o["status"] in ACTIVE or o["intent"]["strategy"] in active]
            ids = {o["id"] for o in orders}
            fills = [dict(r) for r in db.execute("SELECT * FROM paper_fills ORDER BY rowid") if r["order_id"] in ids]
            events = [dict(r, detail=json.loads(r["detail"])) for r in db.execute("SELECT * FROM paper_events ORDER BY id DESC LIMIT 100")]
            contracts = {}
            for row in db.execute("SELECT payload FROM paper_checks ORDER BY rowid"):
                for group in json.loads(row[0]).get("selection", {}).values():
                    if isinstance(group, dict):
                        for pair in group.values():
                            if isinstance(pair, list) and pair and isinstance(pair[0], dict):
                                c = pair[0]
                                if c.get("tradingsymbol"):
                                    contracts["NFO:" + c["tradingsymbol"]] = {k: c.get(k) for k in ("strike", "instrument_type", "expiry", "lot_size")}
            return {"as_of": self.now().isoformat(), "positions": positions, "orders": orders,
                    "fills": fills, "events": events, "contracts": contracts,
                    "history_scope": "Latest 100 events and 30 orders, plus all orders for open paper groups and all outstanding orders."}
