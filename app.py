"""Loopback-only local app. Kite access is limited to market-data reads."""
import argparse
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import secrets
import socket
from threading import Event, RLock, Thread
from urllib.parse import parse_qs, urlsplit

from kite_client import KiteAuthError, KiteClient, KiteError, KiteNetworkError, KiteRateLimitError
from range_spot import earliest_replay_date, history_windows, replay_spot_range
from replay import ReplayDataError, replay_signals
from scenarios import CATALOG, run_scenario
from spot_proxy import SpotProxyDataError, build_spot_proxy_ledger, validate_spot_proxy_day
from store import Store
from backtest_config import model_config
from strategy_config import COMPARISON, REVIEW, CLOSE_BOUNDARY, CURRENT_MODES, strategy_config
from option_model import simulate_options
from option_prices import OptionPriceArchive
from historical_options import collect_historical_option_prices
from market_sessions import session_windows, session_minutes, latest_completed_session, download_bounds
from paper_runtime import PaperRuntime
from paper_replay import PaperReplay, request_settings
from credential_store import CredentialStore, CredentialStoreError
from tradetron_delivery import TradetronDeliveryError, dispatch_snapshot_entries
from tradetron_link import TradetronLinkState, TradetronLinkStore
from tradetron_runtime import TradetronSignalRuntime
from deployment_runtime import DeploymentRuntimeRegistry

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/trading-desk.js": ("trading-desk.js", "text/javascript; charset=utf-8"),
          "/static/styles.css": ("styles.css", "text/css; charset=utf-8")}
IST = timezone(timedelta(hours=5, minutes=30))
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{6,128}\Z")
VERIFIED_CLOSED_DATES = {
    # Independently verified label for the spot report. Other whole-empty days
    # now follow the user's explicitly labelled assumed-holiday policy.
    date(2026, 9, 14): "Ganesh Chaturthi",
}
HOLIDAY_SOURCE = "https://www.nseindia.com/resources/exchange-communication-holidays?apiversion=v2"
# This is deliberately not a general order or signal route.  It exists only
# for the user-approved, one-time Live Offline plumbing check against the
# newly imported private Tradetron strategy.
PAPER_BOOTSTRAP_STRATEGY_ID = "999056854"
PAPER_BOOTSTRAP_CONFIRMATION = "paper-bootstrap-four-legs"


class ReplayCancelled(Exception):
    pass


def spot_proxy_for_range(candles, checked):
    """Apply the requested missing-price policy; stop only at blocking errors."""
    by_day = {}
    for row in candles:
        day = datetime.fromisoformat(row["timestamp_ist"]).astimezone(IST).date()
        by_day.setdefault(day, []).append(row)
    sessions, ledger = [], None
    blocked = None
    observed = datetime.fromisoformat(checked["observed_until"])
    closed_dates = set(VERIFIED_CLOSED_DATES) | {date.fromisoformat(day["date"]) for day in checked["days"]
                                                if (day.get("assumed_holiday") or day.get("verified_closed_date")) and day["received_candle_count"] == 0}
    ignore_missing_minutes = checked.get("missing_minute_policy") == "skip"
    for day in checked["days"]:
        chosen = date.fromisoformat(day["date"])
        calendar_mode = checked.get("complete_hours_only") is True
        if (not session_windows(chosen) if calendar_mode else chosen.weekday() >= 5) or chosen in closed_dates:
            if day["received_candle_count"]:
                blocked = {"date": day["date"], "reason": "Prices were returned for a closed or special-session date. Its session hours need checking."}
                break
            continue
        if day["status"] != "complete" and not (ignore_missing_minutes and day.get("missing_minutes_ignored")):
            blocked = {"date": day["date"], "reason": day.get("reason", "This day has incomplete spot prices."),
                       "missing_times": day.get("missing_times", []),
                       "duplicate_times": day.get("duplicate_times", [])}
            break
        day_input = {"candles": by_day.get(chosen, []),
                     "replay": replay_signals(by_day.get(chosen, []), chosen, observed, complete_hours_only=calendar_mode,
                                              strategy_mode=checked.get("strategy", {}).get("id", REVIEW))}
        try:
            validate_spot_proxy_day(day_input, ignore_missing_minutes=ignore_missing_minutes)
        except (SpotProxyDataError, ValueError) as exc:
            blocked = {"date": day["date"], "reason": str(exc),
                       "missing_times": getattr(exc, "missing_times", []),
                       "duplicate_times": getattr(exc, "duplicate_times", [])}
            break
        sessions.append(day_input)

    if sessions:
        ledger = build_spot_proxy_ledger(sessions, non_trading_dates=closed_dates, ignore_missing_minutes=ignore_missing_minutes)

    if ledger is None:
        ledger = {"mode": "spot_points_proxy_only", "status": "unavailable", "starting_state": "flat",
                  "positions": [], "closed_positions": [], "open_positions": [], "events": [],
                  "closed_spot_points_proxy": None, "open_spot_points_proxy": None,
                  "session_count": 0, "first_session": None, "last_session": None, "assumptions": []}
    elif blocked:
        ledger["status"] = "partial"
    ledger["blocked_from"] = blocked
    ledger["warnings"] = []
    if ledger.get("skipped_minute_count"):
        ledger["warnings"].append(f"Skipped {ledger['skipped_minute_count']} missing minutes. Triggers in those gaps are unknown; results are approximate.")
    if blocked:
        ledger["warnings"].append(f"Position outcomes from {blocked['date']} onward are unknown: {blocked['reason']}")
    if not sessions and not blocked:
        ledger["warnings"].append("There are no checked trading sessions in this date window.")
    ledger["hedge_schedule"] = "Monday at 15:15 IST in the monthly expiry week; hedge prices and positions are excluded from this spot-only calculation."
    ledger["assumed_holidays"] = [day["date"] for day in checked["days"] if day.get("assumed_holiday") and not day.get("verified_closed_date")]
    ledger["approximate_due_to_assumed_holidays"] = bool(ledger["assumed_holidays"])
    ledger["approximate"] = bool(ledger.get("approximate") or ledger["assumed_holidays"])
    if ledger["assumed_holidays"]:
        ledger["warnings"].append("Missing whole days are unverified assumed holidays: " + ", ".join(ledger["assumed_holidays"]) + ". Results are approximate; missing trades or exits cannot be ruled out.")
    if ledger.get("uncertain_positions"):
        ledger["warnings"].append(f"{len(ledger['uncertain_positions'])} uncertain exits touched both stop and target. They have no exact fill or P&L and are excluded from closed totals.")
    if ledger.get("assumed_stop_first_count"):
        ledger["warnings"].append(f"{ledger['assumed_stop_first_count']} same-minute collisions use the named review mode's stop-first assumption, not a known price path; results are approximate.")
    ledger["validation_status"] = ("incomplete" if ledger["status"] != "complete" else "uncertain" if ledger.get("uncertain_positions")
                                   else "approximate" if ledger["approximate"] else "validated")
    ledger["strategy"] = checked.get("strategy", strategy_config(REVIEW))
    return ledger


