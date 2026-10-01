"""Strict, paper-only signal contract for the private Tradetron executor.

Tradetron is deliberately an executor here: the portal owns the BANKNIFTY
hourly model, original spot stop/target, position state, and rollover timing.
This module only turns a *validated* portal decision into named runtime
variables for the one private Tradetron template.  It never sends a request.
"""
from dataclasses import dataclass
from datetime import date
import re
from typing import Mapping


PROTOCOL_VERSION = "banknifty-paper-executor-v1"
STRIKE_STEP = 500
LOT_SIZE = 30
EVENT_TYPES = frozenset(("entry", "exit", "hedge_replacement"))
DIRECTIONS = frozenset(("CE", "PE"))
SYMBOL_PATTERN = re.compile(r"BANKNIFTY[A-Z0-9]+(?:CE|PE)\Z")


@dataclass(frozen=True)
class PortalSignal:
    """One already-decided portal event, in exchange units rather than lots."""

    event_id: str
    event_type: str
    direction: str
    sequence: int
    expiry: str
    quantity: int
    expiry_offset: int
    short_symbol: str | None = None
    short_strike: int | None = None
    hedge_symbol: str | None = None
    hedge_strike: int | None = None
    previous_hedge_symbol: str | None = None
    previous_hedge_strike: int | None = None
    previous_expiry: str | None = None
    previous_expiry_offset: int | None = None
    signal_hour_close: float | None = None
    stop_spot: float | None = None
    target_spot: float | None = None


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive whole number.")
    return value


def _strike(value, label):
    value = _positive_int(value, label)
    if value % STRIKE_STEP:
        raise ValueError(f"{label} must be a {STRIKE_STEP}-point BANKNIFTY strike.")
    return value


def _symbol(value, direction, label):
    if not isinstance(value, str) or not SYMBOL_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a BANKNIFTY option symbol.")
    if not value.endswith(direction):
        raise ValueError(f"{label} must be a {direction} option symbol.")
    return value


def _expiry(value):
    if not isinstance(value, str):
        raise ValueError("Expiry must use the YYYY-MM-DD format.")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("Expiry must use the YYYY-MM-DD format.") from None
    return value


def _expiry_offset(value, label):
    if isinstance(value, bool) or value not in (0, 1):
        raise ValueError(f"{label} must be 0 (current month) or 1 (next month).")
    return value


def _spot(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} must be a positive spot price.")
    return float(value)


def _require(payload: Mapping, name):
    value = payload.get(name)
    if value is None:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} is required.")
    return value


