"""Invented option observations; no broker requests or live trades."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from app import spot_proxy_for_range
from backtest_config import model_config
from backtest_fees import FeeSchedule
from option_model import simulate_options, premium_value
from option_prices import OptionPriceArchive, OptionPriceBook, resolve_premium
from range_spot import replay_spot_range
from strategy_config import CLOSE_BOUNDARY, CLOSE_REVIEW
from tests.test_spot_proxy import make_day
from tests.test_paper_services import NOW, intent, quote
from paper_execution import PaperExecution
from paper_runtime import PaperRuntime
from engine import IST


def contract(strike=54500, kind="PE", expiry="2026-09-29"):
    return {"instrument":"BANKNIFTY","option_type":kind,"expiry":expiry,"strike":strike,"symbol":f"BANKNIFTY26SEP{strike}{kind}"}


def candle(clock,price,close=None):
    close = price if close is None else close
    return [f"2026-09-24T{clock}:00+05:30",price,max(price,close)+10,min(price,close)-10,close,100]


def run_model(book,mode=CLOSE_BOUNDARY,hedges=False):
    day=datetime(2026,9,24,tzinfo=IST).date()
    rows=make_day(day,{0:"bull"})
    rows[70]["high"]=54700
    checked=replay_spot_range(rows,str(day),str(day),datetime(2026,9,30,tzinfo=IST),complete_hours_only=True,strategy_mode=mode)
    spot=spot_proxy_for_range(rows,checked)
    cfg=model_config({"sell_lots":1,"ce_hedge_lots":int(hedges),"pe_hedge_lots":int(hedges)})
    return simulate_options(rows,spot,config=cfg,option_prices=book),cfg


class PriceBookTests(unittest.TestCase):
    def test_exact_contract_open_and_completed_close(self):
        book=OptionPriceBook(); c=contract()
        book.add_candle(c,candle("10:15",950,960),"test","synthetic_test")
        opened,_=book.lookup(c,"2026-09-24T10:15:00+05:30")
        closed,_=book.lookup(c,"2026-09-24T10:16:00+05:30",field="close")
        self.assertEqual((opened["premium"],closed["premium"]),(950,960))
        self.assertIsNone(book.lookup(contract(expiry="2026-10-27"),"2026-09-24T10:15:00+05:30")[0])
        self.assertIsNone(book.lookup(contract(kind="CE"),"2026-09-24T10:15:00+05:30")[0])

    def test_no_future_close_stale_candle_or_zero_volume(self):
        book=OptionPriceBook(); c=contract()
        book.add_candle(c,candle("10:15",950),"test")
        self.assertIsNone(book.lookup(c,"2026-09-24T10:15:00+05:30",field="close",candle_at="2026-09-24T10:15:00+05:30")[0])
        self.assertIsNone(book.lookup(c,"2026-09-24T10:17:00+05:30",field="close")[0])
        bad=candle("10:16",950); bad[5]=0
        with self.assertRaises(ValueError): book.add_candle(c,bad,"zero")

    def test_conflicts_do_not_pick_favourable_revision(self):
        book=OptionPriceBook(); c=contract()
        for price in (950,950,960): book.add_candle(c,candle("10:15",price),str(price))
        observation,reason=book.lookup(c,"2026-09-24T10:15:00+05:30")
        self.assertIsNone(observation); self.assertIn("Conflicting",reason)
        self.assertEqual(book.describe()["conflicting_minutes"],1)

    def test_selection_no_future_price_wrong_expiry_or_non500_strike(self):
        book=OptionPriceBook()
        for c,row in [(contract(54500),candle("10:15",950)),(contract(55000),candle("10:15",1150)),
                      (contract(54000),candle("10:16",1000)),(contract(54600),candle("10:15",1000)),
                      (contract(54000,expiry="2026-10-27"),candle("10:15",1000))]:
            book.add_candle(c,row,"test")
        selected,price=book.select_short("BANKNIFTY","2026-09-29","PE","2026-09-24T10:15:00+05:30",54000)
        self.assertEqual((selected["strike"],price["premium"]),(54500,950))

    def test_archive_roundtrip_and_readonly_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            old=Path(folder)/"old.sqlite3"
            with sqlite3.connect(old) as db:
                db.execute("CREATE TABLE downloads(id TEXT,identity TEXT,payload TEXT)")
                spec={"underlying":"BANKNIFTY","kind":"PE","expiry":"2026-09-29","strike_cents":5450000,"symbol":contract()["symbol"],"interval":"minute","from":"2026-09-24","to":"2026-09-24","source":"kite"}
                db.execute("INSERT INTO downloads VALUES (?,?,?)",("batch",json.dumps(spec),json.dumps([candle("10:15",950)])))
            db.close()
            before=old.read_bytes()
            archive=OptionPriceArchive(Path(folder)/"new.sqlite3",old)
            archive.record_candles(contract(),[candle("10:25",700)],"synthetic_test")
            book=archive.snapshot("2026-09-24","2026-09-24")
            self.assertEqual(book.describe()["candle_count"],2)
            self.assertEqual(old.read_bytes(),before)
            self.assertEqual(OptionPriceArchive(archive.path).snapshot("2026-09-24","2026-09-24").describe()["candle_count"],1)

    def test_missing_cache_falls_back_with_visible_note(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=OptionPriceArchive(Path(folder)/"new.db",Path(folder)/"missing.db")
            book=archive.snapshot("2026-09-24","2026-09-24")
            self.assertIn("unavailable",book.notes[0])
            self.assertFalse((Path(folder)/"missing.db").exists())

    def test_future_stream_quote_never_used_and_saving_is_durable(self):
        c=contract(56000,"CE", "2026-10-27"); c["symbol"]="BANKNIFTY26OCT56000CE"
        q=quote(source="zerodha_stream")
        with tempfile.TemporaryDirectory() as folder:
            archive=OptionPriceArchive(Path(folder)/"new.db")
            archive.record_quotes([(c,q)])
            book=archive.snapshot("2026-09-30","2026-09-30")
            self.assertEqual(book.lookup(c,NOW,side="SELL")[0]["premium"],499)
            self.assertIsNone(book.lookup(c,NOW-timedelta(seconds=1))[0])
            self.assertIsNone(book.lookup(c,NOW+timedelta(seconds=6))[0])

    def test_runtime_archives_only_identity_matched_option_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=OptionPriceArchive(Path(folder)/"options.db")
            runtime=PaperRuntime(None,Path(folder)/"paper.db",now=lambda:NOW,option_archive=archive)
            runtime.contracts=[{"instrument_token":123,"expiry":"2026-10-27","instrument_type":"CE","strike":56000,"tradingsymbol":"BANKNIFTY26OCT56000CE"}]
            runtime._record_quotes([quote(source="zerodha_stream"),quote(symbol="NSE:NIFTY BANK",token=260105,source="zerodha_stream")])
            book=archive.snapshot("2026-09-30","2026-09-30")
            self.assertEqual(book.describe()["quote_count"],1)
            self.assertIsNotNone(runtime.stream.on_quotes)


class RecordedModelTests(unittest.TestCase):
    def test_recorded_entry_exit_pnl_and_fees(self):
        book=OptionPriceBook()
        book.add_candle(contract(),candle("10:15",950),"entry","synthetic_test")
        book.add_candle(contract(),candle("10:25",700,710),"exit","synthetic_test")
        result,cfg=run_model(book)
        p=result["ledgers"]["selling"][0]
        self.assertEqual(result["status"],"complete",result["blocked_from"])
        self.assertEqual((p["entry_premium"],p["exit_premium"],p["gross_pnl"]),(950,710,7200))
        self.assertEqual(p["pnl_basis"],"recorded")
        self.assertIsNone(p["valuation"]["time_decay"])
        self.assertEqual(p["entry_charges"],FeeSchedule().calculate(datetime.fromisoformat(p["entry_at"]),"SELL",950,30))
        self.assertEqual(p["target_spot"],54700)
        self.assertEqual(result["pricing_summary"]["recorded"]["closed_net"],result["combined"]["realised_net"])

    def test_recorded_entry_missing_exit_uses_original_formula_from_recorded_premium(self):
        book=OptionPriceBook(); book.add_candle(contract(),candle("10:15",950),"entry")
        result,cfg=run_model(book); p=result["ledgers"]["selling"][0]
        estimate=premium_value("PE",p["entry_spot"],p["exit_spot"],950,p["entry_at"],p["exit_at"],20,cfg)
        self.assertEqual(p["exit_premium"],estimate["premium"])
        self.assertEqual(p["pnl_basis"],"mixed")
        self.assertIn("No matching",p["exit_pricing"]["fallback_reason"])

    def test_unknown_entry_contract_never_adopts_later_priced_contract(self):
        book=OptionPriceBook(); book.add_candle(contract(),candle("10:25",700),"later")
        result,_=run_model(book); p=result["ledgers"]["selling"][0]
        self.assertIsNone(p["contract"])
        self.assertEqual(p["pnl_basis"],"estimated")
        self.assertIn("not identifiable",p["exit_pricing"]["fallback_reason"])

    def test_review_does_not_use_exit_candle_close_before_its_timestamp(self):
        book=OptionPriceBook()
        for row in (candle("10:16",950),candle("10:25",700)):
            book.add_candle(contract(),row,"test")
        result,_=run_model(book,CLOSE_REVIEW); p=result["ledgers"]["selling"][0]
        self.assertEqual(p["pnl_basis"],"mixed")
        self.assertIn("future",p["exit_pricing"]["fallback_reason"])

    def test_known_hedge_can_mix_estimated_entry_recorded_exit_without_moving_trigger(self):
        book=OptionPriceBook(); hedge=contract(55000,"CE")
        # Initial hedge spot 53,500 gives premium 500 and unchanged trigger 54,500.
        book.add_candle(hedge,candle("10:25",850),"hedge-exit")
        result,_=run_model(book,hedges=True)
        p=result["ledgers"]["ce_hedge"][0]
        self.assertEqual(p["entry_premium"],500)
        self.assertEqual(p["trigger_spot"],54500)
        self.assertEqual(p["pnl_basis"],"mixed")
        self.assertEqual(p["exit_premium"],850)

    def test_empty_archive_preserves_legacy_model_totals(self):
        before,_=run_model(None,hedges=True)
        after,_=run_model(OptionPriceBook(),hedges=True)
        self.assertEqual(before["combined"],after["combined"])
        self.assertEqual(before["charges"],after["charges"])

    def test_rollover_prices_use_exact_old_and_new_expiry_and_keep_spot_target(self):
        rows=make_day(datetime(2026,9,28,tzinfo=IST).date(),{0:"bull"})
        rows[360].update(open=54400,low=54400,high=54700,close=54700)
        checked=replay_spot_range(rows,"2026-09-28","2026-09-28",datetime(2026,9,30,tzinfo=IST),complete_hours_only=True,strategy_mode=CLOSE_BOUNDARY)
        book=OptionPriceBook()
        for c,row in [(contract(),candle("10:15",950)),(contract(),candle("15:15",870)),
                      (contract(expiry="2026-10-27"),candle("15:15",990,700))]:
            row[0]=row[0].replace("2026-09-24","2026-09-28")
            book.add_candle(c,row,"test")
        result=simulate_options(rows,spot_proxy_for_range(rows,checked),config=model_config({"sell_lots":1,"ce_hedge_lots":0,"pe_hedge_lots":0}),option_prices=book)
        self.assertEqual(result["status"],"complete",result["blocked_from"])
        legs=result["ledgers"]["selling"]
        self.assertEqual([(p["entry_premium"],p["exit_premium"]) for p in legs],[(950,870),(990,700)])
        self.assertTrue(all(p["target_spot"]==54700 for p in legs))
        self.assertEqual(legs[1]["exit_reason"],"target")


class PaperValuationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now=NOW
        self.path=Path(self.temp.name)/"paper.db"
        self.service=PaperExecution(self.path,now=lambda:self.now)
        o=self.service.submit("entry",intent())
        self.service.match(o["id"],quote(),healthy=True,valuation_context={"option_type":"CE","entry_spot":56000,"daily_decay":30})

    def spot(self):
        return quote(at=self.now,symbol="NSE:NIFTY BANK",token=260105,last_price=56400)

    def test_fresh_quote_wins_over_model_and_never_changes_fills(self):
        mark=quote(at=self.now,bids=[{"price":600,"quantity":100}],asks=[{"price":602,"quantity":100}])
        result=self.service.valuation(quotes={mark["symbol"]:mark},spot_quote=self.spot())
        self.assertEqual(result["positions"][0]["pnl_basis"],"recorded")
        self.assertEqual(result["positions"][0]["marks"][0]["gross_pnl"],(600-501)*30)
        self.assertEqual(self.service.status()["fill_count"],1)

    def test_missing_or_stale_option_uses_model_with_fresh_spot(self):
        self.now+=timedelta(minutes=1)
        q=quote()
        result=self.service.valuation(quotes={q["symbol"]:q},spot_quote=self.spot())
        mark=result["positions"][0]["marks"][0]["pricing"]
        self.assertEqual(mark["basis"],"estimated")
        self.assertEqual(result["positions"][0]["pnl_basis"],"mixed")
        self.assertAlmostEqual(mark["premium"],710.98)
        self.assertEqual(self.service.status()["fill_count"],1)

    def test_missing_spot_and_option_remains_unknown_not_zero(self):
        result=self.service.valuation()
        self.assertIsNone(result["open_net"]); self.assertIsNone(result["combined_net"])
        self.assertEqual(result["positions"][0]["pnl_basis"],"unavailable")

    def test_saved_entry_reference_survives_restart(self):
        self.now+=timedelta(minutes=1)
        restarted=PaperExecution(self.path,now=lambda:self.now)
        result=restarted.valuation(spot_quote=self.spot())
        self.assertTrue(restarted.status()["paused"])
        self.assertEqual(result["positions"][0]["pnl_basis"],"mixed")

    def test_partial_close_keeps_closed_profit_separate_and_charges_once(self):
        self.now+=timedelta(seconds=1)
        o=self.service.submit("part-exit",intent(side="SELL",close_only=True,quantity=10))
        q=quote(at=self.now,bids=[{"price":600,"quantity":100}],asks=[{"price":601,"quantity":100}])
        self.service.match(o["id"],q,healthy=True)
        v=self.service.valuation(quotes={q["symbol"]:q})
        self.assertEqual(v["positions"][0]["quantity"],20)
        self.assertAlmostEqual(v["combined_net"],(600-501)*30-v["estimated_charges"],places=2)
        self.assertGreater(v["closed_net"],0); self.assertGreater(v["open_net"],0)


if __name__=="__main__": unittest.main()
