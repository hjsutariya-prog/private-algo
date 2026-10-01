"""Deterministic artificial fixtures. No real quotes, paper P&L or broker orders."""
from datetime import date, datetime
from engine import HedgeReplacement, IST, rollover_due, rollover_session, short_roll_decision

CATALOG = [
    {"id": "hedge_full_fill", "title": "Buy first, then release old hedge", "description": "Check that one complete replacement purchase permits exactly one old-hedge sale."},
    {"id": "hedge_partial_rejected", "title": "Partial or failed purchase", "description": "The old hedge stays protected after pending, partial, rejected or uncertain test updates."},
    {"id": "monday_rollover_holiday", "title": "Monday rollover and a holiday", "description": "Check 3:15 PM timing using artificial exchange calendars and an actual-expiry input."},
    {"id": "short_exit_priority", "title": "Stop or target takes priority", "description": "A short that has hit its stop or target exits instead of rolling; its original reference stays unchanged."},
    {"id": "duplicate_isolation", "title": "Duplicate updates and separate strategies", "description": "Repeated updates cannot release a second sale; one strategy's update cannot act on another."},
]


def run_scenario(scenario_id, lots):
    spec = next((x for x in CATALOG if x["id"] == scenario_id), None)
    if not spec:
        raise ValueError("Choose a known order-safety check.")
    events, checks = [], []

    def event(name, action, detail):
        events.append({"step": len(events) + 1, "event": name, "action": action, "detail": detail})

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})

    # Deliberately NOT the current BANKNIFTY lot size. Fixture units make it clear
    # that production contract quantities have not been verified or connected.
    fixture_lot_size = 10
    gate = HedgeReplacement(lots, fixture_lot_size)
    if scenario_id in ("hedge_full_fill", "hedge_partial_rejected", "duplicate_isolation"):
        intent = gate.start("TEST-OLD-CE", "TEST-NEXT-CE", "TEST-BUY-A")
        event("Replacement requested", intent["action"], f"Artificial order: {lots} test lots × {fixture_lot_size} fixture units. No order is sent.")
    if scenario_id == "hedge_full_fill":
        first = gate.on_buy_update("TEST-BUY-A", "COMPLETE", gate.quantity)
        event("Full matching purchase", first["action"], first["detail"])
        repeat = gate.on_buy_update("TEST-BUY-A", "COMPLETE", gate.quantity)
        event("Duplicate completion", repeat["action"], repeat["detail"])
        check("Full purchase permits old-hedge sale", first["action"] == "SELL_OLD")
        check("Duplicate completion cannot create another sale", repeat["action"] == "HOLD_OLD")
        check("Cycle finishes only after full old-hedge sale", gate.mark_old_sold("TEST-SELL-A", gate.quantity, "COMPLETE"))
    elif scenario_id == "hedge_partial_rejected":
        for status, quantity in [("PENDING", 0), ("OPEN", gate.quantity // 2), ("REJECTED", gate.quantity // 2), ("UNKNOWN", gate.quantity // 2), ("COMPLETE", gate.quantity // 2)]:
            update = gate.on_buy_update("TEST-BUY-A", status, quantity)
            event(f"{status}: {quantity}/{gate.quantity} fixture units", update["action"], "The old hedge is not released for sale.")
            check(f"{status} with {quantity} units retains the old hedge", update["action"] == "HOLD_OLD")
    elif scenario_id == "monday_rollover_holiday":
        expiry = date(2026, 9, 29)
        monday, tuesday = date(2026, 9, 28), expiry
        # These calendars are deliberately synthetic, not claims about NSE dates.
        regular, holiday = {monday, tuesday}, {tuesday}
        for name, cal in [("Monday open (fixture)", regular), ("Monday holiday (fixture)", holiday)]:
            selected = rollover_session(expiry, cal)
            before = rollover_due(datetime.combine(selected, datetime.min.time(), IST).replace(hour=15, minute=14), expiry, cal)
            at = rollover_due(datetime.combine(selected, datetime.min.time(), IST).replace(hour=15, minute=15), expiry, cal)
            event(name, "ROLL_DATE_CHECK", f"Selected {selected.isoformat()} at 15:15 IST from the supplied test calendar.")
            check(f"{name}: wait before 15:15", not before)
            check(f"{name}: due at 15:15", at)
        check("Holiday moves to first supplied session before expiry", rollover_session(expiry, holiday) == tuesday)
        check("A holiday itself cannot trigger a roll", not rollover_due(datetime(2026, 9, 28, 15, 15, tzinfo=IST), expiry, holiday))
    elif scenario_id == "short_exit_priority":
        for side, spot, reason in [("bull", 49500, "stop"), ("bull", 51000, "target"), ("bear", 50500, "stop"), ("bear", 49000, "target")]:
            outcome = short_roll_decision(side, 50000, spot, True)
            event(f"{side} short reaches {reason}", outcome["action"], "Terminal exit wins even when a rollover is due. Original spot reference: 50,000 (fixture).")
            check(f"{side} {reason} takes priority", outcome["action"] == "EXIT" and outcome["reason"] == reason)
            check(f"{side} {reason} preserves original reference", outcome["entry_spot"] == 50000)
        hold = short_roll_decision("bull", 50000, 50100, True)
        check("A non-terminal short can request a roll without resetting E", hold["action"] == "ROLL" and hold["entry_spot"] == 50000)
    elif scenario_id == "duplicate_isolation":
        other = HedgeReplacement(lots, fixture_lot_size)
        other.start("TEST-OLD-PE", "TEST-NEXT-PE", "TEST-BUY-B")
        try:
            other.on_buy_update("TEST-BUY-A", "COMPLETE", other.quantity)
            isolated = False
        except ValueError:
            isolated = True
        event("Wrong strategy's order update", "BLOCKED", "The order reference does not match; the other strategy cannot release its hedge.")
        check("Order references are isolated", isolated and other.phase == "BUY_PENDING")
        actions = [gate.on_buy_update("TEST-BUY-A", "COMPLETE", gate.quantity)["action"] for _ in range(3)]
        event("Completion delivered three times", "ONE_SALE_INTENT", "Only the first matching full completion releases a sale.")
        check("Only one sale is released", actions.count("SELL_OLD") == 1)
        check("The other strategy still retains its hedge", other.phase == "BUY_PENDING")
    passed = bool(checks) and all(c["passed"] for c in checks)
    return {"scenario_id": scenario_id, "title": spec["title"], "test_data": True,
            "result": "passed" if passed else "failed", "events": events, "checks": checks,
            "summary": f"{sum(c['passed'] for c in checks)}/{len(checks)} offline checks passed. Artificial inputs only—not a market backtest or paper-trading result.",
            "strategy_lots": lots, "fixture_lot_size": fixture_lot_size}
