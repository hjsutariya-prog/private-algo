"""Synthetic date-range spot data tests; no broker, trades, or P&L."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import IST  # noqa: E402
from range_spot import earliest_replay_date, history_windows, replay_spot_range  # noqa: E402


MONDAY = date(2026, 9, 28)
AFTER_RANGE = datetime(2026, 10, 1, 9, 0, tzinfo=IST)


def minute_day(day, *, crossed=False, fragment=False):
    opening = datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15)
    result = []
    for minute in range(375 if fragment else 360):
        if crossed and minute < 60:
            prices = (53500, 53800, 52900, 53700)
        elif crossed and minute < 120:
            prices = (53800, 54001, 53200, 53300)
        else:
            prices = (54000, 54000, 54000, 54000)
        result.append({
            "timestamp_ist": (opening + timedelta(minutes=minute)).isoformat(),
            **dict(zip(("open", "high", "low", "close"), prices)),
            "volume": 0,
        })
    return result


class SpotRangeTests(unittest.TestCase):
    def test_complete_no_signal_and_no_data_days_are_distinct(self):
        candles = minute_day(MONDAY, crossed=True) + minute_day(date(2026, 9, 29))
        result = replay_spot_range(candles, MONDAY, "2026-09-30", AFTER_RANGE)
        self.assertEqual(result["mode"], "past_spot_signals_range_only")
        self.assertEqual(result["calendar_day_count"], 3)
        self.assertEqual(result["status_counts"], {
            "complete": 2, "incomplete": 0, "no_price_data": 1, "weekend": 0, "holiday": 0,
        })
        self.assertEqual(result["total_signals"], 2)
        self.assertEqual([x["status"] for x in result["days"]],
                         ["complete", "complete", "no_price_data"])
        self.assertEqual(result["days"][0]["signal_count"], 2)
        self.assertEqual(result["days"][0]["minute_count"], 360)
        self.assertEqual(len(result["days"][0]["bars"]), 6)
        self.assertEqual(result["days"][0]["bars"][0]["scheduled_check_at"],
                         "2026-09-28T10:15:20+05:30")
        self.assertEqual(result["days"][1]["signal_count"], 0)
        self.assertEqual(result["days"][1]["signals"], [])
        self.assertNotIn("signals", result["days"][2])
        self.assertNotIn("bars", result["days"][2])
        self.assertIn("does not establish a holiday", result["days"][2]["reason"])
        self.assertNotIn("pnl", result)
        self.assertNotIn("trades", result)

    def test_inclusive_weekend_dates_have_explicit_status(self):
        result = replay_spot_range([], "2026-09-25", MONDAY, AFTER_RANGE)
        self.assertEqual([x["date"] for x in result["days"]],
                         ["2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28"])
        self.assertEqual([x["status"] for x in result["days"]],
                         ["no_price_data", "weekend", "weekend", "no_price_data"])
        self.assertEqual(result["status_counts"]["weekend"], 2)
        self.assertEqual(result["total_signals"], 0)

    def test_missing_minute_makes_whole_day_incomplete_without_signals(self):
        candles = minute_day(MONDAY, crossed=True)
        del candles[5]
        result = replay_spot_range(candles, MONDAY, MONDAY, AFTER_RANGE)
        day = result["days"][0]
        self.assertEqual(day["status"], "incomplete")
        self.assertEqual(day["received_candle_count"], 359)
        self.assertEqual(day["minute_count"], 359)
        self.assertEqual(day["missing_minutes"], 1)
        self.assertEqual(day["duplicate_minutes"], 0)
        self.assertEqual(day["missing_times"], ["2026-09-28T09:20:00+05:30"])
        self.assertNotIn("signals", day)
        self.assertNotIn("bars", day)
        self.assertEqual(result["total_signals"], 0)

    def test_duplicate_minute_makes_whole_day_incomplete(self):
        candles = minute_day(MONDAY, crossed=True)
        candles.append(dict(candles[0]))
        day = replay_spot_range(candles, MONDAY, MONDAY, AFTER_RANGE)["days"][0]
        self.assertEqual(day["status"], "incomplete")
        self.assertEqual(day["duplicate_minutes"], 1)
        self.assertEqual(day["duplicate_times"], ["2026-09-28T09:15:00+05:30"])
        self.assertNotIn("signal_count", day)

    def test_invalid_prices_do_not_erase_other_complete_days(self):
        first = minute_day(MONDAY, crossed=True)
        second = minute_day(date(2026, 9, 29))
        second[10]["high"] = -1
        result = replay_spot_range(first + second, MONDAY, date(2026, 9, 29), AFTER_RANGE)
        self.assertEqual([x["status"] for x in result["days"]], ["complete", "incomplete"])
        self.assertEqual(result["total_signals"], 2)
        self.assertIn("Invalid spot minute prices", result["days"][1]["reason"])
        self.assertNotIn("bars", result["days"][1])

    def test_maximum_range_is_inclusive_and_bounded(self):
        start = earliest_replay_date(MONDAY)
        result = replay_spot_range([], start, MONDAY, AFTER_RANGE)
        self.assertEqual(result["calendar_day_count"], (MONDAY - start).days + 1)
        self.assertGreater(len(result["days"]), 1825)
        with self.assertRaisesRegex(ValueError, "five years"):
            replay_spot_range([], start - timedelta(days=1), MONDAY, AFTER_RANGE)

    def test_leap_day_and_batch_boundaries_have_no_holes_or_overlap(self):
        self.assertEqual(earliest_replay_date(date(2024, 2, 29)), date(2019, 2, 28))
        windows = list(history_windows("2021-09-29", "2026-09-29"))
        self.assertEqual(len(windows), 31)
        self.assertEqual(windows[0][0], date(2021, 9, 29))
        self.assertEqual(windows[-1][1], date(2026, 9, 29))
        for index, (start, end) in enumerate(windows):
            self.assertLess((end - start).days, 59)
            if index:
                self.assertEqual(start, windows[index - 1][1] + timedelta(days=1))

    def test_unsorted_utc_and_outside_range_candles(self):
        candles = minute_day(MONDAY, crossed=True)
        for item in candles:
            item["timestamp_ist"] = datetime.fromisoformat(item["timestamp_ist"]).astimezone(timezone.utc)
        outside = {**candles[0], "timestamp_ist": "2026-09-29T09:15:00+05:30"}
        result = replay_spot_range([outside] + list(reversed(candles)),
                                   MONDAY, MONDAY, AFTER_RANGE)
        self.assertEqual(result["ignored_candles_outside_range"], 1)
        self.assertEqual(result["days"][0]["status"], "complete")
        self.assertEqual(result["total_signals"], 2)

    def test_last_fifteen_minutes_can_be_absent(self):
        day = replay_spot_range(minute_day(MONDAY, fragment=False),
                                MONDAY, MONDAY, AFTER_RANGE)["days"][0]
        self.assertEqual(day["status"], "complete")
        self.assertEqual(day["received_candle_count"], 360)

    def test_not_yet_finished_day_is_incomplete_not_no_signal(self):
        candles = minute_day(MONDAY)[:45]
        cutoff = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
        day = replay_spot_range(candles, MONDAY, MONDAY, cutoff)["days"][0]
        self.assertEqual(day["status"], "incomplete")
        self.assertNotIn("signals", day)
        self.assertIn("15:15:20", day["reason"])

    def test_invalid_range_and_candle_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "End date"):
            replay_spot_range([], MONDAY, "2026-09-27", AFTER_RANGE)
        with self.assertRaisesRegex(ValueError, "future IST date"):
            replay_spot_range([], MONDAY, "2026-10-02", AFTER_RANGE)
        with self.assertRaisesRegex(ValueError, "timezone"):
            replay_spot_range([], MONDAY, MONDAY, datetime(2026, 9, 28, 16, 0))
        with self.assertRaisesRegex(ValueError, "Start date"):
            replay_spot_range([], "nonsense", MONDAY, AFTER_RANGE)
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            replay_spot_range([], "20260928", MONDAY, AFTER_RANGE)
        with self.assertRaisesRegex(ValueError, "minute candle"):
            replay_spot_range({}, MONDAY, MONDAY, AFTER_RANGE)
        with self.assertRaisesRegex(ValueError, "after the observed-until"):
            future = {**minute_day(MONDAY)[0], "timestamp_ist": "2026-10-02T09:15:00+05:30"}
            replay_spot_range([future], MONDAY, MONDAY, AFTER_RANGE)


if __name__ == "__main__":
    unittest.main()
