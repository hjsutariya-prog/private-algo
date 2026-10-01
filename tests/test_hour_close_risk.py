"""Hourly-close risk anchors must never be replaced by execution prices."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import unittest

from app import KiteState, spot_proxy_for_range
from engine import IST
from option_model import simulate_options
from range_spot import replay_spot_range
from replay import replay_signals
from spot_proxy import build_spot_proxy_ledger, SpotProxyDataError
from strategy_config import CLOSE_BOUNDARY, CLOSE_REVIEW, COMPARISON, REVIEW, strategy_config
from tests.test_spot_proxy import make_day
from tests.test_special_sessions import special_rows

DAY = date(2026,9,24)


def run(rows, mode=CLOSE_BOUNDARY):
    first, last = rows[0]["timestamp_ist"][:10], rows[-1]["timestamp_ist"][:10]
    checked = replay_spot_range(rows,first,last,datetime(2026,9,30,23,tzinfo=IST),
                               complete_hours_only=True,empty_days_are_holidays=True,ignore_missing_minutes=True,strategy_mode=mode)
    return spot_proxy_for_range(rows,checked),checked


class HourCloseRiskTests(unittest.TestCase):
    def test_both_timings_share_close_reference_despite_different_execution(self):
        rows = make_day(DAY,{0:"bull"})  # Hour close 53,700, not boundary open 54,000.
        rows[61].update(open=54100,high=54100,low=54100,close=54100)
        boundary,_ = run(rows,CLOSE_BOUNDARY)
        review,_ = run(rows,CLOSE_REVIEW)
        for output in (boundary,review):
            p = output["positions"][0]
            self.assertEqual((p["risk_reference_spot"],p["stop_spot"],p["target_spot"]),(53700,53200,54700))
            self.assertEqual(p["risk_reference_at"],"2026-09-24T10:15:00+05:30")
            self.assertEqual(p["risk_reference_basis"],"completed_signal_hour_close")
        self.assertEqual(boundary["positions"][0]["entry_spot"],54000)
        self.assertEqual(review["positions"][0]["entry_spot"],54100)

    def test_1015_means_hour_ending_1015_not_minute_ending_1016(self):
        rows = make_day(DAY,{0:"bull"})
        rows[60].update(open=54000,high=54600,low=54000,close=54600)
        p = run(rows,CLOSE_REVIEW)[0]["positions"][0]
        self.assertEqual(p["risk_reference_spot"],rows[59]["close"])
        self.assertNotEqual(p["risk_reference_spot"],rows[60]["close"])

    def test_bear_uses_close_plus_stop_minus_target(self):
        p = run(make_day(DAY,{0:"bear"}),CLOSE_REVIEW)[0]["positions"][0]
        self.assertEqual((p["risk_reference_spot"],p["stop_spot"],p["target_spot"]),(54300,54800,53300))

    def test_all_six_hour_boundaries_have_correct_reference(self):
        for slot in range(6):
            with self.subTest(slot=slot):
                p = run(make_day(DAY,{slot:"bull"}))[0]["positions"][0]
                self.assertEqual(p["risk_reference_at"],f"2026-09-24T{10+slot:02d}:15:00+05:30")
                self.assertEqual(p["risk_reference_spot"],53700)

    def test_reference_does_not_trail_on_later_hour_close(self):
        rows = make_day(DAY,{0:"bull",1:"bull"})
        rows[60].update(open=54000,high=54000,low=53500,close=54000)
        rows[119].update(open=54500,high=54500,low=54500,close=54500)
        spot,_ = run(rows)
        self.assertEqual(len(spot["positions"]),1)
        self.assertEqual(spot["positions"][0]["stop_spot"],53200)
        self.assertTrue(any(e.get("reason")=="side_already_open" for e in spot["events"]))

    def test_target_points_are_measured_from_actual_entry_for_pnl(self):
        rows = make_day(DAY,{0:"bull"})
        rows[70].update(high=54700)
        p = run(rows)[0]["positions"][0]
        self.assertEqual((p["exit_reason"],p["exit_spot"],p["spot_points_proxy"]),("target",54700,700))
        # A 1000-point target reference does not promise 1000 points from execution.
        self.assertEqual(p["target_spot"]-p["risk_reference_spot"],1000)

    def test_gap_exit_uses_actual_price_but_keeps_original_stop(self):
        rows = make_day(DAY,{5:"bull"})+make_day(date(2026,9,25))
        rows[375].update(open=53000,high=53000,low=53000,close=53000)
        p = run(rows)[0]["positions"][0]
        self.assertEqual((p["stop_spot"],p["exit_spot"],p["exit_price_basis"]),(53200,53000,"minute_open_gap"))

    def test_entry_at_or_outside_either_threshold_skips_in_both_directions(self):
        for side,prices in (("bull",[53200,53100,54700,54800]),("bear",[54800,54900,53300,53200])):
            for mode,minute in ((CLOSE_BOUNDARY,60),(CLOSE_REVIEW,61)):
                for price in prices:
                    with self.subTest(side=side,mode=mode,price=price):
                        rows=make_day(DAY,{0:side})
                        rows[minute].update(open=price,high=price,low=price,close=price)
                        spot,_=run(rows,mode)
                        first=[e for e in spot["events"] if e.get("signal_at")==f"2026-09-24T10:15:{'00' if minute==60 else '20'}+05:30"]
                        self.assertEqual(first[0]["type"],"signal_skipped")
                        self.assertEqual(first[0]["reason"],"entry_outside_hour_close_risk")

    def test_missing_hour_close_never_substitutes_execution_price(self):
        rows = make_day(DAY,{0:"bull"})
        del rows[59]
        spot,checked=run(rows)
        self.assertFalse(any(e["type"]=="proxy_entry" and e["at"].startswith("2026-09-24T10:") for e in spot["events"]))
        self.assertEqual(checked["days"][0]["ignored_bars"][0]["reason"],"missing_or_duplicate_minutes")

    def test_tampered_signal_close_is_rejected(self):
        rows=make_day(DAY,{0:"bull"})
        replay=replay_signals(rows,DAY,datetime(2026,9,25,tzinfo=IST),complete_hours_only=True,strategy_mode=CLOSE_BOUNDARY)
        replay["signals"][0]["risk_reference_spot"]=54000
        with self.assertRaises(SpotProxyDataError): build_spot_proxy_ledger([{"candles":rows,"replay":replay}])

    def test_option_legs_keep_parent_risk_through_rollover(self):
        rows=make_day(date(2026,9,28),{0:"bull"})
        rows[360].update(open=54400,high=54700,low=54400,close=54700)
        spot,_=run(rows)
        options=simulate_options(rows,spot)
        self.assertEqual(options["status"],"complete",options["blocked_from"])
        legs=options["ledgers"]["selling"]
        self.assertEqual(len(legs),2)
        self.assertEqual([p["entry_spot"] for p in legs],[54000,54400])
        self.assertTrue(all((p["risk_reference_spot"],p["stop_spot"],p["target_spot"])==(53700,53200,54700) for p in legs))
        self.assertEqual(legs[-1]["exit_reason"],"target")
        self.assertEqual(legs[-1]["exit_spot"],54700)

    def test_option_adapter_rejects_reanchored_or_mismatched_risk(self):
        rows=make_day(DAY,{0:"bull"})
        original,_=run(rows)
        for key,value in (("risk_reference_spot",54000),("risk_reference_at","2026-09-24T10:16:00+05:30"),("stop_spot",53500),("target_spot",55000)):
            spot=deepcopy(original)
            spot["events"][0][key]=value
            result=simulate_options(rows,spot)
            self.assertEqual(result["status"],"unavailable")
            self.assertEqual(result["orders"],[])
        spot=deepcopy(original)
        spot["positions"][0]["stop_spot"]=53000
        self.assertEqual(simulate_options(rows,spot)["status"],"unavailable")

    def test_closed_split_session_signal_is_still_skipped(self):
        day=date(2024,5,18)
        for mode in (CLOSE_BOUNDARY,CLOSE_REVIEW):
            spot,checked=run(special_rows(day,signal=True),mode)
            self.assertEqual(spot["positions"],[])
            self.assertEqual(spot["events"][0]["reason"],"market_closed_at_entry")
            self.assertEqual(checked["days"][0]["signals"][0]["risk_reference_at"],"2024-05-18T12:30:00+05:30")

    def test_legacy_modes_remain_explicit_and_not_allowed_for_new_api_runs(self):
        for mode in (COMPARISON,REVIEW):
            self.assertEqual(strategy_config(mode)["risk_reference_basis"],"execution_entry_spot")
            with self.assertRaisesRegex(ValueError,"Legacy"):
                KiteState.replay_dates({"start_date":"2026-09-01","strategy_mode":mode})
        self.assertNotEqual(CLOSE_BOUNDARY,COMPARISON)


if __name__=="__main__": unittest.main()
