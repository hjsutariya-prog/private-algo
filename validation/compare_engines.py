"""Read-only cached-price comparison. Run from private-algo; no broker/network.

Reference source is unchanged on disk. The two weekly-cap overrides and uniform
grid reproduce scripts/compare_2025_old_rules_no_weekly.py, plus its lower hedge
formula. Financial alignment is report-only: revalue open legs at one common
time/price and apply the same grouped dated fee schedule to both sets of legs.
"""
import hashlib
import inspect
import json
import sqlite3
import sys
import types
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OLD = Path(r"C:\Users\Harshal Sutariya\buildweek\algo")
sys.path[:0] = [str(ROOT.parent), str(OLD)]
from app import spot_proxy_for_range
from backtest_config import model_config
from backtest_fees import FeeSchedule
from engine import IST
from market_sessions import session_windows
from option_model import simulate_options
from range_spot import replay_spot_range
from strategy_config import COMPARISON
from algo_engine.domain import Candle
import algo_engine.thousand_levels_hourly as old_spot
import algo_engine.short_option_hedge_model as old_option


def reference():
    source = inspect.getsource(old_spot)
    for a, b in (
        ("if entries[week][side] >= 1 or positions[side] is not None or pending[side] is not None:", "if positions[side] is not None or pending[side] is not None:"),
        ("if entries[current_week][side] >= 1 or positions[side] is not None:", "if positions[side] is not None:"),
    ):
        assert source.count(a) == 1
        source = source.replace(a, b)
    module = types.ModuleType("algo_engine.validation_no_weekly")
    module.__package__ = "algo_engine"
    sys.modules[module.__name__] = module
    exec(compile(source, "<reference-no-weekly>", "exec"), module.__dict__)
    module.reference_grid_cents = lambda close: 100_000
    old_option.hedge_entry_premium_points = lambda cents: Decimal(cents // 1_000_000) * 100
    return module


def load_cache(start, end):
    db = sqlite3.connect((OLD / ".local/history.sqlite3").as_uri() + "?mode=ro", uri=True)
    records = db.execute("SELECT id,identity,payload FROM downloads WHERE json_extract(identity,'$.symbol')='NIFTY BANK' AND json_extract(identity,'$.interval')='minute' AND json_extract(identity,'$.to')>=? AND json_extract(identity,'$.from')<=? ORDER BY rowid", (start, end)).fetchall()
    db.close()
    rows, conflicts, hashes = {}, [], []
    for digest, identity, payload in records:
        raw = json.loads(payload)
        values = raw["data"]["candles"] if isinstance(raw, dict) else raw
        contributed = False
        for r in values:
            if not start <= r[0][:10] <= end:
                continue
            key = at(r[0]).isoformat()
            row = dict(timestamp_ist=key, open=r[1], high=r[2], low=r[3], close=r[4])
            if key in rows and rows[key] != row:
                conflicts.append({"at": key, "earlier": rows[key], "later": row, "source": digest})
            rows[key] = row
            contributed = True
        if contributed:
            hashes.append(digest)
    ordered = [rows[stamp] for stamp in sorted(rows)]
    return ordered, {"source_batches": len(hashes), "source_hashes": hashes,
                     "canonical_candles_sha256": hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest(),
                     "overlap_conflicts": len(conflicts), "revisions": conflicts,
                     "revision_policy": "Newest stored batch wins per timestamp, before running either engine. Every differing revision is retained here; neither source is independently verified."}


def at(value):
    if value is None:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=IST) if "T" not in value else datetime.fromisoformat(value)


def stamp(value):
    return at(value).isoformat() if value else None


def rows_to_bars(rows):
    return [Candle(at(r["timestamp_ist"]), *[round(r[k] * 100) for k in ("open", "high", "low", "close")]) for r in rows]


