"""Regression tests for the paper-only streaming strategy producer."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from engine import IST
from live_strategy_runner import PaperStrategyRunner, RUNNER_CONFIRMATION
from tradetron_runtime import ARMING_CONFIRMATION, TradetronSignalRuntime


class LinkedExecutor:
    details = {"strategy_id": "999056854", "auth_token": "test", "signal_key": "test", "signal_value": "1"}


class RecordingDelivery:
    calls = []
    def __init__(self, details): self.details = details
    def signal(self, signal):
        self.calls.append(signal)
        return {"http_status": 200, "runtime_variables": ["test"]}


class Candles:
    def __init__(self): self.rows = {}
    def hour(self, start, now):
        value = self.rows.get(start.isoformat())
        if value is None:
            raise ValueError("Incomplete observed hour; no new entry is permitted.")
        return value


class Calendar:
    def is_open(self, at): return True
    def rollover(self, expiry): return datetime(2026, 10, 26, 15, 15, tzinfo=IST)


class Market:
    def __init__(self):
        self.calendar = Calendar()
        self.stream = type("Stream", (), {"candles": Candles()})()
        self.replacements = 0

    def entry(self, start="2026-10-01T09:15:00+05:30"):
        end = datetime.fromisoformat(start).replace(hour=10, minute=15)
        self.stream.candles.rows[datetime.fromisoformat(start).isoformat()] = {
            "start": start, "end": end.isoformat(), "open": 53500, "high": 54600, "low": 53000, "close": 54500,
        }

    def strategy_entry_selection(self, direction, at):
        strike = 54500 if direction == "PE" else 54000
        hedge = 53500 if direction == "PE" else 55000
        return {"spot": 54500, "expiry": "2026-10-27", "expiry_offset": 0, "quantity": 30,
                "short_symbol": f"BANKNIFTY26OCT{strike}{direction}", "short_strike": strike,
                "hedge_symbol": f"BANKNIFTY26OCT{hedge}{direction}", "hedge_strike": hedge,
                "hedge_trigger_spot": 53000 if direction == "PE" else 56000}

    def strategy_replacement_selection(self, direction, at, position, *, rollover):
        self.replacements += 1
        expiry = "2026-11-24" if rollover else position["hedge_expiry"]
        offset = 1 if rollover else position["hedge_expiry_offset"]
        strike = 53000 - 500 * self.replacements if direction == "PE" else 56000 + 500 * self.replacements
        return {"spot": 54500, "expiry": expiry, "expiry_offset": offset,
                "hedge_symbol": f"BANKNIFTY26{'NOV' if rollover else 'OCT'}{strike}{direction}",
                "hedge_strike": strike,
                # The first scheduled replacement is deliberately monitored in
                # the same 15:15 minute by the next quote.
                "hedge_trigger_spot": 54400 if rollover and direction == "PE" else 53000 if direction == "PE" else 56000}


def quote(at, price):
    return {"timestamp": at.isoformat(), "last_price": price, "symbol": "NSE:NIFTY BANK"}


class LiveStrategyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        RecordingDelivery.calls = []
        root = Path(self.temp.name)
        self.sender = TradetronSignalRuntime(LinkedExecutor(), root / "events.sqlite3", RecordingDelivery)
        self.runner = PaperStrategyRunner("deployment-one", self.sender, root / "runner.sqlite3")
        self.market = Market()

    def start(self):
        self.sender.arm(ARMING_CONFIRMATION)
        self.runner.start(RUNNER_CONFIRMATION)

    def enter_pe(self):
        self.market.entry()
        self.start()
        at = datetime(2026, 10, 1, 10, 15, tzinfo=IST)
        self.runner.observe(self.market, quote(at, 54500))
        pending = self.runner.store.state("PE")["pending"]
        self.assertEqual(pending["event_type"], "entry")
        self.runner.confirm(pending["event_id"])
        return at

    def test_complete_hour_creates_one_independent_fixed_risk_entry(self):
        at = self.enter_pe()
        state = self.runner.store.state("PE")
        self.assertEqual(state["phase"], "active")
        self.assertEqual((state["position"]["signal_hour_close"], state["position"]["stop_spot"], state["position"]["target_spot"]),
                         (54500.0, 54000.0, 55500.0))
        self.runner.observe(self.market, quote(at + timedelta(seconds=10), 54500))
        self.assertEqual(len(RecordingDelivery.calls), 1)
        self.assertEqual(self.runner.store.recent_hours()[0]["outcome"], "entry_sent")

    def test_target_exit_uses_original_hour_close_not_the_latest_quote(self):
        at = self.enter_pe()
        self.runner.observe(self.market, quote(at + timedelta(minutes=2), 55500))
        pending = self.runner.store.state("PE")["pending"]
        self.assertEqual((pending["event_type"], pending["reason"]), ("exit", "spot_target"))
        self.runner.confirm(pending["event_id"])
        self.assertEqual(self.runner.store.state("PE")["phase"], "flat")

    def test_opening_gap_exit_happens_before_scheduled_rollover(self):
        self.enter_pe()
        rollover = datetime(2026, 10, 26, 15, 15, tzinfo=IST)
        self.runner.observe(self.market, quote(rollover, 53900))
        pending = self.runner.store.state("PE")["pending"]
        self.assertEqual((pending["event_type"], pending["reason"]), ("exit", "spot_stop"))
        self.assertEqual(self.market.replacements, 0)

    def test_rollover_wins_over_old_hedge_trigger_and_new_hedge_is_monitored_same_minute(self):
        self.enter_pe()
        rollover = datetime(2026, 10, 26, 15, 15, tzinfo=IST)
        self.runner.observe(self.market, quote(rollover, 54500))
        first = self.runner.store.state("PE")["pending"]
        self.assertEqual((first["event_type"], first["reason"]), ("hedge_replacement", "scheduled_rollover"))
        self.runner.confirm(first["event_id"])
        position = self.runner.store.state("PE")["position"]
        self.assertEqual((position["hedge_expiry"], position["stop_spot"], position["target_spot"]), ("2026-11-24", 54000.0, 55500.0))
        # The old-hedge trigger is ignored at rollover. The new hedge receives
        # the next 15:15 tick and can replace immediately if its own target is hit.
        self.runner.observe(self.market, quote(rollover + timedelta(seconds=20), 54300))
        second = self.runner.store.state("PE")["pending"]
        self.assertEqual((second["event_type"], second["reason"]), ("hedge_replacement", "hedge_target"))
        self.assertGreaterEqual(self.market.replacements, 2)

    def test_disarmed_runner_never_queues_a_trade(self):
        self.market.entry()
        at = datetime(2026, 10, 1, 10, 15, tzinfo=IST)
        self.runner.observe(self.market, quote(at, 54500))
        self.assertEqual(RecordingDelivery.calls, [])
        self.assertEqual(self.sender.status()["pending_events"], 0)

    def test_restart_with_recorded_position_requires_manual_review(self):
        self.enter_pe()
        restarted = PaperStrategyRunner("deployment-one", self.sender, Path(self.temp.name) / "runner.sqlite3")
        self.assertTrue(restarted.status()["blocked"])
        self.assertIn("restarted", restarted.status()["message"])


if __name__ == "__main__":
    unittest.main()
