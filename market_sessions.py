"""Known BANKNIFTY spot trading windows, with explicit historical exceptions.

Windows are normal market hours, not pre-open/closing-auction or mock sessions.
Sources for exceptions are recorded in BACKTEST_MODEL.md.
"""
from datetime import datetime, time, timedelta

from backtest_calendar import HOLIDAY_DATES
from engine import IST

VERSION = "nse-session-hours-reviewed-2026-09-30-v1"
REGULAR = (("09:15", "15:30"),)
SPECIAL_SESSIONS = {
    "2021-11-04": (("18:15", "19:15"),),
    "2022-10-24": (("18:15", "19:15"),),
    "2023-11-12": (("18:15", "19:15"),),
    "2024-01-20": REGULAR,
    "2024-03-02": (("09:15", "10:00"), ("11:30", "12:30")),
    "2024-05-18": (("09:15", "10:00"), ("11:30", "12:30")),
    "2024-11-01": (("18:00", "19:00"),),
    "2025-02-01": REGULAR,
    "2025-10-21": (("13:45", "14:45"),),
    "2026-02-01": REGULAR,
}


def session_windows(day, assumed_holidays=()):
    if day in assumed_holidays:
        return []
    times = SPECIAL_SESSIONS.get(day.isoformat())
    if times is None:
        if day.weekday() >= 5 or day in HOLIDAY_DATES:
            return []
        times = REGULAR
    return [(datetime.combine(day, time.fromisoformat(start), IST),
             datetime.combine(day, time.fromisoformat(end), IST)) for start, end in times]


def session_minutes(day):
    return [start + timedelta(minutes=minute) for start, end in session_windows(day)
            for minute in range(int((end - start).total_seconds() // 60))]


def next_session_minute(stamp, assumed_holidays=()):
    """Expected next price minute; scheduled breaks are not missing candles."""
    candidate = stamp + timedelta(minutes=1)
    for offset in range(370):
        day = candidate.date() + timedelta(days=offset)
        for start, end in session_windows(day, assumed_holidays):
            if candidate < end:
                return max(candidate, start)
    raise ValueError("No configured next trading session within one year.")


def latest_completed_session(now):
    for offset in range(370):
        day = now.date() - timedelta(days=offset)
        windows = session_windows(day)
        if windows:
            start, end = windows[-1]
            # A full-hour close must also reach its +20-second review time.
            ready = end + timedelta(seconds=20) if (end - start).total_seconds() % 3600 == 0 else end
            if now >= ready:
                return day
    raise ValueError("No completed configured session within one year.")


def download_bounds(first, last):
    opening = session_windows(first)
    closing = session_windows(last)
    return (opening[0][0] if opening else datetime.combine(first, time(0), IST),
            closing[-1][1] - timedelta(minutes=1) if closing else datetime.combine(last, time(23, 59), IST))