def old_trade(t):
    return {"side": "bull" if t["side"] == "Long" else "bear", "signal_at": stamp(t["signal_known_at"]),
            "entry_at": stamp(t["entryTime"]), "entry": t["entry"] / 100, "stop": t["stop"] / 100, "target": t["target"] / 100,
            "state": {"CLOSED": "closed", "UNCERTAIN": "uncertain", "OPEN_AT_RANGE_END": "open"}[t["state"]],
            "exit_bar": stamp(t.get("exitBar")), "exit_time": stamp(t.get("exitTimeUsed")),
            "exit": t.get("exit") / 100 if t.get("exit") is not None else None, "points": t.get("spot_points")}


def new_trade(t):
    return {"side": t["side"], "signal_at": t["signal_at"], "entry_at": t["entry_at"], "entry": t["entry_spot"],
            "stop": t["stop_spot"], "target": t["target_spot"], "state": t["status"],
            "exit_bar": t.get("exit_at"), "exit_time": t.get("exit_valuation_at"), "exit": t.get("exit_spot"),
            "points": round(t["spot_points_proxy"], 2) if t["status"] != "open" and t.get("spot_points_proxy") is not None else None}


def compare_lists(old, new, identity):
    a, b = {identity(x): x for x in old}, {identity(x): x for x in new}
    assert len(a) == len(old) and len(b) == len(new), "Comparison identities must be unique"
    return {"old_count": len(old), "new_count": len(new), "matched": sum(a[k] == b[k] for k in a.keys() & b.keys()),
            "only_old": [a[k] for k in a.keys() - b.keys()], "only_new": [b[k] for k in b.keys() - a.keys()],
            "changed": [{"old": a[k], "new": b[k]} for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]}


def canonical_legs(old_cash, new_cash):
    a, b = [], []
    for kind, legs in (("selling", old_cash["short_legs"]), ("hedge", old_cash["hedge_legs"])):
        for p in legs:
            reason = {"CLOSED_ON_SELLING_ROLL": "monthly_rollover", "CLOSED_ON_MONDAY_ROLL": "monthly_rollover",
                      "CLOSED_ON_HEDGE_TARGET": "hedge_replacement", "VALUED_AT_RANGE_END": "open", "CLOSED": "strategy_exit"}[p["state"]]
            a.append({"kind": kind, "side": p["option_type"], "entry_at": stamp(p["entry_time"]), "entry_spot": p["entry_spot_cents"] / 100,
                      "entry_premium": p["entry_premium_points"], "expiry": p["contract_expiry"], "reason": reason,
                      "exit_at": None if reason == "open" else stamp(p["exit_time"]),
                      "exit_spot": None if reason == "open" else p["exit_spot_cents"] / 100})
    for kind, legs in new_cash["ledgers"].items():
        for p in legs:
            reason = "open" if p["status"] == "open" else p["exit_reason"] if p["exit_reason"] in ("monthly_rollover", "hedge_replacement") else "strategy_exit"
            b.append({"kind": "selling" if kind == "selling" else "hedge", "side": p["option_type"], "entry_at": p["entry_at"],
                      "entry_spot": p["entry_spot"], "entry_premium": p["entry_premium"], "expiry": p["expiry"], "reason": reason,
                      "exit_at": p.get("exit_at"), "exit_spot": p.get("exit_spot")})
    return a, b


