"""Synthetic one-hour/special-session tests. No broker orders or credentials."""
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import Mock, patch

from app import spot_proxy_for_range, KiteState
from engine import IST
from market_sessions import session_minutes, session_windows, next_session_minute, latest_completed_session, download_bounds
from option_model import simulate_options
from range_spot import replay_spot_range
from replay import replay_signals
from tests.test_spot_proxy import make_day


def special_rows(day, *, signal=False):
    rows = [{"timestamp_ist": stamp.isoformat(), "open": 54000, "high": 54000, "low": 54000, "close": 54000}
            for stamp in session_minutes(day)]
    if signal:
        first = 45 if len(rows) == 105 else 0
        rows[first].update(open=53500, high=53500, low=52900, close=53500)
        rows[first + 59].update(open=53700, high=53700, low=53700, close=53700)
    return rows


def check(rows, first, last=None):
    last = last or first
    observed = datetime.combine(last + timedelta(days=1), datetime.min.time(), IST)
    result = replay_spot_range(rows, first, last, observed, empty_days_are_holidays=True, complete_hours_only=True)
    spot = spot_proxy_for_range(rows, result)
    options = simulate_options(rows, spot, assumed_holidays={date.fromisoformat(day["date"]) for day in result["days"] if day.get("assumed_holiday")})
    return result, spot, options


