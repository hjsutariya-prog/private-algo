"""Regressions from the independent 30 September audit; no broker calls."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import unittest

from app import spot_proxy_for_range, KiteState
from engine import IST
from market_sessions import session_minutes
from option_model import simulate_options
from range_spot import replay_spot_range
from replay import replay_signals
from spot_proxy import build_spot_proxy_ledger
from strategy_config import COMPARISON, REVIEW
from tests.test_spot_proxy import make_day
from tests.test_option_model import entry, ledger


def run_spot(rows, mode=COMPARISON, assume=True, skip=True):
    checked = replay_spot_range(rows, rows[0]["timestamp_ist"][:10], rows[-1]["timestamp_ist"][:10],
                               datetime(2026, 9, 30, 23, tzinfo=IST), complete_hours_only=True,
                               empty_days_are_holidays=assume, ignore_missing_minutes=skip, strategy_mode=mode)
    return spot_proxy_for_range(rows, checked), checked


def exit_event(at="2026-09-28T15:15:00+05:30", basis="threshold_touched_within_minute", parent="bull-1", spot=57000):
    return {"type": "proxy_exit", "at": at, "position_id": parent, "spot": spot,
            "reason": "target", "price_basis": basis}


class RolloverRegressionTests(unittest.TestCase):
    def test_rollover_old_conflict_suppressed_new_hedge_trigger_processed(self):
        rows = make_day(date(2026, 9, 28), base=56000)
        rows[359]["high"] = rows[360]["high"] = 57000
        out = simulate_options(rows, ledger("2026-09-28"))
        ce = out["ledgers"]["ce_hedge"]
        self.assertEqual([p.get("exit_reason") for p in ce], ["monthly_rollover", "hedge_replacement", None])
        self.assertEqual(ce[1]["entry_at"], "2026-09-28T15:15:00+05:30")
        self.assertEqual(ce[1]["exit_at"], "2026-09-28T15:16:00+05:30")
        self.assertEqual(ce[2]["entry_at"], ce[1]["exit_at"])
        self.assertEqual(len(out["ledgers"]["pe_hedge"]), 2)
        self.assertEqual(out["validation_status"], "validated")

    def test_opening_gap_exit_precedes_roll_survivor_then_intraminute_exit(self):
        rows = make_day(date(2026, 9, 28), base=56000)
        rows[360].update(open=57000, high=57500, low=56000, close=57000)
        gap = exit_event(basis="minute_open_gap")
        later = exit_event(parent="bear-1", spot=57500)
        later["reason"] = "stop"
        events = [entry("2026-09-28"), entry("2026-09-28", parent="bear-1", side="bear", spot=57000), gap, later]
        original = deepcopy(events)
        out = simulate_options(rows, ledger("2026-09-28", events))
        self.assertEqual(events, original)
        self.assertEqual(out["rollovers"][0]["selling_positions_reopened"], 1)
        bull = [p for p in out["ledgers"]["selling"] if p["parent_spot_trade"] == "bull-1"]
        bear = [p for p in out["ledgers"]["selling"] if p["parent_spot_trade"] == "bear-1"]
        self.assertEqual(len(bull), 1)
        self.assertEqual(bull[0]["exit_reason"], "target")
        self.assertEqual([p["exit_reason"] for p in bear], ["monthly_rollover", "stop"])
        self.assertEqual(bear[0]["exit_spot"], 57000)
        self.assertEqual(bear[1]["entry_spot"], 57000)
        self.assertEqual(bear[1]["exit_spot"], 57500)
        self.assertTrue(out["event_validation"]["all_processed"])

    def test_original_parent_stop_target_survive_roll_at_different_spot(self):
        rows = make_day(date(2026, 9, 28), {0: "bull"})
        rows[360].update(open=54400, high=55000, low=54400, close=55000)
        spot, _ = run_spot(rows)
        parent = spot["closed_positions"][0]
        self.assertEqual((parent["stop_spot"], parent["target_spot"]), (53500, 55000))
        out = simulate_options(rows, spot)
        legs = out["ledgers"]["selling"]
        self.assertEqual([p["exit_reason"] for p in legs], ["monthly_rollover", "target"])
        self.assertEqual(legs[-1]["exit_spot"], 55000)
        self.assertEqual(legs[-1]["exit_at"], "2026-09-28T15:16:00+05:30")

    def test_final_minute_replacement_has_close_but_no_invented_next_open(self):
        rows = make_day(date(2026, 9, 28), base=56000)
        rows[-1]["high"] = 57000
        out = simulate_options(rows, ledger("2026-09-28"))
        self.assertEqual(out["ledgers"]["ce_hedge"][-1]["exit_at"], "2026-09-28T15:30:00+05:30")
        self.assertEqual(out["pending_replacements"][0]["due_at"], "2026-09-29T09:15:00+05:30")
        self.assertEqual(out["summary"]["ce_hedge"]["open_count"], 0)


class EventValidationTests(unittest.TestCase):
    def check_invalid(self, events, phrase):
        rows = make_day(date(2026, 9, 28), base=56000)
        out = simulate_options(rows, ledger("2026-09-28", events))
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["validation_status"], "incomplete")
        self.assertIn(phrase, out["blocked_from"]["reason"])
        self.assertEqual(out["orders"], [])
        self.assertFalse(out["event_validation"]["all_processed"])

    def test_unmatched_entry_second_rejected(self):
        self.check_invalid([entry("2026-09-28", "10:16:20")], "Unmatched")

    def test_unmatched_exit_second_rejected(self):
        self.check_invalid([entry("2026-09-28"), exit_event(at="2026-09-28T15:15:20+05:30")], "Unmatched")

    def test_unknown_parent_rejected(self):
        self.check_invalid([exit_event()], "Unknown")

    def test_duplicate_entry_rejected(self):
        self.check_invalid([entry("2026-09-28"), entry("2026-09-28")], "Duplicate")

    def test_same_side_second_parent_rejected(self):
        self.check_invalid([entry("2026-09-28"), entry("2026-09-28", parent="bull-2")], "active position")

    def test_duplicate_exit_rejected(self):
        self.check_invalid([entry("2026-09-28"), exit_event(), exit_event()], "already closed")

    def test_reversed_events_rejected(self):
        self.check_invalid([entry("2026-09-28", "14:00:00"), entry("2026-09-28", "12:00:00", "bear-1", "bear")], "sequence")

    def test_exit_before_same_minute_entry_rejected(self):
        self.check_invalid([exit_event(at="2026-09-28T10:16:00+05:30"), entry("2026-09-28")], "Unknown")

    def test_outside_session_entry_rejected(self):
        self.check_invalid([entry("2026-09-28", "15:30:00")], "Unmatched")

    def test_positions_cannot_hide_missing_exit_event(self):
        rows = make_day(date(2026, 9, 28), base=56000)
        out = simulate_options(rows, ledger("2026-09-28", [entry("2026-09-28")], positions=[{"position_id": "bull-1", "status": "closed"}]))
        self.assertEqual(out["status"], "unavailable")
        self.assertIn("position status", out["blocked_from"]["reason"])


class StrategyAndQualityTests(unittest.TestCase):
    def test_timing_modes_cannot_mix(self):
        rows = make_day(date(2026, 9, 24), {0: "bull"})
        rows[61].update(open=54200, high=54200, low=54200, close=54200)
        boundary, _ = run_spot(rows)
        review, _ = run_spot(rows, REVIEW)
        self.assertEqual(boundary["positions"][0]["entry_at"], "2026-09-24T10:15:00+05:30")
        self.assertEqual(review["positions"][0]["entry_at"], "2026-09-24T10:16:00+05:30")
        self.assertEqual(boundary["positions"][0]["entry_spot"], 54000)
        self.assertEqual(review["positions"][0]["entry_spot"], 54200)
        with self.assertRaisesRegex(ValueError, "different strategy"):
            build_spot_proxy_ledger([{"candles": rows, "replay": replay_signals(rows, "2026-09-24", datetime(2026,9,30,tzinfo=IST), strategy_mode=mode)} for mode in (COMPARISON, REVIEW)])

    def test_collision_is_uncertain_and_option_result_stops_without_fill(self):
        rows = make_day(date(2026, 9, 24), {0: "bull"})
        rows[70].update(open=54000, high=55000, low=53500, close=54000)
        spot, _ = run_spot(rows)
        self.assertEqual(len(spot["uncertain_positions"]), 1)
        self.assertEqual(spot["closed_positions"], [])
        self.assertIsNone(spot["uncertain_positions"][0]["exit_spot"])
        self.assertEqual(spot["validation_status"], "uncertain")
        out = simulate_options(rows, spot)
        self.assertEqual(out["status"], "partial")
        self.assertIn("Uncertain strategy exit", out["blocked_from"]["reason"])
        self.assertEqual(out["summary"]["selling"]["closed_count"], 0)
        self.assertFalse(out["event_validation"]["all_processed"])

    def test_missing_scheduled_day_approximation_propagates_without_minute_gaps(self):
        rows = make_day(date(2026,9,24), base=56000) + make_day(date(2026,9,28), base=56000)
        spot, checked = run_spot(rows)
        missing = checked["days"][1]
        self.assertTrue(missing["assumed_holiday"])
        self.assertNotIn("verified_closed_date", missing)
        self.assertEqual(spot["validation_status"], "approximate")
        out = simulate_options(rows, spot)  # no separate holiday argument needed
        self.assertEqual(out["skipped_minute_count"], 0)
        self.assertEqual(out["validation_status"], "approximate")
        self.assertTrue(out["approximate_due_to_assumed_holidays"])
        self.assertIn("2026-09-25", " ".join(out["warnings"]))

    def test_missing_day_strict_policy_blocks(self):
        rows = make_day(date(2026,9,24)) + make_day(date(2026,9,28))
        spot, _ = run_spot(rows, assume=False)
        self.assertEqual(spot["status"], "partial")
        self.assertEqual(spot["blocked_from"]["date"], "2026-09-25")

    def test_known_holiday_not_uncertain(self):
        rows = make_day(date(2026,9,11), base=56000) + make_day(date(2026,9,15), base=56000)
        spot, checked = run_spot(rows)
        self.assertTrue(checked["days"][3]["verified_closed_date"])
        self.assertEqual(spot["assumed_holidays"], [])
        self.assertEqual(simulate_options(rows, spot)["validation_status"], "validated")

    def test_january_20_special_session_can_enter(self):
        rows = make_day(date(2024,1,20), {0: "bull"})
        spot, checked = run_spot(rows)
        self.assertTrue(checked["days"][0]["special_session"])
        self.assertEqual(spot["positions"][0]["entry_at"], "2024-01-20T10:15:00+05:30")

    def test_split_session_full_hour_at_close_skips_entry_short_fragment_never_signals(self):
        stamps = session_minutes(date(2024,3,2))
        rows = [{"timestamp_ist": t.isoformat(), "open": 53000, "high": 53500, "low": 52900, "close": 53400} for t in stamps]
        spot, checked = run_spot(rows)
        self.assertEqual(checked["days"][0]["completed_hour_count"], 1)
        self.assertEqual(spot["positions"], [])
        self.assertEqual(spot["events"][0]["reason"], "market_closed_at_entry")
        rows[44]["high"] = 54000
        out = simulate_options(rows, spot)
        self.assertEqual(out["ledgers"]["ce_hedge"][0]["exit_at"], "2024-03-02T10:00:00+05:30")
        self.assertEqual(out["ledgers"]["ce_hedge"][1]["entry_at"], "2024-03-02T11:30:00+05:30")

    def test_final_full_hour_enters_but_final_fragment_does_not_signal(self):
        spot, checked = run_spot(make_day(date(2026,9,24), {5: "bull"}))
        self.assertEqual(spot["positions"][0]["entry_at"], "2026-09-24T15:15:00+05:30")
        self.assertEqual(checked["days"][0]["completed_hour_count"], 6)
        self.assertEqual(checked["days"][0]["ignored_bars"][-1]["reason"], "short_session_fragment")

    def test_unknown_mode_rejected_by_api_before_download(self):
        with self.assertRaisesRegex(ValueError, "timing"):
            KiteState.replay_dates({"start_date": "2026-09-01", "strategy_mode": "mixed"})

    def test_no_weekly_cap_and_one_active_position_per_direction(self):
        rows = make_day(date(2026,9,24), {0:"bull", 2:"bull", 4:"bull"})
        spot, _ = run_spot(rows)
        self.assertEqual(len([p for p in spot["positions"] if p["side"] == "bull"]), 3)
        active = set()
        for event in spot["events"]:
            if event["type"] == "proxy_entry":
                self.assertNotIn(event["side"], active)
                active.add(event["side"])
            elif event["type"] in ("proxy_exit", "proxy_uncertain"):
                active.remove(event["side"])

    def test_review_mode_stop_first_is_explicitly_approximate(self):
        rows = make_day(date(2026,9,24), {0:"bull"})
        rows[70].update(high=55000, low=53500)
        spot, _ = run_spot(rows, REVIEW)
        self.assertEqual(spot["assumed_stop_first_count"], 1)
        self.assertEqual(spot["validation_status"], "approximate")
        out = simulate_options(rows, spot)
        self.assertEqual(out["validation_status"], "approximate")
        self.assertIn("assumed stop-first", " ".join(out["warnings"]))


if __name__ == "__main__":
    unittest.main()
