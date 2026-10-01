"""Synthetic logic tests only: no quotes, broker, real paper trade, or backtest.

The calendars and orders here are invented fixtures. Passing these tests does not
validate exchange calendars, Tradetron, Zerodha fills, margins, or live trading.
"""

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (  # noqa: E402
    HedgeReplacement,
    hedge_levels,
    rollover_due,
    rollover_session,
    strict_signal,
    short_roll_decision,
    terminal_exit,
)


IST = timezone(timedelta(hours=5, minutes=30))


class StrictSignalTests(unittest.TestCase):
    def test_bull_selects_crossed_level_closest_to_close(self):
        self.assertEqual(
            strict_signal(51500, 53800, 51200, 53750),
            {"side": "bull", "level": 53000},
        )

    def test_bear_selects_crossed_level_closest_to_close(self):
        self.assertEqual(
            strict_signal(53500, 54500, 51200, 51250),
            {"side": "bear", "level": 52000},
        )

    def test_bull_exact_close_level_is_excluded(self):
        self.assertEqual(
            strict_signal(51500, 54000, 51200, 54000),
            {"side": "bull", "level": 53000},
        )

    def test_bear_exact_close_level_is_excluded(self):
        self.assertEqual(
            strict_signal(53500, 54500, 51000, 51000),
            {"side": "bear", "level": 52000},
        )

    def test_bull_low_touch_is_not_crossing(self):
        self.assertIsNone(strict_signal(53200, 53800, 53000, 53700))

    def test_bear_high_touch_is_not_crossing(self):
        self.assertIsNone(strict_signal(53800, 54000, 53200, 53300))

    def test_no_level_between_both_exact_boundaries(self):
        self.assertIsNone(strict_signal(53200, 54000, 53000, 54000))
        self.assertIsNone(strict_signal(53800, 54000, 53000, 53000))

    def test_level_can_be_crossed_by_wick_not_candle_body(self):
        self.assertEqual(
            strict_signal(53200, 53800, 52999, 53700),
            {"side": "bull", "level": 53000},
        )
        self.assertEqual(
            strict_signal(53800, 54001, 53200, 53300),
            {"side": "bear", "level": 54000},
        )

    def test_direction_and_doji_are_respected(self):
        self.assertIsNone(strict_signal(53500, 55000, 51000, 53500))
        self.assertIsNone(strict_signal(53100, 53900, 53100, 53900))
        self.assertIsNone(strict_signal(53900, 53900, 53100, 53100))

    def test_incoherent_ohlc_is_rejected(self):
        for candle in (
            (54000, 53000, 52000, 52500),
            (52500, 53000, 52000, 54000),
            (52000, 54000, 53000, 53500),
            (53500, 54000, 53000, 52000),
            (53500, 53000, 54000, 53500),
        ):
            with self.subTest(candle=candle), self.assertRaises(ValueError):
                strict_signal(*candle)

    def test_nonfinite_or_nonpositive_price_is_rejected_in_every_field(self):
        for field in range(4):
            for invalid in (0, -1, math.inf, -math.inf, math.nan):
                candle = [53100, 54200, 52900, 54100]
                candle[field] = invalid
                with self.subTest(field=field, value=invalid):
                    with self.assertRaises(ValueError):
                        strict_signal(*candle)


class HedgeLevelsTests(unittest.TestCase):
    def test_call_hedge_formula(self):
        self.assertEqual(
            hedge_levels(54123, "CE"),
            {"reference": 54123.0, "band": 500, "strike": 56000, "target": 55123.0},
        )

    def test_put_hedge_formula(self):
        self.assertEqual(
            hedge_levels(54123, "PE"),
            {"reference": 54123.0, "band": 500, "strike": 53000, "target": 53123.0},
        )

    def test_exact_thousand_boundary_has_no_extra_rounding_step(self):
        self.assertEqual(hedge_levels(54000, "CE")["strike"], 55000)
        self.assertEqual(hedge_levels(54000, "PE")["strike"], 53000)

    def test_band_changes_at_ten_thousand_boundary(self):
        self.assertEqual(hedge_levels(59999.99, "CE")["band"], 500)
        self.assertEqual(hedge_levels(60000, "CE")["band"], 600)
        self.assertEqual(hedge_levels(60000, "CE")["target"], 61200)
        self.assertEqual(hedge_levels(60000, "PE")["target"], 58800)

    def test_fractional_reference_is_preserved(self):
        result = hedge_levels(54123.45, "PE")
        self.assertEqual(result["reference"], 54123.45)
        self.assertAlmostEqual(result["target"], 53123.45)

    def test_invalid_and_unsupported_low_spot_are_rejected(self):
        for invalid in (0, -1, 9999, math.inf, -math.inf, math.nan):
            for side in ("CE", "PE"):
                with self.subTest(spot=invalid, side=side):
                    with self.assertRaises(ValueError):
                        hedge_levels(invalid, side)

    def test_unknown_side_is_rejected(self):
        with self.assertRaises(ValueError):
            hedge_levels(54000, "INVALID")