class SpecialSessionTests(unittest.TestCase):
    def test_exact_hour_qualifies_but_close_signal_is_skipped(self):
        day = date(2024, 11, 1)
        result, spot, options = check(special_rows(day, signal=True), day)
        self.assertEqual(result["days"][0]["completed_hour_count"], 1)
        self.assertEqual(result["total_signals"], 1)
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(spot["positions"], [])
        self.assertEqual(spot["events"][0]["reason"], "market_closed_at_entry")
        self.assertEqual(spot["events"][0]["signal_at"], "2024-11-01T19:00:20+05:30")
        self.assertEqual(options["status"], "complete")

    def test_59_minutes_do_not_make_a_candle(self):
        day = date(2024, 11, 1)
        rows = special_rows(day, signal=True)[:-1]
        result, spot, options = check(rows, day)
        detail = result["days"][0]
        self.assertEqual(detail["status"], "partial_hours")
        self.assertEqual(detail["completed_hour_count"], 0)
        self.assertEqual(detail["signal_count"], 0)
        self.assertEqual(detail["ignored_bars"][0]["received_unique_minutes"], 59)
        self.assertEqual(spot["status"], "unavailable")
        self.assertEqual(options["orders"], [])

    def test_closing_signal_does_not_open_on_next_trading_day(self):
        first, last = date(2024, 11, 1), date(2024, 11, 4)
        _, spot, options = check(special_rows(first, signal=True) + make_day(last), first, last)
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(spot["positions"], [])
        self.assertEqual(len(spot["events"]), 1)
        self.assertEqual(spot["events"][0]["reason"], "market_closed_at_entry")
        self.assertEqual(options["ledgers"]["selling"], [])

    def test_complete_hour_waits_for_20_second_review(self):
        day = date(2024, 11, 1)
        result = replay_signals(special_rows(day, signal=True), day,
                                datetime(2024, 11, 1, 19, 0, 19, tzinfo=IST), complete_hours_only=True)
        self.assertEqual(result["bars"], [])
        self.assertEqual(result["ignored_bars"][0]["reason"], "review_not_due")

    def test_duplicate_cannot_replace_missing_minute(self):
        day = date(2024, 11, 1)
        rows = special_rows(day, signal=True)
        rows[20] = dict(rows[19])
        result, _, _ = check(rows, day)
        self.assertEqual(result["days"][0]["completed_hour_count"], 0)
        self.assertEqual(result["days"][0]["duplicate_minutes"], 1)
        self.assertEqual(result["days"][0]["missing_minutes"], 1)

    def test_45_minute_period_is_not_combined_across_break(self):
        day = date(2024, 3, 2)
        result, spot, options = check(special_rows(day, signal=True), day)
        detail = result["days"][0]
        self.assertEqual(detail["completed_hour_count"], 1)
        self.assertEqual(detail["expected_session_minutes"], 105)
        self.assertEqual(detail["bars"][0]["bar_start"], "2024-03-02T11:30:00+05:30")
        self.assertEqual(detail["ignored_bars"][0]["scheduled_minutes"], 45)
        self.assertEqual(detail["ignored_bars"][0]["reason"], "short_session_fragment")
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(options["status"], "complete")

    def test_other_complete_hours_survive_one_incomplete_hour(self):
        day = date(2024, 1, 20)
        rows = make_day(day, {0: "bull", 2: "bear"})
        del rows[5]
        result, spot, _ = check(rows, day)
        self.assertEqual(result["days"][0]["completed_hour_count"], 5)
        self.assertEqual(result["total_signals"], 1)
        self.assertEqual(result["days"][0]["signals"][0]["side"], "bear")
        self.assertEqual(spot["status"], "unavailable")

    def test_complete_saturday_enters_normally_and_carries_forward(self):
        day = date(2024, 1, 20)
        rows = make_day(day, {5: "bull"}) + make_day(date(2024, 1, 23))
        result, spot, options = check(rows, day, date(2024, 1, 23))
        self.assertEqual(result["days"][0]["completed_hour_count"], 6)
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(spot["positions"][0]["entry_at"], "2024-01-20T15:16:00+05:30")
        self.assertEqual(options["status"], "complete")
        self.assertEqual(options["rollovers"][0]["at"], "2024-01-23T15:15:00+05:30")

    def test_monitor_stops_during_short_period_even_without_hourly_bar(self):
        first, special = date(2024, 5, 17), date(2024, 5, 18)
        partial = special_rows(special)
        partial[10].update(low=53500)
        result, spot, options = check(make_day(first, {5: "bull"}) + partial, first, special)
        position = spot["closed_positions"][0]
        self.assertEqual(position["exit_at"], "2024-05-18T09:25:00+05:30")
        self.assertEqual(position["exit_reason"], "stop")
        self.assertEqual(result["days"][1]["completed_hour_count"], 1)
        self.assertEqual(options["status"], "complete")
        self.assertEqual(options["ledgers"]["selling"][0]["exit_at"], position["exit_at"])

    def test_hedge_replacement_waits_across_scheduled_break(self):
        day = date(2024, 5, 18)
        rows = special_rows(day)
        rows[44].update(high=55000)
        _, _, options = check(rows, day)
        old, new = options["ledgers"]["ce_hedge"]
        self.assertEqual(old["exit_at"], "2024-05-18T10:00:00+05:30")
        self.assertEqual(new["entry_at"], "2024-05-18T11:30:00+05:30")
        self.assertEqual(len(options["ledgers"]["pe_hedge"]), 1)

    def test_missing_special_minute_still_blocks_open_position_tracking(self):
        first, special = date(2024, 5, 17), date(2024, 5, 18)
        rows = special_rows(special)
        del rows[20]
        result, spot, options = check(make_day(first, {5: "bull"}) + rows, first, special)
        self.assertEqual(result["days"][1]["completed_hour_count"], 1)
        self.assertEqual(spot["status"], "partial")
        self.assertEqual(options["status"], "partial")
        self.assertEqual(options["as_of"], "2024-05-17T15:30:00+05:30")

    def test_saturday_no_data_is_assumed_holiday_not_a_price_gap(self):
        first, last = date(2024, 1, 19), date(2024, 1, 23)
        result, spot, options = check(make_day(first) + make_day(last), first, last)
        self.assertEqual(result["days"][1]["status"], "holiday")
        self.assertTrue(result["days"][1]["assumed_holiday"])
        self.assertEqual(spot["status"], "complete")
        self.assertEqual(options["status"], "complete")

    def test_unknown_special_day_shows_hour_but_does_not_certify_missing_monitoring_hours(self):
        day = date(2026, 9, 26)
        result, spot, options = check(make_day(day, {0: "bull"})[:60], day)
        self.assertEqual(result["days"][0]["completed_hour_count"], 1)
        self.assertFalse(result["days"][0]["session_verified"])
        self.assertEqual(spot["status"], "unavailable")
        self.assertEqual(options["status"], "unavailable")

    def test_evening_data_is_inside_requested_bounds(self):
        start, end = download_bounds(date(2024, 11, 1), date(2024, 11, 1))
        self.assertEqual(start.hour, 18)
        self.assertEqual(end.strftime("%H:%M"), "18:59")

    def test_download_recovers_one_missing_special_session_minute(self):
        day = date(2024, 11, 1)
        rows = special_rows(day, signal=True)
        client = Mock()
        client.historical_minutes.side_effect = [rows[:-1], [rows[-1]]]
        state = KiteState()
        state.client, state.index_token = client, 260105
        with patch.object(state, "replay_dates", return_value=(day, day)), patch.object(state, "_client", return_value=client):
            result = state.replay_spot_range({"start_date": day.isoformat()})
        calls = client.historical_minutes.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1:], download_bounds(day, day))
        self.assertEqual(calls[1].args[1].strftime("%H:%M:%S"), "18:59:00")
        self.assertEqual(calls[1].args[2].strftime("%H:%M:%S"), "18:59:59")
        self.assertEqual(result["range"]["recovered_minutes"], [rows[-1]["timestamp_ist"]])
        self.assertEqual(result["range"]["days"][0]["completed_hour_count"], 1)
        self.assertEqual(result["spot_proxy"]["status"], "complete")
        self.assertEqual(result["option_model"]["status"], "complete")

    def test_range_accepts_completed_budget_sunday(self):
        with patch("app.datetime") as clock:
            clock.now.return_value = datetime(2026, 2, 1, 16, tzinfo=IST)
            self.assertEqual(KiteState.replay_dates({"start_date": "2026-02-01"}), (date(2026, 2, 1), date(2026, 2, 1)))

    def test_latest_completed_date_includes_budget_sunday_and_waits_for_evening(self):
        self.assertEqual(latest_completed_session(datetime(2026, 2, 1, 16, tzinfo=IST)), date(2026, 2, 1))
        self.assertEqual(latest_completed_session(datetime(2024, 11, 1, 16, tzinfo=IST)), date(2024, 10, 31))
        self.assertEqual(latest_completed_session(datetime(2024, 11, 1, 19, 0, 20, tzinfo=IST)), date(2024, 11, 1))

    def test_muhurat_is_part_of_next_price_schedule(self):
        next_price = next_session_minute(datetime(2024, 10, 31, 15, 29, tzinfo=IST))
        self.assertEqual(next_price.isoformat(), "2024-11-01T18:00:00+05:30")


if __name__ == "__main__":
    unittest.main()
