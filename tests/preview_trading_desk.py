"""Manual browser fixtures on 8766 only. Temporary databases, no broker client,
no writes accepted. Never run this server against the application's saved data.
Run: python -B -m tests.preview_trading_desk
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, parse_qs
from app import LocalServer, Handler, ROOT
from store import Store


def sample(case, base):
    now = datetime.now(timezone.utc)
    at = now.isoformat()
    old = (now-timedelta(minutes=5)).isoformat()
    expiry = (now+timedelta(days=25)).date().isoformat()
    desk = {"as_of":at,"positions":[],"orders":[],"fills":[],"events":[],"contracts":{},"history_scope":"Invented preview evidence only."}
    valuation = {"as_of":at,"closed_net":1250.5,"open_net":200,"combined_net":1450.5,"positions":[]}
    for i,(symbol,qty,strike) in enumerate([("NFO:BANKNIFTYFIX56000CE",-30,56000),("NFO:BANKNIFTYFIX57000CE",30 if case=="protected" else 10,57000)]):
        order_id="fixture-"+str(i)
        desk["positions"].append(dict(strategy="ISOLATED-FIXTURE",symbol=symbol,quantity=qty))
        desk["contracts"][symbol]=dict(expiry=expiry,instrument_type="CE",strike=strike,lot_size=30)
        desk["orders"].append(dict(id=order_id,key="fixture-hedge" if i else "fixture-short",created=old,status="COMPLETE" if abs(qty)==30 else "PARTIAL",filled=abs(qty),average_price=500,reason=None,
                                   intent=dict(strategy="ISOLATED-FIXTURE",symbol=symbol,expiry=expiry,side="BUY" if qty>0 else "SELL",quantity=30,close_only=False)))
        desk["fills"].append(dict(order_id=order_id,strategy="ISOLATED-FIXTURE",symbol=symbol,side="BUY" if qty>0 else "SELL",quantity=abs(qty),price=500,at=old))
        desk["events"].append(dict(id=i,order_id=order_id,event="COMPLETE" if abs(qty)==30 else "PARTIAL",at=old,detail={"quantity":abs(qty)}))
        valuation["positions"].append(dict(strategy="ISOLATED-FIXTURE",symbol=symbol,quantity=qty,open_net=100,pnl_basis="recorded",marks=[dict(quantity=qty,pricing=dict(basis="recorded",premium=505,source={"observed_at":at}))]))
    if case != "protected":
        desk["orders"].append(dict(id="fixture-rejected",key="replacement",created=old,status="REJECTED",filled=0,average_price=None,reason="Fixture: replacement quote unavailable",intent=dict(strategy="ISOLATED-FIXTURE",symbol="NFO:BANKNIFTYFIX58000CE",expiry=expiry,side="BUY",quantity=30,close_only=False)))
        desk["events"].insert(0,dict(id=3,order_id="fixture-rejected",event="REJECTED",at=old,detail={"reason":"Fixture: replacement quote unavailable"}))
    for p in valuation["positions"]:
        if case=="stale":
            p["marks"][0]["pricing"]["source"]["observed_at"]=old
    base.update(desk=desk,valuation=valuation,busy=case=="protected",expiry=expiry,checks=[])
    base["paper"].update(positions=desk["positions"],fill_count=2,paused=case!="protected")
    base["stream"].update(state="connected",fresh_count=0 if case=="stale" else 2,last_packet=old if case=="stale" else at,message="ISOLATED FIXTURE · no actual market connection",quotes=[dict(symbol=s,timestamp=old if case=="stale" else at,received_at=old if case=="stale" else at,last_price=56000 if s.startswith("NSE") else 500) for s in ("NSE:NIFTY BANK","NFO:BANKNIFTYFIX56000CE")])
    base["calendar"].update(open_now=True,verified_today=True,message="Fixture open session; not the actual market")
    return base


class PreviewHandler(Handler):
    def do_GET(self):
        if not self._guard():
            return
        request=urlsplit(self.path)
        if request.path=="/":
            case=parse_qs(request.query).get("case",["protected"])[0]
            self.server.fixture_case=case if case in {"protected","partial","stale"} else "protected"
            html=(ROOT/"static"/"index.html").read_text(encoding="utf-8")
            banner='<aside class="notice error" role="alert">ISOLATED TEST PREVIEW · Invented positions and prices · All writes blocked. <a href="/?case=protected#main">Protected</a> · <a href="/?case=partial#main">Partial/rejected</a> · <a href="/?case=stale#main">Stale</a></aside>'
            return self._respond(200,html.replace('<main id="main">','<main id="main">'+banner).encode(),"text/html; charset=utf-8")
        if request.path=="/api/paper/status":
            return self._respond(200,sample(self.server.fixture_case,self.server.paper.status()))
        if request.path=="/api/kite/status":
            return self._respond(200,{"status":{"configured":True,"connected":True,"session_expires_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),"message":"ISOLATED FIXTURE","latest_completed_date":datetime.now().date().isoformat()}})
        if request.path=="/api/kite/instruments/status":
            return self._respond(200,{"status":{"available":True,"instrument_count":2,"banknifty_contract_count":2,"lot_sizes":[30],"synced_at":datetime.now(timezone.utc).isoformat()}})
        return super().do_GET()

    def do_POST(self):
        self._respond(403,{"error":"All writes are blocked in this isolated preview. No broker connection exists."})
    do_PATCH=do_POST


if __name__=="__main__":
    with tempfile.TemporaryDirectory(prefix="private-algo-desk-preview-") as folder:
        server=LocalServer(("127.0.0.1",8766),Store(Path(folder)/"fixture.sqlite3"))
        server.RequestHandlerClass=PreviewHandler
        server.fixture_case="protected"
        print("ISOLATED TEST PREVIEW: http://127.0.0.1:8766/ · all writes blocked",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
