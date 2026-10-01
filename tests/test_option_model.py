"""Model arithmetic, lifecycle and cash reconciliation; invented spot prices only."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

from app import spot_proxy_for_range
from backtest_config import model_config
from backtest_calendar import MonthlyCalendar, CalendarUnavailable
from backtest_fees import FeeSchedule
from option_model import movement_contribution, premium_value, hedge_terms, simulate_options
from range_spot import replay_spot_range
from tests.test_spot_proxy import make_day
from engine import IST


def stamp(day, clock="09:15:00"):
    return datetime.fromisoformat(day + "T" + clock + "+05:30")


def ledger(last, events=(), **extra):
    return {"status": "complete", "last_session": last, "events": list(events), **extra}


def entry(day="2026-09-24", clock="10:16:00", parent="bull-1", side="bull", spot=56000):
    return {"type": "proxy_entry", "at": stamp(day, clock).isoformat(), "position_id": parent,
            "side": side, "spot": spot, "intended_short_option_side": "PE" if side == "bull" else "CE"}


class PremiumTests(unittest.TestCase):
    def setUp(self):
        self.config = model_config()

    def test_user_sold_put_example(self):
        value = premium_value("PE", 56000, 56400, 1000, stamp("2026-09-24"), stamp("2026-09-25"), 20, self.config)
        self.assertEqual(value["movement_contribution"], -190)
        self.assertEqual(value["time_decay"], 20)
        self.assertEqual(value["premium"], 790)
        self.assertEqual(1000 - value["premium"], 210)

    def test_bands_are_marginal_not_whole_movement_multipliers(self):
        for movement, expected in [(0, 0), (200, 100), (400, 210), (450, 240), (-200, -100), (-400, -190), (-450, -210), (-10000, -550), (2200, 1650), (2400, 1850)]:
            with self.subTest(movement=movement):
                self.assertEqual(movement_contribution(movement, self.config), Decimal(expected))

    def test_no_selling_loss_cap_and_zero_premium_floor(self):
        value = premium_value("CE", 56000, 60000, 1000, stamp("2026-09-24"), stamp("2026-09-24"), 20, self.config)
        self.assertGreater(value["premium"] - 1000, 200)
        value = premium_value("PE", 56000, 58000, 500, stamp("2026-09-24"), stamp("2026-09-25"), 30, self.config)
        self.assertEqual(value["premium"], 0)

    def test_net_movement_not_cumulative_path_and_fractional_weekend_decay(self):
        start, end = stamp("2026-09-25", "15:15:00"), stamp("2026-09-28", "09:15:00")
        for spot in (57000, 55000, 56000):
            value = premium_value("CE", 56000, spot, 500, start, end, 30, self.config)
        self.assertEqual(value["movement_contribution"], 0)
        self.assertEqual(value["time_decay"], 82.5)
        self.assertEqual(value["premium"], 417.5)

    def test_lower_hedge_examples_strikes_and_unbounded_formula(self):
        for spot, premium in [(32431, 300), (45000, 400), (56000, 500), (65000, 600), (75000, 700)]:
            self.assertEqual(hedge_terms("CE", spot, self.config)["entry_premium"], premium)
        self.assertEqual(hedge_terms("CE", 56000, self.config)["trigger_spot"], 57000)
        self.assertEqual(hedge_terms("PE", 56000, self.config)["trigger_spot"], 55000)
        self.assertEqual(hedge_terms("CE", 56431, self.config)["strike"], 58000)
        self.assertEqual(hedge_terms("PE", 56431, self.config)["strike"], 55000)

    def test_profile_is_explicit_and_quantity_validation_rejects_booleans(self):
        with self.assertRaisesRegex(ValueError, "explicit"):
            model_config(instrument="NIFTY")
        for config in ({"sell_lots": True}, {"units_per_lot": 0}, {"foo": 1}, {"ce_hedge_lots": -1}):
            with self.assertRaises(ValueError):
                model_config(config)


class CalendarFeeTests(unittest.TestCase):
    def test_historical_monthly_expiry_transitions_and_holidays(self):
        calendar = MonthlyCalendar()
        for year, month, expected in [(2023, 1, "2023-01-25"), (2023, 3, "2023-03-29"), (2023, 6, "2023-06-28"),
                                       (2024, 2, "2024-02-29"), (2024, 3, "2024-03-27"), (2024, 12, "2024-12-24"),
                                       (2025, 1, "2025-01-30"), (2025, 8, "2025-08-28"), (2025, 9, "2025-09-30"),
                                       (2026, 3, "2026-03-30")]:
            self.assertEqual(calendar.expiry(year, month).isoformat(), expected)
        self.assertEqual(calendar.rollover(calendar.expiry(2024, 1)), stamp("2024-01-23", "15:15:00"))
        self.assertEqual(calendar.rollover(calendar.expiry(2026, 1)), stamp("2026-01-27", "15:15:00"))
        with self.assertRaises(CalendarUnavailable):
            calendar.expiry(2027, 1)

    def test_assumed_holiday_moves_rollover_but_never_changes_expiry(self):
        calendar = MonthlyCalendar(assumed_holidays={date(2026, 9, 28)})
        self.assertEqual(calendar.expiry(2026, 9), date(2026, 9, 29))
        self.assertEqual(calendar.rollover(calendar.expiry(2026, 9)), stamp("2026-09-29", "15:15:00"))

    def test_brokerage_is_per_order_and_stt_stamp_are_side_specific(self):
        fees = FeeSchedule()
        one = fees.calculate(stamp("2026-09-24"), "BUY", 1000, 30)
        four = fees.calculate(stamp("2026-09-24"), "BUY", 1000, 120)
        sell = fees.calculate(stamp("2026-09-24"), "SELL", 1000, 120)
        self.assertEqual(one["brokerage"], four["brokerage"])
        self.assertEqual(one["brokerage"], 20)
        self.assertEqual(four["stt"], 0); self.assertEqual(sell["stamp_duty"], 0)
        self.assertEqual(sell["stt"], 180); self.assertEqual(four["stamp_duty"], 3.6)

    def test_fee_effective_dates_and_missing_schedule(self):
        fees = FeeSchedule()
        for day, expected in [("2023-03-31", 50), ("2023-04-03", 62.5), ("2024-09-30", 62.5), ("2024-10-01", 100), ("2026-03-31", 100), ("2026-04-01", 150)]:
            self.assertEqual(fees.calculate(stamp(day), "SELL", 1000, 100)["stt"], expected)
        with self.assertRaisesRegex(ValueError, "configuration"):
            fees.calculate(stamp("2020-01-01"), "BUY", 1000, 100)
        self.assertEqual(fees.calculate(stamp("2026-10-01"), "SELL", 1000, 100)["stt"], 150)


class OptionLifecycleTests(unittest.TestCase):
    def run_model(self, rows, events=(), **kwargs):
        return simulate_options(rows, ledger(rows[-1]["timestamp_ist"][:10], events), **kwargs)

    def test_hedges_exist_without_any_selling_trade_and_no_exit_fees(self):
        result = self.run_model(make_day(date(2026, 9, 24), base=56000))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(result["summary"]["selling"]["open_count"], 0)
        self.assertEqual(result["summary"]["ce_hedge"]["open_count"], 1)
        self.assertEqual(result["combined"]["realised_net"], 0)
        for group in ("ce_hedge", "pe_hedge"):
            self.assertNotIn("exit_at", result["ledgers"][group][0])
            self.assertNotIn("exit_charges", result["ledgers"][group][0])

    def test_replacement_uses_trigger_and_next_open_only_for_triggered_side(self):
        rows = make_day(date(2026, 9, 24), base=56000)
        rows[5].update(open=56000, high=57300, low=56000, close=56000)
        rows[6].update(open=56500, high=56500, low=56000, close=56000)
        result = self.run_model(rows)
        old, new = result["ledgers"]["ce_hedge"]
        self.assertEqual(old["exit_spot"], 57000)
        self.assertEqual(old["exit_at"], "2026-09-24T09:21:00+05:30")
        self.assertEqual(new["entry_spot"], 56500)
        self.assertEqual(new["entry_at"], old["exit_at"])
        self.assertEqual(new["strike"], 58000)
        self.assertEqual(len(result["ledgers"]["pe_hedge"]), 1)
        self.assertEqual(new["expiry"], old["expiry"])

    def test_rollover_resets_option_reference_not_parent_spot_trade(self):
        rows = sum((make_day(day, base=56000) for day in (date(2026,9,24), date(2026,9,25), date(2026,9,28))), [])
        original = ledger("2026-09-28", [entry()]); snapshot = deepcopy(original)
        result = simulate_options(rows, original)
        self.assertEqual(original, snapshot)
        sold = result["ledgers"]["selling"]
        self.assertEqual(len(sold), 2)
        self.assertEqual(sold[0]["exit_reason"], "monthly_rollover")
        self.assertEqual(sold[1]["entry_premium"], 1000)
        self.assertEqual(sold[0]["parent_spot_trade"], sold[1]["parent_spot_trade"])
        self.assertEqual(sold[1]["expiry"], "2026-10-27")
        self.assertEqual(sold[1]["expiry"], result["ledgers"]["pe_hedge"][-1]["expiry"])
        roll_orders = [o for o in result["orders"] if o["at"] == "2026-09-28T15:15:00+05:30"]
        self.assertEqual([o["side"] for o in roll_orders], ["BUY", "SELL", "SELL", "BUY", "BUY", "SELL"])

    def test_intraminute_exit_follows_rollover_and_new_hedge_is_monitored(self):
        rows = make_day(date(2026, 9, 28), base=56000)
        rows[359].update(high=57000)  # Confirmation coincides with 15:15 rollover.
        rows[360].update(high=57000)
        exiting = {"type": "proxy_exit", "at": "2026-09-28T15:15:00+05:30", "position_id": "bull-1",
                   "spot": 57000, "reason": "target", "price_basis": "threshold_touched_within_minute"}
        result = self.run_model(rows, [entry("2026-09-28"), exiting])
        self.assertEqual(len(result["ledgers"]["selling"]), 2)
        self.assertEqual(result["rollovers"][0]["selling_positions_reopened"], 1)
        self.assertEqual([p.get("exit_reason") for p in result["ledgers"]["ce_hedge"]], ["monthly_rollover", "hedge_replacement", None])

    def test_missing_rollover_minute_stops_without_substituting_next_price(self):
        rows = make_day(date(2026,9,28), base=56000)
        del rows[360]
        result = self.run_model(rows)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["rollovers"], [])
        self.assertIn("no candle or rollover price was substituted", result["blocked_from"]["reason"])

    def test_empty_day_policy_skips_whole_day_not_partial_day(self):
        rows = make_day(date(2026,9,24), base=56000) + make_day(date(2026,9,28), base=56000)
        checked = replay_spot_range(rows, "2026-09-24", "2026-09-28", stamp("2026-09-29"), empty_days_are_holidays=True)
        self.assertEqual(checked["days"][1]["status"], "holiday")
        self.assertTrue(checked["days"][1]["assumed_holiday"])
        spot = spot_proxy_for_range(rows, checked)
        self.assertEqual(spot["status"], "complete")
        result = simulate_options(rows, spot, assumed_holidays={date(2026,9,25)})
        self.assertEqual(result["status"], "complete")
        rows.pop(5)
        broken = replay_spot_range(rows, "2026-09-24", "2026-09-28", stamp("2026-09-29"), empty_days_are_holidays=True)
        self.assertEqual(broken["days"][0]["status"], "incomplete")

    def test_realised_open_and_all_charges_reconcile_without_hedge_duplication(self):
        rows = make_day(date(2026,9,24), base=56000)
        rows[90].update(high=57000)
        result = self.run_model(rows, [entry()])
        all_positions = sum(result["ledgers"].values(), [])
        self.assertAlmostEqual(sum(p["charges"] for p in all_positions), result["charges"]["total"], places=2)
        self.assertAlmostEqual(sum(p["net_pnl"] for p in all_positions), result["combined"]["total_net"], places=2)
        self.assertAlmostEqual(result["combined"]["realised_net"] + result["combined"]["open_net"], result["combined"]["total_net"], places=2)

    def test_daily_marks_reconcile_to_final_open_option_result_without_creating_exits(self):
        rows = make_day(date(2026,9,24), base=56000) + make_day(date(2026,9,25), base=56200)
        result = self.run_model(rows, [entry()])
        daily = result["daily_pnl"]
        self.assertEqual([row["date"] for row in daily], ["2026-09-24", "2026-09-25"])
        self.assertEqual([row["open_leg_count"] for row in daily], [3, 3])
        self.assertAlmostEqual(daily[-1]["total_net"], result["combined"]["total_net"], places=2)
        self.assertAlmostEqual(daily[1]["daily_net"], daily[1]["total_net"] - daily[0]["total_net"], places=2)
        self.assertEqual(len(result["orders"]), 3)  # two hedges and one short entry, with no forced EOD exits
        self.assertTrue(all(position["status"] == "open" for positions in result["ledgers"].values() for position in positions))
        self.assertTrue(all(row["valuation"] == "end_of_session_mark" and not row["approximate"] for row in daily))

    def test_incomplete_final_session_does_not_claim_an_end_of_day_mark(self):
        rows = make_day(date(2026,9,24), base=56000)
        rows.pop()
        result = simulate_options(rows, ledger("2026-09-24", status="partial"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["daily_pnl"], [])

    def test_daily_mark_waits_for_the_final_window_of_a_split_special_session(self):
        from tests.test_special_sessions import special_rows
        day = date(2024, 3, 2)
        result = simulate_options(special_rows(day), ledger(day.isoformat(), session_calendar_version="test"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual([(row["date"], row["at"]) for row in result["daily_pnl"]], [("2024-03-02", "2024-03-02T12:30:00+05:30")])

    def test_end_trigger_is_pending_without_fictional_replacement_order(self):
        rows = make_day(date(2026,9,24), base=56000); rows[-1].update(high=57000)
        result = self.run_model(rows)
        self.assertEqual(len(result["orders"]), 3)
        self.assertEqual(result["pending_replacements"][0]["due_at"], "2026-09-25T09:15:00+05:30")

    def test_unhedged_preset_and_empty_prefix(self):
        result = self.run_model(make_day(date(2026,9,24), base=56000), [entry()], config=model_config({"sell_lots": 1, "ce_hedge_lots": 0, "pe_hedge_lots": 0}))
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(result["ledgers"]["selling"][0]["quantity"], 30)
        result = simulate_options([], ledger(None, status="unavailable"))
        self.assertEqual(result["status"], "unavailable"); self.assertEqual(result["orders"], [])

    def test_session_end_trigger_reopens_after_weekend_and_assumed_holiday(self):
        rows = make_day(date(2026, 9, 24), base=56000) + make_day(date(2026, 9, 28), base=56400)
        rows[374].update(high=57000)
        result = self.run_model(rows, assumed_holidays={date(2026, 9, 25)})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["ledgers"]["ce_hedge"][1]["entry_at"], "2026-09-28T09:15:00+05:30")
        self.assertEqual(result["ledgers"]["ce_hedge"][1]["entry_spot"], 56400)

    def test_assumed_monday_holiday_rolls_tuesday_without_resetting_parent(self):
        rows = make_day(date(2026, 9, 25), base=56000) + make_day(date(2026, 9, 29), base=56400)
        result = self.run_model(rows, [entry("2026-09-25")], assumed_holidays={date(2026, 9, 28)})
        self.assertEqual(result["rollovers"][0]["at"], "2026-09-29T15:15:00+05:30")
        self.assertEqual(result["ledgers"]["selling"][-1]["entry_spot"], 56400)
        self.assertEqual(result["ledgers"]["selling"][-1]["parent_spot_trade"], "bull-1")

    def test_bad_next_expiry_stops_before_any_rollover_order(self):
        class LimitedCalendar(MonthlyCalendar):
            def next_expiry(self, expiry):
                raise CalendarUnavailable("Next contract calendar unavailable")
        rows = make_day(date(2026, 9, 28), base=56000)
        result = self.run_model(rows, [entry("2026-09-28")], calendar=LimitedCalendar())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["orders"]), 3)
        self.assertEqual(result["as_of"], "2026-09-28T15:15:00+05:30")
        self.assertEqual(result["ledgers"]["selling"][0]["status"], "open")

    def test_sold_calls_follow_bear_strategy_exits(self):
        rows = make_day(date(2026, 9, 24), base=56000)
        event = entry(side="bear", parent="other-strategy-42")
        exit_event = {"type": "proxy_exit", "at": stamp("2026-09-24", "12:00:00").isoformat(), "position_id": "other-strategy-42",
                      "spot": 57000, "reason": "stop", "price_basis": "threshold_touched_within_minute"}
        result = self.run_model(rows, [event, exit_event])
        sold = result["ledgers"]["selling"][0]
        self.assertEqual(sold["option_type"], "CE")
        self.assertEqual(sold["exit_reason"], "stop")
        self.assertEqual(sold["status"], "closed")
        self.assertLess(sold["premium_points_pnl"], -200)
        self.assertEqual(result["summary"]["ce_hedge"]["open_count"], 1)

    def test_carry_in_is_not_silently_reconstructed_with_invented_premiums(self):
        result = simulate_options(make_day(date(2026,9,24), base=56000), ledger("2026-09-24", starting_state="explicit_carry_in"))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("carry-in", result["blocked_from"]["reason"])
        self.assertEqual(result["orders"], [])

    def test_changed_comparison_units_are_correctly_labelled(self):
        config = model_config({"units_per_lot": 15})
        self.assertIn("15 units", config["quantity_basis"])
        self.assertNotIn("30 units", config["quantity_basis"])


if __name__ == "__main__":
    unittest.main()
