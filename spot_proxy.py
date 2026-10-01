"""A BANKNIFTY spot-point proxy ledger, never an option trade or cash P&L.

The caller supplies validated, chronological scheduled-session minute candles and
the matching validated spot-signal replay for each day. No broker orders,
option prices, fills, charges, margin, or hedge values are inferred here.
"""

from collections import defaultdict
from strategy_config import REVIEW, COMPARISON, strategy_config
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from engine import IST, _price
from replay import ReplayDataError, _minute_prices, _minute_stamp, replay_signals
from market_sessions import session_minutes, session_windows, VERSION as SESSION_VERSION


_OPEN = time(9, 15)
_SESSION_MINUTES = 375
_STOP_POINTS = Decimal("500")
_TARGET_POINTS = Decimal("1000")


class SpotProxyDataError(ReplayDataError):
    """Incomplete or inconsistent data; do not display a partial proxy result."""


def _date(value, label):
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be YYYY-MM-DD.") from exc
    if type(value) is not date:
        raise ValueError(f"{label} must be YYYY-MM-DD.")
    return value


def _number(value):
    return float(value)


def _points(side, entry, spot):
    return spot - entry if side == "bull" else entry - spot


def _thresholds(side, entry):
    if side == "bull":
        return entry - _STOP_POINTS, entry + _TARGET_POINTS
    return entry + _STOP_POINTS, entry - _TARGET_POINTS


def validate_spot_proxy_day(day_input, *, ignore_missing_minutes=False):
    if not isinstance(day_input, dict) or "candles" not in day_input or "replay" not in day_input:
        raise ValueError("Each day needs candles and its matching spot-signal replay.")
    replay = day_input["replay"]
    if not isinstance(replay, dict) or replay.get("mode") != "past_spot_signals_only":
        raise ValueError("Each day needs a validated past spot-signal replay.")
    day = _date(replay.get("session_date"), "Replay date")
    calendar_mode = replay.get("complete_hours_only") is True
    if day.weekday() >= 5 and not calendar_mode:
        raise SpotProxyDataError(f"{day.isoformat()} is a weekend, not a regular session.")
    candles = day_input["candles"]
    if isinstance(candles, (str, bytes, dict)):
        raise ValueError("Each day needs minute candle objects.")
    try:
        source = iter(candles)
    except TypeError as exc:
        raise ValueError("Each day needs minute candle objects.") from exc
    opening = datetime.combine(day, _OPEN, IST)
    stamps = session_minutes(day) if calendar_mode else [opening + timedelta(minutes=minute) for minute in range(_SESSION_MINUTES)]
    if not stamps:
        raise SpotProxyDataError(f"Session hours for {day.isoformat()} are not configured; open-position monitoring cannot be verified from sparse prices alone.")
    expected = set(stamps)
    grouped = defaultdict(list)
    for index, candle in enumerate(source):
        stamp = _minute_stamp(candle, index)
        if stamp not in expected:
            if calendar_mode and stamp.date() == day:
                continue  # Known pre/post-session prices are not market minutes.
            raise SpotProxyDataError(
                f"Candle {index} is outside the regular session for {day.isoformat()}."
            )
        grouped[stamp].append((_minute_prices(candle, index), candle))
    missing = [stamp for stamp in stamps if not grouped[stamp]]
    duplicates = [stamp for stamp in stamps if len(grouped[stamp]) > 1]
    duplicate_count = sum(len(grouped[stamp]) - 1 for stamp in duplicates)
    if duplicates or (missing and not ignore_missing_minutes):
        raise SpotProxyDataError(
            f"Cannot calculate a spot-point proxy for {day.isoformat()}: "
            f"{len(missing)} missing and {duplicate_count} duplicate scheduled session minutes.",
            minute_count=sum(len(grouped[stamp]) for stamp in stamps),
            missing_minutes=len(missing),
            duplicate_minutes=duplicate_count,
            missing_times=[stamp.isoformat() for stamp in missing],
            duplicate_times=[stamp.isoformat() for stamp in duplicates],
        )
    review_cutoff = stamps[-1] + timedelta(minutes=1, seconds=20)
    if ignore_missing_minutes:
        stamps = [stamp for stamp in stamps if grouped[stamp]]
    if not stamps:
        raise SpotProxyDataError(f"No available session prices for {day.isoformat()}.")
    ordered = [grouped[stamp][0] for stamp in stamps]
    full_candles = [item[1] for item in ordered]
    canonical = replay_signals(full_candles, day, review_cutoff, complete_hours_only=calendar_mode,
                               strategy_mode=replay.get("strategy", {}).get("id", REVIEW))
    keys = ("session_date", "minute_count", "completed_hour_count", "bars", "signals")
    if any(replay.get(key) != canonical[key] for key in keys):
        raise SpotProxyDataError(
            f"Spot signals for {day.isoformat()} do not match its minute prices; no proxy was calculated."
        )
    return day, stamps, [item[0] for item in ordered], canonical["signals"]


