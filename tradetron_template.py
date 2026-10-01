"""Build the private BANKNIFTY portal-executor import for Tradetron.

The template is deliberately paper-only.  It contains no API token and does
not send requests.  The portal is responsible for deciding every entry, exit,
and hedge replacement; Tradetron only receives named runtime values.
"""
from __future__ import annotations

import json
from pathlib import Path


FORMAT = "tt-strategy/v2"
UNDERLYING = "NIFTY BANK"
INSTRUMENT = 1854
# This is the current BANKNIFTY contract size used by the one-lot paper
# deployment. Tradetron's runtime-driven strike/expiry formulas require Qty,
# rather than Lots, so the template must state the equivalent unit count.
BANKNIFTY_LOT_UNITS = 30


class _Ids:
    def __init__(self):
        self.value = 20000

    def next(self) -> int:
        self.value += 1
        return self.value


def _value(value):
    return {"type": "value", "value": str(value)}


def _keyword(name, params, ids):
    """A formula element at the current expression level."""
    return {"name": name, "kid": ids.next(), "params": params}


def _nested_keyword(name, params, ids):
    """A formula element used as an argument of another element."""
    return {"type": "keyword", "keyword": _keyword(name, params, ids)}


def _runtime(name, ids):
    return _keyword("Get Runtime Number", [_value(name)], ids)


def _nested_runtime(name, ids):
    return _nested_keyword("Get Runtime Number", [_value(name)], ids)


def _runtime_text(name):
    return f"get_runtime ( 'strategy_id', 'run_counter', '{name}' )"


def _runtime_display(name):
    return f"Get Runtime Number ( 'strategy_id', 'run_counter', '{name}' )"


def _number_text(value):
    return f"tt_Number ( '{value}' )"


def _number_display(value):
    return f"Number ( '{value}' )"


def _number(value, ids):
    return _keyword("Number", [_value(value)], ids)


def _rule(elements, ids):
    return {"type": "rule", "value": f"builder-basic_rule_{ids.next()}", "elements": elements}


def _formula(elements, ids):
    return json.dumps({"operator": "and", "operands": [_rule(elements, ids)]}, separators=(",", ":"))


def _condition(rules, ids):
    return json.dumps({"operator": "and", "operands": [_rule(rule, ids) for rule in rules]}, separators=(",", ":"))


def _runtime_value(name, source, ids):
    return {
        "variableName": name,
        "value": f"( {_runtime_text(source)} )",
        "display": f"( {_runtime_display(source)} )",
        "json": _formula([_runtime(source, ids)], ids),
    }


def _zero_value(name, ids):
    return {
        "variableName": name,
        "value": f"( {_number_text(0)} )",
        "display": f"( {_number_display(0)} )",
        "json": _formula([_number(0, ids)], ids),
    }


def _month_expiry(offset_name, ids):
    return _formula([
        {"name": "Select Expiry", "kid": ids.next(), "params": [
            _value("month"), _nested_runtime(offset_name, ids),
        ]}
    ], ids)


def _leg(direction, side, strike_name, expiry_offset_name, ids):
    expiry_value = f"( tt_expiry_offset ( 'month', {_runtime_text(expiry_offset_name)} ) )"
    expiry_display = f"( Select Expiry ( 'month', {_runtime_display(expiry_offset_name)} ) )"
    strike_value = f"( {_runtime_text(strike_name)} )"
    strike_display = f"( {_runtime_display(strike_name)} )"
    return {
        "instrument": INSTRUMENT,
        "list": 0,
        "optionType": direction,
        "expiryType": "Fx",
        "expiry": expiry_value,
        "expiryJson": _month_expiry(expiry_offset_name, ids),
        "expiryDisplay": expiry_display,
        "strikeType": "Fx",
        "strike": strike_value,
        "strikeJson": _formula([_runtime(strike_name, ids)], ids),
        "strikeDisplay": strike_display,
        # One current BANKNIFTY lot in units. Tradetron accepts runtime
        # strike/expiry formulas only when the position quantity type is Qty.
        # The paper deployment multiplier remains the only size control.
        "qty": str(BANKNIFTY_LOT_UNITS),
        "qtyType": "Qty",
        "qtyDisplay": str(BANKNIFTY_LOT_UNITS),
        "qtyJson": None,
        "qtyExprDisplay": None,
        "limitPrice": None,
        "limitPriceDisplay": None,
        "limitPriceJson": None,
        "limitPriceExprDisplay": None,
        "buySell": side,
        "entryLimit": None,
        "entryTrigger": None,
        "sLLimit": None,
        "sLTrigger": None,
        "sLTriggerDisplay": None,
        "sLTriggerJson": None,
        "sLTriggerExprDisplay": None,
        "targetLimit": None,
        "targetTrigger": None,
        "productType": "NRML",
        "underlyingSymbol": UNDERLYING,
        "isOvernightProtectionLeg": "No",
    }