class KiteState:
    """Memory-only login session, with optional Windows-protected app details."""

    def __init__(self, factory=KiteClient, option_archive=None, credential_store=None):
        self.factory = factory
        self.option_archive = option_archive
        self.paper_replay = None
        self.lock = RLock()
        self.api_key = None
        self.api_secret = None
        self.client = None
        self.session_expires_at = None
        self.nonce = None
        self.nonce_deadline = None
        self.generation = 0
        self.message = "Add your Kite Connect app details to begin."
        self.instruments = []
        self.instrument_count = 0
        self.synced_at = None
        self.index_token = None
        self.replay_job = None
        self.replay_cancel = None
        self.backtest_store = None
        self.credential_store = credential_store
        self.credentials_saved = False
        self.credential_storage_warning = None
        if credential_store:
            try:
                details = credential_store.load()
                if details:
                    self.api_key, self.api_secret = details["api_key"], details["api_secret"]
                    self.credentials_saved = True
                    self.message = "Saved app details loaded. Sign in at Zerodha for today's data session."
            except CredentialStoreError as exc:
                self.credential_storage_warning = str(exc)
                self.message = str(exc)

    def _connected(self):
        if self.session_expires_at and datetime.now(timezone.utc) >= self.session_expires_at:
            self.client = None
            self.session_expires_at = None
            self.index_token = None
            self.message = "The Zerodha session expired. Sign in again."
        return self.client is not None and self.session_expires_at is not None

    def status(self, port):
        with self.lock:
            connected = self._connected()
            return {"configured": bool(self.api_key and self.api_secret),
                    "connected": connected,
                    "masked_api_key": "••••" + self.api_key[-4:] if self.api_key else None,
                    "session_expires_at": self.session_expires_at.isoformat() if connected else None,
                    "credentials_in_memory": bool(self.api_key and self.api_secret),
                    "credentials_saved": self.credentials_saved,
                    "credential_storage_available": bool(self.credential_store and self.credential_store.available),
                    "credential_storage_warning": self.credential_storage_warning,
                    "message": self.message,
                    "latest_completed_date": latest_completed_session(datetime.now(IST)).isoformat(),
                    "redirect_url": f"http://127.0.0.1:{port}/api/kite/callback"}

    def configure(self, payload, port):
        if set(payload) not in ({"api_key", "api_secret"}, {"api_key", "api_secret", "remember"}):
            raise ValueError("Enter the API key and secret from your Kite Connect app.")
        remember = payload.get("remember", False)
        if not isinstance(remember, bool):
            raise ValueError("Choose whether to remember these app details.")
        key, secret = payload["api_key"], payload["api_secret"]
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise ValueError("The API key format is invalid.")
        if not isinstance(secret, str) or not KEY_PATTERN.fullmatch(secret):
            raise ValueError("The API secret format is invalid.")
        with self.lock:
            if remember:
                if not self.credential_store or not self.credential_store.available:
                    raise CredentialStoreError("Remembering app details is unavailable. Turn off Remember to use this session only.")
                self.credential_store.save(key, secret)
            elif self.credential_store:
                self.credential_store.forget()
            self.credentials_saved = remember
            self.credential_storage_warning = None
            self.api_key, self.api_secret = key, secret
            self.client = None
            self.session_expires_at = None
            self.nonce = None
            self.nonce_deadline = None
            self.instruments = []
            self.instrument_count = 0
            self.synced_at = None
            self.index_token = None
            self.generation += 1
            self.message = ("App details saved with Windows account protection. Sign in at Zerodha."
                            if remember else "App details are in memory for this session only. Sign in at Zerodha.")
            return self.status(port)

    def clear(self, port):
        with self.lock:
            if self.credential_store:
                self.credential_store.forget()
            self.credentials_saved = False
            self.credential_storage_warning = None
            self.api_key = None
            self.api_secret = None
            self.client = None
            self.session_expires_at = None
            self.nonce = None
            self.nonce_deadline = None
            self.instruments = []
            self.instrument_count = 0
            self.synced_at = None
            self.index_token = None
            self.generation += 1
            self.message = "Connection details were cleared from this app's memory and any saved copy was removed."
            return self.status(port)

    def login_url(self):
        with self.lock:
            if not self.api_key or not self.api_secret:
                raise ValueError("Add your Kite Connect app details first.")
            self.nonce = secrets.token_urlsafe(24)
            self.nonce_deadline = datetime.now(timezone.utc) + timedelta(minutes=15)
            self.message = "Complete the Zerodha sign-in in your browser."
            return self.factory(self.api_key).login_url({"state": self.nonce})

    def finish_login(self, query):
        data = parse_qs(query, keep_blank_values=True)
        status = data.get("status", [""])[0]
        token = data.get("request_token", [""])[0]
        state = data.get("state", [""])[0]
        with self.lock:
            if (not self.nonce or not self.nonce_deadline
                    or datetime.now(timezone.utc) > self.nonce_deadline
                    or not hmac.compare_digest(state, self.nonce)):
                self.message = "Sign-in link expired or did not match. Try signing in again."
                raise ValueError("Sign-in link expired or did not match.")
            self.nonce = None
            self.nonce_deadline = None
            if status != "success" or not token or len(token) > 512:
                self.message = "Zerodha did not complete sign-in. Try again."
                raise ValueError("Zerodha did not complete sign-in.")
            key, secret, generation = self.api_key, self.api_secret, self.generation
        client = self.factory(key)
        try:
            client.exchange_request_token(token, secret)
        except KiteNetworkError:
            with self.lock:
                self.message = "The local app could not reach Zerodha to finish sign-in. Check the connection and try again."
            raise
        except KiteError:
            with self.lock:
                self.message = "Zerodha did not accept this sign-in. Check the Kite app details and try again."
            raise
        now_ist = datetime.now(IST)
        expiry_ist = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
        if expiry_ist <= now_ist:
            expiry_ist += timedelta(days=1)
        with self.lock:
            if generation != self.generation:
                raise ValueError("App details changed during sign-in. Try again.")
            self.client = client
            self.session_expires_at = expiry_ist.astimezone(timezone.utc)
            self.index_token = None
            self.message = "Zerodha sign-in completed. Market data can now be requested."

    def _client(self):
        with self.lock:
            if not self._connected():
                raise ValueError("Sign in to Zerodha before requesting market data.")
            return self.client

    def _auth_failed(self, client):
        with self.lock:
            if client is self.client:
                self.client = None
                self.session_expires_at = None
                self.index_token = None
                self.message = "Zerodha session is no longer valid. Sign in again."

    def instrument_status(self):
        with self.lock:
            today = datetime.now(IST).date()
            available = bool(self.instruments and self.synced_at and self.synced_at.astimezone(IST).date() == today)
            expiries = sorted({row["expiry"] for row in self.instruments if row.get("expiry")})
            lots = sorted({row["lot_size"] for row in self.instruments if row.get("lot_size", 0) > 0})
            return {"available": available, "instrument_count": self.instrument_count,
                    "banknifty_contract_count": len(self.instruments),
                    "synced_at": self.synced_at.isoformat() if self.synced_at else None,
                    "message": "Today’s BANKNIFTY option list is loaded." if available else "Load today’s option list after signing in.",
                    "lot_sizes": lots, "expiries": expiries}

    def sync_instruments(self):
        client = self._client()
        try:
            rows = client.instruments("NFO")
        except KiteAuthError:
            self._auth_failed(client)
            raise
        filtered = [row for row in rows
                    if row.get("exchange") == "NFO"
                    and row.get("instrument_type") in {"CE", "PE"}
                    and (row.get("name") == "BANKNIFTY" or row.get("tradingsymbol", "").startswith("BANKNIFTY"))
                    and row.get("expiry") and row.get("lot_size", 0) > 0]
        if not filtered:
            raise KiteError("Zerodha returned no current BANKNIFTY option contracts. Nothing was saved.")
        with self.lock:
            if client is not self.client:
                raise ValueError("The Zerodha session changed. Try loading the list again.")
            self.instruments = filtered
            self.instrument_count = len(rows)
            self.synced_at = datetime.now(timezone.utc)
            return self.instrument_status()

    def spot(self):
        client = self._client()
        try:
            data = client.banknifty_spot()
        except KiteAuthError:
            self._auth_failed(client)
            raise
        price = data["last_price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            raise KiteError("Zerodha did not return a valid BANKNIFTY price.")
        with self.lock:
            if client is not self.client:
                raise ValueError("The Zerodha session changed. Fetch a fresh quote.")
            self.index_token = data["instrument_token"]
            return {"instrument": "NSE:NIFTY BANK", "last_price": price,
                    "timestamp": data.get("timestamp_ist") or data.get("timestamp"),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "source": "Zerodha Kite Connect"}

    def replay_spot_day(self, payload):
        if set(payload) != {"date"} or not isinstance(payload["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["date"]):
            raise ValueError("Choose one completed market date in YYYY-MM-DD format.")
        try:
            selected = date.fromisoformat(payload["date"])
        except ValueError:
            raise ValueError("Choose a valid market date.") from None
        now = datetime.now(IST)
        if selected.weekday() >= 5:
            raise ReplayDataError("Weekends are not regular weekday sessions. Choose a market weekday.")
        if selected > now.date() or (selected == now.date() and now.time() < time(15, 30)):
            raise ReplayDataError("Wait until the regular session is complete at 15:30 IST before checking this day.")
        client = self._client()
        with self.lock:
            token = self.index_token if client is self.client else None
        if token is None:
            try:
                quote = client.banknifty_spot()
            except KiteAuthError:
                self._auth_failed(client)
                raise
            token = quote["instrument_token"]
            with self.lock:
                if client is not self.client:
                    raise ValueError("The Zerodha session changed. Try the check again.")
                self.index_token = token
        start = datetime.combine(selected, time(9, 15), IST)
        end = datetime.combine(selected, time(15, 29), IST)
        try:
            candles = client.historical_minutes(token, start, end)
        except KiteAuthError:
            self._auth_failed(client)
            raise
        with self.lock:
            if client is not self.client:
                raise ValueError("The Zerodha session changed. Try the check again.")
        observed_until = datetime.now(IST)
        recovered_minute_at = None
        try:
            result = replay_signals(candles, selected, observed_until)
        except ReplayDataError as incomplete:
            if (incomplete.missing_minutes != 1 or incomplete.duplicate_minutes
                    or len(incomplete.missing_times) != 1):
                raise
            missing = datetime.fromisoformat(incomplete.missing_times[0])
            try:
                recovered = client.historical_minutes(token, missing, missing + timedelta(seconds=59))
            except KiteAuthError:
                self._auth_failed(client)
                raise
            with self.lock:
                if client is not self.client:
                    raise ValueError("The Zerodha session changed. Try the check again.")
            if len(recovered) != 1 or recovered[0]["timestamp_ist"] != missing.isoformat():
                raise incomplete
            result = replay_signals([*candles, *recovered], selected, observed_until)
            recovered_minute_at = missing.isoformat()
        with self.lock:
            if client is not self.client:
                raise ValueError("The Zerodha session changed. Try the check again.")
        return {**result, "date": selected.isoformat(), "checked_at": datetime.now(timezone.utc).isoformat(),
                "recovered_minute_at": recovered_minute_at,
                "source": "Zerodha Kite Connect · NSE:NIFTY BANK past one-minute candles"}

    @staticmethod
    def replay_dates(payload):
        if (set(payload) - {"start_date", "model_config", "strategy_mode", "return_base"} or not isinstance(payload.get("start_date"), str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["start_date"])):
            raise ValueError("Choose a start date in YYYY-MM-DD format.")
        model_config(payload.get("model_config"))
        return_base = payload.get("return_base")
        if return_base is not None and (isinstance(return_base, bool) or not isinstance(return_base, int) or not 1 <= return_base <= 1_000_000_000):
            raise ValueError("Use a whole-number return base from ₹1 to ₹1,00,00,00,000.")
        selected_mode = payload.get("strategy_mode", CLOSE_BOUNDARY)
        strategy_config(selected_mode)
        if selected_mode not in CURRENT_MODES:
            raise ValueError("Choose the current hourly-close SL/target setting. Legacy entry-price risk is available only for saved-report comparison.")
        try:
            selected = date.fromisoformat(payload["start_date"])
        except ValueError:
            raise ValueError("Choose a valid start date.") from None
        now = datetime.now(IST)
        latest_complete = latest_completed_session(now)
        if selected > latest_complete:
            raise ValueError("Choose a date on or before the latest completed trading session.")
        earliest = earliest_replay_date(latest_complete)
        if selected < earliest:
            raise ValueError(f"Choose {earliest.isoformat()} or later: the replay supports up to five years.")
        return selected, latest_complete

    def start_replay_job(self, payload, *, purpose="research"):
        selected, end_date = self.replay_dates(payload)
        self._client()
        if purpose not in {"research", "paper_replay"}:
            raise ValueError("Unknown backtest type.")
        with self.lock:
            if self.replay_job and self.replay_job["status"] == "running":
                raise ValueError("A replay is already running. Wait for it or stop it first.")
            cancel = Event()
            job = {"id": secrets.token_hex(16), "status": "running", "start_date": selected.isoformat(),
                   "end_date": end_date.isoformat(), "purpose": purpose, "cancel_requested": False,
                   "progress": {"phase": "starting", "completed": 0, "total": 1,
                                "message": "Preparing historical spot data…"}}
            self.replay_job, self.replay_cancel = job, cancel
        Thread(target=self._run_replay_job, args=(dict(payload), job, cancel), daemon=True).start()
        return self.replay_job_status(job["id"])

    def _run_replay_job(self, payload, job, cancel):
        def report(phase, completed, total, message):
            with self.lock:
                job["progress"] = {"phase": phase, "completed": completed, "total": total, "message": message}
        try:
            result = self.replay_spot_range(payload, progress=report, cancel_event=cancel)
            if cancel.is_set():
                raise ReplayCancelled()
        except ReplayCancelled:
            with self.lock:
                job.update(status="cancelled", message="Replay stopped. No partial result was saved.")
        except (ValueError, KiteError) as exc:
            with self.lock:
                job.update(status="failed", error=str(exc))
        except Exception:
            with self.lock:
                job.update(status="failed", error="The replay could not finish. Try again; no orders were sent.")
        else:
            if self.paper_replay is not None:
                try:
                    self.paper_replay.capture_matching(result)
                except Exception:
                    job["paper_replay_warning"] = "The paper position snapshot was not updated. Its previous valuation date still applies; review the replay warnings."
            with self.lock:
                job.update(status="complete", result=result)
            if self.backtest_store is not None:
                try:
                    saved = self.backtest_store.add_backtest(result, purpose=job.get("purpose", "research"))
                except Exception:
                    job["history_warning"] = "This result is available now but could not be saved in backtest history."
                else:
                    job["history_id"] = saved["id"]

    def replay_job_status(self, identity=None):
        with self.lock:
            if self.replay_job is None and identity is None:
                return None
            if self.replay_job is None or (identity is not None and self.replay_job["id"] != identity):
                raise KeyError("Replay no longer available. Start it again.")
            return dict(self.replay_job)

    def cancel_replay_job(self, identity):
        with self.lock:
            job = self.replay_job_status(identity)
            if job["status"] == "running":
                self.replay_cancel.set()
                self.replay_job["cancel_requested"] = True
            return self.replay_job_status(identity)

    def replay_spot_range(self, payload, *, progress=None, cancel_event=None):
        selected, end_date = self.replay_dates(payload)
        report = progress or (lambda *args: None)

        client = self._client()
        def check_active():
            if cancel_event is not None and cancel_event.is_set():
                raise ReplayCancelled()
            with self.lock:
                if client is not self.client:
                    raise ValueError("The Zerodha session changed. Try the check again.")

        with self.lock:
            token = self.index_token if client is self.client else None
        if token is None:
            try:
                quote = client.banknifty_spot()
            except KiteAuthError:
                self._auth_failed(client)
                raise
            token = quote["instrument_token"]
            with self.lock:
                if client is not self.client:
                    raise ValueError("The Zerodha session changed. Try the check again.")
                self.index_token = token

        windows = list(history_windows(selected, end_date))
        candles, by_day = [], {}
        for index, (first, last) in enumerate(windows):
            check_active()
            report("download", index, len(windows), f"Downloading batch {index + 1} of {len(windows)}: {first.isoformat()} to {last.isoformat()}")
            try:
                batch = client.historical_minutes(token, *download_bounds(first, last))
            except KiteAuthError:
                self._auth_failed(client)
                raise
            check_active()
            for row in batch:
                day = datetime.fromisoformat(row["timestamp_ist"]).astimezone(IST).date()
                if not first <= day <= last:
                    raise ValueError("A historical batch returned prices outside its requested dates.")
                by_day.setdefault(day, []).append(row)
            candles.extend(batch)
            report("download", index + 1, len(windows), f"Downloaded {index + 1} of {len(windows)} batches, through {last.isoformat()}")

        recovered = []
        day_count = (end_date - selected).days + 1
        for offset in range(day_count):
            check_active()
            day = selected + timedelta(days=offset)
            report("coverage", offset + 1, day_count, f"Checking price coverage: {day.isoformat()} ({offset + 1} of {day_count} days)")
            expected = set(session_minutes(day))
            if not expected:
                continue
            day_rows = by_day.get(day, [])
            day_rows = [row for row in day_rows if datetime.fromisoformat(row["timestamp_ist"]).astimezone(IST) in expected]
            if len(day_rows) != len(expected) - 1:
                continue
            seen = [datetime.fromisoformat(row["timestamp_ist"]).astimezone(IST) for row in day_rows]
            if not seen or len(set(seen)) != len(seen):
                continue
            missing = expected - set(seen)
            if len(missing) != 1:
                continue
            minute = missing.pop()
            try:
                replacement = client.historical_minutes(token, minute, minute + timedelta(seconds=59))
            except KiteAuthError:
                self._auth_failed(client)
                raise
            check_active()
            if len(replacement) == 1 and replacement[0]["timestamp_ist"] == minute.isoformat():
                candles.append(replacement[0])
                recovered.append(minute.isoformat())

        checked_at = datetime.now(timezone.utc).isoformat()
        check_active()
        report("signals", 0, 1, "Building completed hourly bars and checking spot signals…")
        result = replay_spot_range(candles, selected, end_date, datetime.now(IST), empty_days_are_holidays=True, complete_hours_only=True, ignore_missing_minutes=True,
                                   strategy_mode=payload.get("strategy_mode", CLOSE_BOUNDARY))
        check_active()
        for day in result["days"]:
            closed = VERIFIED_CLOSED_DATES.get(date.fromisoformat(day["date"]))
            if closed:
                day["verified_closed_date"] = closed
                day["holiday_source"] = HOLIDAY_SOURCE
        result.update({"checked_at": checked_at, "source": "Zerodha Kite Connect · NSE:NIFTY BANK past one-minute candles",
                       "recovered_minutes": recovered,
                       "download_batches": len(windows), "maximum_years": 5,
                       "empty_day_policy": "Assume a holiday only after a successful download returns no candles for an entire day. Partial days and failed API requests are not holidays.",
                       "latest_completed_date": end_date.isoformat()})
        report("positions", 0, 1, "Calculating spot entries, exits and point results…")
        proxy = spot_proxy_for_range(candles, result)
        check_active()
        config = model_config(payload.get("model_config"))
        assumed_holidays = {date.fromisoformat(day["date"]) for day in result["days"] if day.get("assumed_holiday")}
        option_history = None
        if self.option_archive:
            report("options", 0, 1, "Finding exact BANKNIFTY option contracts and recorded entry prices…")
            try:
                prices, option_history = collect_historical_option_prices(
                    client, self.option_archive, candles, proxy, config, selected, end_date,
                    assumed_holidays=assumed_holidays, check_active=check_active, progress=report)
            except KiteAuthError:
                self._auth_failed(client)
                raise
        else:
            prices = None
        option_result = simulate_options(candles, proxy, config=config, assumed_holidays=assumed_holidays,
                                         check_active=check_active, option_prices=prices)
        option_result["option_history"] = option_history
        option_result["reporting"] = {"return_base": payload.get("return_base"),
                                      "return_basis": "User-supplied planning base; not capital, margin or a risk limit."}
        check_active()
        report("complete", 1, 1, "Replay ready.")
        return {"range": result, "spot_proxy": proxy, "option_model": option_result}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR can put two apps on the same port. Require one owner.
    allow_reuse_address = False
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
    def __init__(self, address, store, kite_factory=KiteClient, option_history=None):
        self.store = store
        self.kite = KiteState(kite_factory)
        self.paper = None
        self.tradetron = None
        super().__init__(address, Handler)
        try:
            self.kite = KiteState(kite_factory, credential_store=CredentialStore(
                store.path.parent / (store.path.stem + "-kite.dpapi")))
            self.kite.backtest_store = store
            archive = OptionPriceArchive(store.path.parent / (store.path.stem + "-options.sqlite3"), option_history)
            self.kite.option_archive = archive
            self.paper_replay = PaperReplay(store.path.parent / (store.path.stem + "-replay.sqlite3"))
            self.kite.paper_replay = self.paper_replay
            self.paper = PaperRuntime(self.kite, store.path.parent / (store.path.stem + "-paper.sqlite3"), option_archive=archive)
            self.tradetron = TradetronLinkState(TradetronLinkStore(
                store.path.parent / (store.path.stem + "-tradetron.dpapi")))
            self.tradetron_runtime = TradetronSignalRuntime(
                self.tradetron, store.path.parent / (store.path.stem + "-tradetron-events.sqlite3"))
            self.deployment_runtimes = DeploymentRuntimeRegistry(store, store.path.parent, self.tradetron)
        except Exception:
            super().server_close()
            raise

    def server_close(self):
        if self.paper is not None:
            self.paper.stream.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "PrivateAlgoDesk/0.1"

    def _respond(self, status, payload, content_type="application/json; charset=utf-8"):
        data = json.dumps(payload, allow_nan=False).encode() if content_type.startswith("application/json") else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, write=False):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self._respond(403, {"error": "This prototype is available only on this computer."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            self._respond(403, {"error": "Requests from another website are blocked."})
            return False
        if write and self.headers.get("X-Local-Request") != "PrivateAlgoDesk":
            self._respond(403, {"error": "Use the local dashboard to change a draft."})
            return False
        return True

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Streaming request bodies are not supported.")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("The request must contain JSON.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request length.")
        if not 0 < length <= 16384:
            raise ValueError("The request is empty or too large.")
        payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite values are not allowed.")))
        if not isinstance(payload, dict):
            raise ValueError("A JSON object is required.")
        return payload

    def _return_from_kite(self, success):
        self.send_response(303)
        self.send_header("Location", f"/?kite={'connected' if success else 'failed'}#kite-connection")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._guard():
            return
        requested = urlsplit(self.path)
        route = requested.path
        if route == "/api/kite/callback":
            try:
                self.server.kite.finish_login(requested.query)
            except (ValueError, KiteError):
                self._return_from_kite(False)
            else:
                self._return_from_kite(True)
            return
        try:
            if route in ASSETS:
                filename, mime = ASSETS[route]
                self._respond(200, (ROOT / "static" / filename).read_bytes(), mime)
            elif route == "/api/health":
                self._respond(200, {"ok": True, "mode": "offline", "live_enabled": False})
            elif route == "/api/state":
                strategies = self.server.store.list_strategies()
                readiness = [dict(item, ready=False) for item in self.server.paper.status()["readiness"]]
                for strategy in strategies:
                    strategy["readiness"] = readiness
                self._respond(200, {"mode": "offline", "live_enabled": False,
                                     "broker": {"name": "Zerodha", "connected": self.server.kite.status(self.server.server_address[1])["connected"]},
                                     "strategies": strategies,
                                     "deployments": self.server.deployment_runtimes.list(),
                                     "scenario_catalog": CATALOG,
                                     "recent_runs": self.server.store.recent_runs()})
            elif route == "/api/kite/status":
                self._respond(200, {"status": self.server.kite.status(self.server.server_address[1])})
            elif route == "/api/tradetron/status":
                self._respond(200, {"status": self.server.tradetron.status(),
                                    "runner": self.server.tradetron_runtime.status()})
            elif route == "/api/paper/status":
                self._respond(200, {**self.server.paper.status(), "historical_replay": self.server.paper_replay.status()})
            elif route == "/api/paper/evidence":
                self._respond(200, {"checks": self.server.paper.execution.status()["checks"]})
            elif route == "/api/kite/login-url":
                self._respond(200, {"login_url": self.server.kite.login_url()})
            elif route == "/api/kite/instruments/status":
                self._respond(200, {"status": self.server.kite.instrument_status()})
            elif route == "/api/kite/spot":
                self._respond(200, {"quote": self.server.kite.spot()})
            elif route == "/api/replay/spot-range/jobs/current":
                self._respond(200, {"job": self.server.kite.replay_job_status()})
            elif route.startswith("/api/replay/spot-range/jobs/"):
                self._respond(200, {"job": self.server.kite.replay_job_status(route.rsplit("/", 1)[1])})
            elif route == "/api/backtests":
                self._respond(200, {"backtests": self.server.store.recent_backtests()})
            elif route == "/api/deployments":
                self._respond(200, {"deployments": self.server.deployment_runtimes.list()})
            elif route.startswith("/api/backtests/"):
                self._respond(200, {"backtest": self.server.store.get_backtest(route.rsplit("/", 1)[1])})
            elif route.startswith("/api/runs/"):
                self._respond(200, {"run": self.server.store.get_run(route.rsplit("/", 1)[1])})
            else:
                self._respond(404, {"error": "Page not found. Live trading is not available in this prototype."})
        except KiteRateLimitError as exc:
            self._respond(429, {"error": str(exc)})
        except KiteAuthError as exc:
            self._respond(401, {"error": str(exc)})
        except KiteError as exc:
            self._respond(502, {"error": str(exc)})
        except ValueError as exc:
            self._respond(400, {"error": str(exc)})
        except KeyError as exc:
            self._respond(404, {"error": str(exc).strip("'")})
        except Exception:
            self._respond(500, {"error": "The local app could not read its data. No orders were sent."})

    def _write(self):
        if not self._guard(write=True):
            return
        route = urlsplit(self.path).path
        try:
            payload = self._body()
            if self.command == "POST" and route == "/api/strategies":
                self._respond(201, {"strategy": self.server.store.create_strategy(payload)})
            elif self.command == "POST" and route == "/api/deployments":
                deployment = self.server.store.create_deployment(payload)
                self._respond(201, {"deployment": self.server.deployment_runtimes.describe(deployment)})
            elif self.command == "POST" and len(route.split("/")) == 6 and route.startswith("/api/deployments/") and route.endswith("/tradetron/link"):
                deployment_id = route.split("/")[3]
                self._respond(200, {"deployment": self.server.deployment_runtimes.configure(deployment_id, payload)})
            elif self.command == "POST" and len(route.split("/")) == 6 and route.startswith("/api/deployments/") and route.endswith("/tradetron/disconnect"):
                if payload:
                    raise ValueError("Disconnecting a paper deployment does not take settings.")
                deployment_id = route.split("/")[3]
                self._respond(200, {"deployment": self.server.deployment_runtimes.disconnect(deployment_id)})
            elif self.command == "POST" and len(route.split("/")) == 6 and route.startswith("/api/deployments/") and route.endswith("/tradetron/migrate-legacy"):
                if set(payload) != {"confirmation"}:
                    raise ValueError("Moving the legacy protected link requires its local confirmation.")
                deployment_id = route.split("/")[3]
                self._respond(200, {"deployment": self.server.deployment_runtimes.migrate_legacy(deployment_id, payload["confirmation"])})
            elif self.command == "PATCH" and route.startswith("/api/strategies/"):
                self._respond(200, {"strategy": self.server.store.update_strategy(route.rsplit("/", 1)[1], payload)})
            elif self.command == "POST" and route == "/api/scenarios/run":
                if set(payload) != {"strategy_id", "scenario_id"} or not all(isinstance(v, str) for v in payload.values()):
                    raise ValueError("Choose a saved strategy and one check.")
                strategy = self.server.store.get_strategy(payload["strategy_id"])
                result = run_scenario(payload["scenario_id"], strategy["effective_lots"])
                self._respond(201, {"run": self.server.store.add_run(strategy, result)})
            elif self.command == "POST" and route == "/api/kite/config":
                self.server.paper.stream.stop()
                self._respond(200, {"status": self.server.kite.configure(payload, self.server.server_address[1])})
            elif self.command == "POST" and route == "/api/kite/disconnect":
                if payload:
                    raise ValueError("This request does not take settings.")
                self.server.paper.stream.stop()
                self._respond(200, {"status": self.server.kite.clear(self.server.server_address[1])})
            elif self.command == "POST" and route == "/api/tradetron/link":
                self._respond(200, {"status": self.server.tradetron.configure(payload)})
            elif self.command == "POST" and route == "/api/tradetron/disconnect":
                if payload:
                    raise ValueError("This request does not take settings.")
                self._respond(200, {"status": self.server.tradetron.clear()})
            elif self.command == "POST" and route == "/api/tradetron/paper-bootstrap":
                if payload != {"confirmation": PAPER_BOOTSTRAP_CONFIRMATION}:
                    raise ValueError("This one-time paper check requires its exact local confirmation.")
                details = self.server.tradetron.details
                if not details or details.get("strategy_id") != PAPER_BOOTSTRAP_STRATEGY_ID:
                    raise ValueError("Link the new Portal executor paper strategy before this check.")
                snapshot = self.server.paper_replay.snapshot()
                if not snapshot:
                    raise ValueError("There is no saved four-leg September paper replay to check.")
                # The delivery helper accepts only the exact CE and PE snapshot
                # pairs, sends hedge values before each trigger, and cannot
                # create a broker order or choose a new instrument.
                result = dispatch_snapshot_entries(details, snapshot)
                self.server.tradetron.mark_delivery_verified()
                self._respond(200, {"delivery": result,
                                    "notice": "Tradetron received paper-test runtime values. No Zerodha order was sent by this app."})
            elif self.command == "POST" and route == "/api/tradetron/paper-bootstrap/trigger":
                if payload != {"confirmation": PAPER_BOOTSTRAP_CONFIRMATION}:
                    raise ValueError("This one-time paper check requires its exact local confirmation.")
                raise ValueError("The directional JSON paper payload triggers its own entry. Run the paper bootstrap instead.")
            elif self.command == "POST" and route == "/api/tradetron/paper-bootstrap/initialise":
                if payload != {"confirmation": PAPER_BOOTSTRAP_CONFIRMATION}:
                    raise ValueError("This one-time paper check requires its exact local confirmation.")
                details = self.server.tradetron.details
                if not details or details.get("strategy_id") != PAPER_BOOTSTRAP_STRATEGY_ID:
                    raise ValueError("Link the new Portal executor paper strategy before this check.")
                self._respond(200, {"initialisation": {"managed_by": "Tradetron template",
                                                        "values": {"ce_entry_seen": 0, "pe_entry_seen": 0}},
                                    "notice": "The template owns the two entry counters. No Tradetron signal or Zerodha order was sent."})
            elif self.command == "POST" and route == "/api/paper/replay/refresh":
                if payload:
                    raise ValueError("The September paper replay uses its displayed fixed settings; no order instructions are accepted.")
                self._respond(202, {"job": self.server.kite.start_replay_job(request_settings(), purpose="paper_replay")})
            elif self.command == "POST" and route in {"/api/paper/stream/start", "/api/paper/stream/stop", "/api/paper/check", "/api/paper/pause", "/api/paper/resume", "/api/paper/close-retained"}:
                if payload:
                    raise ValueError("Paper controls do not accept credentials, prices or order instructions.")
                runtime = self.server.paper
                if route.endswith("/start"):
                    self._respond(200, {"stream": runtime.start()})
                elif route.endswith("/stop"):
                    runtime.stream.stop()
                    self._respond(200, runtime.status())
                elif route.endswith("/check"):
                    self._respond(200, {"check": runtime.check()})
                elif route.endswith("/pause"):
                    runtime.execution.pause()
                    self._respond(200, runtime.status())
                elif route.endswith("/close-retained"):
                    self._respond(200, runtime.close_retained())
                else:
                    if runtime.lock.locked():
                        raise ValueError("Wait until the paper check stops before reconciliation.")
                    runtime.execution.reconcile_and_resume()
                    self._respond(200, runtime.status())
            elif self.command == "POST" and route == "/api/kite/instruments/sync":
                if payload:
                    raise ValueError("This request does not take settings.")
                self._respond(200, {"status": self.server.kite.sync_instruments()})
            elif self.command == "POST" and route == "/api/replay/spot-signals":
                self._respond(200, {"check": self.server.kite.replay_spot_day(payload)})
            elif self.command == "POST" and route == "/api/replay/spot-range":
                self._respond(200, self.server.kite.replay_spot_range(payload))
            elif self.command == "POST" and route == "/api/replay/spot-range/jobs":
                self._respond(202, {"job": self.server.kite.start_replay_job(payload)})
            elif self.command == "POST" and route.startswith("/api/replay/spot-range/jobs/") and route.endswith("/cancel"):
                if payload:
                    raise ValueError("Stopping a replay does not take settings.")
                self._respond(200, {"job": self.server.kite.cancel_replay_job(route.split("/")[-2])})
            else:
                self._respond(404, {"error": "This action is unavailable. No broker order endpoint exists."})
        except ReplayDataError as exc:
            self._respond(422, {"error": str(exc), "quality": {"minute_count": exc.minute_count,
                       "missing_minutes": exc.missing_minutes, "duplicate_minutes": exc.duplicate_minutes,
                       "missing_times": exc.missing_times, "duplicate_times": exc.duplicate_times}})
        except KiteRateLimitError as exc:
            self._respond(429, {"error": str(exc)})
        except KiteAuthError as exc:
            self._respond(401, {"error": str(exc)})
        except KiteError as exc:
            self._respond(502, {"error": str(exc)})
        except TradetronDeliveryError as exc:
            self.server.tradetron.mark_delivery_rejected()
            self._respond(502, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError) as exc:
            self._respond(400, {"error": str(exc)})
        except KeyError as exc:
            self._respond(404, {"error": str(exc).strip("'")})
        except Exception:
            self._respond(500, {"error": "The local change failed. No broker orders were sent."})

    do_POST = _write
    do_PATCH = _write

    def do_OPTIONS(self):
        self._respond(403, {"error": "Cross-site requests are not allowed."})

    def log_message(self, fmt, *args):
        # Do not log bodies, credentials or financial account information.
        print(f"Local dashboard: {args[1] if len(args) > 1 else 'request'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Private strategy desk with market-data reads; cannot place trades.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "private-algo.sqlite3")
    parser.add_argument("--option-history", type=Path, default=Path.home() / "buildweek" / "algo" / ".local" / "history.sqlite3",
                        help="Existing option download cache to read without modifying it; missing files use labelled estimates.")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port between 1024 and 65535.")
    server = LocalServer(("127.0.0.1", args.port), Store(args.data), option_history=args.option_history)
    print(f"Private Algo Desk: http://127.0.0.1:{args.port} — market-data reads only; no broker orders", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
