"""Read-only cached September validation; never connects to a broker."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import LocalServer, spot_proxy_for_range
from engine import IST
from option_model import simulate_options
from option_prices import OptionPriceArchive
from range_spot import replay_spot_range
from store import Store
from strategy_config import CLOSE_BOUNDARY


def check(cache, workspace):
    with closing(sqlite3.connect(cache.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        record=db.execute("SELECT id,payload FROM downloads WHERE json_extract(identity,'$.symbol')='NIFTY BANK' AND json_extract(identity,'$.from')='2026-09-01' AND json_extract(identity,'$.to')='2026-09-23' AND json_extract(identity,'$.interval')='minute' ORDER BY rowid DESC LIMIT 1").fetchone()
    raw=json.loads(record[1]); raw=raw["data"]["candles"] if isinstance(raw,dict) else raw
    rows=[dict(timestamp_ist=r[0],open=r[1],high=r[2],low=r[3],close=r[4]) for r in raw]
    archive=OptionPriceArchive(workspace/"options.sqlite3",cache)
    book=archive.snapshot("2026-09-01","2026-09-23")
    checked=replay_spot_range(rows,"2026-09-01","2026-09-23",datetime(2026,9,30,tzinfo=IST),
                              complete_hours_only=True,ignore_missing_minutes=True,empty_days_are_holidays=True,strategy_mode=CLOSE_BOUNDARY)
    checked["source"]="SAVED ZERODHA SPOT DATA · 1–23 September 2026 · not live prices"
    spot=spot_proxy_for_range(rows,checked)
    recorded=simulate_options(rows,spot,option_prices=book)
    estimated=simulate_options(rows,spot)
    fields=("entry_at","exit_at","entry_spot","exit_spot","trigger_spot","parent_spot_trade","expiry")
    for key in recorded["ledgers"]:
        assert [{f:p.get(f) for f in fields} for p in recorded["ledgers"][key]] == [{f:p.get(f) for f in fields} for p in estimated["ledgers"][key]],key
    assert recorded["event_validation"]["all_processed"],recorded["blocked_from"]
    count=sum(s["positions"] for basis,s in recorded["pricing_summary"].items() if basis in ("recorded","mixed"))
    assert count>0,"No saved option price reached a valuation"
    for key in ("realised_net","open_net"):
        summary_key="closed_net" if key=="realised_net" else "open_net"
        assert abs(sum(s[summary_key] for s in recorded["pricing_summary"].values())-recorded["combined"][key]) < .011
    evidence={"scope":"Existing saved September prices, not live fills; current hourly-close risk rules",
              "spot_download_hash":record[0],"spot_minutes":len(rows),"option_data":book.describe(),
              "status":recorded["status"],"validation_status":recorded["validation_status"],"pricing_summary":recorded["pricing_summary"],
              "unchanged_strategy_and_hedge_events":True,"recorded_first_totals":recorded["combined"],"model_only_totals":estimated["combined"],
              "warnings":recorded["warnings"],"examples":[{"ledger":p["ledger"],"contract":p["contract"],"entry_at":p["entry_at"],"exit_at":p.get("exit_at"),
                  "entry_pricing":p["entry_pricing"],"exit_or_mark_pricing":p.get("exit_pricing",p.get("mark_pricing")),"pnl_basis":p.get("pnl_basis"),"net_pnl":p.get("net_pnl")}
                 for group in recorded["ledgers"].values() for p in group]}
    return evidence,{"range":checked,"spot_proxy":spot,"option_model":recorded}


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--cache",type=Path,default=Path.home()/"buildweek/algo/.local/history.sqlite3")
    parser.add_argument("--preview",action="store_true")
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="recorded-prices-check-") as folder:
        evidence,result=check(args.cache,Path(folder))
        (Path(__file__).parent/"recorded-prices-check.json").write_text(json.dumps(evidence,indent=2,ensure_ascii=False),encoding="utf-8")
        print(json.dumps({k:v for k,v in evidence.items() if k!="examples"},indent=2),flush=True)
        if args.preview:
            server=LocalServer(("127.0.0.1",8766),Store(Path(folder)/"preview.sqlite3"))
            server.kite.replay_job={"id":"cached-price-preview","status":"complete","start_date":"2026-09-01","end_date":"2026-09-23","result":result}
            print("Read-only saved-data preview: http://127.0.0.1:8766/#past-data-check",flush=True)
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
