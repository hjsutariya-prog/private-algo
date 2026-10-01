"""Persistent historical paper starting point, never an execution ledger.

Reuses the existing hourly model unchanged. A snapshot cannot submit orders,
become a paper fill, or establish current protection. Other research settings
cannot silently replace this explicitly named starting point.
"""
from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

from backtest_config import model_config
from engine import IST
from market_sessions import latest_completed_session, session_windows
from strategy_config import CLOSE_BOUNDARY, strategy_config

START = "2026-09-01"
QUANTITY_KEYS = ("sell_lots", "ce_hedge_lots", "pe_hedge_lots", "units_per_lot")
QUANTITIES = {k: model_config()[k] for k in QUANTITY_KEYS}


def request_settings():
    return {"start_date": START, "strategy_mode": CLOSE_BOUNDARY, "model_config": dict(QUANTITIES)}


def project_result(result, source):
    period, options, spot = result["range"], result["option_model"], result["spot_proxy"]
    if (period.get("start_date") != START or options.get("strategy") != strategy_config(CLOSE_BOUNDARY)
            or options.get("config") != model_config(QUANTITIES)):
        raise ValueError("This result has different dates, timing rules or quantities from the September paper replay.")
    if (options.get("status") != "complete" or not options.get("event_validation", {}).get("all_processed")
            or spot.get("status") != "complete" or spot.get("uncertain_positions")):
        raise ValueError("Incomplete or uncertain results cannot establish an open-position snapshot.")
    end = date.fromisoformat(period["end_date"])
    as_of = datetime.fromisoformat(options["as_of"])
    if as_of.tzinfo is None or not START <= as_of.astimezone(IST).date().isoformat() <= end.isoformat():
        raise ValueError("The replay valuation time is outside the checked range.")
    legs = [dict(p) for group in options["ledgers"].values() for p in group if p["status"] == "open"]
    for leg in legs:
        if leg.get("mark_at") != options["as_of"] or type(leg.get("quantity")) is not int or leg["quantity"] <= 0:
            raise ValueError("An open replay leg lacks a consistent valuation or quantity.")
    return {"kind":"historical_paper_replay", "is_execution":False,
            "start_date":START, "end_date":end.isoformat(), "as_of":options["as_of"],
            "saved_at":datetime.now(timezone.utc).isoformat(), "source":source,
            "strategy":options["strategy"], "config":options["config"],
            "starting_state":"Flat on 1 September; independent model hedges begin at the first checked minute.",
            "draft_link":"Shared model, not a saved draft version or an automatic deployment.",
            "legs":legs, "pending_replacements":options.get("pending_replacements",[]),
            "combined":options["combined"], "validation_status":options.get("validation_status"),
            "warnings":options.get("warnings",[]), "fee_version":options.get("fee_version"),
            "calendar_version":options.get("calendar_version"),
            "pricing_summary":options.get("pricing_summary"),
            "protection":"Historical model only; not confirmed protection or executable positions."}


class PaperReplay:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS replay_snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT payload FROM replay_snapshot WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def capture(self, result, source="Zerodha historical download"):
        snapshot = project_result(result, source)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM replay_snapshot WHERE id=1").fetchone()
            if row and datetime.fromisoformat(json.loads(row[0])["as_of"]) > datetime.fromisoformat(snapshot["as_of"]):
                return False  # An older cache must never roll the desk backwards.
            db.execute("INSERT OR REPLACE INTO replay_snapshot VALUES (1, ?)", (json.dumps(snapshot, allow_nan=False),))
        return True

    def capture_matching(self, result):
        options=result.get("option_model") or {}
        if (result.get("range",{}).get("start_date") != START
                or options.get("strategy",{}).get("id") != CLOSE_BOUNDARY
                or any(options.get("config",{}).get(k) != v for k,v in QUANTITIES.items())):
            return False
        return self.capture(result)

    def status(self, now=None):
        now=now or datetime.now(IST)
        expected=latest_completed_session(now)
        snapshot=self.snapshot()
        reached=False
        if snapshot:
            windows=session_windows(expected)
            reached=(snapshot["end_date"] >= expected.isoformat() and bool(windows)
                     and datetime.fromisoformat(snapshot["as_of"]) >= windows[-1][1])
        return {"settings":request_settings(), "snapshot":snapshot, "through_latest_close":reached,
                "latest_completed_date":expected.isoformat(),
                "note":"Through the latest completed session only; not intraday positions or live paper executions." if reached
                else "Not current. Missing later sessions can change entries, exits, hedges and rollovers."}


