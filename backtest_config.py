"""Versioned simulation profiles. Explicit instrument selection; no live orders."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation


PROFILES = {
    "BANKNIFTY": {
        "version": "banded-delta-lower-hedge-v1", "instrument": "BANKNIFTY",
        "sold_entry_premium": "1000", "sold_daily_decay": "20", "hedge_daily_decay": "30",
        "band_points": "200", "initial_delta": "0.50", "delta_step": "0.05",
        "strike_step": "1000", "hedge_premium_divisor": "10000", "hedge_premium_step": "100",
        "replacement_multiple": "2", "sell_lots": 4, "ce_hedge_lots": 4, "pe_hedge_lots": 4,
        "units_per_lot": 30, "quantity_basis": "User comparison assumption: fixed 30 units per lot, not historical contract lot sizes",
        "grouping": "One modelled order per leg/action; all lots grouped. No exchange freeze-size splitting simulated.",
    },
}


def model_config(overrides=None, *, instrument="BANKNIFTY"):
    if instrument not in PROFILES:
        raise ValueError(f"No explicit modelling profile for {instrument}; do not reuse BANKNIFTY settings implicitly.")
    config = deepcopy(PROFILES[instrument])
    if overrides is None:
        return config
    allowed = {"sell_lots", "ce_hedge_lots", "pe_hedge_lots", "units_per_lot"}
    if not isinstance(overrides, dict) or set(overrides) - allowed:
        raise ValueError("Unknown option-model quantity setting.")
    for key, value in overrides.items():
        minimum = 0 if key.endswith("hedge_lots") else 1
        maximum = 10000 if key == "units_per_lot" else 100
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be a whole number from {minimum} to {maximum}.")
        config[key] = value
    config["quantity_basis"] = f"User comparison assumption: fixed {config['units_per_lot']} units per lot, not historical contract lot sizes"
    return config


def decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Model values must be finite numbers.") from exc
    if not result.is_finite():
        raise ValueError("Model values must be finite numbers.")
    return result