def _event_condition(incoming, seen, ids):
    return {
        "value": f"( {_runtime_text(incoming)} > {_runtime_text(seen)} )",
        "display": f"( {_runtime_display(incoming)} > {_runtime_display(seen)} )",
        "json": _condition([[_runtime(incoming, ids), {"name": ">", "params": []}, _runtime(seen, ids)]], ids),
    }


def _event_block(kind, name, condition, legs, variables):
    return {
        "type": kind,
        "conditionValue": condition["value"],
        "conditionJson": condition["json"],
        "conditionDisplay": condition["display"],
        "subType": "None",
        "visible": 1,
        "legs": legs,
        "extra": {"name": name, "variables": variables},
    }


def _direction_set(direction, ids):
    prefix = f"pa_{direction.lower()}"
    entry_seen = f"{direction.lower()}_entry_seen"
    exit_seen = f"{direction.lower()}_exit_seen"
    hedge_seen = f"{direction.lower()}_hedge_seen"
    entry = _event_block(
        "Entry",
        f"{direction} portal entry · buy hedge before short",
        _event_condition(f"{prefix}_entry_seq", entry_seen, ids),
        [
            _leg(direction, "B", f"{prefix}_hedge_strike", f"{prefix}_expiry_offset", ids),
            _leg(direction, "S", f"{prefix}_short_strike", f"{prefix}_expiry_offset", ids),
        ],
        [_runtime_value(entry_seen, f"{prefix}_entry_seq", ids)],
    )
    exit_block = _event_block(
        "Exit",
        f"{direction} portal exit · short before hedge",
        _event_condition(f"{prefix}_exit_seq", exit_seen, ids),
        [],
        [_runtime_value(exit_seen, f"{prefix}_exit_seq", ids)],
    )
    repair = _event_block(
        "Repair Continuous",
        f"{direction} portal hedge replacement · buy new before sell old",
        _event_condition(f"{prefix}_hedge_seq", hedge_seen, ids),
        [
            _leg(direction, "B", f"{prefix}_hedge_strike", f"{prefix}_expiry_offset", ids),
            _leg(direction, "S", f"{prefix}_previous_hedge_strike", f"{prefix}_previous_expiry_offset", ids),
        ],
        [_runtime_value(hedge_seen, f"{prefix}_hedge_seq", ids)],
    )
    # Tradetron evaluates set conditions in listed order.  Repair must stay
    # before Exit so a hedge-replacement signal is never hidden behind it.
    return {"list": 0, "name": f"{direction} · portal controlled · PAPER ONLY", "conditions": [entry, repair, exit_block]}


