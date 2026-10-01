"""Inclusive past-date BANKNIFTY spot-signal summaries, never option trades.

This module only groups already-fetched spot minute candles. The app opts into
independent complete-hour checks against the historical session calendar.
Missing minutes and empty days are never silently called "no signal".
"""

from collections import defaultdict
from datetime import date, timedelta
import re

from replay import ReplayDataError, _aware_time, _minute_stamp, replay_signals
from market_sessions import session_windows
from backtest_calendar import HOLIDAY_DATES
from strategy_config import REVIEW, strategy_config


MAX_HISTORY_YEARS = 5
HISTORY_BATCH_DAYS = 59


def earliest_replay_date(end_date):
    """Five calendar years, clamping 29 February to 28 February."""
    end = _chosen_date(end_date, "End date")
    try:
        return end.replace(year=end.year - MAX_HISTORY_YEARS)
    except ValueError:
        return end.replace(year=end.year - MAX_HISTORY_YEARS, day=28)


def history_windows(start_date, end_date):
    """Inclusive, non-overlapping batches below Kite's 60-day minute limit."""
    start = _chosen_date(start_date, "Start date")
    end = _chosen_date(end_date, "End date")
    if start > end:
        raise ValueError("End date must be on or after start date.")
    if start < earliest_replay_date(end):
        raise ValueError("Choose a date range of up to five years.")
    while start <= end:
        finish = min(start + timedelta(days=HISTORY_BATCH_DAYS - 1), end)
        yield start, finish
        start = finish + timedelta(days=1)


def _chosen_date(value, label):
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError(f"{label} must be a date in YYYY-MM-DD format.")
        try:
            value = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be a date in YYYY-MM-DD format.") from exc
    if type(value) is not date:
        raise ValueError(f"{label} must be a date in YYYY-MM-DD format.")
    return value