def _close(position, *, stamp, spot, reason, price_basis, events, last_exit_bound,
           both_touched=False, rules=None):
    side = position["side"]
    valuation_at = stamp + timedelta(minutes=1) if price_basis != "minute_open_gap" and rules["intraminute_valuation"] == "minute_end" else stamp
    position.update(status="closed", exit_at=stamp.isoformat(), exit_valuation_at=valuation_at.isoformat(), exit_spot=_number(spot),
                    exit_reason=reason, exit_price_basis=price_basis,
                    spot_points_proxy=_number(_points(side, Decimal(str(position["entry_spot"])), spot)))
    events.append({
        "type": "proxy_exit", "position_id": position["position_id"],
        "side": side, "at": stamp.isoformat(), "spot": _number(spot),
        "reason": reason, "price_basis": price_basis,
        "timestamp_precision": "minute_open" if price_basis == "minute_open_gap" else "minute_bar",
        "both_stop_and_target_touched": both_touched,
        "valuation_at": valuation_at.isoformat(),
        "spot_points_proxy": position["spot_points_proxy"],
    })
    last_exit_bound[side] = stamp if price_basis == "minute_open_gap" else stamp + timedelta(minutes=1)


def build_spot_proxy_ledger(day_inputs, *, initial_positions=None, non_trading_dates=(), ignore_missing_minutes=False):
    """Return directional spot-point proxies across complete trading sessions.

    ``day_inputs`` is an ascending iterable of ``{"candles": ..., "replay": ...}``
    objects. Each day needs all scheduled BANKNIFTY spot minute candles exactly
    once, including short periods not used for entry candles. Replays with
    ``complete_hours_only`` use the configured historical session windows;
    legacy callers require 375 minutes, 09:15–15:29 IST. Its ``replay`` must
    match a fresh computation. Missing scheduled dates must be supplied in
    ``non_trading_dates``; the caller is responsible for the closure policy.
    ``ignore_missing_minutes=True`` skips absent candles, reports the exact gaps
    and resumes monitoring at available prices, producing approximate results.

    ``initial_positions`` defaults to flat. To explicitly carry in a prior
    directional exposure, pass e.g. ``{"bull": 54000.0}`` (one per side). This
    supplies only an entry spot, never an option contract or verified fill.
    Bull is the intended short-PE direction, bear the intended short-CE
    direction. Stops are adverse 500 spot points; targets favourable 1,000.
    The replay's named strategy controls boundary versus reviewed entries and
    uncertain versus assumed-stop-first collisions. Timing modes cannot mix.
    A signal without an entry minute in that session is skipped, never carried.
    At a new session's open, gap exits use that minute's actual open.
    Open positions carry overnight and are marked at the final minute close.
    Neither a signed spot-point value nor its total is option or rupee P&L.
    """
    if isinstance(day_inputs, (str, bytes, dict)):
        raise ValueError("Supply completed sessions in chronological order.")
    try:
        inputs = list(day_inputs)
    except TypeError as exc:
        raise ValueError("Supply completed sessions in chronological order.") from exc
    if not inputs:
        raise SpotProxyDataError("No completed spot sessions were supplied; no proxy was calculated.")
    modes = {item.get("replay", {}).get("strategy", {}).get("id", REVIEW) for item in inputs}
    if len(modes) != 1:
        raise ValueError("Cannot combine sessions from different strategy timing rules.")
    rules = strategy_config(modes.pop())
    comparison = rules["collision_policy"] == "uncertain"
    hour_close_risk = rules["risk_reference_basis"] == "completed_signal_hour_close"
    if initial_positions is None:
        initial_positions = {}
    if not isinstance(initial_positions, dict) or any(side not in ("bull", "bear") for side in initial_positions):
        raise ValueError("Initial positions must map bull/bear to their entry spot prices.")
    carry_in = {side: _price(spot) for side, spot in initial_positions.items()}
    if isinstance(non_trading_dates, (str, bytes, dict)):
        raise ValueError("Non-trading dates must be supplied as individual dates.")
    try:
        closed_dates = {_date(value, "Non-trading date") for value in non_trading_dates}
    except TypeError as exc:
        raise ValueError("Non-trading dates must be supplied as individual dates.") from exc
    calendar_mode = any(item.get("replay", {}).get("complete_hours_only") for item in inputs)
    if not calendar_mode and any(day.weekday() >= 5 for day in closed_dates):
        raise ValueError("Weekends need not be listed as non-trading dates.")

    if ignore_missing_minutes and not all(item.get("replay", {}).get("complete_hours_only") is True for item in inputs):
        raise ValueError("Skipping missing prices requires independent complete-hour checks.")
    days = [validate_spot_proxy_day(item, ignore_missing_minutes=ignore_missing_minutes) for item in inputs]
    gaps = []
    if ignore_missing_minutes:
        for day, stamps, _, _ in days:
            present = set(stamps)
            missing = [stamp.isoformat() for stamp in session_minutes(day) if stamp not in present]
            if missing:
                gaps.append({"date": day.isoformat(), "count": len(missing), "missing_times": missing})
    session_dates = [item[0] for item in days]
    if session_dates != sorted(set(session_dates)):
        raise SpotProxyDataError("Sessions must be unique and in chronological order.")
    if closed_dates & set(session_dates):
        raise SpotProxyDataError("A non-trading date was also supplied as a trading session.")
    for previous, following in zip(session_dates, session_dates[1:]):
        candidate = previous + timedelta(days=1)
        while candidate < following:
            scheduled = bool(session_windows(candidate)) if calendar_mode else candidate.weekday() < 5
            if scheduled and candidate not in closed_dates:
                raise SpotProxyDataError(
                    f"No candles for {candidate.isoformat()} and it is not marked as an NSE non-trading day; "
                    "no carry-forward proxy was calculated."
                )
            candidate += timedelta(days=1)

    positions = []
    active = {}
    counters = defaultdict(int)
    events = []
    last_exit_bound = {}
    for side, entry in carry_in.items():
        stop, target = _thresholds(side, entry)
        position = {
            "position_id": f"{side}-initial", "side": side,
            "intended_short_option_side": "PE" if side == "bull" else "CE",
            "source": "explicit_initial_spot_exposure", "status": "open",
            "signal_level": None, "signal_at": None, "entry_at": None,
            "entry_spot": _number(entry), "entry_price_basis": "user_supplied_spot_reference",
            "risk_reference_spot": _number(entry), "risk_reference_at": None,
            "risk_reference_basis": "user_supplied_spot_reference",
            "stop_spot": _number(stop), "target_spot": _number(target),
        }
        positions.append(position)
        active[side] = position

    for day, stamps, prices, signals in days:
        by_entry = defaultdict(list)
        for signal in signals:
            decision = datetime.fromisoformat(signal["scheduled_check_at"])
            entry_stamp = decision.replace(second=0, microsecond=0) + timedelta(minutes=rules["entry_delay_minutes"])
            if entry_stamp not in stamps:
                if calendar_mode:
                    missing_entry = any(start <= entry_stamp < end for start, end in session_windows(day))
                    events.append({"type": "signal_skipped", "side": signal["side"], "signal_at": decision.isoformat(),
                                   "at": decision.isoformat(), "reason": "entry_price_missing" if missing_entry else "market_closed_at_entry", "level": signal["level"]})
                    continue
                raise SpotProxyDataError(
                    f"Signal on {day.isoformat()} has no complete next-minute entry reference."
                )
            by_entry[entry_stamp].append(signal)
        for stamp, (open_raw, high_raw, low_raw, close_raw) in zip(stamps, prices):
            open_, high, low = map(_price, (open_raw, high_raw, low_raw))
            active_before_open = set(active)
            for side in ("bull", "bear"):
                position = active.get(side)
                if position is None:
                    continue
                stop = Decimal(str(position["stop_spot"]))
                target = Decimal(str(position["target_spot"]))
                reason = ("stop" if open_ <= stop else "target" if open_ >= target else None) if side == "bull" else (
                    "stop" if open_ >= stop else "target" if open_ <= target else None)
                if reason:
                    _close(position, stamp=stamp, spot=open_, reason=reason,
                           price_basis="minute_open_gap", events=events,
                           last_exit_bound=last_exit_bound, rules=rules)
                    del active[side]

            for signal in by_entry[stamp]:
                side = signal["side"]
                decision = datetime.fromisoformat(signal["scheduled_check_at"])
                if side in active or comparison and side in active_before_open:
                    events.append({"type": "signal_skipped", "side": side,
                                   "signal_at": decision.isoformat(), "at": stamp.isoformat(),
                                   "reason": "side_already_open", "level": signal["level"]})
                    continue
                if side in last_exit_bound and (decision < last_exit_bound[side] if comparison else decision <= last_exit_bound[side]):
                    events.append({"type": "signal_skipped", "side": side,
                                   "signal_at": decision.isoformat(), "at": stamp.isoformat(),
                                   "reason": "not_provably_after_prior_exit", "level": signal["level"]})
                    continue
                reference = _price(signal["risk_reference_spot"]) if hour_close_risk else open_
                stop, target = _thresholds(side, reference)
                risk = {"risk_reference_spot": _number(reference),
                        "risk_reference_at": signal["bar_end"] if hour_close_risk else stamp.isoformat(),
                        "risk_reference_basis": rules["risk_reference_basis"],
                        "stop_spot": _number(stop), "target_spot": _number(target)}
                if hour_close_risk and not min(stop, target) < open_ < max(stop, target):
                    events.append({"type": "signal_skipped", "side": side, "signal_at": decision.isoformat(),
                                   "at": stamp.isoformat(), "reason": "entry_outside_hour_close_risk", "level": signal["level"],
                                   "entry_spot": _number(open_), **risk})
                    continue
                counters[side] += 1
                position = {
                    "position_id": f"{side}-{counters[side]}", "side": side,
                    "intended_short_option_side": "PE" if side == "bull" else "CE",
                    "source": "completed_hour_spot_signal", "status": "open",
                    "signal_level": signal["level"], "signal_at": decision.isoformat(),
                    "entry_at": stamp.isoformat(), "entry_spot": _number(open_),
                    "entry_price_basis": "completed_hour_boundary_open" if comparison else "next_full_minute_open",
                    **risk,
                }
                positions.append(position)
                active[side] = position
                events.append({"type": "proxy_entry", "position_id": position["position_id"],
                               "side": side, "intended_short_option_side": position["intended_short_option_side"],
                               "signal_level": signal["level"], "signal_at": decision.isoformat(),
                               "at": stamp.isoformat(), "spot": _number(open_),
                               "price_basis": position["entry_price_basis"], **risk})

            for side in ("bull", "bear"):
                position = active.get(side)
                if position is None:
                    continue
                stop = Decimal(str(position["stop_spot"]))
                target = Decimal(str(position["target_spot"]))
                stop_hit = low <= stop if side == "bull" else high >= stop
                target_hit = high >= target if side == "bull" else low <= target
                if stop_hit and target_hit and rules["collision_policy"] == "uncertain":
                    position.update(status="uncertain", exit_at=stamp.isoformat(), exit_spot=None,
                                    exit_reason="stop_and_target_same_minute", spot_points_proxy=None,
                                    exit_valuation_at=(stamp + timedelta(minutes=1)).isoformat())
                    events.append({"type": "proxy_uncertain", "position_id": position["position_id"],
                                   "side": side, "at": stamp.isoformat(), "reason": "stop_and_target_same_minute",
                                   "known_at": (stamp + timedelta(minutes=1)).isoformat()})
                    last_exit_bound[side] = stamp + timedelta(minutes=1)
                    del active[side]
                    continue
                if stop_hit or target_hit:
                    reason = "stop" if stop_hit else "target"
                    _close(position, stamp=stamp, spot=stop if stop_hit else target,
                           reason=reason, price_basis="threshold_touched_within_minute",
                           events=events, last_exit_bound=last_exit_bound,
                           both_touched=stop_hit and target_hit, rules=rules)
                    del active[side]

    final_stamp = days[-1][1][-1] + timedelta(minutes=1)
    final_close = _price(days[-1][2][-1][3])
    for position in active.values():
        position.update(mark_at=final_stamp.isoformat(), mark_spot=_number(final_close),
                        mark_price_basis="last_session_minute_close" if calendar_mode else "last_regular_minute_close",
                        spot_points_proxy=_number(_points(position["side"],
                                                          Decimal(str(position["entry_spot"])), final_close)))
    closed = [position for position in positions if position["status"] == "closed"]
    opened = [position for position in positions if position["status"] == "open"]
    uncertain = [position for position in positions if position["status"] == "uncertain"]
    assumed_collisions = sum(bool(event.get("both_stop_and_target_touched")) for event in events)
    return {
        "mode": "spot_points_proxy_only", "status": "complete",
        "strategy": rules, "uncertain_positions": uncertain,
        "validation_status": "uncertain" if uncertain else "approximate" if gaps or assumed_collisions else "validated",
        "approximate": bool(gaps or uncertain or assumed_collisions), "assumed_stop_first_count": assumed_collisions,
        "starting_state": "explicit_carry_in" if carry_in else "flat",
        "first_session": session_dates[0].isoformat(),
        "last_session": session_dates[-1].isoformat(), "session_count": len(days),
        "regular_minutes_per_session": _SESSION_MINUTES,
        "session_calendar_version": SESSION_VERSION if calendar_mode else None,
        "session_minutes": {day.isoformat(): len(stamps) for day, stamps, _, _ in days},
        "missing_minute_policy": "skip" if ignore_missing_minutes else "stop",
        "skipped_minute_count": sum(gap["count"] for gap in gaps), "data_gaps": gaps,
        "approximate_due_to_missing_minutes": bool(gaps),
        "initial_state": "explicit_carry_in" if carry_in else "assumed_flat_at_first_session",
        "events": events, "positions": positions,
        "closed_positions": closed, "open_positions": opened,
        "closed_spot_points_proxy": _number(sum((Decimal(str(item["spot_points_proxy"])) for item in closed), Decimal(0))),
        "open_spot_points_proxy": _number(sum((Decimal(str(item["spot_points_proxy"])) for item in opened), Decimal(0))),
        "assumptions": [
            "This is BANKNIFTY spot-point direction only; it is not option P&L, rupees, or a broker fill.",
            "No option strike, premium, hedge value, charges, slippage, or margin is estimated.",
            "The start is flat unless initial spot exposures were supplied explicitly.",
            rules["label"],
            ("SL and target remain anchored to the completed signal hour's close, not the execution price or later hourly closes. Entry at or beyond either threshold is skipped."
             if hour_close_risk else "Legacy risk rule: SL and target use execution-entry spot, not the signal-hour close."),
            "Opening gaps exit at the minute open. Intraminute stop/target collisions are " + rules["collision_policy"] + ".",
            "Uncertain exits have no exact price or P&L and are excluded from closed totals; those totals are not the full strategy result.",
            "The exact second and path inside a minute are unknown. Positions carry overnight; no daily square-off is assumed.",
            "A new position after an exit requires a signal provably later than that exit.",
            "Caller-supplied non-trading dates are not independently verified against the NSE calendar.",
        ] + (["Missing prices are skipped, never filled in. Monitoring resumes at the next available minute, so stops, targets or hedge triggers inside gaps may be missed. Entries with a missing scheduled entry price are skipped. Results are approximate."] if ignore_missing_minutes else []),
    }