def build_template():
    """Return an importable one-strategy paper template.

    A deployment must remain paper and keep the delivery lock until a real
    paper run verifies Tradetron's exact expiry-FX and repair-fill behaviour.
    """
    ids = _Ids()
    variables = []
    for direction in ("ce", "pe"):
        for event in ("entry", "exit", "hedge"):
            variables.append(_zero_value(f"pa_{direction}_{event}_seq", ids))
        variables.append(_zero_value(f"pa_{direction}_expiry_offset", ids))
        variables.append(_zero_value(f"pa_{direction}_previous_expiry_offset", ids))
        for seen in ("entry", "exit", "hedge"):
            variables.append(_zero_value(f"{direction}_{seen}_seen", ids))

    universal_exit = _event_block(
        "Universal Exit",
        "Disabled: the portal sends independent CE/PE exits; never close every position together.",
        {
            "value": f"( {_number_text(0)} == {_number_text(1)} )",
            "display": f"( {_number_display(0)} == {_number_display(1)} )",
            "json": _condition([[_number(0, ids), {"name": "==", "params": []}, _number(1, ids)]], ids),
        },
        [],
        [],
    )
    # Tradetron's exported v2 files include this hidden placeholder before the
    # universal-exit block.  Keeping it makes the import match its own schema
    # without adding any overnight or automatic-close behaviour.
    overnight_placeholder = {
        "type": "Repair Once",
        "conditionValue": None,
        "conditionJson": None,
        "conditionDisplay": None,
        "subType": "Overnight Protection",
        "visible": 0,
        "legs": [],
        "extra": {"name": None, "variables": []},
    }
    return {
        "_format": FORMAT,
        "_readme": {
            "about": "Private BANKNIFTY portal executor. Paper only; do not deploy live.",
            "logic": "The portal owns timing, monthly-expiry routing, original 500-point stop and 1,000-point target, and event identity.",
            "do_not": "Do not replace the named runtime counters with Tradetron price rules or enable deployment before paper verification.",
        },
        "name": "BANKNIFTY · Portal executor · PAPER ONLY",
        "description": (
            "<p><strong>PAPER ONLY — DO NOT DEPLOY LIVE.</strong></p>"
            "<p>The local portal decides every BANKNIFTY entry, exit and hedge replacement. "
            "CE and PE are separate sets. Each entry buys the protective option before selling the short; "
            "each exit closes the short before the hedge; each hedge replacement buys the new hedge before selling the old one.</p>"
            "<p>One template lot is intentional. Use the paper deployment multiplier for size. "
            "The portal keeps the completed-hour, fixed 500-point stop and 1,000-point target; Tradetron must not add its own price exits.</p>"
        ),
        "tags": [],
        "variables": variables,
        "sets": [_direction_set("CE", ids), _direction_set("PE", ids), {"name": None, "conditions": [overnight_placeholder, universal_exit]}],
        "currency": "INR",
        "priceExecution": "Market Price",
        "tranchSize": 100,
        "tslToCostAtProfit": 0,
        "tslProfitIncrease": 0,
        "tslSlIncrease": 0,
        "runFrequency": "Continuous",
        "firstTradeAfterOpen": 1,
        "lastTradeBeforeClose": 60,
        "rolloverExpiryPeriod": "Dont Rollover",
        "capitalRequired": 250000,
        "priceRevisionAttempts": 1,
        "tranchWaitSecs": 1,
        "exitShortsFirst": 1,
        "priceRevisionTimeout": 0,
        "reactivateEvery": 2,
        "finalAction": "MARKET",
        "exitAtMarket": 0,
        "runEvery": 1,
        "rolloverExpiryTime": "1500",
        "priceTicksMultiple": "1",
        "pythonCode": None,
        "isPublic": 0,
        "minMultiple": 1,
        "maxMultiple": 100,
        "freePeriod": None,
        "fixedFee": "0.00",
        "fixedFeePeriod": "1",
        "variableFee": "0.00",
        "hurdleRate": 0,
        "recreatePositionsInNewFollowers": "No",
        "allowDuplication": 0,
        "confirmationPerSubscriber": 0,
        "hideSubscribers": "No",
        "strategyPublicLinkAccess": "Yes",
        "strategyPublicLink": None,
        "displayPositionsToNonSubscribers": 0,
        "highWatermark": "Yes",
        "showLiveAutoDeployment": 0,
        "feeShare": None,
        "isApproved": 0,
        "feeFrequency": "Month",
        "backtestLinkSubscribers": None,
        "backtestLinkNonsubscribers": None,
    }


def write_template(path: Path):
    path.write_text(json.dumps(build_template(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_template(Path(__file__).resolve().parents[1] / "strategy-work" / "portal-executor-paper.import.json")
