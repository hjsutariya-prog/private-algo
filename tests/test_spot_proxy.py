"""Synthetic spot-only proxy tests; none of these are option fills or P&L."""

from datetime import date, datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import IST  # noqa: E402
from replay import replay_signals  # noqa: E402
from spot_proxy import SpotProxyDataError, build_spot_proxy_ledger  # noqa: E402


FIRST = date(2026, 9, 28)
SECOND = date(2026, 9, 29)


def make_day(day, signal_slots=None, base=54000):
    opening = datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15)
    candles = []
    for minute in range(375):
        candles.append({"timestamp_ist": (opening + timedelta(minutes=minute)).isoformat(),
                        "open": base, "high": base, "low": base, "close": base})
    for slot, side in (signal_slots or {}).items():
        first, last = slot * 60, slot * 60 + 59
        if side == "bull":
            candles[first].update(open=53500, high=53500, low=52900, close=53500)
            candles[last].update(open=53700, high=53700, low=53700, close=53700)
        elif side == "bear":
            candles[first].update(open=54500, high=55001, low=54500, close=54500)
            candles[last].update(open=54300, high=54300, low=54300, close=54300)
        else:
            raise AssertionError("Fixture signal must be bull or bear")
    return candles


def day_input(day, candles):
    observed = datetime.combine(day, datetime.min.time(), IST).replace(hour=15, minute=30)
    return {"candles": candles, "replay": replay_signals(candles, day, observed)}