class RolloverCalendarTests(unittest.TestCase):
    def setUp(self):
        self.expiry = date(2026, 9, 29)
        self.monday = date(2026, 9, 28)
        self.sessions = {self.monday, self.expiry}

    def test_normal_expiry_week_rolls_on_monday(self):
        self.assertEqual(rollover_session(self.expiry, self.sessions), self.monday)

    def test_monday_holiday_rolls_on_next_supplied_session(self):
        self.assertEqual(rollover_session(self.expiry, {self.expiry}), self.expiry)

    def test_actual_expiry_shifted_to_previous_week_uses_that_week(self):
        actual_expiry = date(2026, 9, 25)
        supplied = {date(2026, 9, 21), date(2026, 9, 24), actual_expiry}
        self.assertEqual(rollover_session(actual_expiry, supplied), date(2026, 9, 21))

    def test_calendar_not_weekday_assumption_is_authoritative(self):
        expiry = date(2026, 9, 27)
        saturday = date(2026, 9, 26)
        self.assertEqual(rollover_session(expiry, {saturday, expiry}), saturday)

    def test_sessions_outside_expiry_week_do_not_count(self):
        for sessions in (set(), {date(2026, 9, 27)}, {date(2026, 9, 30)}):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                rollover_session(self.expiry, sessions)

    def test_cutoff_is_inclusive_at_1515_india_time(self):
        self.assertFalse(rollover_due(datetime(2026, 9, 28, 15, 14, 59, tzinfo=IST), self.expiry, self.sessions))
        self.assertTrue(rollover_due(datetime(2026, 9, 28, 15, 15, tzinfo=IST), self.expiry, self.sessions))

    def test_timezone_is_converted_not_assumed(self):
        self.assertFalse(rollover_due(datetime(2026, 9, 28, 9, 44, 59, tzinfo=timezone.utc), self.expiry, self.sessions))
        self.assertTrue(rollover_due(datetime(2026, 9, 28, 9, 45, tzinfo=timezone.utc), self.expiry, self.sessions))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            rollover_due(datetime(2026, 9, 28, 15, 15), self.expiry, self.sessions)

    def test_missed_monday_cutoff_is_due_next_valid_session(self):
        self.assertTrue(rollover_due(datetime(2026, 9, 29, 9, 15, tzinfo=IST), self.expiry, self.sessions))

    def test_no_roll_before_selected_day_or_after_expiry(self):
        for now in (
            datetime(2026, 9, 27, 15, 15, tzinfo=IST),
            datetime(2026, 9, 30, 9, 15, tzinfo=IST),
        ):
            with self.subTest(now=now):
                self.assertFalse(rollover_due(now, self.expiry, self.sessions))

    def test_never_due_on_missing_exchange_session(self):
        expiry = date(2026, 10, 1)
        sessions = {date(2026, 9, 28), expiry}
        self.assertFalse(rollover_due(datetime(2026, 9, 29, 16, 0, tzinfo=IST), expiry, sessions))

    def test_rollover_waits_outside_regular_market_hours(self):
        for now in (
            datetime(2026, 9, 28, 15, 30, tzinfo=IST),
            datetime(2026, 9, 28, 20, 0, tzinfo=IST),
            datetime(2026, 9, 29, 9, 14, 59, tzinfo=IST),
        ):
            with self.subTest(now=now):
                self.assertFalse(rollover_due(now, self.expiry, self.sessions))