def parse_portal_signal(payload: Mapping) -> PortalSignal:
    """Reject anything that is not a known BANKNIFTY portal decision.

    No caller can choose an arbitrary side, arbitrary exchange symbol, or a
    custom order action.  The shape is intentionally smaller than a broker
    order because this is not a broker API.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("A portal signal must be an object.")
    event_type = payload.get("event_type")
    direction = payload.get("direction")
    event_id = payload.get("event_id")
    if event_type not in EVENT_TYPES:
        raise ValueError("Use entry, exit, or hedge_replacement for the event type.")
    if direction not in DIRECTIONS:
        raise ValueError("Direction must be CE or PE.")
    if not isinstance(event_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{7,127}", event_id):
        raise ValueError("Event ID must be a stable portal identifier.")
    sequence = _positive_int(_require(payload, "sequence"), "Sequence")
    expiry = _expiry(_require(payload, "expiry"))
    expiry_offset = _expiry_offset(_require(payload, "expiry_offset"), "Expiry offset")
    quantity = _positive_int(_require(payload, "quantity"), "Quantity")
    if quantity % LOT_SIZE:
        raise ValueError(f"Quantity must be a whole BANKNIFTY lot ({LOT_SIZE} units).")

    common = dict(event_id=event_id, event_type=event_type, direction=direction,
                  sequence=sequence, expiry=expiry, expiry_offset=expiry_offset, quantity=quantity)
    if event_type == "exit":
        disallowed = ("short_symbol", "short_strike", "hedge_symbol", "hedge_strike",
                      "previous_hedge_symbol", "previous_hedge_strike", "previous_expiry",
                      "previous_expiry_offset", "signal_hour_close",
                      "stop_spot", "target_spot")
        if any(payload.get(name) is not None for name in disallowed):
            raise ValueError("An exit only carries its direction, sequence, expiry, and quantity.")
        return PortalSignal(**common)

    hedge_symbol = _symbol(_require(payload, "hedge_symbol"), direction, "Hedge symbol")
    hedge_strike = _strike(_require(payload, "hedge_strike"), "Hedge strike")
    if event_type == "hedge_replacement":
        previous_symbol = _symbol(_require(payload, "previous_hedge_symbol"), direction, "Previous hedge symbol")
        previous_strike = _strike(_require(payload, "previous_hedge_strike"), "Previous hedge strike")
        previous_expiry = _expiry(_require(payload, "previous_expiry"))
        previous_expiry_offset = _expiry_offset(_require(payload, "previous_expiry_offset"), "Previous expiry offset")
        if hedge_symbol == previous_symbol or hedge_strike == previous_strike:
            raise ValueError("A hedge replacement must select a different hedge.")
        if any(payload.get(name) is not None for name in ("short_symbol", "short_strike", "signal_hour_close", "stop_spot", "target_spot")):
            raise ValueError("A hedge replacement cannot change the short or its spot risk levels.")
        return PortalSignal(**common, hedge_symbol=hedge_symbol, hedge_strike=hedge_strike,
                            previous_hedge_symbol=previous_symbol, previous_hedge_strike=previous_strike,
                            previous_expiry=previous_expiry, previous_expiry_offset=previous_expiry_offset)

    short_symbol = _symbol(_require(payload, "short_symbol"), direction, "Short symbol")
    short_strike = _strike(_require(payload, "short_strike"), "Short strike")
    if short_symbol == hedge_symbol or short_strike == hedge_strike:
        raise ValueError("Short and hedge must be different BANKNIFTY options.")
    # The protective option is further out of the money than the short.
    if (direction == "CE" and hedge_strike <= short_strike) or (direction == "PE" and hedge_strike >= short_strike):
        raise ValueError("The hedge must be further out of the money than the short.")
    reference = _spot(_require(payload, "signal_hour_close"), "Signal-hour close")
    stop = _spot(_require(payload, "stop_spot"), "Stop")
    target = _spot(_require(payload, "target_spot"), "Target")
    expected_stop = reference + 500 if direction == "CE" else reference - 500
    expected_target = reference - 1000 if direction == "CE" else reference + 1000
    if abs(stop - expected_stop) > 0.0001 or abs(target - expected_target) > 0.0001:
        raise ValueError("Stop and target must be the fixed 500/1,000 points from the signal-hour close.")
    return PortalSignal(**common, short_symbol=short_symbol, short_strike=short_strike,
                        hedge_symbol=hedge_symbol, hedge_strike=hedge_strike,
                        signal_hour_close=reference, stop_spot=stop, target_spot=target)


def runtime_pairs(signal: PortalSignal) -> dict[str, str]:
    """Return only the named variables the one-template builder may consume."""
    prefix = "pa_" + signal.direction.lower()
    pairs = {prefix + "_event_id": signal.event_id, prefix + "_expiry": signal.expiry,
             prefix + "_expiry_offset": str(signal.expiry_offset),
             prefix + "_qty": str(signal.quantity)}
    if signal.event_type == "entry":
        pairs.update({prefix + "_entry_seq": str(signal.sequence),
                      prefix + "_short_strike": str(signal.short_strike),
                      prefix + "_hedge_strike": str(signal.hedge_strike),
                      prefix + "_short_symbol": signal.short_symbol,
                      prefix + "_hedge_symbol": signal.hedge_symbol,
                      prefix + "_signal_close": _number(signal.signal_hour_close),
                      prefix + "_stop_spot": _number(signal.stop_spot),
                      prefix + "_target_spot": _number(signal.target_spot)})
    elif signal.event_type == "exit":
        pairs[prefix + "_exit_seq"] = str(signal.sequence)
    else:
        pairs.update({prefix + "_hedge_seq": str(signal.sequence),
                      prefix + "_hedge_strike": str(signal.hedge_strike),
                      prefix + "_hedge_symbol": signal.hedge_symbol,
                      prefix + "_previous_hedge_strike": str(signal.previous_hedge_strike),
                      prefix + "_previous_hedge_symbol": signal.previous_hedge_symbol,
                      prefix + "_previous_expiry": signal.previous_expiry,
                      prefix + "_previous_expiry_offset": str(signal.previous_expiry_offset)})
    return pairs


def _number(value):
    return str(int(value)) if float(value).is_integer() else str(value)


def template_contract():
    """Non-secret, user-visible contract the private strategy must implement."""
    return {
        "version": PROTOCOL_VERSION,
        "mode": "paper_only",
        "signals_locked": True,
        "execution_order": {
            "entry": ["BUY protective hedge", "SELL short option"],
            "exit": ["BUY short option", "SELL protective hedge"],
            "hedge_replacement": ["BUY new protective hedge", "SELL previous protective hedge"],
        },
        "directions": {
            direction: {
                "entry": [f"pa_{direction.lower()}_entry_seq", f"pa_{direction.lower()}_short_strike",
                          f"pa_{direction.lower()}_hedge_strike", f"pa_{direction.lower()}_qty",
                          f"pa_{direction.lower()}_expiry", f"pa_{direction.lower()}_expiry_offset"],
                "exit": [f"pa_{direction.lower()}_exit_seq", f"pa_{direction.lower()}_qty"],
                "hedge_replacement": [f"pa_{direction.lower()}_hedge_seq",
                                      f"pa_{direction.lower()}_previous_hedge_strike",
                                      f"pa_{direction.lower()}_previous_expiry_offset",
                                      f"pa_{direction.lower()}_hedge_strike", f"pa_{direction.lower()}_qty"],
            }
            for direction in sorted(DIRECTIONS)
        },
        "safeguards": [
            "Only BANKNIFTY CE/PE symbols and 500-point strikes are accepted.",
            "Quantity is in units and must be a whole current BANKNIFTY lot.",
            "Entry keeps the portal's fixed 500-point stop and 1,000-point target from the completed hour close.",
            "A replacement cannot change the short or its original stop and target.",
            "Each direction has independent entry, exit, and hedge-replacement counters.",
        ],
    }
