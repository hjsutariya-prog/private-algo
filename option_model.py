"""Reusable spot-to-option simulation. Consumes a strategy ledger; never creates signals.

Recorded option prices can override estimates; neither is a broker fill. Calendar,
fees and instrument parameters are explicit dependencies, reusable by future
strategy adapters without coupling their entry/exit logic to this module.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from backtest_config import decimal, model_config
from backtest_calendar import MonthlyCalendar, HOLIDAY_DATES
from backtest_fees import FeeSchedule, COMPONENTS, rounded
from engine import IST
from market_sessions import session_windows, next_session_minute
from strategy_config import REVIEW, strategy_config
from option_prices import resolve_premium, combined_basis, POLICY


def timestamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("Model timestamps must have a time zone.")
    return result.astimezone(IST)


def movement_contribution(movement, config):
    movement = decimal(movement)
    remaining, result = abs(movement), Decimal(0)
    band, delta, step = (decimal(config[key]) for key in ("band_points", "initial_delta", "delta_step"))
    if band <= 0 or step <= 0 or not 0 <= delta <= 1:
        raise ValueError("Invalid delta band configuration.")
    while remaining:
        portion = min(remaining, band)
        result += portion * delta
        remaining -= portion
        delta = min(Decimal(1), delta + step) if movement >= 0 else max(Decimal(0), delta - step)
        if delta == 0:
            break
        if delta == 1:
            result += remaining
            break
    return result if movement >= 0 else -result


def premium_value(option_type, entry_spot, spot, entry_premium, entry_at, at, daily_decay, config):
    if option_type not in ("CE", "PE"):
        raise ValueError("Option type must be CE or PE.")
    start, finish = timestamp(entry_at), timestamp(at)
    if finish < start or min(decimal(entry_spot), decimal(spot)) <= 0 or decimal(entry_premium) < 0:
        raise ValueError("Invalid option valuation inputs.")
    elapsed = Decimal(str((finish - start).total_seconds())) / 60
    movement = decimal(spot) - decimal(entry_spot)
    if option_type == "PE":
        movement = -movement
    contribution = movement_contribution(movement, config)
    decay = decimal(daily_decay) * elapsed / 1440
    if decay < 0:
        raise ValueError("Time decay cannot be negative.")
    premium = rounded(max(Decimal(0), decimal(entry_premium) + contribution - decay))
    return {"premium": float(premium), "net_spot_movement": float(movement),
            "movement_contribution": float(contribution), "time_decay": float(decay), "elapsed_minutes": float(elapsed)}


def hedge_terms(option_type, spot, config):
    spot, step = decimal(spot), decimal(config["strike_step"])
    premium = (spot / decimal(config["hedge_premium_divisor"])).to_integral_value(rounding=ROUND_FLOOR) * decimal(config["hedge_premium_step"])
    if premium <= 0 or step <= 0:
        raise ValueError("The configured hedge formula needs a positive premium and strike step for this instrument.")
    if option_type == "CE":
        strike = (spot / step).to_integral_value(rounding=ROUND_CEILING) * step + step
    elif option_type == "PE":
        strike = (spot / step).to_integral_value(rounding=ROUND_FLOOR) * step - step
    else:
        raise ValueError("Option type must be CE or PE.")
    if strike <= 0:
        raise ValueError("The hedge strike is not positive under this instrument configuration.")
    distance = premium * decimal(config["replacement_multiple"])
    return {"strike": float(strike), "entry_premium": float(premium), "replacement_distance": float(distance),
            "trigger_spot": float(spot + distance if option_type == "CE" else spot - distance)}


def validate_strategy_events(spot_ledger, rows, calendar_mode):
    """Validate the whole adapter stream before recording any model orders."""
    rules = strategy_config(spot_ledger.get("strategy", {}).get("id", REVIEW))
    row_by_time = {timestamp(row["timestamp_ist"]): row for row in rows}
    stamps = {timestamp(row["timestamp_ist"]) for row in rows
              if not calendar_mode or any(a <= timestamp(row["timestamp_ist"]) < b
                                          for a, b in session_windows(timestamp(row["timestamp_ist"]).date()))}
    active, used, events = {}, {}, []
    previous = None
    for event in spot_ledger.get("events", []):
        if not isinstance(event, dict):
            raise ValueError("Strategy events must be objects.")
        kind = event.get("type")
        if kind == "signal_skipped":
            continue
        if kind not in ("proxy_entry", "proxy_exit", "proxy_uncertain"):
            raise ValueError(f"Unknown strategy event type: {kind}.")
        at = timestamp(event.get("at"))
        if at not in stamps:
            raise ValueError(f"Unmatched strategy event timestamp {at.isoformat()}; an exact supplied session minute is required.")
        parent = event.get("position_id")
        if not isinstance(parent, str) or not parent:
            raise ValueError("Every strategy event needs a position reference.")
        phase = 1 if kind == "proxy_entry" else 0 if event.get("price_basis") == "minute_open_gap" else 2
        key = (at, phase)
        if previous is not None and key < previous:
            raise ValueError("Inconsistent strategy event sequence: timestamps and opening/entry/intraminute phases must be chronological.")
        previous = key
        if kind == "proxy_entry":
            side = event.get("side")
            if side not in ("bull", "bear") or event.get("intended_short_option_side") != ("PE" if side == "bull" else "CE"):
                raise ValueError("Strategy entry direction and option type disagree.")
            if parent in used or side in active.values():
                raise ValueError("Duplicate position reference or more than one active position in a direction.")
            if rules["risk_reference_basis"] == "completed_signal_hour_close":
                reference_at = timestamp(event.get("risk_reference_at"))
                reference = decimal(event.get("risk_reference_spot", 0))
                start = reference_at - timedelta(hours=1)
                window_matches = any(a <= start and reference_at <= b and (start-a).total_seconds() % 3600 == 0
                                     for a, b in session_windows(start.date()))
                if (not window_matches or any(start+timedelta(minutes=i) not in row_by_time for i in range(60))
                        or reference != decimal(row_by_time[reference_at-timedelta(minutes=1)]["close"])
                        or event.get("risk_reference_basis") != "completed_signal_hour_close"
                        or timestamp(event.get("signal_at")) != reference_at+timedelta(seconds=rules["decision_delay_seconds"])
                        or at != reference_at+timedelta(minutes=rules["entry_delay_minutes"])):
                    raise ValueError("Strategy risk reference does not match the complete signal-hour close and entry timing.")
                stop, target = (reference-500, reference+1000) if side == "bull" else (reference+500, reference-1000)
                if decimal(event.get("stop_spot", 0)) != stop or decimal(event.get("target_spot", 0)) != target:
                    raise ValueError("Strategy stop/target must use the original signal-hour close.")
                entry_price = decimal(event.get("spot", 0))
                if entry_price != decimal(row_by_time[at]["open"]) or not min(stop,target) < entry_price < max(stop,target):
                    raise ValueError("Strategy entry must use its actual minute open within the hourly-close stop/target.")
            active[parent] = used[parent] = side
        else:
            if parent not in active:
                raise ValueError(f"Unknown or already closed position reference: {parent}.")
            if event.get("side", active[parent]) != active[parent]:
                raise ValueError("Strategy exit direction disagrees with its entry.")
            del active[parent]
            if kind == "proxy_exit":
                if event.get("both_stop_and_target_touched") and rules["collision_policy"] == "uncertain":
                    raise ValueError("Comparison mode cannot price a stop/target collision as an exact exit.")
                if event.get("price_basis") not in ("minute_open_gap", "threshold_touched_within_minute"):
                    raise ValueError("Strategy exit needs an explicit opening-gap or intraminute price basis.")
                if not event.get("reason"):
                    raise ValueError("Strategy exit needs a reason.")
                valued_at = timestamp(event.get("valuation_at", event["at"]))
                if valued_at < at or valued_at > at + timedelta(minutes=1) or phase == 0 and valued_at != at:
                    raise ValueError("Strategy exit valuation time is inconsistent with its minute phase.")
        if kind != "proxy_uncertain" and decimal(event.get("spot", 0)) <= 0:
            raise ValueError("Strategy events need a positive spot price.")
        events.append(event)
    if "positions" in spot_ledger:
        positions = spot_ledger["positions"]
        ids = [p.get("position_id") for p in positions]
        if len(ids) != len(set(ids)) or set(ids) != set(used):
            raise ValueError("Strategy positions do not match the supplied entry events.")
        for p in positions:
            terminal = next((e for e in reversed(events) if e["position_id"] == p["position_id"]), None)
            expected = {"proxy_entry": "open", "proxy_exit": "closed", "proxy_uncertain": "uncertain"}[terminal["type"]]
            if p.get("status") != expected:
                raise ValueError("Strategy position status does not match its event sequence.")
            if rules["risk_reference_basis"] == "completed_signal_hour_close":
                original = next(e for e in events if e["type"] == "proxy_entry" and e["position_id"] == p["position_id"])
                if any(p.get(key) != original.get(key) for key in ("risk_reference_spot", "risk_reference_at", "risk_reference_basis", "stop_spot", "target_spot")):
                    raise ValueError("Strategy position changed its original hourly-close risk levels.")
    return events


def simulate_options(candles, spot_ledger, *, config=None, calendar=None, fees=None,
                     assumed_holidays=(), check_active=None, option_prices=None):
    config = model_config() if config is None else dict(config)
    assumed_holidays = set(assumed_holidays) | {timestamp(value + "T00:00:00+05:30").date() for value in spot_ledger.get("assumed_holidays", [])}
    calendar = calendar or MonthlyCalendar(config["instrument"], assumed_holidays)
    fees = fees or FeeSchedule()
    check_active = check_active or (lambda: None)
    rules = strategy_config(spot_ledger.get("strategy", {}).get("id", REVIEW))
    calendar_mode = bool(spot_ledger.get("session_calendar_version"))
    ignore_missing_minutes = spot_ledger.get("missing_minute_policy") == "skip"
    delayed_actions = []
    windows_by_day = {}
    ledgers = {"selling": [], "ce_hedge": [], "pe_hedge": []}
    orders, rollovers, pending, selling, hedges = [], [], {}, {}, {}
    # These are end-of-session valuations, not extra closing orders.  They make
    # daily and monthly research views possible without pretending that an open
    # option was sold at the close.
    daily_pnl = []
    blocked = spot_ledger.get("blocked_from")
    available = spot_ledger.get("last_session")
    first_session = spot_ledger.get("first_session")
    # Never use candles beyond the uninterrupted position history supplied by the strategy.
    rows = sorted((row for row in candles if available and (first_session or "0001-01-01") <= timestamp(row["timestamp_ist"]).date().isoformat() <= available),
                  key=lambda row: timestamp(row["timestamp_ist"]))
    available_stamps = {timestamp(row["timestamp_ist"]) for row in rows}
    skipped_stamps = {timestamp(value) for gap in spot_ledger.get("data_gaps", []) for value in gap.get("missing_times", [])}
    entries, exits, uncertain = defaultdict(list), defaultdict(list), defaultdict(list)
    expected_events = sum(e.get("type") in ("proxy_entry", "proxy_exit", "proxy_uncertain") for e in spot_ledger.get("events", []) if isinstance(e, dict))
    processed_events = 0
    sequence, expiry, last_at, last_spot, previous_stamp = 0, None, None, None, None
    parent_risk = {}

    def order(position, side, at, premium, reason, pricing):
        charges = fees.calculate(at, side, premium, position["quantity"])
        record = {"order_id": f"model-order-{len(orders) + 1}", "position_id": position["position_id"],
                  "ledger": position["ledger"], "at": at.isoformat(), "side": side, "premium": float(premium),
                  "quantity": position["quantity"], "lots": position["lots"], "charges": charges, "reason": reason,
                  "pricing": pricing}
        orders.append(record)
        return charges

    def open_position(kind, option_type, at, spot, contract_expiry, reason, parent=None):
        nonlocal sequence
        lots = config["sell_lots"] if kind == "selling" else config[kind + "_lots"]
        if lots == 0:
            return None
        terms = hedge_terms(option_type, spot, config) if kind != "selling" else {"strike": None, "entry_premium": float(decimal(config["sold_entry_premium"]))}
        observed, contract, missing = None, None, "No option-price archive supplied"
        if option_prices is not None:
            if kind == "selling":
                contract, observed = option_prices.select_short(config["instrument"], contract_expiry, option_type, at, float(spot), terms["entry_premium"])
                missing = "No priced ATM/ITM contract at a 500-point strike at entry; contract selection remains modelled"
                if contract:
                    terms["strike"] = contract["strike"]
            else:
                # Hedge contract and trigger rules remain unchanged by P&L data availability.
                key = (config["instrument"], contract_expiry.isoformat(), option_type, terms["strike"])
                contract = dict(option_prices.contracts.get(key, {"instrument": config["instrument"], "expiry": contract_expiry.isoformat(),
                                                                "option_type": option_type, "strike": terms["strike"], "symbol": None}))
                observed, missing = option_prices.lookup(contract, at, side="BUY")
        pricing = resolve_premium(terms["entry_premium"], observed, missing)
        terms["model_entry_premium"] = terms["entry_premium"]
        terms["entry_premium"] = pricing["premium"]
        sequence += 1
        position = {"position_id": f"option-{sequence}", "ledger": kind, "option_type": option_type,
                    "direction": "SELL" if kind == "selling" else "BUY", "parent_spot_trade": parent,
                    "status": "open", "entry_at": at.isoformat(), "entry_spot": float(spot),
                    "expiry": contract_expiry.isoformat(), "lots": lots, "units_per_lot": config["units_per_lot"],
                    "quantity": lots * config["units_per_lot"], "entry_reason": reason, **terms,
                    "contract": contract, "entry_pricing": pricing,
                    "contract_selection": "Closest recorded entry premium among available ATM/ITM 500-point strikes; incomplete chain coverage is possible" if kind == "selling" and contract else "Existing model contract rule"}
        if kind == "selling":
            position.update(parent_risk.get(parent, {}))
        position["entry_charges"] = order(position, position["direction"], at, terms["entry_premium"], reason, pricing)
        ledgers[kind].append(position)
        return position

    def value(position, at, spot, field="close", candle_at=None):
        estimate = premium_value(position["option_type"], position["entry_spot"], spot, position["entry_premium"],
                             position["entry_at"], at, config["sold_daily_decay"] if position["ledger"] == "selling" else config["hedge_daily_decay"], config)
        observation, reason = (None, "No option-price archive supplied") if option_prices is None else option_prices.lookup(
            position["contract"], at, field=field, side="BUY" if position["direction"] == "SELL" else "SELL", candle_at=candle_at)
        pricing = resolve_premium(estimate["premium"], observation, reason)
        return {**estimate, "premium": pricing["premium"], "pricing": pricing,
                "movement_contribution": estimate["movement_contribution"] if observation is None else None,
                "time_decay": estimate["time_decay"] if observation is None else None,
                "model_estimate": estimate}

    def close(position, at, spot, reason, basis, candle_at=None):
        field = "open" if basis in ("minute_open_gap", "scheduled_minute_open", "next_available_minute_open") else "close"
        valuation = value(position, at, spot, field, candle_at)
        premium = decimal(valuation["premium"])
        pnl = decimal(position["entry_premium"]) - premium if position["direction"] == "SELL" else premium - decimal(position["entry_premium"])
        charges = order(position, "BUY" if position["direction"] == "SELL" else "SELL", at, premium, reason, valuation["pricing"])
        total_fees = rounded(decimal(position["entry_charges"]["total"]) + decimal(charges["total"]))
        gross = rounded(pnl * position["quantity"])
        position.update(status="closed", exit_at=at.isoformat(), exit_spot=float(spot), exit_premium=float(premium),
                        exit_reason=reason, exit_price_basis=basis, valuation=valuation,
                        premium_points_pnl=float(pnl), gross_pnl=float(gross), charges=float(total_fees),
                        net_pnl=float(gross - total_fees), exit_charges=charges,
                        exit_pricing=valuation["pricing"], pnl_basis=combined_basis(position["entry_pricing"], valuation["pricing"]))

    def open_mark(position, at, spot, candle_at=None):
        """Return an open leg's mark without creating an exit, order or fee."""
        valuation = value(position, at, spot, candle_at=candle_at)
        pnl = decimal(position["entry_premium"]) - decimal(valuation["premium"]) if position["direction"] == "SELL" else decimal(valuation["premium"]) - decimal(position["entry_premium"])
        gross = rounded(pnl * position["quantity"])
        net = gross - decimal(position["entry_charges"]["total"])
        return valuation, pnl, gross, net

    def snapshot_end_of_session(at, spot, candle_at):
        """Store an end-of-session marked result while leaving all legs open."""
        realised_net, open_net, open_leg_count = Decimal(0), Decimal(0), 0
        for group in ledgers.values():
            for position in group:
                if position["status"] == "closed":
                    realised_net += decimal(position.get("net_pnl", 0))
                    continue
                _, _, _, net = open_mark(position, at, spot, candle_at)
                open_net += net
                open_leg_count += 1
        total_net = rounded(realised_net + open_net)
        previous_total = decimal(daily_pnl[-1]["total_net"]) if daily_pnl else Decimal(0)
        daily_pnl.append({
            "date": at.date().isoformat(), "at": at.isoformat(),
            "realised_net": float(rounded(realised_net)), "open_net": float(rounded(open_net)),
            "total_net": float(total_net), "daily_net": float(rounded(total_net - previous_total)),
            "open_leg_count": open_leg_count,
            "valuation": "end_of_session_mark",
        })

    def next_trading_minute(stamp):
        if calendar_mode:
            return next_session_minute(stamp, assumed_holidays)
        if stamp.time() < time(15, 29):
            return stamp + timedelta(minutes=1)
        day = stamp.date() + timedelta(days=1)
        while day.weekday() >= 5 or day in HOLIDAY_DATES or day in assumed_holidays:
            day += timedelta(days=1)
        return datetime.combine(day, time(9, 15), IST)

    def final_session_minute(stamp):
        if calendar_mode:
            # Split special sessions can have an intraday break.  Only the last
            # configured window is the day's end-of-session mark.
            return stamp == windows_by_day[stamp.date()][-1][1] - timedelta(minutes=1)
        return stamp.time() == time(15, 29)

    try:
        if spot_ledger.get("starting_state") == "explicit_carry_in":
            raise ValueError("Option carry-in requires its own entry premium, time and expiry; spot carry-in alone is insufficient.")
        validated = validate_strategy_events(spot_ledger, rows, calendar_mode)
        parent_risk = {event["position_id"]: {k: event[k] for k in ("risk_reference_spot", "risk_reference_at", "risk_reference_basis", "stop_spot", "target_spot") if k in event}
                       for event in validated if event["type"] == "proxy_entry"}
        expected_events = len(validated)
        for event in validated:
            collection = entries if event["type"] == "proxy_entry" else exits if event["type"] == "proxy_exit" else uncertain
            collection[timestamp(event["at"])].append(event)
        for row in rows:
            stamp = timestamp(row["timestamp_ist"])
            if calendar_mode:
                if stamp.date() not in windows_by_day:
                    windows_by_day[stamp.date()] = session_windows(stamp.date())
                if not any(start <= stamp < end for start, end in windows_by_day[stamp.date()]):
                    continue
            if uncertain[stamp]:
                raise ValueError(f"Uncertain strategy exit at {stamp.isoformat()}: both stop and target touched. No exact option exit or later total was invented.")
            if previous_stamp is None or stamp.date() != previous_stamp.date():
                check_active()
                fees.rates(stamp.date())
            if previous_stamp is not None:
                expected = next_trading_minute(previous_stamp)
                if stamp != expected and not (ignore_missing_minutes and stamp > expected):
                    raise ValueError(f"Missing next model price after {previous_stamp.isoformat()}; no candle or rollover price was substituted.")
                if ignore_missing_minutes:
                    while expected < stamp:
                        skipped_stamps.add(expected)
                        expected = next_trading_minute(expected)
            spot = decimal(row["open"])
            if min(decimal(row[key]) for key in ("open", "high", "low", "close")) <= 0:
                raise ValueError("Option simulation requires positive spot prices.")
            # Validate this minute's new contracts before recording any closing orders.
            for option_type in ("CE", "PE"):
                if config[option_type.lower() + "_hedge_lots"]:
                    hedge_terms(option_type, spot, config)
            if expiry is None:
                expiry = calendar.initial_expiry(stamp)
                rollover_at = calendar.rollover(expiry)
                for option_type in ("CE", "PE"):
                    hedges[option_type] = open_position(option_type.lower() + "_hedge", option_type, stamp, spot, expiry, "initial_hedge")
            rollover_at = calendar.rollover(expiry)
            if stamp > rollover_at and not ignore_missing_minutes:
                raise ValueError(f"Missing scheduled rollover price at {rollover_at.isoformat()}; later prices were not substituted.")
            if stamp.date() > expiry:
                raise ValueError(f"No available rollover price before expiry {expiry.isoformat()}; an expired contract cannot be carried into a later session.")
            is_rollover = stamp >= rollover_at
            next_expiry = calendar.next_expiry(expiry) if is_rollover else None
            if next_expiry:
                calendar.rollover(next_expiry)
            # Only opening-gap exits precede rollover. Later threshold touches
            # cannot cancel a rollover that already happened at the open.
            for event in exits[stamp]:
                if event["price_basis"] == "minute_open_gap":
                    position = selling.pop(event["position_id"])
                    close(position, stamp, decimal(event["spot"]), event["reason"], event["price_basis"])
                    processed_events += 1
            if is_rollover:
                delayed_roll = stamp > rollover_at
                if delayed_roll:
                    delayed_actions.append({"type": "monthly_rollover", "scheduled_at": rollover_at.isoformat(), "at": stamp.isoformat(), "reason": "missing_minutes"})
                roll_basis = "next_available_minute_open" if delayed_roll else "scheduled_minute_open"
                continuing = [(parent, p["option_type"]) for parent, p in selling.items()]
                for p in selling.values():
                    close(p, stamp, spot, "monthly_rollover", roll_basis)
                selling.clear()
                for p in hedges.values():
                    if p:
                        close(p, stamp, spot, "monthly_rollover", roll_basis)
                pending.clear()
                old_expiry, expiry = expiry, next_expiry
                for option_type in ("CE", "PE"):
                    hedges[option_type] = open_position(option_type.lower() + "_hedge", option_type, stamp, spot, expiry, "monthly_rollover")
                for parent, option_type in continuing:
                    selling[parent] = open_position("selling", option_type, stamp, spot, expiry, "monthly_rollover", parent)
                rollovers.append({"at": stamp.isoformat(), "spot": float(spot), "old_expiry": old_expiry.isoformat(),
                                  "new_expiry": expiry.isoformat(), "selling_positions_reopened": len(continuing),
                                  "scheduled_at": rollover_at.isoformat(), "delayed_for_missing_minutes": delayed_roll})
            else:
                for option_type, due in list(pending.items()):
                    if stamp != due and not (ignore_missing_minutes and stamp > due):
                        raise ValueError(f"Missing hedge replacement opening price at {due.isoformat()}.")
                    if stamp > due:
                        delayed_actions.append({"type": "hedge_replacement", "option_type": option_type, "scheduled_at": due.isoformat(), "at": stamp.isoformat(), "reason": "missing_minutes"})
                    hedges[option_type] = open_position(option_type.lower() + "_hedge", option_type, stamp, spot, expiry, "hedge_replacement")
                    del pending[option_type]
            # Entries at this open, then intraminute exits on the current legs.
            for event in entries[stamp]:
                parent = event["position_id"]
                position = open_position("selling", event["intended_short_option_side"], stamp, decimal(event["spot"]), expiry, "spot_entry", parent)
                selling[parent] = position
                processed_events += 1
            for event in exits[stamp]:
                if event["price_basis"] != "minute_open_gap":
                    position = selling.pop(event["position_id"])
                    close(position, timestamp(event.get("valuation_at", event["at"])), decimal(event["spot"]), event["reason"], event["price_basis"], candle_at=stamp)
                    processed_events += 1
            confirmation = stamp + timedelta(minutes=1)
            # A trigger confirmed exactly at rollover yields to the scheduled rollover.
            if is_rollover or confirmation != rollover_at or ignore_missing_minutes and rollover_at not in available_stamps:
                for option_type, position in list(hedges.items()):
                    if position is None:
                        continue
                    trigger = decimal(position["trigger_spot"])
                    hit = decimal(row["high"]) >= trigger if option_type == "CE" else decimal(row["low"]) <= trigger
                    if hit:
                        close(position, confirmation, trigger, "hedge_replacement", "trigger_level_at_completed_candle_simulation", candle_at=stamp)
                        hedges[option_type] = None
                        pending[option_type] = next_trading_minute(stamp)
            last_at, last_spot, previous_stamp = confirmation, decimal(row["close"]), stamp
            if final_session_minute(stamp):
                snapshot_end_of_session(confirmation, last_spot, stamp)
        if processed_events != expected_events:
            raise ValueError(f"Unprocessed strategy events: {expected_events - processed_events}; result cannot be complete.")
    except (ValueError, KeyError, TypeError) as error:
        blocked = {"date": stamp.date().isoformat() if 'stamp' in locals() else None, "reason": str(error)}

    if last_at is not None:
        for group in ledgers.values():
            for p in group:
                if p["status"] != "open":
                    continue
                valuation, pnl, gross, net = open_mark(p, last_at, last_spot, previous_stamp)
                p.update(mark_at=last_at.isoformat(), mark_spot=float(last_spot), mark_premium=valuation["premium"],
                         valuation=valuation, premium_points_pnl=float(pnl), gross_pnl=float(gross),
                         charges=p["entry_charges"]["total"], net_pnl=float(net),
                         mark_pricing=valuation["pricing"], pnl_basis=combined_basis(p["entry_pricing"], valuation["pricing"]))

    def total(items, key):
        return float(rounded(sum((decimal(item.get(key, 0)) for item in items), Decimal(0))))

    summaries = {}
    for kind, positions in ledgers.items():
        closed = [p for p in positions if p["status"] == "closed"]
        opened = [p for p in positions if p["status"] == "open"]
        summaries[kind] = {"closed_count": len(closed), "open_count": len(opened),
                           "realised_gross": total(closed, "gross_pnl"), "realised_charges": total(closed, "charges"),
                           "realised_net": total(closed, "net_pnl"), "open_gross": total(opened, "gross_pnl"),
                           "open_entry_charges": total(opened, "charges"), "open_net": total(opened, "net_pnl")}
    charges = {key: total([o["charges"] for o in orders], key) for key in COMPONENTS}
    charges["total"] = total([o["charges"] for o in orders], "total")
    combined = {key: total(summaries.values(), key) for key in ("realised_gross", "realised_charges", "realised_net", "open_gross", "open_entry_charges", "open_net")}
    combined["total_net"] = float(rounded(decimal(combined["realised_net"]) + decimal(combined["open_net"])))
    status = "unavailable" if last_at is None else "partial" if blocked or spot_ledger.get("status") != "complete" else "complete"
    gaps_by_day = defaultdict(list)
    for missing in sorted(skipped_stamps):
        gaps_by_day[missing.date().isoformat()].append(missing.isoformat())
    data_gaps = [{"date": day, "count": len(missing), "missing_times": missing} for day, missing in gaps_by_day.items()]
    unverified_days = sorted(day.isoformat() for day in assumed_holidays if session_windows(day))
    assumed_collisions = sum(bool(e.get("both_stop_and_target_touched")) for e in spot_ledger.get("events", []) if isinstance(e, dict))
    approximate = bool(skipped_stamps or delayed_actions or unverified_days or assumed_collisions)
    # A skipped minute or assumed missing session makes later end-of-session
    # marks approximate too; no prices or fills are invented to fill that gap.
    uncertain_from = [missing.date() for missing in skipped_stamps]
    uncertain_from.extend(timestamp(action["at"]).date() for action in delayed_actions)
    uncertain_from.extend(assumed_holidays)
    for snapshot in daily_pnl:
        snapshot["approximate"] = any(day <= timestamp(snapshot["at"]).date() for day in uncertain_from)
    warnings = []
    if unverified_days:
        warnings.append("Unverified missing whole days treated as assumed holidays: " + ", ".join(unverified_days) + ". Missed trades, exits and rollover prices cannot be ruled out; totals are approximate.")
    if skipped_stamps or delayed_actions:
        warnings.append("Missing minute prices were skipped without interpolation; missed fills are unknown and totals are approximate.")
    if blocked:
        warnings.append(blocked["reason"])
    if assumed_collisions:
        warnings.append(f"{assumed_collisions} stop/target collisions use an assumed stop-first path; this is an approximate result, not a known fill sequence.")
    pricing_summary = {basis: {"positions": 0, "closed_net": 0.0, "open_net": 0.0} for basis in ("recorded", "mixed", "estimated")}
    for group in ledgers.values():
        for p in group:
            bucket = pricing_summary[p.get("pnl_basis", "estimated")]
            bucket["positions"] += 1
            key = "closed_net" if p["status"] == "closed" else "open_net"
            bucket[key] = float(rounded(decimal(bucket[key])+decimal(p.get("net_pnl",0))))
    if option_prices is not None:
        warnings.extend(option_prices.describe()["notes"])
        if option_prices.conflicts or option_prices.rejected:
            warnings.append("Invalid or conflicting option observations were excluded; affected prices use labelled estimates.")
        if sum(s["positions"] for s in pricing_summary.values()):
            approximate = True
            warnings.append("Option P&L is a backtest estimate, including recorded candle/quote price proxies rather than guaranteed fills. Price coverage separates recorded, mixed and model-only legs.")
    return {"mode": "recorded_first_options" if option_prices is not None else "modelled_option_premiums",
            "label": "Option P&L · recorded prices first" if option_prices is not None else "Modelled option premiums", "status": status,
            "pricing_policy": POLICY if option_prices is not None else "model_only_legacy",
            "pricing_summary": pricing_summary, "option_data": option_prices.describe() if option_prices is not None else None,
            "strategy": rules, "validation_status": "incomplete" if status != "complete" else "approximate" if approximate else "validated",
            "approximate": approximate, "warnings": warnings, "assumed_holidays": unverified_days,
            "approximate_due_to_assumed_holidays": bool(unverified_days),
            "event_validation": {"supplied": expected_events, "processed": processed_events, "all_processed": processed_events == expected_events and not blocked},
            "config": config, "calendar_version": calendar.version, "fee_version": fees.version,
            "session_calendar_version": spot_ledger.get("session_calendar_version"),
            "missing_minute_policy": "skip" if ignore_missing_minutes else "stop",
            "skipped_minute_count": len(skipped_stamps),
            "data_gaps": data_gaps, "delayed_actions": delayed_actions,
            "approximate_due_to_missing_minutes": bool(skipped_stamps or delayed_actions),
            "as_of": last_at.isoformat() if last_at else None, "blocked_from": blocked,
            "daily_pnl": daily_pnl,
            "ledgers": ledgers, "orders": orders, "rollovers": rollovers, "summary": summaries,
            "charges": charges, "combined": combined,
            "pending_replacements": [{"option_type": side, "due_at": due.isoformat(), "status": "Awaiting next trading minute; no order or exit fee invented"} for side, due in pending.items()],
            "assumptions": [
                "Recorded option observations are used first for an exact identified contract and time; missing, conflicting or unusable observations fall back to the premium model. Every price and P&L record is labelled. Neither historical prices nor paper estimates prove a broker fill." if option_prices is not None else "Modelled option premiums, not actual option prices or executable fills. No volatility, margin or guaranteed protection is modelled.",
                "Recorded minute opens/closes are price proxies, not exact intraminute fills. No later price or different contract is substituted. Unidentified model-only selling legs remain estimated for their entire life.",
                "Fallback movement and calendar-time decay use each leg's own entry premium, including a recorded entry premium when available. Recorded premiums change P&L and charges, not spot stops, targets or hedge trigger distances.",
                "Hedges start at the first checked minute, continue between selling trades, and use the current monthly cycle (next month after scheduled rollover).",
                "Replacement closes at the completed candle's trigger level, then opens at the next trading minute's open. A session-end or scheduled-break trigger waits for the next configured session opening.",
                "Trigger-level valuation is a simulation assumption, including gap-through triggers; it is not a guaranteed fill.",
                "Strategy: " + rules["label"] + ". Intraminute time decay uses " + rules["intraminute_valuation"] + "; exact execution seconds remain unknown.",
                "Expiry dates use the historical monthly NSE schedule. An empty data day is only an assumed holiday for this replay and cannot rewrite expiry dates.",
                config["quantity_basis"], config["grouping"],
                "Charges are date-configured resident-account estimates, rounded to paise per modelled order; actual contract-note rounding, account-specific extras and freeze-limit splits may differ.",
                "Fee sources were reviewed on 30 September 2026. The latest published rates remain active until this configuration is updated; later revisions are not downloaded automatically.",
                "Open values use the last checked spot close; only their entry charges are included. No fictional closing orders, charges or forced end-of-test exits.",
                "Daily and monthly P&L use end-of-session option marks. They value open legs at the supplied session close without creating a closing order or exit charge, so they remain a research estimate rather than broker MTM.",
            ] + (["Missing minute prices are skipped without interpolation. Triggers within gaps may be missed. Pending hedge replacements and scheduled rollovers use the next available minute open, before expiry. Missing entry prices skip that entry. Elapsed-time decay still includes gaps; totals are approximate."] if ignore_missing_minutes else [])}