class SpotProxyTests(unittest.TestCase):
    def test_entry_at_next_full_minute_and_fixed_target(self):
        candles = make_day(FIRST, {0: "bull"})
        candles[70].update(open=54000, high=55000, low=54000, close=54000)
        ledger = build_spot_proxy_ledger([day_input(FIRST, candles)])
        self.assertEqual((ledger["mode"], ledger["status"], ledger["starting_state"]),
                         ("spot_points_proxy_only", "complete", "flat"))
        self.assertEqual(ledger["initial_state"], "assumed_flat_at_first_session")
        self.assertEqual(ledger["session_count"], 1)
        self.assertEqual(ledger["regular_minutes_per_session"], 375)
        self.assertEqual(len(ledger["closed_positions"]), 1)
        position = ledger["closed_positions"][0]
        self.assertEqual(position["side"], "bull")
        self.assertEqual(position["intended_short_option_side"], "PE")
        self.assertEqual(position["signal_at"], "2026-09-28T10:15:20+05:30")
        self.assertEqual(position["entry_at"], "2026-09-28T10:16:00+05:30")
        self.assertEqual(position["entry_spot"], 54000)
        self.assertEqual((position["stop_spot"], position["target_spot"]), (53500, 55000))
        self.assertEqual((position["exit_reason"], position["exit_spot"], position["spot_points_proxy"]),
                         ("target", 55000, 1000))
        self.assertEqual(ledger["closed_spot_points_proxy"], 1000)
        self.assertEqual(ledger["open_spot_points_proxy"], 0)
        self.assertEqual([event["type"] for event in ledger["events"]], ["proxy_entry", "proxy_exit"])
        self.assertIn("not option P&L", ledger["assumptions"][0])
        self.assertNotIn("rupee_pnl", ledger)

    def test_same_minute_stop_and_target_chooses_stop(self):
        candles = make_day(FIRST, {0: "bull"})
        candles[61].update(open=54000, high=55000, low=53500, close=54000)
        ledger = build_spot_proxy_ledger([day_input(FIRST, candles)])
        position = ledger["closed_positions"][0]
        self.assertEqual((position["exit_reason"], position["exit_spot"], position["spot_points_proxy"]),
                         ("stop", 53500, -500))
        self.assertEqual(position["exit_price_basis"], "threshold_touched_within_minute")
        self.assertTrue(ledger["events"][1]["both_stop_and_target_touched"])
        self.assertEqual(ledger["events"][1]["timestamp_precision"], "minute_bar")

    def test_bear_stop_is_up_and_target_is_down(self):
        candles = make_day(FIRST, {0: "bear"})
        candles[70].update(open=54000, high=54000, low=53000, close=54000)
        ledger = build_spot_proxy_ledger([day_input(FIRST, candles)])
        position = ledger["closed_positions"][0]
        self.assertEqual(position["intended_short_option_side"], "CE")
        self.assertEqual((position["stop_spot"], position["target_spot"]), (54500, 53000))
        self.assertEqual((position["exit_reason"], position["spot_points_proxy"]), ("target", 1000))

    def test_overnight_gap_exits_at_actual_next_open_not_threshold(self):
        first = make_day(FIRST, {5: "bull"})
        second = make_day(SECOND)
        second[0].update(open=53000, high=53000, low=53000, close=53000)
        ledger = build_spot_proxy_ledger([day_input(FIRST, first), day_input(SECOND, second)])
        position = ledger["closed_positions"][0]
        self.assertEqual(position["entry_at"], "2026-09-28T15:16:00+05:30")
        self.assertEqual(position["exit_at"], "2026-09-29T09:15:00+05:30")
        self.assertEqual(position["exit_price_basis"], "minute_open_gap")
        self.assertEqual(position["exit_spot"], 53000)
        self.assertEqual(position["spot_points_proxy"], -1000)

    def test_open_position_is_marked_not_closed_at_end_of_day(self):
        candles = make_day(FIRST, {5: "bull"})
        ledger = build_spot_proxy_ledger([day_input(FIRST, candles)])
        self.assertEqual(len(ledger["closed_positions"]), 0)
        self.assertEqual(len(ledger["open_positions"]), 1)
        position = ledger["open_positions"][0]
        self.assertEqual(position["status"], "open")
        self.assertEqual(position["mark_at"], "2026-09-28T15:30:00+05:30")
        self.assertEqual(position["mark_spot"], 54000)
        self.assertEqual(position["spot_points_proxy"], 0)

    def test_explicit_initial_position_and_flat_default(self):
        candles = make_day(FIRST)
        default = build_spot_proxy_ledger([day_input(FIRST, candles)])
        self.assertEqual(default["positions"], [])
        initial = build_spot_proxy_ledger([day_input(FIRST, candles)],
                                          initial_positions={"bull": 55000})
        self.assertEqual(initial["starting_state"], "explicit_carry_in")
        self.assertEqual(len(initial["closed_positions"]), 1)
        self.assertEqual(initial["closed_positions"][0]["source"], "explicit_initial_spot_exposure")
        self.assertEqual(initial["closed_positions"][0]["entry_at"], None)
        self.assertEqual(initial["closed_positions"][0]["spot_points_proxy"], -1000)
        self.assertEqual(initial["events"][0]["price_basis"], "minute_open_gap")

    def test_repeat_same_side_requires_new_signal_after_exit(self):
        candles = make_day(FIRST, {0: "bull", 1: "bull"}, base=53000)
        candles[70].update(open=53000, high=54000, low=53000, close=53000)
        ledger = build_spot_proxy_ledger([day_input(FIRST, candles)])
        self.assertEqual([event["type"] for event in ledger["events"]],
                         ["proxy_entry", "proxy_exit", "proxy_entry"])
        self.assertEqual(len([p for p in ledger["positions"] if p["side"] == "bull"]), 2)

    def test_same_side_while_open_is_skipped(self):
        candles = make_day(FIRST, {0: "bull", 1: "bull"}, base=53000)
        ledger = build_spot_proxy_ledger([day_input(FIRST, candles)])
        self.assertEqual(len(ledger["positions"]), 1)
        self.assertEqual(ledger["events"][1]["type"], "signal_skipped")
        self.assertEqual(ledger["events"][1]["reason"], "side_already_open")

    def test_opposite_sides_have_independent_position_slots(self):
        candles = make_day(FIRST)
        candles[0].update(open=54000, high=54500, low=54000, close=54000)
        ledger = build_spot_proxy_ledger(
            [day_input(FIRST, candles)], initial_positions={"bull": 54000, "bear": 54000})
        self.assertEqual([(p["side"], p["status"]) for p in ledger["positions"]],
                         [("bull", "open"), ("bear", "closed")])
        self.assertEqual(ledger["closed_positions"][0]["exit_reason"], "stop")
        self.assertEqual(ledger["closed_spot_points_proxy"], -500)
        self.assertEqual(ledger["open_spot_points_proxy"], 0)

    def test_signal_during_intraminute_exit_is_not_provably_fresh(self):
        candles = make_day(FIRST, {0: "bull"}, base=53000)
        candles[60].update(open=53000, high=53000, low=52500, close=53000)
        ledger = build_spot_proxy_ledger(
            [day_input(FIRST, candles)], initial_positions={"bull": 53000})
        self.assertEqual(len(ledger["positions"]), 1)
        self.assertEqual(ledger["events"][-1]["type"], "signal_skipped")
        self.assertEqual(ledger["events"][-1]["reason"], "not_provably_after_prior_exit")

    def test_signal_after_known_minute_open_gap_exit_is_fresh(self):
        candles = make_day(FIRST, {0: "bull"}, base=53000)
        candles[60].update(open=52000, high=52000, low=52000, close=52000)
        ledger = build_spot_proxy_ledger(
            [day_input(FIRST, candles)], initial_positions={"bull": 53000})
        self.assertEqual([event["type"] for event in ledger["events"]],
                         ["proxy_exit", "proxy_entry"])
        self.assertEqual(ledger["positions"][1]["entry_at"], "2026-09-28T10:16:00+05:30")

    def test_missing_final_fragment_minute_and_duplicate_reject(self):
        candles = make_day(FIRST, {5: "bull"})
        replay = day_input(FIRST, candles)["replay"]
        with self.assertRaises(SpotProxyDataError) as caught:
            build_spot_proxy_ledger([{"candles": candles[:-1], "replay": replay}])
        self.assertEqual(caught.exception.missing_minutes, 1)
        self.assertEqual(caught.exception.missing_times, ["2026-09-28T15:29:00+05:30"])
        with self.assertRaises(SpotProxyDataError) as caught:
            build_spot_proxy_ledger([{"candles": [*candles, candles[-1]], "replay": replay}])
        self.assertEqual(caught.exception.duplicate_minutes, 1)

    def test_inconsistent_replay_and_unexplained_weekday_gap_reject(self):
        candles = make_day(FIRST, {0: "bull"})
        item = day_input(FIRST, candles)
        item["replay"]["signals"] = []
        with self.assertRaisesRegex(SpotProxyDataError, "do not match"):
            build_spot_proxy_ledger([item])
        third = date(2026, 9, 30)
        with self.assertRaisesRegex(SpotProxyDataError, "2026-09-29"):
            build_spot_proxy_ledger([day_input(FIRST, candles), day_input(third, make_day(third))])
        accepted = build_spot_proxy_ledger(
            [day_input(FIRST, candles), day_input(third, make_day(third))],
            non_trading_dates=[SECOND])
        self.assertEqual(accepted["session_count"], 2)
        self.assertIn("not independently verified", accepted["assumptions"][-1])

    def test_rejects_bad_initial_and_unsorted_sessions(self):
        candles = make_day(FIRST)
        with self.assertRaises(ValueError):
            build_spot_proxy_ledger([day_input(FIRST, candles)], initial_positions={"bull": 0})
        with self.assertRaises(ValueError):
            build_spot_proxy_ledger([day_input(FIRST, candles)], initial_positions={"PE": 54000})
        with self.assertRaises(SpotProxyDataError):
            build_spot_proxy_ledger([day_input(SECOND, make_day(SECOND)), day_input(FIRST, candles)])
        with self.assertRaises(SpotProxyDataError):
            build_spot_proxy_ledger([])


if __name__ == "__main__":
    unittest.main()