def replay_spot_range(candles, start_date, end_date, observed_until, *, empty_days_are_holidays=False, complete_hours_only=False, ignore_missing_minutes=False, strategy_mode=REVIEW):
    """Return status and exact completed-hour spot signals for each chosen day.

    The dates are inclusive and limited to five calendar years. ``candles`` is an
    iterable of KiteClient historical-minute dicts for BANKNIFTY *spot*, with
    aware ``timestamp_ist`` values. The caller must verify that instrument;
    this function cannot infer it from prices. ``observed_until`` is an aware
    time and the range cannot extend beyond that IST date. A weekday with no
    candles is ``no_price_data`` by default. The app opts into
    ``empty_days_are_holidays`` only after successful provider downloads; such
    dates are labelled assumed holidays, not independently verified closures.
    With ``complete_hours_only=True``, known special-session windows are used;
    each full hour with 60 unique consecutive minutes contributes signals.
    Partial days retain those signals as ``partial_hours`` while position
    monitoring remains unverified. Unknown special dates can show observed
    full hours but cannot certify their trading schedule. Otherwise the legacy
    six-hour weekday validation is used. Invalid data gives ``incomplete``.
    ``ignore_missing_minutes`` keeps missing-minute dates eligible for position
    simulation, with explicit approximate-result metadata. It does not relax
    full-hour entry validation or ignore duplicate/invalid prices.

    No option prices, positions, fills, targets, stops, or P&L are calculated.
    """
    rules = strategy_config(strategy_mode)
    if ignore_missing_minutes and not complete_hours_only:
        raise ValueError("Skipping missing prices requires independent complete-hour checks.")
    start = _chosen_date(start_date, "Start date")
    end = _chosen_date(end_date, "End date")
    if end < start:
        raise ValueError("End date must be on or after start date.")
    calendar_days = (end - start).days + 1
    if start < earliest_replay_date(end):
        raise ValueError("Choose a date range of up to five years.")
    cutoff = _aware_time(observed_until, "Observed-until")
    if end > cutoff.date():
        raise ValueError("The selected date range cannot include a future IST date.")
    if isinstance(candles, (str, bytes, dict)):
        raise ValueError("Supply an iterable of minute candle objects.")
    try:
        source = iter(candles)
    except TypeError as exc:
        raise ValueError("Supply an iterable of minute candle objects.") from exc

    by_day = defaultdict(list)
    ignored_outside_range = 0
    for index, candle in enumerate(source):
        stamp = _minute_stamp(candle, index)
        if stamp > cutoff:
            raise ValueError(f"Candle {index} is after the observed-until time.")
        if start <= stamp.date() <= end:
            by_day[stamp.date()].append(candle)
        else:
            ignored_outside_range += 1

    days = []
    total_signals = 0
    counts = {"complete": 0, "incomplete": 0, "no_price_data": 0, "weekend": 0}
    counts["holiday"] = 0
    if complete_hours_only:
        counts["partial_hours"] = 0
    for offset in range(calendar_days):
        chosen = start + timedelta(days=offset)
        day_candles = by_day[chosen]
        basic = {"date": chosen.isoformat(), "received_candle_count": len(day_candles)}
        if chosen.weekday() >= 5 and (not complete_hours_only or not day_candles and not session_windows(chosen)):
            day = {
                **basic,
                "status": "weekend",
                "reason": "Weekend; no configured trading session or supplied prices." if complete_hours_only else "Weekend; special exchange sessions are not checked by this calendar.",
            }
        elif not day_candles and chosen in HOLIDAY_DATES and not session_windows(chosen):
            day = {**basic, "status": "holiday", "assumed_holiday": False,
                   "verified_closed_date": "Closure in the configured exchange calendar",
                   "reason": "No scheduled session in the configured exchange calendar."}
        elif not day_candles:
            day = {
                **basic,
                "status": "holiday" if empty_days_are_holidays else "no_price_data",
                "assumed_holiday": empty_days_are_holidays,
                "reason": "Assumed holiday under your empty-day rule: Zerodha supplied no candles for the entire day. Not independently verified." if empty_days_are_holidays else "No spot minute prices were supplied; this does not establish a holiday or a no-signal day.",
            }
        else:
            try:
                replay = replay_signals(day_candles, chosen, cutoff, complete_hours_only=complete_hours_only, strategy_mode=strategy_mode)
            except ReplayDataError as exc:
                day = {
                    **basic,
                    "status": "incomplete",
                    "reason": str(exc),
                    "minute_count": exc.minute_count,
                    "missing_minutes": exc.missing_minutes,
                    "duplicate_minutes": exc.duplicate_minutes,
                    "missing_times": exc.missing_times,
                    "duplicate_times": exc.duplicate_times,
                }
            except ValueError as exc:
                day = {
                    **basic,
                    "status": "incomplete",
                    "reason": f"Invalid spot minute prices: {exc}",
                    "minute_count": None,
                    "missing_minutes": None,
                    "duplicate_minutes": None,
                    "missing_times": [],
                    "duplicate_times": [],
                }
            else:
                day = {
                    **basic,
                    "status": "complete",
                    "minute_count": replay["minute_count"],
                    "completed_hour_count": replay["completed_hour_count"],
                    "signal_count": len(replay["signals"]),
                    "signals": replay["signals"],
                    "bars": replay["bars"],
                }
                if complete_hours_only:
                    for key in ("complete_hours_only", "session_verified", "special_session", "session_windows", "session_calendar_version", "expected_hour_count", "expected_session_minutes", "ignored_bars", "missing_minutes", "duplicate_minutes", "missing_times", "duplicate_times"):
                        day[key] = replay[key]
                    if replay["missing_minutes"] or replay["duplicate_minutes"] or not replay["session_verified"] or any(bar["reason"] == "review_not_due" for bar in replay["ignored_bars"]):
                        day["status"] = "partial_hours"
                        day["reason"] = "Only complete 60-minute candles were checked for entries. Missing/duplicate minutes or unverified session hours still prevent complete position tracking."
                        if ignore_missing_minutes and replay["session_verified"] and not replay["duplicate_minutes"] and not any(bar["reason"] == "review_not_due" for bar in replay["ignored_bars"]):
                            day["missing_minutes_ignored"] = True
                            day["reason"] = "Missing minutes were skipped under your backtest rule. Only complete 60-minute candles can open trades. Stops, targets and hedge triggers during missing minutes cannot be checked; results are approximate."
                    else:
                        day["reason"] = "Every complete 60-minute candle was checked. Short session fragments are ignored for entries but remain available for position monitoring."
                total_signals += day["signal_count"]
        counts[day["status"]] += 1
        days.append(day)

    return {
        "mode": "past_spot_signals_range_only",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "observed_until": cutoff.isoformat(),
        "complete_hours_only": complete_hours_only,
        "strategy": rules,
        "assumed_holidays": [day["date"] for day in days if day.get("assumed_holiday")],
        "approximate_due_to_assumed_holidays": any(day.get("assumed_holiday") for day in days),
        "missing_minute_policy": "skip" if ignore_missing_minutes else "stop",
        "calendar_day_count": calendar_days,
        "status_counts": counts,
        "total_signals": total_signals,
        "days": days,
        "ignored_candles_outside_range": ignored_outside_range,
        "warning": "Spot signals and completed hourly spot bars only. Known session hours are configured separately; signals alone are not trades, fills or P&L." if complete_hours_only else "Spot signals and completed hourly spot bars only. No exchange calendar, option trades, fills, or P&L are inferred.",
    }
