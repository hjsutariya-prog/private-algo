"""Value saved paper fills without creating, changing or fabricating fills."""
from collections import defaultdict, deque
from decimal import Decimal

from backtest_config import decimal, model_config
from backtest_fees import FeeSchedule, rounded
from option_model import premium_value
from option_prices import at_time, fresh_depth_price, resolve_premium, POLICY


def fresh_spot(quote, at):
    try:
        if quote.get("symbol") != "NSE:NIFTY BANK" or quote.get("source") not in ("zerodha_stream", "zerodha_snapshot", "synthetic_test"):
            return None
        if any(not 0 <= (at_time(at)-at_time(quote[k])).total_seconds() <= 5 for k in ("timestamp", "received_at")):
            return None
        value = decimal(quote["last_price"])
        return value if value > 0 else None
    except (ValueError,KeyError,TypeError,AttributeError):
        return None


def value_paper_fills(fills, orders, contexts, quotes, spot_quote, at):
    """FIFO quantities split closed P&L from open marks and allocate fees once."""
    at, config, fees = at_time(at), model_config(), FeeSchedule()
    spot = fresh_spot(spot_quote, at)
    orders = {o["id"]: o for o in orders}
    fee_per_unit = {}
    for identity,o in orders.items():
        if o["filled"]:
            cost = fees.calculate(at_time(o["created"]),o["intent"]["side"],o["value"]/o["filled"],o["filled"])["total"]
            fee_per_unit[identity] = decimal(cost)/o["filled"]
    groups = defaultdict(list)
    for fill in fills:
        groups[(fill["strategy"],fill["symbol"])].append(fill)
    results, closed_gross, closed_fees, open_gross, open_fees = [], Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    all_available = True
    for (strategy,symbol), rows in groups.items():
        lots = deque()
        gross = costs = Decimal(0)
        for fill in rows:
            sign = 1 if fill["side"] == "BUY" else -1
            remaining, price = fill["quantity"], decimal(fill["price"])
            unit_fee = fee_per_unit[fill["order_id"]]
            while remaining and lots and lots[0]["sign"] != sign:
                lot = lots[0]
                quantity = min(remaining,lot["quantity"])
                gross += (price-lot["price"])*quantity*lot["sign"]
                costs += (lot["unit_fee"]+unit_fee)*quantity
                remaining -= quantity
                lot["quantity"] -= quantity
                if not lot["quantity"]: lots.popleft()
            if remaining:
                lots.append({"sign":sign,"quantity":remaining,"price":price,"unit_fee":unit_fee,"fill":fill})
        marks, unrealised, entry_costs, available = [], Decimal(0), Decimal(0), True
        for lot in lots:
            fill, direction = lot["fill"], "SELL" if lot["sign"] == 1 else "BUY"
            intent = orders[fill["order_id"]]["intent"]
            valid_time = at_time(fill["at"]) <= at and at.date().isoformat() <= intent["expiry"]
            observation = fresh_depth_price(quotes.get(symbol),direction,at,symbol=symbol,token=intent["token"]) if valid_time else None
            context = contexts.get(fill["id"])
            pricing = None
            if observation:
                pricing = resolve_premium(0,observation)
            elif spot is not None and context and valid_time:
                estimated = premium_value(context["option_type"],context["entry_spot"],spot,lot["price"],fill["at"],at,
                                          context["daily_decay"],config)
                pricing = resolve_premium(estimated["premium"],reason="Fresh option quote missing; estimated from the saved fill premium, entry spot and fresh spot")
                pricing["spot_observed_at"] = spot_quote["timestamp"]
            entry_costs += lot["unit_fee"]*lot["quantity"]
            if pricing is None:
                available = False
                marks.append({"quantity":lot["quantity"]*lot["sign"],"pricing":None,
                              "reason":"No fresh option quote and no usable fresh spot/entry reference; P&L is unknown"})
            else:
                pnl = (decimal(pricing["premium"])-lot["price"])*lot["quantity"]*lot["sign"]
                unrealised += pnl
                marks.append({"quantity":lot["quantity"]*lot["sign"],"pricing":pricing,"gross_pnl":float(rounded(pnl))})
        closed_gross += gross
        closed_fees += costs
        open_gross += unrealised
        open_fees += entry_costs
        all_available = all_available and available
        results.append({"strategy":strategy,"symbol":symbol,"quantity":sum(l["quantity"]*l["sign"] for l in lots),
                        "closed_net":float(rounded(gross-costs)),"open_net":float(rounded(unrealised-entry_costs)) if available else None,
                        "marks":marks,"pnl_basis":"unavailable" if not available else "mixed" if any(m["pricing"]["basis"]=="estimated" for m in marks) else "recorded"})
    closed_net, opened_net = rounded(closed_gross-closed_fees), rounded(open_gross-open_fees)
    return {"policy":POLICY,"as_of":at.isoformat(),"closed_net":float(closed_net),
            "open_net":float(opened_net) if all_available else None,
            "combined_net":float(rounded(closed_net+opened_net)) if all_available else None,
            "positions":results,"complete":all_available,
            "estimated_charges":float(rounded(closed_fees+open_fees)),
            "note":"Paper P&L, not broker profit. Saved bid/ask fills determine closed P&L. Open marks prefer fresh option quotes, then labelled premium estimates using fresh spot; missing spot leaves the value unknown. No estimated mark creates a fill."}
