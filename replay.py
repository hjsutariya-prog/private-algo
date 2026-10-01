"""Deterministic BANKNIFTY *spot signal* replay; no trades or option prices.

The caller must prove that the candles came from the BANKNIFTY spot instrument.
The app checks complete hours against configured historical session windows.
The default legacy mode still requires six regular-session hours.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from engine import IST, strict_signal
from market_sessions import session_windows, SPECIAL_SESSIONS, VERSION as SESSION_VERSION


_SESSION_OPEN = time(9, 15)
_FULL_HOURS = 6  # 09:15–15:15; the remaining 15 minutes are not an hourly bar.
_BAR_LENGTH = timedelta(minutes=60)
_DECISION_DELAY = timedelta(seconds=20)
from strategy_config import REVIEW, strategy_config
_REQUIRED_MINUTES = _FULL_HOURS * 60  # 09:15 through 15:14 inclusive.
_REGULAR_SESSION_MINUTES = 375  # 09:15 through 15:29 inclusive.
_DIAGNOSTIC_LIMIT = 10


class ReplayDataError(ValueError):
    """Incomplete/ambiguous past-day data; never interpret as 'no signal'."""

    def __init__(self, message, *, minute_count=0, missing_minutes=0, duplicate_minutes=0,
                 missing_times=None, duplicate_times=None):
        super().__init__(message)
        self.minute_count = minute_count
        self.missing_minutes = missing_minutes
        self.duplicate_minutes = duplicate_minutes
        self.missing_times = list(missing_times or ())[:_DIAGNOSTIC_LIMIT]
        self.duplicate_times = list(duplicate_times or ())[:_DIAGNOSTIC_LIMIT]


def _aware_time(value, label):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO timestamp with a timezone.") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timestamp with a timezone.")
    return value.astimezone(IST)


def _minute_stamp(value, index):
    if not isinstance(value, dict):
        raise ValueError(f"Candle {index} must be an object.")
    if "timestamp_ist" in value and "date" in value:
        raise ValueError(f"Candle {index} has two possible timestamps.")
    stamp = _aware_time(value.get("timestamp_ist", value.get("date")), f"Candle {index} timestamp")
    if stamp.second or stamp.microsecond:
        raise ValueError(f"Candle {index} must start exactly on a minute boundary.")
    return stamp


def _minute_prices(value, index):
    try:
        open_, high, low, close = (value[key] for key in ("open", "high", "low", "close"))
    except KeyError as exc:
        raise ValueError(f"Candle {index} is missing {exc.args[0]}.") from exc
    try:
        # Reuse the strategy's own strict price/coherence checks. Its result is
        # irrelevant here; a minute-level signal is never interpreted as an entry.
        strict_signal(open_, high, low, close)
    except ValueError as exc:
        raise ValueError(f"Candle {index} has invalid spot prices: {exc}") from exc
    return open_, high, low, close


def replay_signals(candles, session_date, observed_until, *, complete_hours_only=False, strategy_mode=REVIEW):
    """Check completed hours of BANKNIFTY spot candles.

    ``complete_hours_only=True`` selects independent full-hour checks against
    the historical session calendar, including special sessions. The following
    describes the legacy default, retained for existing callers/tests.

    ``candles`` is an iterable of KiteClient historical-minute dicts with
    timezone-aware ``timestamp_ist`` (ISO string) and numeric
    ``open/high/low/close``. A ``date`` datetime or ISO string is also accepted
    for direct test fixtures, but a candle cannot contain both time fields.
    ``session_date`` is a ``date`` or YYYY-MM-DD string. ``observed_until`` is
    an aware datetime at/after 15:15:20 IST that day. The 360 minutes
    09:15–15:14 must occur exactly once; any gap or duplicate raises
    ReplayDataError with numeric counts and up to 10 ISO IST timestamps for
    each problem. The 15:15–15:29 fragment is ignored, not required. Weekends
    are rejected. This still cannot verify holidays or special sessions.

    The JSON-compatible result gives 6 completed hours, each anchored at
    09:15 IST, and signals scheduled 20 seconds after the completed hour.
    The warning explicitly says this does not verify the exchange calendar.
    No orders, options, fills, stops, targets, or P&L are simulated.
    """
    rules = strategy_config(strategy_mode)
    delay = timedelta(seconds=rules["decision_delay_seconds"])
    if complete_hours_only:
        return replay_session_signals(candles, session_date, observed_until, strategy_mode=strategy_mode)
    cutoff = _aware_time(observed_until, "Observed-until")
    if isinstance(session_date, str):
        try:
            session_date = date.fromisoformat(session_date)
        except ValueError as exc:
            raise ValueError("Choose a date in YYYY-MM-DD format.") from exc
    if type(session_date) is not date:
        raise ValueError("Choose a date in YYYY-MM-DD format.")
    if session_date.weekday() >= 5:
        raise ReplayDataError("Weekend dates are not regular weekday sessions; no signals were checked.")
    opening = datetime.combine(session_date, _SESSION_OPEN, IST)
    full_hours_end = opening + timedelta(minutes=_REQUIRED_MINUTES)
    regular_session_end = opening + timedelta(minutes=_REGULAR_SESSION_MINUTES)
    if cutoff < full_hours_end + delay:
        raise ReplayDataError("Wait until 15:15:20 IST: all six full hourly bars must finish before checking signals.")
    if isinstance(candles, (str, bytes, dict)):
        raise ValueError("Supply an iterable of minute candle objects.")
    try:
        source = iter(candles)
    except TypeError as exc:
        raise ValueError("Supply an iterable of minute candle objects.") from exc

    minutes = defaultdict(list)
    ignored_outside_chosen_day = 0
    ignored_outside_regular_hours = 0
    ignored_partial_final_segment = 0
    for index, raw in enumerate(source):
        stamp = _minute_stamp(raw, index)
        if stamp > cutoff:
            raise ValueError(f"Candle {index} is after the observed-until time.")
        if stamp.date() != session_date:
            ignored_outside_chosen_day += 1
            continue
        if full_hours_end <= stamp < regular_session_end:
            ignored_partial_final_segment += 1
            continue
        if not opening <= stamp < regular_session_end:
            ignored_outside_regular_hours += 1
            continue
        minutes[stamp].append(_minute_prices(raw, index))

    stamps = [opening + timedelta(minutes=minute) for minute in range(_REQUIRED_MINUTES)]
    entries = [minutes[stamp] for stamp in stamps]
    missing = sum(not entry for entry in entries)
    duplicates = sum(max(0, len(entry) - 1) for entry in entries)
    if missing or duplicates:
        raise ReplayDataError(
            f"Cannot check this day: {missing} missing and {duplicates} duplicate minute candles. No signals were produced.",
            minute_count=sum(map(len, entries)),
            missing_minutes=missing,
            duplicate_minutes=duplicates,
            missing_times=[stamp.isoformat() for stamp, entry in zip(stamps, entries) if not entry][:_DIAGNOSTIC_LIMIT],
            duplicate_times=[stamp.isoformat() for stamp, entry in zip(stamps, entries) if len(entry) > 1][:_DIAGNOSTIC_LIMIT],
        )

    bars = []
    signals = []
    for slot in range(_FULL_HOURS):
        start = opening + slot * _BAR_LENGTH
        end = start + _BAR_LENGTH
        decision = end + delay
        hour = entries[slot * 60:(slot + 1) * 60]
        first, last = hour[0][0], hour[-1][0]
        open_ = first[0]
        high = max(entry[0][1] for entry in hour)
        low = min(entry[0][2] for entry in hour)
        close = last[3]
        signal = strict_signal(open_, high, low, close)
        identity = {
            "bar_start": start.isoformat(),
            "bar_end": end.isoformat(),
            "scheduled_check_at": decision.isoformat(),
        }
        bars.append({
            **identity,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "signal": signal,
        })
        if signal is not None:
            risk = ({"risk_reference_spot": close, "risk_reference_at": end.isoformat(),
                     "risk_reference_basis": "completed_signal_hour_close"}
                    if rules["risk_reference_basis"] == "completed_signal_hour_close" else {})
            signals.append({**identity, **signal, **risk})
    return {
        "mode": "past_spot_signals_only",
        "session_date": session_date.isoformat(),
        "observed_until": cutoff.isoformat(),
        "minute_count": _REQUIRED_MINUTES,
        "completed_hour_count": len(bars),
        "bar_minutes": 60,
        "decision_delay_seconds": rules["decision_delay_seconds"], "strategy": rules,
        "bars": bars,
        "signals": signals,
        "warning": "Six complete weekday hours do not verify the exchange calendar or represent simulated trades. The 15:15–15:29 fragment is not checked.",
        "ignored_candles": {
            "outside_chosen_day": ignored_outside_chosen_day,
            "outside_regular_hours": ignored_outside_regular_hours,
            "partial_final_segment": ignored_partial_final_segment,
        },
    }


def replay_session_signals(candles, session_date, observed_until, *, strategy_mode=REVIEW):
    """Evaluate each full 60-minute window independently, including specials.

    Anchor at each declared session opening. Never combine periods across a
    scheduled break or shift an hour's anchor to conceal a missing minute.
    Position monitoring applies its own explicit missing-minute policy.
    """
    rules = strategy_config(strategy_mode)
    delay = timedelta(seconds=rules["decision_delay_seconds"])
    day = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
    if type(day) is not date:
        raise ValueError("Choose a date in YYYY-MM-DD format.")
    cutoff = _aware_time(observed_until, "Observed-until")
    if isinstance(candles, (str, bytes, dict)):
        raise ValueError("Supply an iterable of minute candle objects.")
    minutes = defaultdict(list)
    outside_day = 0
    for index, raw in enumerate(candles):
        stamp = _minute_stamp(raw, index)
        if stamp > cutoff:
            raise ValueError(f"Candle {index} is after the observed-until time.")
        if stamp.date() != day:
            outside_day += 1
            continue
        minutes[stamp].append(_minute_prices(raw, index))
    windows = session_windows(day)
    verified = bool(windows)
    if not windows and minutes:
        # Show complete hours even on an unconfigured special date, but do not
        # certify its position-monitoring calendar from the observed data alone.
        ordered = sorted(minutes)
        start = previous = ordered[0]
        for stamp in ordered[1:]:
            if stamp != previous + timedelta(minutes=1):
                windows.append((start, previous + timedelta(minutes=1)))
                start = stamp
            previous = stamp
        windows.append((start, previous + timedelta(minutes=1)))
    expected = {start + timedelta(minutes=i) for start, end in windows
                for i in range(int((end - start).total_seconds() // 60))}
    missing = sorted(stamp for stamp in expected if not minutes[stamp])
    duplicates = sorted(stamp for stamp in expected if len(minutes[stamp]) > 1)
    bars, signals, skipped = [], [], []
    hour_count = 0
    for start, end in windows:
        cursor = start
        while cursor < end:
            finish = min(cursor + _BAR_LENGTH, end)
            stamps = [cursor + timedelta(minutes=i) for i in range(int((finish - cursor).total_seconds() // 60))]
            unique_count = sum(len(minutes[stamp]) == 1 for stamp in stamps)
            identity = {"bar_start": cursor.isoformat(), "bar_end": finish.isoformat(),
                        "scheduled_check_at": (finish + delay).isoformat()}
            is_hour = len(stamps) == 60
            hour_count += int(is_hour)
            reason = ("short_session_fragment" if not is_hour else "missing_or_duplicate_minutes" if unique_count != 60
                      else "review_not_due" if finish + delay > cutoff else None)
            if reason:
                skipped.append({**identity, "reason": reason, "received_unique_minutes": unique_count,
                                "scheduled_minutes": len(stamps)})
            else:
                hour = [minutes[stamp][0] for stamp in stamps]
                open_, high, low, close = hour[0][0], max(p[1] for p in hour), min(p[2] for p in hour), hour[-1][3]
                signal = strict_signal(open_, high, low, close)
                bars.append({**identity, "open": open_, "high": high, "low": low, "close": close, "signal": signal})
                if signal is not None:
                    risk = ({"risk_reference_spot": close, "risk_reference_at": finish.isoformat(),
                             "risk_reference_basis": "completed_signal_hour_close"}
                            if rules["risk_reference_basis"] == "completed_signal_hour_close" else {})
                    signals.append({**identity, **signal, **risk})
            cursor = finish
    return {"mode": "past_spot_signals_only", "session_date": day.isoformat(), "observed_until": cutoff.isoformat(),
            "complete_hours_only": True, "session_calendar_version": SESSION_VERSION, "session_verified": verified,
            "special_session": day.isoformat() in SPECIAL_SESSIONS or day.weekday() >= 5 or not verified,
            "session_windows": [{"open": start.isoformat(), "close": end.isoformat()} for start, end in windows],
            "minute_count": len(bars) * 60, "completed_hour_count": len(bars), "expected_hour_count": hour_count,
            "expected_session_minutes": len(expected), "bar_minutes": 60,
            "decision_delay_seconds": rules["decision_delay_seconds"], "strategy": rules,
            "bars": bars, "signals": signals, "ignored_bars": skipped,
            "missing_minutes": len(missing), "duplicate_minutes": sum(len(minutes[s]) - 1 for s in duplicates),
            "missing_times": [s.isoformat() for s in missing[:10]], "duplicate_times": [s.isoformat() for s in duplicates[:10]],
            "warning": "Only 60 unique consecutive minutes form an entry candle. Short or incomplete hours are ignored for entries, not for existing-position monitoring. Session-close signals are skipped if their next-minute entry is outside trading hours.",
            "ignored_candles": {"outside_chosen_day": outside_day, "outside_session_hours": sum(len(values) for stamp, values in minutes.items() if stamp not in expected)}}