class TerminalExitTests(unittest.TestCase):
    def test_bull_exact_stop_and_target_are_inclusive(self):
        self.assertEqual(terminal_exit("bull", 54000, 53500), "stop")
        self.assertEqual(terminal_exit("bull", 54000, 55000), "target")
        self.assertIsNone(terminal_exit("bull", 54000, 53500.01))
        self.assertIsNone(terminal_exit("bull", 54000, 54999.99))

    def test_bear_exact_stop_and_target_are_inclusive(self):
        self.assertEqual(terminal_exit("bear", 54000, 54500), "stop")
        self.assertEqual(terminal_exit("bear", 54000, 53000), "target")
        self.assertIsNone(terminal_exit("bear", 54000, 54499.99))
        self.assertIsNone(terminal_exit("bear", 54000, 53000.01))

    def test_gap_beyond_boundaries_still_exits(self):
        self.assertEqual(terminal_exit("bull", 54000, 52000), "stop")
        self.assertEqual(terminal_exit("bull", 54000, 56000), "target")
        self.assertEqual(terminal_exit("bear", 54000, 56000), "stop")
        self.assertEqual(terminal_exit("bear", 54000, 52000), "target")

    def test_each_direction_uses_its_own_entry(self):
        self.assertEqual(terminal_exit("bull", 54000, 55000), "target")
        self.assertIsNone(terminal_exit("bear", 55200, 55000))

    def test_terminal_exit_wins_over_due_rollover(self):
        for side, spot, reason in (
            ("bull", 53500, "stop"), ("bull", 55000, "target"),
            ("bear", 54500, "stop"), ("bear", 53000, "target"),
        ):
            with self.subTest(side=side, spot=spot):
                result = short_roll_decision(side, 54000, spot, True)
                self.assertEqual(result["action"], "EXIT")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["entry_spot"], 54000)

    def test_nonterminal_rollover_preserves_original_entry(self):
        result = short_roll_decision("bull", 54000, 54500, True)
        self.assertEqual(result["action"], "ROLL")
        self.assertEqual(result["entry_spot"], 54000)
        self.assertEqual(short_roll_decision("bull", 54000, 54500, False)["action"], "HOLD")