def price_reference_legs(legs, mark_time, mark_spot):
    """Independent old arithmetic, same quantity, dated fees and final mark."""
    fees = FeeSchedule()
    totals = {"realised_gross": Decimal(0), "realised_charges": Decimal(0), "open_gross": Decimal(0), "open_entry_charges": Decimal(0)}
    records = []
    for p in legs:
        opened, sold = p["reason"] == "open", p["kind"] == "selling"
        end, price = (mark_time, mark_spot) if opened else (at(p["exit_at"]), p["exit_spot"])
        v = old_option._model_leg(option_type=p["side"], entry_spot_cents=round(p["entry_spot"] * 100), exit_spot_cents=round(price * 100),
                                  entry_at=at(p["entry_at"]), exit_at=end, entry_points=Decimal(str(p["entry_premium"])), theta_per_day=Decimal(20 if sold else 30))
        entry_fee = fees.calculate(at(p["entry_at"]), "SELL" if sold else "BUY", p["entry_premium"], 120)["total"]
        exit_fee = 0 if opened else fees.calculate(end, "BUY" if sold else "SELL", v["exit_premium_points"], 120)["total"]
        gross = (Decimal(str(p["entry_premium"])) - Decimal(str(v["exit_premium_points"]))) * (1 if sold else -1) * 120
        charge = Decimal(str(entry_fee)) + Decimal(str(exit_fee))
        totals["open_gross" if opened else "realised_gross"] += gross
        totals["open_entry_charges" if opened else "realised_charges"] += charge
        records.append({**p, "valuation_at": end.isoformat(), "valuation_spot": price, "premium": v["exit_premium_points"], "gross": float(gross), "charges": float(charge)})
    totals["realised_net"] = totals["realised_gross"] - totals["realised_charges"]
    totals["open_net"] = totals["open_gross"] - totals["open_entry_charges"]
    totals["total_net"] = totals["realised_net"] + totals["open_net"]
    return {k: float(v) for k, v in totals.items()}, records


