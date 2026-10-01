"""Long-history batching and background-job tests use invented prices only."""
from datetime import date, datetime, timedelta, timezone
from threading import Event
import time
import unittest
from unittest.mock import patch

import app
from app import KiteState
from engine import IST
from kite_client import KiteNetworkError
from tests.test_kite_integration import FakeKiteClient
from tests.test_spot_proxy import make_day


class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 9, 30, 8, 0, tzinfo=IST)
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


class LongReplayTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("app.datetime", FixedNow)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.state = KiteState(FakeKiteClient)
        self.state.client = FakeKiteClient("examplekey123")
        self.state.session_expires_at = FixedNow.now(timezone.utc) + timedelta(hours=1)

    def wait_job(self, identity):
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            job = self.state.replay_job_status(identity)
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("Background replay did not finish")

    def test_five_years_requests_every_date_once_in_bounded_batches(self):
        calls, progress = [], []
        def history(token, start, end):
            calls.append((start, end))
            return []
        self.state.client.historical_minutes = history
        result = self.state.replay_spot_range({"start_date": "2021-09-29"},
                                              progress=lambda *step: progress.append(step))
        self.assertEqual(result["range"]["start_date"], "2021-09-29")
        self.assertEqual(result["range"]["end_date"], "2026-09-29")
        self.assertEqual(result["range"]["calendar_day_count"], 1827)
        self.assertEqual(len(calls), 31)
        self.assertEqual(calls[0][0].date(), date(2021, 9, 29))
        self.assertEqual(calls[-1][1].date(), date(2026, 9, 29))
        for index, (start, end) in enumerate(calls):
            self.assertLess(end - start, timedelta(days=60))
            if index:
                self.assertEqual(start.date(), calls[index - 1][1].date() + timedelta(days=1))
        self.assertEqual(progress[-1][0], "complete")
        self.assertEqual(result["spot_proxy"]["status"], "unavailable")
        self.assertIsNone(result["spot_proxy"]["closed_spot_points_proxy"])

    def test_too_old_date_is_rejected_before_requesting_data(self):
        with patch.object(self.state.client, "historical_minutes") as fetch:
            with self.assertRaisesRegex(ValueError, "five years"):
                self.state.replay_spot_range({"start_date": "2021-09-28"})
            fetch.assert_not_called()

    def test_monday_morning_uses_same_friday_boundary_as_date_picker(self):
        class MondayMorning(FixedNow):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 9, 28, 8, 0, tzinfo=IST)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch("app.datetime", MondayMorning):
            start, end = self.state.replay_dates({"start_date": "2021-09-25"})
        self.assertEqual(start, date(2021, 9, 25))
        self.assertEqual(end, date(2026, 9, 25))

    def test_position_carries_across_download_boundary_without_replaying_each_prefix(self):
        calls = []
        def history(token, start, end):
            calls.append((start.date(), end.date()))
            rows, chosen = [], start.date()
            while chosen <= end.date():
                if chosen.weekday() < 5 and chosen != date(2026, 9, 14):
                    candles = make_day(chosen, {5: "bull"} if chosen == date(2026, 8, 28) else {})
                    if chosen == date(2026, 8, 31):
                        candles[0].update(open=55000, high=55000, low=55000, close=55000)
                    rows.extend(candles)
                chosen += timedelta(days=1)
            return rows
        self.state.client.historical_minutes = history
        with patch("app.build_spot_proxy_ledger", wraps=app.build_spot_proxy_ledger) as build:
            result = self.state.replay_spot_range({"start_date": "2026-07-01"})
        self.assertEqual(calls, [(date(2026, 7, 1), date(2026, 8, 28)),
                                 (date(2026, 8, 29), date(2026, 9, 29))])
        self.assertEqual(build.call_count, 1)
        position = result["spot_proxy"]["closed_positions"][0]
        self.assertEqual(position["entry_at"], "2026-08-28T15:15:00+05:30")
        self.assertEqual(result["spot_proxy"]["strategy"]["id"], "hour_close_boundary_uncertain_v2")
        self.assertEqual(position["exit_at"], "2026-08-31T09:15:00+05:30")
        self.assertEqual(position["spot_points_proxy"], 1000)

    def test_background_failure_does_not_publish_partial_download_as_success(self):
        calls = []
        def history(token, start, end):
            calls.append(start)
            if len(calls) == 2:
                raise KiteNetworkError("Test download interrupted.")
            return []
        self.state.client.historical_minutes = history
        job = self.state.start_replay_job({"start_date": "2021-09-29"})
        finished = self.wait_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertNotIn("result", finished)
        self.assertEqual(len(calls), 2)

    def test_cancel_and_duplicate_start_do_not_launch_extra_downloads(self):
        entered, release = Event(), Event()
        calls = []
        def history(token, start, end):
            calls.append(start)
            entered.set()
            release.wait(3)
            return []
        self.state.client.historical_minutes = history
        job = self.state.start_replay_job({"start_date": "2021-09-29"})
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(ValueError, "already running"):
                self.state.start_replay_job({"start_date": "2021-09-29"})
            self.assertEqual(self.state.replay_job_status()["id"], job["id"])
            self.assertTrue(self.state.cancel_replay_job(job["id"])["cancel_requested"])
        finally:
            release.set()
        finished = self.wait_job(job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertNotIn("result", finished)
        self.assertEqual(len(calls), 1)

    def test_background_result_can_be_recovered_after_browser_refresh(self):
        self.state.client.historical_minutes = lambda *args: []
        started = self.state.start_replay_job({"start_date": "2026-09-01"})
        finished = self.wait_job(started["id"])
        restored = self.state.replay_job_status()
        self.assertEqual(restored["id"], finished["id"])
        self.assertEqual(restored["status"], "complete")
        self.assertEqual(restored["result"]["range"]["end_date"], "2026-09-29")
        self.assertNotIn("client", restored)
        self.assertNotIn("api_secret", restored)

    def test_app_automatically_uses_saved_option_prices(self):
        import tempfile
        from pathlib import Path
        from option_prices import OptionPriceArchive
        from tests.test_option_prices import contract,candle
        with tempfile.TemporaryDirectory() as folder:
            self.state.option_archive=OptionPriceArchive(Path(folder)/"options.db")
            self.state.option_archive.record_candles(contract(),[candle("10:15",950),candle("10:25",700,710)],"synthetic_test")
            rows=make_day(date(2026,9,24),{0:"bull"}); rows[70]["high"]=54700
            for day in (25,28,29): rows.extend(make_day(date(2026,9,day)))
            self.state.client.historical_minutes=lambda *args:rows
            result=self.state.replay_spot_range({"start_date":"2026-09-24"})
            p=result["option_model"]["ledgers"]["selling"][0]
            self.assertEqual(p["pnl_basis"],"recorded")
            self.assertEqual((p["entry_premium"],p["exit_premium"]),(950,710))
            self.assertEqual(result["option_model"]["pricing_policy"],"recorded_first_estimate_fallback_v1")


if __name__ == "__main__":
    unittest.main()
