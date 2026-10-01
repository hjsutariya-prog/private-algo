"""Offline rule checks only. This module has no broker or network integration."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

IST = timezone(timedelta(hours=5, minutes=30))


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Prices must be positive finite numbers.")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Prices must be positive finite numbers.")
    return Decimal(str(value))


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive whole number.")
    return value


def strict_signal(open_, high, low, close):
    o, h, l, c = map(_price, (open_, high, low, close))
    if l > min(o, c) or h < max(o, c) or l > h:
        raise ValueError("The candle prices are inconsistent.")
    if c > o:
        level = int((c / 1000).to_integral_value(rounding=ROUND_CEILING)) * 1000 - 1000
        if l < level < c:
            return {"side": "bull", "level": level}
    if c < o:
        level = int((c / 1000).to_integral_value(rounding=ROUND_FLOOR)) * 1000 + 1000
        if c < level < h:
            return {"side": "bear", "level": level}
    return None


def hedge_levels(spot, side):
    h = _price(spot)
    if side not in ("CE", "PE"):
        raise ValueError("Hedge side must be CE or PE.")
    band = int(h // 10000) * 100
    rounding = ROUND_CEILING if side == "CE" else ROUND_FLOOR
    strike = int((h / 1000).to_integral_value(rounding=rounding)) * 1000
    strike += 1000 if side == "CE" else -1000
    if band <= 0 or strike <= 0:
        raise ValueError("This reference is outside the supported BANKNIFTY test range.")
    target = h + (2 * band if side == "CE" else -2 * band)
    return {"reference": float(h), "band": band, "strike": strike, "target": float(target)}


def rollover_session(expiry_date, sessions):
    """Uses a supplied calendar; does not invent holidays or an expiry weekday."""
    if type(expiry_date) is not date or any(type(d) is not date for d in sessions):
        raise ValueError("Supply an actual expiry date and dated exchange sessions.")
    monday = expiry_date - timedelta(days=expiry_date.weekday())
    eligible = sorted(d for d in sessions if monday <= d <= expiry_date)
    if not eligible:
        raise ValueError("No supplied session on or after Monday and no later than expiry.")
    return eligible[0]


def rollover_due(now, expiry, sessions):
    """Regular-session rehearsal only. A missed cutoff remains due on later sessions."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A time with a timezone is required.")
    roll_day = rollover_session(expiry, sessions)
    local = now.astimezone(IST)
    day = local.date()
    if day not in sessions or not roll_day <= day <= expiry:
        return False
    if not time(9, 15) <= local.time() < time(15, 30):
        return False
    return day > roll_day or local.time() >= time(15, 15)


def terminal_exit(side, entry_spot, spot):
    e, current = _price(entry_spot), _price(spot)
    if side == "bull":
        return "stop" if current <= e - 500 else "target" if current >= e + 1000 else None
    if side == "bear":
        return "stop" if current >= e + 500 else "target" if current <= e - 1000 else None
    raise ValueError("Direction must be bull or bear.")


def short_roll_decision(side, entry_spot, spot, due):
    reason = terminal_exit(side, entry_spot, spot)
    return {"action": "EXIT" if reason else "ROLL" if due else "HOLD",
            "reason": reason, "entry_spot": entry_spot}


class HedgeReplacement:
    """A testable intent gate, NOT an order executor or broker fill verifier.

    Callers must eventually supply authenticated, reconciled order updates. A fake
    COMPLETE event can prove a test branch, never that an exchange trade happened.
    One instance belongs to one replacement, not to an entire broker account.
    """

    def __init__(self, lots, lot_size):
        self.quantity = _positive_int(lots, "Lots") * _positive_int(lot_size, "Lot size")
        self.phase = "IDLE"
        self.buy_order_id = self.sell_order_id = None
        self.old_contract = self.new_contract = None
        self.last_filled_quantity = 0

    def start(self, old_contract, new_contract, buy_order_id):
        if self.phase != "IDLE":
            raise ValueError("A replacement is already in progress or completed.")
        if not all(isinstance(x, str) and x.strip() for x in (old_contract, new_contract, buy_order_id)):
            raise ValueError("Exact contract references and an order identifier are required.")
        if old_contract == new_contract:
            raise ValueError("Same-contract replacement needs separate reconciliation; it is blocked here.")
        self.old_contract, self.new_contract = old_contract, new_contract
        self.buy_order_id = buy_order_id
        self.phase = "BUY_PENDING"
        return {"action": "BUY_NEW", "quantity": self.quantity,
                "order_id": buy_order_id, "contract": new_contract}

    def _validate_update(self, status, filled_quantity):
        if status not in {"OPEN", "PENDING", "COMPLETE", "REJECTED", "CANCELLED", "UNKNOWN"}:
            raise ValueError("Unrecognised order status; retain the old hedge.")
        if type(filled_quantity) is not int or not 0 <= filled_quantity <= self.quantity:
            raise ValueError("Invalid or excess filled quantity; reconciliation is required.")

    def on_buy_update(self, order_id, status, filled_quantity):
        if self.phase == "IDLE" or order_id != self.buy_order_id:
            raise ValueError("The update does not match this replacement buy.")
        self._validate_update(status, filled_quantity)
        self.last_filled_quantity = max(self.last_filled_quantity, filled_quantity)
        if self.phase == "BUY_PENDING" and status == "COMPLETE" and filled_quantity == self.quantity:
            self.phase = "SELL_PENDING"
            return {"action": "SELL_OLD", "quantity": self.quantity,
                    "contract": self.old_contract, "detail": "Full matching buy completion received once."}
        return {"action": "HOLD_OLD", "quantity": 0,
                "detail": "No new sale authorised: wait for full completion or reconcile the existing action."}

    def mark_old_sold(self, sell_order_id, filled_quantity, status):
        if self.phase not in ("SELL_PENDING", "COMPLETE"):
            raise ValueError("The replacement buy has not released an old-hedge sale.")
        self._validate_update(status, filled_quantity)
        if not isinstance(sell_order_id, str) or not sell_order_id.strip():
            raise ValueError("An old-hedge sale identifier is required.")
        if self.sell_order_id is not None and self.sell_order_id != sell_order_id:
            raise ValueError("The sale update belongs to a different order.")
        self.sell_order_id = sell_order_id
        if status == "COMPLETE" and filled_quantity == self.quantity:
            self.phase = "COMPLETE"
            return True
        return False