def compare(start, end):
    rows, source = load_cache(start, end)
    # No synthetic candles, interpolation, changed prices or forced closing fills.
    checked = replay_spot_range(rows, start, end, datetime(2026,9,30,23,tzinfo=IST), complete_hours_only=True,
                                empty_days_are_holidays=True, ignore_missing_minutes=True, strategy_mode=COMPARISON)
    spot = spot_proxy_for_range(rows, checked)
    option = simulate_options(rows, spot)
    bars = rows_to_bars(rows)
    ref = reference()
    old = ref.thousand_levels_hourly_replay(bars, date.fromisoformat(start), date.fromisoformat(end), stop_cents=50000, target_cents=100000)
    a, b = [old_trade(t) for t in old.trades], [new_trade(t) for t in spot["positions"]]
    trades = compare_lists(a, b, lambda t: (t["entry_at"], t["side"]))
    old_signals = [{"at": (h.start + timedelta(hours=1)).astimezone(IST).isoformat(), "side": "bull" if s == 1 else "bear", "level": level/100}
                   for h in ref.aggregate_hourly(bars) for s in (1,-1) if (level := ref._level(h,s)) is not None]
    new_signals = [{"at": s["bar_end"], "side": s["side"], "level": s["level"]} for d in checked["days"] for s in d.get("signals", [])]
    result = {"range": [start, end], "source": source, "candles": len(rows), "first": rows[0]["timestamp_ist"], "last": rows[-1]["timestamp_ist"],
              "reference_source_sha256": {str(p.relative_to(OLD)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                          [OLD/"algo_engine/thousand_levels_hourly.py", OLD/"algo_engine/short_option_hedge_model.py", OLD/"scripts/compare_2025_old_rules_no_weekly.py"]},
              "strategy": spot["strategy"], "start_state": "flat, hedges begin at first minute", "quantity": "4 lots of 30 units per leg; grouped dated fees",
              "signals": compare_lists(old_signals, new_signals, lambda t: (t["at"], t["side"])), "trades": trades,
              "new_validation": option["validation_status"], "warnings": option["warnings"], "missing_minutes": option["skipped_minute_count"],
              "data_gaps": option["data_gaps"],
              "spot_states": dict(Counter(p["status"] for p in spot["positions"])), "event_validation": option["event_validation"],
              "rollovers": option["rollovers"], "delayed_actions": option["delayed_actions"]}
    try:
        old_cash = old_option.build_comparison(old.trades, bars, monday_rollover=True)
    except ValueError as exc:
        result["reference_option_blocked"] = str(exc)
        return result
    old_legs, new_legs = canonical_legs(old_cash, option)
    result["legs"] = compare_lists(old_legs, new_legs, lambda p: (p["kind"], p["side"], p["entry_at"]))
    result["hedge_replacements"] = {"old": sum(p["reason"] == "hedge_replacement" for p in old_legs), "new": sum(p["reason"] == "hedge_replacement" for p in new_legs)}
    mark_at, mark_spot = at(option["as_of"]), rows[-1]["close"]
    result["common_final_mark"] = {"at": mark_at.isoformat(), "spot": mark_spot, "basis": "last supplied candle close at its completed-minute boundary"}
    reference_totals, reference_records = price_reference_legs(old_legs, mark_at, mark_spot)
    result["aligned_reference_totals"] = reference_totals
    result["new_totals"] = option["combined"]
    result["aligned_differences_new_minus_reference"] = {key: round(option["combined"][key] - value, 2) for key, value in reference_totals.items()}
    result["aligned_reference_legs"] = reference_records
    result["new_legs"] = sum(option["ledgers"].values(), [])
    result["legacy_headline_four_lot_closed"] = old_cash["quantity_comparison"]["with_hedge"]
    result["legacy_final_mark"] = {"at": rows[-1]["timestamp_ist"], "spot": rows[-1]["open"]}
    indexed = {(p["ledger"] == "selling", p["option_type"], p["entry_at"]): p for p in result["new_legs"]}
    money_differences = []
    for p in reference_records:
        candidate = indexed.get((p["kind"] == "selling", p["side"], p["entry_at"]))
        if candidate is None:
            continue
        gross = round(candidate["gross_pnl"] - p["gross"], 2)
        charges = round(candidate["charges"] - p["charges"], 2)
        if gross or charges:
            money_differences.append({"kind": p["kind"], "side": p["side"], "entry_at": p["entry_at"],
                                      "reference_exit": p["exit_at"], "new_exit": candidate.get("exit_at"),
                                      "gross_difference": gross, "charges_difference": charges, "net_difference": round(gross-charges,2)})
    result["per_leg_money_differences"] = money_differences
    # Assert event equivalence before certifying the financial reconciliation.
    assert trades["matched"] == trades["old_count"] == trades["new_count"]
    assert not result["signals"]["only_old"] and not result["signals"]["changed"]
    assert not result["legs"]["only_old"] and not result["legs"]["only_new"]
    assert result["event_validation"]["all_processed"]
    if start == "2026-09-01":
        assert result["signals"]["matched"] == 10 and result["legs"]["matched"] == 7
        assert all(value == 0 for value in result["aligned_differences_new_minus_reference"].values())
    else:
        assert result["signals"]["only_new"] == [{"at":"2025-10-21T14:45:00+05:30", "side":"bear", "level":58000}]
        assert len(result["legs"]["changed"]) == len(money_differences) == 1
        assert money_differences[0] == {"kind":"hedge", "side":"CE", "entry_at":"2026-07-27T15:15:00+05:30",
                                       "reference_exit":"2026-08-04T09:15:00+05:30", "new_exit":"2026-08-03T15:30:00+05:30",
                                       "gross_difference":2662.8, "charges_difference":5.11, "net_difference":2657.69}
        assert len(result["rollovers"]) == 20
        assert sum(p["reason"] == "monthly_rollover" for p in old_legs) == sum(p["reason"] == "monthly_rollover" for p in new_legs)
        assert round(sum(p["net_difference"] for p in money_differences), 2) == result["aligned_differences_new_minus_reference"]["total_net"]
    result["reconciliation_assertions"] = "passed; no unexplained event or cash differences"
    return result


if __name__ == "__main__":
    cases = [("2026-09-01", "2026-09-23"), ("2025-01-01", "2026-09-22")]
    output = []
    for start, end in cases:
        print("Comparing", start, end, flush=True)
        result = compare(start, end)
        output.append(result)
        print(json.dumps({k: v for k,v in result.items() if k not in ("source", "aligned_reference_legs", "new_legs", "rollovers")}, indent=2), flush=True)
    (ROOT / "comparison-results.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
