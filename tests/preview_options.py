"""Isolated browser fixture, never a real broker session or user database.

Run from the project: python -m tests.preview_options
"""
from datetime import date, datetime
import argparse
from pathlib import Path
import tempfile

from app import LocalServer, spot_proxy_for_range
from engine import IST
from option_model import simulate_options
from range_spot import replay_spot_range
from store import Store
from tests.test_spot_proxy import make_day
from strategy_config import COMPARISON, CLOSE_REVIEW


def preview_special_result():
    from tests.test_special_sessions import special_rows, check
    special = special_rows(date(2024, 5, 18), signal=True)
    special[10].update(low=53500)
    rows = make_day(date(2024, 5, 17), {5: "bull"}) + special + make_day(date(2024, 5, 21))
    checked, spot, options = check(rows, date(2024, 5, 17), date(2024, 5, 21))
    checked["source"] = "SYNTHETIC SPECIAL-SESSION TEST — invented prices, not Zerodha data"
    checked["checked_at"] = "2024-05-22T00:00:00+05:30"
    return {"range": checked, "spot_proxy": spot, "option_model": options}


def preview_result():
    rows = make_day(date(2026, 9, 24), {0: "bull", 2: "bear"})
    rows[70].update(high=55000)  # Target exit and call hedge replacement.
    rows += make_day(date(2026, 9, 25))
    rows += make_day(date(2026, 9, 28))
    checked = replay_spot_range(rows, "2026-09-24", "2026-09-29", datetime(2026, 9, 30, tzinfo=IST), empty_days_are_holidays=True)
    checked["source"] = "SYNTHETIC UI TEST ONLY — invented prices, not Zerodha data"
    checked["checked_at"] = "2026-09-30T00:00:00+05:30"
    proxy = spot_proxy_for_range(rows, checked)
    model = simulate_options(rows, proxy, assumed_holidays={date(2026, 9, 29)})
    assert model["status"] == "complete", model["blocked_from"]
    return {"range": checked, "spot_proxy": proxy, "option_model": model}


def preview_gap_result():
    from tests.test_missing_minute_policy import run_backtest
    first, last = date(2025, 12, 15), date(2025, 12, 16)
    rows = make_day(first, {0: "bull"})
    del rows[360]
    checked, spot, model = run_backtest(rows + make_day(last), first, last)
    checked["source"] = "SYNTHETIC MISSING-MINUTE TEST — invented prices, not Zerodha data"
    return {"range": checked, "spot_proxy": spot, "option_model": model}


def preview_hour_close_result():
    rows = make_day(date(2026, 9, 24), {0: "bull"})
    rows[61].update(open=54100, high=54100, low=54100, close=54100)
    rows[70].update(high=54700)
    checked = replay_spot_range(rows, "2026-09-24", "2026-09-24", datetime(2026, 9, 30, tzinfo=IST),
                               complete_hours_only=True, strategy_mode=CLOSE_REVIEW)
    checked["source"] = "SYNTHETIC HOUR-CLOSE TEST — invented prices, not Zerodha data"
    spot = spot_proxy_for_range(rows, checked)
    options = simulate_options(rows, spot)
    assert options["status"] == "complete", options["blocked_from"]
    position = spot["positions"][0]
    assert (position["risk_reference_spot"], position["entry_spot"], position["stop_spot"], position["target_spot"]) == (53700, 54100, 53200, 54700)
    assert position["spot_points_proxy"] == 600
    return {"range": checked, "spot_proxy": spot, "option_model": options}


def preview_validation_result():
    rows = make_day(date(2026,9,24), {0:"bull"}) + make_day(date(2026,9,28))
    rows[375+360]["high"] = 55000
    checked = replay_spot_range(rows, "2026-09-24", "2026-09-28", datetime(2026,9,30,tzinfo=IST),
                               empty_days_are_holidays=True, complete_hours_only=True, ignore_missing_minutes=True, strategy_mode=COMPARISON)
    checked["source"] = "SYNTHETIC VALIDATION TEST — missing whole day and rollover-minute trigger; not Zerodha prices"
    spot = spot_proxy_for_range(rows, checked)
    options = simulate_options(rows, spot)
    assert options["validation_status"] == "approximate"
    assert options["event_validation"]["all_processed"]
    return {"range": checked, "spot_proxy": spot, "option_model": options}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--special", action="store_true")
    parser.add_argument("--gaps", action="store_true")
    parser.add_argument("--validation", action="store_true")
    parser.add_argument("--hour-close", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="option-model-ui-") as folder:
        server = LocalServer(("127.0.0.1", 8766), Store(Path(folder) / "preview.sqlite3"))
        result = preview_hour_close_result() if args.hour_close else preview_validation_result() if args.validation else preview_gap_result() if args.gaps else preview_special_result() if args.special else preview_result()
        server.kite.replay_job = {"id": "synthetic-option-preview", "status": "complete", "start_date": result["range"]["start_date"], "end_date": result["range"]["end_date"], "result": result}
        print("SYNTHETIC preview only: http://127.0.0.1:8766/#past-data-check", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
