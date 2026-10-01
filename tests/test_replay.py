"""Synthetic spot-candle tests only; no broker, calendar, trades or P&L."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import IST  # noqa: E402
from replay import ReplayDataError, replay_signals  # noqa: E402


DAY = date(2026, 9, 28)  # A weekday fixture, NOT a verified exchange session.
OPEN = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
AFTER_CLOSE = datetime(2026, 9, 28, 15, 30, tzinfo=IST)


def day_candles():
    result = []
    for minute in range(375):
        if minute < 60:
            prices = (53500, 53800, 52900, 53700)  # strict bull across 53,000
        elif minute < 120:
            prices = (53800, 54001, 53200, 53300)  # strict bear across 54,000
        else:
            prices = (54000, 54000, 54000, 54000)
        result.append(dict(zip(("open", "high", "low", "close"), prices),
                           date=(OPEN + timedelta(minutes=minute)).isoformat()))
    return result


class ReplayTests(unittest.TestCase):
    def test_exact_anchored_hours_and_twenty_second_schedule(self):
        result = replay_signals(day_candles(), DAY, AFTER_CLOSE)
        self.assertEqual(result["mode"], "past_spot_signals_only")
        self.assertEqual(result["minute_count"], 360)
        self.assertEqual(result["completed_hour_count"], 6)
        self.assertEqual(result["bars"][0]["bar_start"], "2026-09-28T09:15:00+05:30")
        self.assertEqual(result["bars"][0]["bar_end"], "2026-09-28T10:15:00+05:30")
        self.assertEqual(result["bars"][0]["scheduled_check_at"], "2026-09-28T10:15:20+05:30")
        self.assertEqual(result["bars"][-1]["bar_start"], "2026-09-28T14:15:00+05:30")
        self.assertEqual(result["bars"][-1]["bar_end"], "2026-09-28T15:15:00+05:30")
        self.assertEqual(result["bars"][-1]["scheduled_check_at"], "2026-09-28T15:15:20+05:30")
        self.assertEqual(len(result["signals"]), 2)
        self.assertEqual([(x["side"], x["level"]) for x in result["signals"]],
                         [("bull", 53000), ("bear", 54000)])
        self.assertIsNone(result["bars"][2]["signal"])
        self.assertIn("do not verify the exchange calendar", result["warning"])
        self.assertEqual(result["ignored_candles"]["partial_final_segment"], 15)
        for absent in ("orders", "fills", "pnl", "options", "trade_count"):
            self.assertNotIn(absent, result)

    def test_price_bar_uses_first_open_max_high_min_low_last_close(self):
        candles = day_candles()
        candles[0].update(open=53100, high=53100, low=53100, close=53100)
        candles[1].update(open=53100, high=55000, low=52900, close=53100)
        candles[59].update(open=53100, high=53700, low=53000, close=53700)
        first = replay_signals(candles, DAY, AFTER_CLOSE)["bars"][0]
        self.assertEqual((first["open"], first["high"], first["low"], first["close"]),
                         (53100, 55000, 52900, 53700))

    def test_unsorted_utc_timestamps_produce_same_result(self):
        candles = day_candles()
        for item in candles:
            item["date"] = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
        forward = replay_signals(candles, DAY.isoformat(), AFTER_CLOSE)
        backward = replay_signals(list(reversed(candles)), DAY, AFTER_CLOSE)
        self.assertEqual(forward["bars"], backward["bars"])

    def test_accepts_kite_client_historical_minutes_shape(self):
        candles = day_candles()
        for item in candles:
            item["timestamp_ist"] = item.pop("date")
            item["volume"] = 0
        result = replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(result["minute_count"], 360)
        self.assertEqual([(x["side"], x["level"]) for x in result["signals"]],
                         [("bull", 53000), ("bear", 54000)])

    def test_missing_minute_rejects_entire_day_and_reports_gap(self):
        candles = day_candles()
        del candles[5]
        with self.assertRaises(ReplayDataError) as caught:
            replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(caught.exception.missing_minutes, 1)
        self.assertEqual(caught.exception.duplicate_minutes, 0)
        self.assertEqual(caught.exception.minute_count, 359)
        self.assertEqual(caught.exception.missing_times, ["2026-09-28T09:20:00+05:30"])
        self.assertEqual(caught.exception.duplicate_times, [])
        self.assertIn("No signals were produced", str(caught.exception))

    def test_duplicate_minute_rejects_entire_day(self):
        candles = day_candles()
        candles.append(dict(candles[200]))
        with self.assertRaises(ReplayDataError) as caught:
            replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(caught.exception.missing_minutes, 0)
        self.assertEqual(caught.exception.duplicate_minutes, 1)
        self.assertEqual(caught.exception.minute_count, 361)
        self.assertEqual(caught.exception.duplicate_times, ["2026-09-28T12:35:00+05:30"])

    def test_last_fifteen_minutes_are_ignored_not_required(self):
        candles = day_candles()[:360]
        result = replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(result["minute_count"], 360)
        self.assertEqual(result["completed_hour_count"], 6)
        self.assertEqual(result["ignored_candles"]["partial_final_segment"], 0)
        candles.append({"date": "2026-09-28T15:15:00+05:30"})  # No OHLC: still ignored.
        result = replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(result["ignored_candles"]["partial_final_segment"], 1)

    def test_last_required_minute_gap_invalidates_sixth_hour(self):
        candles = day_candles()[:359]
        with self.assertRaises(ReplayDataError) as caught:
            replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(caught.exception.missing_minutes, 1)
        self.assertEqual(caught.exception.missing_times, ["2026-09-28T15:14:00+05:30"])

    def test_error_timestamp_samples_are_bounded_to_ten(self):
        candles = day_candles()[12:360]
        candles.extend(dict(item) for item in candles[:12])
        with self.assertRaises(ReplayDataError) as caught:
            replay_signals(candles, DAY, AFTER_CLOSE)
        self.assertEqual(caught.exception.missing_minutes, 12)
        self.assertEqual(caught.exception.duplicate_minutes, 12)
        self.assertEqual(len(caught.exception.missing_times), 10)
        self.assertEqual(len(caught.exception.duplicate_times), 10)
        self.assertEqual(caught.exception.missing_times[0], "2026-09-28T09:15:00+05:30")
        self.assertEqual(caught.exception.duplicate_times[0], "2026-09-28T09:27:00+05:30")

    def test_no_data_and_weekend_are_not_no_signal(self):
        with self.assertRaises(ReplayDataError) as caught:
            replay_signals([], DAY, AFTER_CLOSE)
        self.assertEqual(caught.exception.missing_minutes, 360)
        with self.assertRaises(ReplayDataError):
            replay_signals(day_candles(), date(2026, 9, 27), AFTER_CLOSE)

    def test_exact_last_check_boundary(self):
        candles = day_candles()[:360]
        with self.assertRaises(ReplayDataError):
            replay_signals(candles, DAY, datetime(2026, 9, 28, 15, 15, 19, tzinfo=IST))
        result = replay_signals(candles, DAY, datetime(2026, 9, 28, 15, 15, 20, tzinfo=IST))
        self.assertEqual(result["completed_hour_count"], 6)

    def test_outside_day_and_regular_hours_do_not_make_a_seventh_bar(self):
        candles = day_candles()
        candles.append({**candles[0], "date": "2026-09-28T09:14:00+05:30"})
        candles.append({**candles[0], "date": "2026-09-29T09:15:00+05:30"})
        result = replay_signals(candles, DAY, AFTER_CLOSE + timedelta(days=1))
        self.assertEqual(result["completed_hour_count"], 6)
        self.assertEqual(result["ignored_candles"], {
            "outside_chosen_day": 1, "outside_regular_hours": 1, "partial_final_segment": 15,
        })

    def test_rejects_invalid_input_and_future_data(self):
        candles = day_candles()
        with self.assertRaises(ValueError):
            replay_signals(candles, DAY, datetime(2026, 9, 28, 15, 30))
        with self.assertRaises(ValueError):
            replay_signals(candles, "not-a-date", AFTER_CLOSE)
        with self.assertRaises(ValueError):
            replay_signals(candles, DAY, AFTER_CLOSE - timedelta(days=1))
        bad = day_candles()
        bad[0]["date"] = "2026-09-28T09:15:01+05:30"
        with self.assertRaisesRegex(ValueError, "minute boundary"):
            replay_signals(bad, DAY, AFTER_CLOSE)
        bad = day_candles()
        bad[0]["date"] = "2026-09-28T09:15:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            replay_signals(bad, DAY, AFTER_CLOSE)
        bad = day_candles()
        bad[0]["high"] = 1
        with self.assertRaisesRegex(ValueError, "invalid spot prices"):
            replay_signals(bad, DAY, AFTER_CLOSE)
        bad = day_candles()
        bad[0]["timestamp_ist"] = bad[0]["date"]
        with self.assertRaisesRegex(ValueError, "two possible timestamps"):
            replay_signals(bad, DAY, AFTER_CLOSE)


if __name__ == "__main__":
    unittest.main()
