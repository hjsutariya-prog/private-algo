"""User-requested missing-price policy; all prices below are invented."""
from datetime import date, datetime, timedelta
import unittest

from app import spot_proxy_for_range
from engine import IST
from option_model import simulate_options
from range_spot import replay_spot_range
from tests.test_spot_proxy import make_day


def run_backtest(rows, first, last=None):
    last = last or first
    checked = replay_spot_range(rows, first, last, datetime.combine(last + timedelta(days=1), datetime.min.time(), IST),
                                empty_days_are_holidays=True, complete_hours_only=True, ignore_missing_minutes=True)
    spot = spot_proxy_for_range(rows, checked)
    options = simulate_options(rows, spot, assumed_holidays={date.fromisoformat(day["date"]) for day in checked["days"] if day.get("assumed_holiday")})
    return checked, spot, options


class MissingMinutePolicyTests(unittest.TestCase):
    def test_december_15_1515_gap_continues_through_following_day(self):
        day, following = date(2025, 12, 15), date(2025, 12, 16)
        rows = make_day(day)
        del rows[360]
        checked, spot, options = run_backtest(rows + make_day(following), day, following)
        self.assertEqual(checked["days"][0]["completed_hour_count"], 6)
        self.assertTrue(checked["days"][0]["missing_minutes_ignored"])
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(spot["last_session"], following.isoformat())
        self.assertIsNone(spot["blocked_from"])
        self.assertEqual(spot["skipped_minute_count"], 1)
        self.assertEqual(spot["data_gaps"][0]["missing_times"], ["2025-12-15T15:15:00+05:30"])
        self.assertEqual(options["status"], "complete")
        self.assertEqual(options["as_of"], "2025-12-16T15:30:00+05:30")
        self.assertTrue(options["approximate_due_to_missing_minutes"])

    def test_incomplete_hour_stays_ineligible_other_hour_can_enter(self):
        day = date(2025, 12, 15)
        rows = make_day(day, {0: "bull", 2: "bear"})
        del rows[5]
        checked, spot, _ = run_backtest(rows, day)
        self.assertEqual(checked["days"][0]["completed_hour_count"], 5)
        self.assertEqual(checked["total_signals"], 1)
        self.assertEqual(spot["positions"][0]["side"], "bear")

    def test_missing_entry_price_skips_only_that_entry(self):
        day = date(2025, 12, 15)
        rows = make_day(day, {0: "bull"})
        del rows[61]
        _, spot, _ = run_backtest(rows, day)
        self.assertEqual(spot["positions"], [])
        self.assertEqual(spot["events"][0]["reason"], "entry_price_missing")

    def test_missing_minutes_resume_stop_checks_at_next_observed_open(self):
        day = date(2025, 12, 15)
        rows = make_day(day, {0: "bull"})
        rows[72].update(open=53400, high=53400, low=53400, close=53400)
        del rows[70:72]
        _, spot, options = run_backtest(rows, day)
        position = spot["closed_positions"][0]
        self.assertEqual(position["exit_at"], "2025-12-15T10:27:00+05:30")
        self.assertEqual(position["exit_spot"], 53400)
        self.assertEqual(position["stop_spot"], 53500)
        self.assertEqual(spot["skipped_minute_count"], 2)
        self.assertEqual(options["ledgers"]["selling"][0]["exit_at"], position["exit_at"])

    def test_missing_replacement_price_defers_to_next_available_minute(self):
        day = date(2025, 12, 15)
        rows = make_day(day)
        rows[5].update(high=55000)
        rows[7].update(open=54100, high=54100, low=54100, close=54100)
        del rows[6]
        _, _, options = run_backtest(rows, day)
        old, new = options["ledgers"]["ce_hedge"]
        self.assertEqual(old["exit_at"], "2025-12-15T09:21:00+05:30")
        self.assertEqual(new["entry_at"], "2025-12-15T09:22:00+05:30")
        self.assertEqual(new["entry_spot"], 54100)
        self.assertEqual(options["delayed_actions"][0]["scheduled_at"], old["exit_at"])
        self.assertEqual(len(options["ledgers"]["pe_hedge"]), 1)

    def test_rollover_uses_next_available_price_once(self):
        day = date(2026, 9, 28)
        rows = make_day(day, {0: "bull"})
        del rows[360:362]
        _, spot, options = run_backtest(rows, day)
        self.assertEqual(options["status"], "complete")
        self.assertEqual(len(options["rollovers"]), 1)
        roll = options["rollovers"][0]
        self.assertEqual(roll["scheduled_at"], "2026-09-28T15:15:00+05:30")
        self.assertEqual(roll["at"], "2026-09-28T15:17:00+05:30")
        self.assertTrue(roll["delayed_for_missing_minutes"])
        self.assertEqual(len(options["ledgers"]["selling"]), 2)
        self.assertEqual({p["parent_spot_trade"] for p in options["ledgers"]["selling"]}, {spot["positions"][0]["position_id"]})
        self.assertEqual(spot["positions"][0]["stop_spot"], 53500)

    def test_exit_wins_over_delayed_rollover(self):
        day = date(2026, 9, 28)
        rows = make_day(day, {0: "bull"})
        rows[361].update(open=53500, high=53500, low=53500, close=53500)
        del rows[360]
        _, _, options = run_backtest(rows, day)
        self.assertEqual(options["rollovers"][0]["selling_positions_reopened"], 0)
        self.assertEqual(len(options["ledgers"]["selling"]), 1)
        self.assertEqual(options["ledgers"]["selling"][0]["exit_reason"], "stop")

    def test_missing_last_minute_marks_actual_last_price_not_invented_close(self):
        day = date(2025, 12, 15)
        _, spot, options = run_backtest(make_day(day, {0: "bull"})[:-1], day)
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(spot["open_positions"][0]["mark_at"], "2025-12-15T15:29:00+05:30")
        self.assertEqual(options["as_of"], "2025-12-15T15:29:00+05:30")
        self.assertEqual(spot["skipped_minute_count"], 1)

    def test_missing_opening_minute_starts_hedges_at_first_available_price(self):
        day = date(2025, 12, 15)
        _, spot, options = run_backtest(make_day(day)[1:], day)
        self.assertEqual(spot["skipped_minute_count"], 1)
        self.assertEqual(options["ledgers"]["ce_hedge"][0]["entry_at"], "2025-12-15T09:16:00+05:30")

    def test_59_minute_special_session_still_cannot_create_entry(self):
        from tests.test_special_sessions import special_rows
        day = date(2024, 11, 1)
        checked, spot, options = run_backtest(special_rows(day, signal=True)[:-1], day)
        self.assertEqual(checked["total_signals"], 0)
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(options["status"], "complete")

    def test_duplicate_or_invalid_prices_are_still_rejected(self):
        day = date(2025, 12, 15)
        for fault in ("duplicate", "invalid"):
            with self.subTest(fault=fault):
                rows = make_day(day)
                if fault == "duplicate":
                    rows[5] = dict(rows[4])
                else:
                    rows[5]["low"] = -1
                checked, spot, options = run_backtest(rows, day)
                self.assertFalse(checked["days"][0].get("missing_minutes_ignored", False))
                self.assertEqual(spot["status"], "unavailable")
                self.assertEqual(options["orders"], [])

    def test_no_gaps_does_not_claim_missing_data(self):
        day = date(2025, 12, 15)
        _, spot, options = run_backtest(make_day(day), day)
        self.assertEqual(spot["skipped_minute_count"], 0)
        self.assertFalse(options["approximate_due_to_missing_minutes"])

    def test_known_trigger_before_missing_rollover_minute_is_not_discarded(self):
        day = date(2026, 9, 28)
        rows = make_day(day)
        rows[359].update(high=55000)
        del rows[360]
        _, _, options = run_backtest(rows, day)
        old = options["ledgers"]["ce_hedge"][0]
        self.assertEqual(old["exit_reason"], "hedge_replacement")
        self.assertEqual(old["exit_at"], "2026-09-28T15:15:00+05:30")
        self.assertEqual(options["rollovers"][0]["at"], "2026-09-28T15:16:00+05:30")

    def test_shared_option_engine_audits_gaps_itself(self):
        from tests.test_option_model import ledger
        day = date(2025, 12, 15)
        rows = make_day(day)
        del rows[5]
        source = ledger(day.isoformat(), [])
        source["missing_minute_policy"] = "skip"
        options = simulate_options(rows, source)
        self.assertEqual(options["status"], "complete")
        self.assertEqual(options["skipped_minute_count"], 1)
        self.assertTrue(options["approximate_due_to_missing_minutes"])


if __name__ == "__main__":
    unittest.main()