class HedgeReplacementTests(unittest.TestCase):
    def new_replacement(self, buy_id="BUY-1", old="OLD-CE", new="NEW-CE"):
        replacement = HedgeReplacement(lots=4, lot_size=30)
        action = replacement.start(old, new, buy_id)
        self.assertEqual(action["action"], "BUY_NEW")
        self.assertEqual(action["quantity"], 120)
        self.assertEqual(action["order_id"], buy_id)
        self.assertEqual(action["contract"], new)
        return replacement

    def test_uses_configured_lot_size_not_hardcoded_units(self):
        replacement = HedgeReplacement(lots=4, lot_size=35)
        self.assertEqual(replacement.start("OLD", "NEW", "B")["quantity"], 140)

    def test_constructor_rejects_invalid_sizes(self):
        for lots, lot_size in ((0, 30), (-1, 30), (4, 0), (4, -1), (1.5, 30), (4, 30.5), (True, 30), (4, True)):
            with self.subTest(lots=lots, lot_size=lot_size):
                with self.assertRaises(ValueError):
                    HedgeReplacement(lots=lots, lot_size=lot_size)

    def test_duplicate_start_is_rejected(self):
        replacement = self.new_replacement()
        with self.assertRaises(ValueError):
            replacement.start("OLD-CE", "NEW-CE", "BUY-2")

    def test_same_contract_cannot_pretend_to_be_rollover(self):
        replacement = HedgeReplacement(lots=4, lot_size=30)
        with self.assertRaises(ValueError):
            replacement.start("SAME", "SAME", "BUY-1")

    def test_partial_pending_rejected_cancelled_unknown_hold_old(self):
        for status, filled in (
            ("OPEN", 0), ("OPEN", 60), ("OPEN", 120),
            ("COMPLETE", 60), ("REJECTED", 0), ("REJECTED", 60),
            ("CANCELLED", 0), ("CANCELLED", 60), ("UNKNOWN", 120),
        ):
            with self.subTest(status=status, filled=filled):
                replacement = self.new_replacement()
                result = replacement.on_buy_update("BUY-1", status, filled)
                self.assertEqual(result["action"], "HOLD_OLD")

    def test_exact_full_confirmed_buy_authorizes_old_sale(self):
        replacement = self.new_replacement()
        replacement.on_buy_update("BUY-1", "OPEN", 60)
        result = replacement.on_buy_update("BUY-1", "COMPLETE", 120)
        self.assertEqual(result["action"], "SELL_OLD")
        self.assertEqual(result["quantity"], 120)

    def test_duplicate_full_updates_never_authorize_second_sale(self):
        replacement = self.new_replacement()
        self.assertEqual(replacement.on_buy_update("BUY-1", "COMPLETE", 120)["action"], "SELL_OLD")
        for status, filled in (("COMPLETE", 120), ("OPEN", 60), ("COMPLETE", 120)):
            self.assertEqual(replacement.on_buy_update("BUY-1", status, filled)["action"], "HOLD_OLD")

    def test_wrong_order_id_cannot_release_old_hedge(self):
        replacement = self.new_replacement()
        with self.assertRaises(ValueError):
            replacement.on_buy_update("ANOTHER-ORDER", "COMPLETE", 120)
        self.assertEqual(replacement.on_buy_update("BUY-1", "OPEN", 0)["action"], "HOLD_OLD")

    def test_overfill_or_invalid_quantity_cannot_release_old_hedge(self):
        for quantity in (121, -1, 120.5):
            with self.subTest(quantity=quantity):
                replacement = self.new_replacement()
                try:
                    result = replacement.on_buy_update("BUY-1", "COMPLETE", quantity)
                except ValueError:
                    pass
                else:
                    self.assertEqual(result["action"], "HOLD_OLD")

    def test_old_sale_cannot_finish_before_buy_confirmation(self):
        replacement = self.new_replacement()
        with self.assertRaises(ValueError):
            replacement.mark_old_sold("SELL-1", 120, "COMPLETE")

    def test_boolean_is_not_a_one_unit_broker_fill(self):
        replacement = HedgeReplacement(lots=1, lot_size=1)
        replacement.start("OLD", "NEW", "BUY-1")
        try:
            result = replacement.on_buy_update("BUY-1", "COMPLETE", True)
        except ValueError:
            pass
        else:
            self.assertEqual(result["action"], "HOLD_OLD")

    def test_old_sale_requires_its_own_complete_full_fill(self):
        replacement = self.new_replacement()
        replacement.on_buy_update("BUY-1", "COMPLETE", 120)
        for status, filled in (("OPEN", 0), ("OPEN", 120), ("COMPLETE", 60), ("REJECTED", 0), ("UNKNOWN", 120)):
            with self.subTest(status=status, filled=filled):
                self.assertFalse(replacement.mark_old_sold("SELL-1", filled, status))
        self.assertTrue(replacement.mark_old_sold("SELL-1", 120, "COMPLETE"))

    def test_wrong_sale_update_does_not_complete_cycle(self):
        replacement = self.new_replacement()
        replacement.on_buy_update("BUY-1", "COMPLETE", 120)
        self.assertFalse(replacement.mark_old_sold("SELL-1", 60, "OPEN"))
        with self.assertRaises(ValueError):
            replacement.mark_old_sold("SELL-WRONG", 120, "COMPLETE")
        self.assertTrue(replacement.mark_old_sold("SELL-1", 120, "COMPLETE"))

    def test_overfilled_sale_cannot_complete_cycle(self):
        replacement = self.new_replacement()
        replacement.on_buy_update("BUY-1", "COMPLETE", 120)
        with self.assertRaises(ValueError):
            replacement.mark_old_sold("SELL-1", 121, "COMPLETE")

    def test_postcompletion_buy_update_cannot_release_another_sale(self):
        replacement = self.new_replacement()
        replacement.on_buy_update("BUY-1", "COMPLETE", 120)
        self.assertTrue(replacement.mark_old_sold("SELL-1", 120, "COMPLETE"))
        self.assertEqual(replacement.on_buy_update("BUY-1", "COMPLETE", 120)["action"], "HOLD_OLD")

    def test_call_and_put_replacements_are_independent(self):
        calls = self.new_replacement("BUY-CE", "OLD-CE", "NEW-CE")
        puts = self.new_replacement("BUY-PE", "OLD-PE", "NEW-PE")
        self.assertEqual(calls.on_buy_update("BUY-CE", "COMPLETE", 120)["action"], "SELL_OLD")
        self.assertEqual(puts.on_buy_update("BUY-PE", "OPEN", 60)["action"], "HOLD_OLD")
        with self.assertRaises(ValueError):
            puts.mark_old_sold("SELL-PE", 120, "COMPLETE")
        self.assertEqual(puts.on_buy_update("BUY-PE", "COMPLETE", 120)["action"], "SELL_OLD")


if __name__ == "__main__":
    unittest.main()