def seed_from_cache(cache_path, destination, option_archive):
    """Explicit one-off import from a successful saved download, read-only.

    Never extend its requested date range or treat uncached later days as holidays.
    """
    from app import spot_proxy_for_range
    from range_spot import replay_spot_range
    from option_model import simulate_options
    cache=Path(cache_path).resolve()
    with closing(sqlite3.connect(cache.as_uri()+"?mode=ro", uri=True)) as db:
        records=db.execute("SELECT id,identity,payload FROM downloads WHERE json_extract(identity,'$.symbol')='NIFTY BANK' AND json_extract(identity,'$.interval')='minute' ORDER BY rowid DESC").fetchall()
    candidates=[]
    latest=latest_completed_session(datetime.now(IST)).isoformat()
    for identity, metadata, payload in records:
        meta=json.loads(metadata)
        if (meta.get("source")=="kite" and meta.get("exchange")=="NSE" and meta.get("token")==260105
                and meta.get("from", "9999") <= START <= meta.get("to", "") <= latest):
            candidates.append((meta["to"], identity, payload))
    if not candidates:
        raise ValueError("No saved BANKNIFTY minute download covers 1 September 2026.")
    # Stable sort keeps the latest cached revision when requested ends tie.
    end, identity, payload=sorted(candidates,key=lambda r:r[0],reverse=True)[0]
    raw=json.loads(payload)
    raw=raw["data"]["candles"] if isinstance(raw,dict) else raw
    rows=[dict(timestamp_ist=r[0],open=r[1],high=r[2],low=r[3],close=r[4]) for r in raw if START <= r[0][:10] <= end]
    checked=replay_spot_range(rows, START, end, datetime.now(IST), complete_hours_only=True,
                             ignore_missing_minutes=True, empty_days_are_holidays=True, strategy_mode=CLOSE_BOUNDARY)
    spot=spot_proxy_for_range(rows,checked)
    options=simulate_options(rows,spot,config=model_config(QUANTITIES),option_prices=option_archive.snapshot(START,end),
                             assumed_holidays={date.fromisoformat(d["date"]) for d in checked["days"] if d.get("assumed_holiday")})
    result={"range":checked,"spot_proxy":spot,"option_model":options}
    destination.capture(result, source=f"Saved Zerodha spot download {identity}; option observations first, estimates when missing")
    return destination.status()


if __name__=="__main__":
    import argparse
    from option_prices import OptionPriceArchive
    parser=argparse.ArgumentParser(description="Reconstruct historical paper positions; cannot submit orders.")
    parser.add_argument("--cache",type=Path,required=True)
    parser.add_argument("--snapshot",type=Path,required=True)
    parser.add_argument("--option-archive",type=Path,required=True)
    args=parser.parse_args()
    value=seed_from_cache(args.cache,PaperReplay(args.snapshot),OptionPriceArchive(args.option_archive,args.cache))
    snap=value["snapshot"]
    print(json.dumps({"as_of":snap["as_of"],"through_latest_close":value["through_latest_close"],"open_legs":len(snap["legs"]),"legs":[{k:p.get(k) for k in ("ledger","option_type","strike","expiry","quantity","entry_at","mark_premium","net_pnl","stop_spot","target_spot")} for p in snap["legs"]]},indent=2))
