"""Real-quote paper acceptance lab, isolated from strategy drafts and backtests.

This exercises execution plumbing; it is not an autonomous strategy deployment.
"""
from datetime import datetime, timezone
import math
from threading import Lock
import time
from uuid import uuid4

from market_stream import MarketStream
from paper_execution import PaperExecution, stamp
from verified_calendar import VerifiedCalendar
from paper_valuation import fresh_spot
from engine import IST


class PaperRuntime:
    def __init__(self, kite, path, *, now=None, option_archive=None, strategy_observer=None):
        self.kite = kite
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calendar = VerifiedCalendar()
        self.option_archive = option_archive
        self.strategy_observer = strategy_observer
        self.strategy_observer_error = None
        self.stream = MarketStream(self.calendar, now=self.now, on_quotes=self._received_quotes)
        self.execution = PaperExecution(path, calendar=self.calendar, now=self.now)
        self.lock = Lock()
        self.contracts = []
        self.index_token = None
        self.client = None
        self.expiry = None
        self.next_expiry = None

    def set_strategy_observer(self, observer):
        """Attach the local paper strategy runner after app startup.

        The observer receives only a fresh BANKNIFTY quote and this read-only
        market adapter. It has no paper-order or broker-order capability.
        """
        self.strategy_observer = observer

    def _received_quotes(self, quotes):
        if self.option_archive:
            self._record_quotes(quotes)
        if not self.strategy_observer:
            return
        for quote in quotes:
            if quote.get("symbol") != "NSE:NIFTY BANK":
                continue
            try:
                self.strategy_observer(dict(quote), self)
                self.strategy_observer_error = None
            except Exception:
                # A strategy decision must never corrupt the price stream or
                # leak implementation details to the browser. Its own durable
                # status remains available on the matching Tradetron bridge.
                self.strategy_observer_error = "A paper strategy runner stopped safely. Review its Tradetron bridge before resuming."

    def _record_quotes(self, quotes):
        contracts = {c["instrument_token"]:c for c in self.contracts}
        items = []
        for quote in quotes:
            c = contracts.get(quote["token"])
            if c:
                spec = {"instrument":"BANKNIFTY","expiry":c["expiry"],"option_type":c["instrument_type"],
                        "strike":c["strike"],"symbol":c["tradingsymbol"]}
                items.append((spec,quote))
        self.option_archive.record_quotes(items)

    def _valuation_inputs(self):
        quotes, spot = {}, None
        if not self.valid_session():
            return quotes, spot
        for c in self.contracts:
            try: quotes["NFO:"+c["tradingsymbol"]] = self.stream.quote(c["instrument_token"])
            except ValueError: pass
        try: spot = self.stream.quote(self.index_token)
        except ValueError: pass
        return quotes, spot

    def valid_session(self):
        try:
            return self.client is not None and self.kite._client() is self.client
        except ValueError:
            return False

    def start(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError("A paper check is running. Wait for its result.")
        try:
            if not self.calendar.is_open(self.now()):
                raise ValueError("The verified exchange session is closed.")
            client = self.kite._client()
            if not self.kite.instrument_status()["available"]:
                self.kite.sync_instruments()
            spot = self.kite.spot()
            if not -1 <= (self.now() - stamp(spot["timestamp"])).total_seconds() <= 5:
                raise ValueError("Zerodha returned a stale spot snapshot; stream selection blocked.")
            with self.kite.lock:
                if self.kite.client is not client:
                    raise ValueError("Zerodha session changed. Start again.")
                contracts = [dict(row) for row in self.kite.instruments]
                token = self.kite.index_token
            expiry = self.calendar.select_expiry(self.now(), contracts)
            monthly = self._monthly_expiries(self.now(), contracts)
            next_expiry = next((value for value in monthly if value > expiry), None)
            subscribed_expiries = {expiry, next_expiry} - {None}
            contracts = [c for c in contracts if c["expiry"] in subscribed_expiries and c["strike"] % 500 == 0]
            if not contracts or len(contracts) >= 3000 or len({c["instrument_token"] for c in contracts}) != len(contracts):
                raise ValueError("No unique, supported monthly contract list is available.")
            self.client, self.index_token, self.contracts, self.expiry, self.next_expiry = client, token, contracts, expiry, next_expiry
            subscriptions = {c["instrument_token"]: {"symbol": "NFO:" + c["tradingsymbol"]} for c in contracts}
            subscriptions[token] = {"symbol": "NSE:NIFTY BANK"}
            return self.stream.start(client, subscriptions, self.valid_session)
        finally:
            self.lock.release()

    def _monthly_expiries(self, at, contracts):
        """Return listed, verified monthly expiries without inferring a date."""
        values = []
        for raw in sorted({c.get("expiry") for c in contracts if isinstance(c.get("expiry"), str)}):
            try:
                expiry = datetime.fromisoformat(raw).date()
                if expiry == self.calendar.expiry(expiry.year, expiry.month):
                    values.append(raw)
            except (ValueError, TypeError):
                continue
        if not values:
            raise ValueError("No listed verified BANKNIFTY monthly expiry is available.")
        # Validate the current date separately, so unsupported calendar years
        # remain blocked rather than selected from a broker instrument list.
        self.calendar.windows(at.astimezone(IST).date())
        return values

    def _contracts_for_expiry(self, expiry):
        return [c for c in self.contracts if c.get("expiry") == expiry]

    def _expiry_offset(self, at, expiry):
        current = self.calendar.select_expiry(at, self.contracts)
        if expiry == current:
            return 0
        monthly = self._monthly_expiries(at, self.contracts)
        if next((value for value in monthly if value > current), None) == expiry:
            return 1
        raise ValueError("The selected option expiry is not the current or next verified month.")

    def _fresh_contract_quotes(self, expiry):
        available = []
        for contract in self._contracts_for_expiry(expiry):
            try:
                quote = self.stream.quote(contract["instrument_token"])
                if quote.get("bids") and quote.get("asks"):
                    available.append((contract, quote))
            except ValueError:
                pass
        return available

    @staticmethod
    def _choose(candidates, target, spot):
        if not candidates:
            raise ValueError("No fresh two-sided quote is available for the required BANKNIFTY option.")
        return min(candidates, key=lambda pair: (abs(pair[1]["last_price"] - target), abs(pair[0]["strike"] - spot)))

    def _selection(self):
        if not self.valid_session():
            raise ValueError("Sign in and start a new stream before testing.")
        if self.calendar.select_expiry(self.now(), self.contracts) != self.expiry:
            raise ValueError("Contract selection has expired. Restart the stream.")
        spot = self.stream.quote(self.index_token)["last_price"]
        premium = math.floor(spot / 10000) * 100
        if premium <= 0:
            raise ValueError("Invalid hedge premium target.")
        available = self._fresh_contract_quotes(self.expiry)
        def choose(kind, hedge, exclude=None):
            candidates = [(c, q) for c, q in available if c["instrument_type"] == kind and c["tradingsymbol"] != exclude
                          and ((c["strike"] >= spot if kind == "CE" else c["strike"] <= spot) if hedge
                               else (c["strike"] <= spot if kind == "CE" else c["strike"] >= spot))]
            if not candidates:
                raise ValueError("No fresh two-sided quote for each required ATM/ITM short and OTM hedge. Wait for prices.")
            return min(candidates, key=lambda pair: (abs(pair[1]["last_price"] - (premium if hedge else 1000)), abs(pair[0]["strike"] - spot)))
        hedges = {k: choose(k, True) for k in ("CE", "PE")}
        return {"spot": spot, "hedge_target_premium": premium, "replacement_distance": 2 * premium,
                "shorts": {k: choose(k, False) for k in ("CE", "PE")}, "hedges": hedges,
                "replacements": {k: choose(k, True, hedges[k][0]["tradingsymbol"]) for k in ("CE", "PE")},
                "fresh_candidates": len(available), "listed_candidates": len(self.contracts),
                "selection_note": "Closest LTP among fresh two-sided quotes at 500-point strikes only. Unquoted/stale contracts are excluded; this is not proof of the closest premium across the entire chain."}

    def strategy_runner_ready(self):
        """Fail closed before an automatic paper signal can be armed."""
        if not self.valid_session():
            raise ValueError("Sign in at Zerodha and start a new price stream before starting automatic paper signals.")
        if not self.calendar.is_open(self.now()):
            raise ValueError("Automatic paper signals can start only during a verified open exchange session.")
        self.stream.quote(self.index_token)
        if self.calendar.select_expiry(self.now(), self.contracts) != self.expiry:
            raise ValueError("The current monthly contract selection changed. Restart the price stream before starting automatic paper signals.")

    def strategy_entry_selection(self, direction, at):
        """Choose exact one-lot current-month legs for a portal entry."""
        if direction not in ("CE", "PE"):
            raise ValueError("Direction must be CE or PE.")
        self.strategy_runner_ready()
        expiry = self.calendar.select_expiry(at, self.contracts)
        spot = self.stream.quote(self.index_token)["last_price"]
        hedge_premium = math.floor(spot / 10000) * 100
        if hedge_premium <= 0:
            raise ValueError("The hedge premium target is invalid.")
        available = [(c, q) for c, q in self._fresh_contract_quotes(expiry) if c.get("instrument_type") == direction]
        short_candidates = [(c, q) for c, q in available if (c["strike"] <= spot if direction == "CE" else c["strike"] >= spot)]
        short, _quote = self._choose(short_candidates, 1000, spot)
        hedge_candidates = [(c, q) for c, q in available
                            if (c["strike"] > short["strike"] if direction == "CE" else c["strike"] < short["strike"])]
        hedge, _hedge_quote = self._choose(hedge_candidates, hedge_premium, spot)
        if short.get("lot_size") != hedge.get("lot_size") or short.get("lot_size") != 30:
            raise ValueError("The listed BANKNIFTY lot size is not the one-lot paper executor size. Do not send a signal.")
        return {"spot": float(spot), "expiry": expiry, "expiry_offset": self._expiry_offset(at, expiry), "quantity": 30,
                "short_symbol": short["tradingsymbol"], "short_strike": int(short["strike"]),
                "hedge_symbol": hedge["tradingsymbol"], "hedge_strike": int(hedge["strike"]),
                "hedge_trigger_spot": float(spot + 2 * hedge_premium if direction == "CE" else spot - 2 * hedge_premium)}

    def strategy_replacement_selection(self, direction, at, position, *, rollover):
        """Select a different hedge; scheduled rolls use the next listed month."""
        if direction not in ("CE", "PE") or not isinstance(position, dict):
            raise ValueError("The saved hedge position is invalid. Review Tradetron before replacing it.")
        self.strategy_runner_ready()
        old_expiry = position.get("hedge_expiry")
        if not isinstance(old_expiry, str):
            raise ValueError("The saved hedge has no expiry. Review Tradetron before replacing it.")
        expiry = old_expiry
        if rollover:
            expiry = self.next_expiry
            if not expiry:
                raise ValueError("The next verified monthly option expiry is not subscribed. Restart prices before rollover.")
        spot = self.stream.quote(self.index_token)["last_price"]
        hedge_premium = math.floor(spot / 10000) * 100
        if hedge_premium <= 0:
            raise ValueError("The hedge premium target is invalid.")
        short_strike = position.get("short_strike")
        if not isinstance(short_strike, int):
            raise ValueError("The saved short strike is invalid. Review Tradetron before replacing a hedge.")
        available = [(c, q) for c, q in self._fresh_contract_quotes(expiry)
                     if c.get("instrument_type") == direction and c.get("tradingsymbol") != position.get("hedge_symbol")]
        candidates = [(c, q) for c, q in available
                      if (c["strike"] > max(short_strike, spot) if direction == "CE" else c["strike"] < min(short_strike, spot))]
        hedge, _quote = self._choose(candidates, hedge_premium, spot)
        if hedge.get("lot_size") != 30:
            raise ValueError("The listed BANKNIFTY lot size is not the one-lot paper executor size. Do not send a signal.")
        return {"spot": float(spot), "expiry": expiry, "expiry_offset": self._expiry_offset(at, expiry),
                "hedge_symbol": hedge["tradingsymbol"], "hedge_strike": int(hedge["strike"]),
                "hedge_trigger_spot": float(spot + 2 * hedge_premium if direction == "CE" else spot - 2 * hedge_premium)}

    def _fill(self, strategy, label, contract, side, *, close=False, depends=None, quantity=None):
        intent = {"strategy": strategy, "symbol": "NFO:" + contract["tradingsymbol"], "side": side,
                  "quantity": contract["lot_size"] if quantity is None else quantity, "lot_size": contract["lot_size"], "token": contract["instrument_token"],
                  "expiry": contract["expiry"], "close_only": close, "depends_on": depends}
        order = self.execution.submit(strategy + ":" + label, intent)
        # One initial attempt plus three retries. Never resubmit a new identity.
        for attempt in range(4):
            if attempt:
                time.sleep(1)
            try:
                if not self.valid_session():
                    raise ValueError("Session ended")
                quote = self.stream.quote(contract["instrument_token"])
                context = None
                try:
                    spot = fresh_spot(self.stream.quote(self.index_token),self.now())
                    if spot is not None:
                        context = {"option_type":contract["instrument_type"],"entry_spot":float(spot),
                                   "daily_decay":20 if "short" in label else 30}
                except ValueError:
                    pass
                order = self.execution.match(order["id"], quote, healthy=True,valuation_context=context)
            except (ValueError, KeyError):
                pass
            if order["status"] == "COMPLETE":
                return order
        self.execution.cancel(order["id"])
        raise ValueError("A paper leg did not fully fill after three retries. Unfilled remainder cancelled; retained positions are shown in the ledger.")

    def check(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError("A paper check is already running.")
        strategy = "acceptance-" + uuid4().hex
        result = {"status": "blocked", "strategy": strategy, "source": "zerodha_stream",
                  "scope": "One-lot paper execution acceptance, not multi-session hourly strategy validation.",
                  "limitations": ["Depth-based simulated fills, not actual option executions.", "No exchange queue, real margin, slippage guarantee or autonomous strategy deployment."]}
        try:
            if any(p["quantity"] for p in self.execution.status()["positions"]):
                raise ValueError("Close retained paper test positions before starting another acceptance test.")
            selection = self._selection()
            # Snapshot evidence and selected lot sizes/expiries are retained.
            result["selection"] = selection
            if len({c["lot_size"] for group in ("shorts", "hedges", "replacements") for c, _ in selection[group].values()}) != 1:
                raise ValueError("Selected contracts have inconsistent lot sizes.")
            if len({c["tradingsymbol"] for group in ("shorts", "hedges", "replacements") for c, _ in selection[group].values()}) != 6:
                raise ValueError("Test requires six distinct legs to keep hedge and short accounting separate.")
            for kind in ("CE", "PE"):
                hedge = selection["hedges"][kind][0]
                short = selection["shorts"][kind][0]
                new = selection["replacements"][kind][0]
                self._fill(strategy, kind + "-hedge", hedge, "BUY")
                self._fill(strategy, kind + "-short", short, "SELL")
                buy = self._fill(strategy, kind + "-replacement-buy", new, "BUY")
                self._fill(strategy, kind + "-old-hedge-close", hedge, "SELL", close=True, depends=buy["id"])
                self._fill(strategy, kind + "-short-close", short, "BUY", close=True)
                self._fill(strategy, kind + "-new-hedge-close", new, "SELL", close=True)
            result["status"], result["message"] = "passed", "Both CE and PE round trips and buy-before-sell replacements completed on real quotes."
        except ValueError as exc:
            result["message"] = str(exc)
        except Exception:
            result["message"] = "Paper check stopped safely. Inspect retained paper positions before retrying."
        finally:
            try:
                quotes, spot = self._valuation_inputs()
                result["evidence"] = self.execution.evidence(strategy,quotes=quotes,spot_quote=spot)
                sources = {e["detail"].get("source") for e in result["evidence"]["events"] if e["event"] == "QUOTE_EVIDENCE"}
                if result["status"] == "passed" and sources != {"zerodha_stream"}:
                    result["status"], result["source"] = "synthetic_passed", "synthetic_test"
                    result["message"] = "Synthetic software exercise passed. This is not a real-quote paper acceptance."
                if any(p["quantity"] for p in result["evidence"]["positions"]):
                    self.execution.pause()
                    result["status"] = "incomplete"
                    result["message"] += " Paper execution paused with open test positions; no price was invented to close them."
                saved = self.execution.record_check(result)
            finally:
                self.lock.release()
        return saved

    def close_retained(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Wait until the current paper check stops.")
        try:
            if not self.valid_session() or not self.calendar.is_open(self.now()):
                raise ValueError("A current stream and open exchange session are required to close paper positions.")
            positions = [p for p in self.execution.status()["positions"] if p["quantity"]]
            contracts = {"NFO:"+c["tradingsymbol"]: c for c in self.contracts}
            # Do not resume if even one retained contract lacks fresh data.
            for position in positions:
                if position["symbol"] not in contracts:
                    raise ValueError("A retained contract is outside the current stream. Do not invent its closing price.")
                self.stream.quote(contracts[position["symbol"]]["instrument_token"])
            self.execution.reconcile_and_resume()
            try:
                for position in positions:
                    self._fill(position["strategy"], "recovery-"+uuid4().hex, contracts[position["symbol"]],
                               "SELL" if position["quantity"] > 0 else "BUY", close=True, quantity=abs(position["quantity"]))
            finally:
                self.execution.pause()  # Closing exposure is not permission to start new tests.
            return self.status()
        finally:
            self.lock.release()

    def status(self):
        ledger = self.execution.status()
        desk = self.execution.desk_records()
        for contract in self.contracts:
            desk["contracts"]["NFO:" + contract["tradingsymbol"]] = {k: contract.get(k) for k in ("strike", "instrument_type", "expiry", "lot_size")}
        quotes, spot = self._valuation_inputs()
        # Full quote/order evidence has a separate on-demand endpoint, not every poll.
        checks = [{k: c[k] for k in ("id", "created_at", "status", "message", "scope") if k in c} for c in ledger["checks"]]
        return {"stream": self.stream.status(), "calendar": self.calendar.status(self.now()),
                "paper": {k: v for k, v in ledger.items() if k not in ("checks", "orders")}, "checks": checks,
                "busy": self.lock.locked(), "expiry": self.expiry,
                "desk": desk,
                "valuation": self.execution.valuation(quotes=quotes,spot_quote=spot),
                "strategy_runner_error": self.strategy_observer_error,
                "readiness": [
                    {"label": "Order execution service", "status": "Paper service built · live adapter disabled"},
                    {"label": "Streaming market data", "status": self.stream.status()["state"]},
                    {"label": "Verified exchange calendar", "status": "2026 schedule reviewed · unknown dates blocked"},
                    {"label": "Real-quote paper tests", "status": "Acceptance passed" if any(c["status"] == "passed" for c in checks) else "Awaiting market-hours acceptance"},
                    {"label": "Live approval", "status": "Not approved · no broker orders"}]}
