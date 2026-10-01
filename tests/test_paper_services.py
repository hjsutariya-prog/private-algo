"""Synthetic regression tests. Passing these is NOT a real-quote acceptance run."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine import IST
from kite_client import KiteClient, KiteNetworkError
from market_stream import MarketStream, ObservedCandles, decode_frame
from paper_execution import PaperExecution
from paper_runtime import PaperRuntime
from verified_calendar import VerifiedCalendar
from market_sessions import session_windows

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=IST)


def intent(symbol="NFO:BANKNIFTY26OCT56000CE", token=123, **kwargs):
    return dict(strategy="test", symbol=symbol, token=token, side="BUY", quantity=30,
                lot_size=30, expiry="2026-10-27", close_only=False, depends_on=None) | kwargs


def quote(at=NOW, symbol="NFO:BANKNIFTY26OCT56000CE", token=123, **kwargs):
    return dict(symbol=symbol, token=token, timestamp=at.isoformat(), received_at=at.isoformat(),
                source="synthetic_test", last_price=500,
                bids=[{"price": 499, "quantity": 100}], asks=[{"price": 501, "quantity": 100}]) | kwargs


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "paper.db"
        self.now = NOW
        self.service = PaperExecution(self.path, now=lambda: self.now, calendar=VerifiedCalendar())

    def buy(self, key="buy", **kwargs):
        order = self.service.submit(key, intent(**kwargs))
        return self.service.match(order["id"], quote(symbol=order["intent"]["symbol"], token=order["intent"]["token"]), healthy=True)

    def test_idempotent_concurrent_submission_and_fills(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            orders = list(pool.map(lambda _: self.service.submit("one", intent()), range(20)))
        self.assertEqual(len({o["id"] for o in orders}), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.match(orders[0]["id"], quote(), healthy=True), range(20)))
        self.assertTrue(all(o["filled"] == 30 for o in results))
        self.assertEqual(self.service.status()["fill_count"], 1)

    def test_key_cannot_change_intent(self):
        self.service.submit("a", intent())
        with self.assertRaisesRegex(ValueError, "different"):
            self.service.submit("a", intent(quantity=60))

    def test_desk_read_is_non_mutating_and_keeps_older_open_exposure(self):
        opened = self.buy()
        pending = self.service.submit("pending-old", intent(strategy="pending"))
        for index in range(35):
            order = self.service.submit("later-"+str(index), intent(strategy="later"))
            self.service.reject(order["id"], "Fixture rejection")
        before = self.service.status()
        desk = self.service.desk_records()
        self.assertIn(opened["id"], {o["id"] for o in desk["orders"]})
        self.assertIn(pending["id"], {o["id"] for o in desk["orders"]})
        self.assertEqual(desk["fills"][0]["quantity"], 30)
        self.assertEqual(desk["positions"], before["positions"])
        self.assertEqual(self.service.status(), before)

    def test_desk_restores_saved_contract_metadata_without_current_prices(self):
        contract = {"tradingsymbol":"BANKNIFTY26OCT56000CE", "strike":56000,
                    "instrument_type":"CE", "expiry":"2026-10-27", "lot_size":30}
        self.service.record_check({"selection":{"hedges":{"CE":[contract,{"last_price":500}]}}})
        desk = self.service.desk_records()
        self.assertEqual(desk["contracts"]["NFO:BANKNIFTY26OCT56000CE"]["strike"], 56000)
        self.assertNotIn("last_price", desk["contracts"]["NFO:BANKNIFTY26OCT56000CE"])

    def test_fill_uses_ask_not_last_price_and_roundtrip_fees(self):
        buy = self.buy()
        self.assertEqual(buy["average_price"], 501)
        sell = self.service.submit("sell", intent(side="SELL", close_only=True))
        sell = self.service.match(sell["id"], quote(), healthy=True)
        self.assertEqual(sell["average_price"], 499)
        evidence = self.service.evidence("test")
        self.assertEqual(evidence["closed_gross"], -60)
        self.assertEqual(len(evidence["estimated_fees"]), 2)
        self.assertTrue(all(f["brokerage"] == 20 for f in evidence["estimated_fees"]))
        self.assertLess(evidence["closed_net_estimate"], -60)
        self.assertTrue(any(e["event"] == "QUOTE_EVIDENCE" for e in evidence["events"]))

    def test_partial_depth_not_reused_across_orders_or_restart(self):
        a = self.service.submit("a", intent())
        b = self.service.submit("b", intent(strategy="other"))
        shallow = quote(asks=[{"price":501,"quantity":20}])
        self.assertEqual(self.service.match(a["id"], shallow, healthy=True)["filled"], 20)
        self.assertEqual(self.service.match(a["id"], shallow, healthy=True)["filled"], 20)
        self.assertEqual(self.service.match(b["id"], shallow, healthy=True)["filled"], 0)
        restarted = PaperExecution(self.path, now=lambda: self.now)
        self.assertTrue(restarted.status()["paused"])
        restarted.reconcile_and_resume()
        c = restarted.submit("c", intent(strategy="third"))
        self.assertEqual(restarted.match(c["id"], shallow, healthy=True)["filled"], 0)

    def test_new_quote_completes_partial_and_charges_one_order(self):
        a = self.service.submit("a", intent())
        self.service.match(a["id"], quote(asks=[{"price":501,"quantity":20}]), healthy=True)
        self.now += timedelta(seconds=1)
        result = self.service.match(a["id"], quote(self.now, asks=[{"price":502,"quantity":20}]), healthy=True)
        self.assertEqual((result["status"], result["filled"]), ("COMPLETE", 30))
        self.assertEqual(len(self.service.evidence("test")["estimated_fees"]), 1)

    def test_new_hedge_must_complete_before_old_sale(self):
        self.buy()
        new = self.service.submit("new", intent(symbol="NFO:BANKNIFTY26OCT57000CE", token=124))
        old = self.service.submit("old", intent(side="SELL", close_only=True, depends_on=new["id"]))
        self.assertEqual(self.service.match(old["id"], quote(), healthy=True)["filled"], 0)
        partial = quote(symbol=new["intent"]["symbol"], token=124, asks=[{"price":501,"quantity":20}])
        self.service.match(new["id"], partial, healthy=True)
        self.assertEqual(self.service.match(old["id"], quote(), healthy=True)["filled"], 0)
        self.now += timedelta(seconds=1)
        self.service.match(new["id"], quote(self.now, symbol=new["intent"]["symbol"], token=124), healthy=True)
        self.assertEqual(self.service.match(old["id"], quote(self.now), healthy=True)["status"], "COMPLETE")

    def test_failed_replacement_retains_old(self):
        self.buy()
        new = self.service.submit("new", intent(symbol="NFO:BANKNIFTY26OCT57000CE", token=124))
        old = self.service.submit("old", intent(side="SELL", close_only=True, depends_on=new["id"]))
        self.service.reject(new["id"])
        self.assertEqual(self.service.match(old["id"], quote(), healthy=True)["filled"], 0)
        self.assertEqual(self.service.status()["positions"][0]["quantity"], 30)

    def test_invalid_dependencies_blocked(self):
        self.buy()
        with self.assertRaises(ValueError):
            self.service.submit("a", intent(side="SELL", close_only=True, depends_on="missing"))
        unrelated = self.service.submit("other", intent(strategy="other", symbol="NFO:BANKNIFTY26OCT57000CE", token=124))
        with self.assertRaises(ValueError):
            self.service.submit("a", intent(side="SELL", close_only=True, depends_on=unrelated["id"]))

    def test_duplicate_close_reserved(self):
        self.buy()
        self.service.submit("close", intent(side="SELL", close_only=True))
        with self.assertRaisesRegex(ValueError, "unreserved"):
            self.service.submit("again", intent(side="SELL", close_only=True))

    def test_no_short_can_close_another_strategy(self):
        self.buy()
        with self.assertRaises(ValueError):
            self.service.submit("close", intent(strategy="other", side="SELL", close_only=True))

    def test_invalid_quotes_never_fill(self):
        a = self.service.submit("a", intent())
        for bad in [quote(NOW-timedelta(seconds=6)), quote(NOW+timedelta(seconds=2)), quote(token=999), quote(source="invented"),
                    quote(asks=[{"price":float("nan"),"quantity":30}]), quote(asks=[{"price":498,"quantity":30}]),
                    quote(asks=[{"price":501,"quantity":True}])]:
            with self.subTest(quote=bad), self.assertRaises(ValueError):
                self.service.match(a["id"], bad, healthy=True)
        self.assertEqual(self.service.status()["fill_count"], 0)

    def test_disconnect_pause_and_session_close_block(self):
        a = self.service.submit("a", intent())
        with self.assertRaises(ValueError):
            self.service.match(a["id"], quote(), healthy=False)
        self.service.pause()
        with self.assertRaises(ValueError):
            self.service.match(a["id"], quote(), healthy=True)
        self.service.reconcile_and_resume()
        self.now = NOW.replace(hour=15, minute=30)
        with self.assertRaises(ValueError):
            self.service.match(a["id"], quote(self.now), healthy=True)

    def test_restart_preserves_partial_and_cancels_unknown_remainder(self):
        a = self.service.submit("a", intent())
        self.service.match(a["id"], quote(asks=[{"price":501,"quantity":20}]), healthy=True)
        restarted = PaperExecution(self.path, now=lambda: NOW)
        self.assertEqual(restarted.order(a["id"])["status"], "UNKNOWN")
        with self.assertRaises(ValueError):
            restarted.submit("b", intent())
        restarted.reconcile_and_resume()
        self.assertEqual(restarted.order(a["id"])["status"], "CANCELLED")
        self.assertEqual(restarted.status()["positions"][0]["quantity"], 20)

    def test_ledger_tampering_stops_reconciliation(self):
        self.buy()
        self.service.pause()
        with self.service.db() as db:
            db.execute("UPDATE paper_orders SET filled=29")
        with self.assertRaisesRegex(ValueError, "Ledger mismatch"):
            self.service.reconcile_and_resume()
        self.assertTrue(self.service.status()["paused"])

    def test_global_limit_and_whole_lots(self):
        self.service.max_units = 60
        self.service.submit("a", intent())
        self.service.submit("b", intent(strategy="other"))
        for change in ({}, {"quantity":31}, {"quantity":True}):
            with self.assertRaises(ValueError):
                self.service.submit("c", intent(**change))

    def test_restart_pauses_even_when_open_position_order_completed(self):
        self.buy()
        restarted = PaperExecution(self.path, now=lambda:NOW)
        self.assertTrue(restarted.status()["paused"])

    def test_day_order_cannot_fill_next_session(self):
        order = self.service.submit("a",intent())
        self.now += timedelta(days=1)
        result = self.service.match(order["id"],quote(self.now),healthy=True)
        self.assertEqual(result["status"],"CANCELLED")
        self.assertEqual(result["filled"],0)

    def test_partial_units_can_be_closed_without_inventing_full_lot(self):
        order = self.service.submit("a",intent())
        self.service.match(order["id"],quote(asks=[{"price":501,"quantity":20}]),healthy=True)
        self.service.cancel(order["id"])
        close = self.service.submit("b",intent(quantity=20,side="SELL",close_only=True))
        self.service.match(close["id"],quote(),healthy=True)
        self.assertEqual(self.service.status()["positions"][0]["quantity"],0)


def frame(token=123, size=184, at=NOW):
    packet = bytearray(size)
    for start, value in [(0, token), (4, 50000), (28 if size == 32 else 60, int(at.timestamp()))]:
        struct.pack_into(">I", packet, start, value)
    if size == 184:
        for pos, price in ((64,49900),(124,50100)):
            struct.pack_into(">II", packet, pos, 30, price)
    return struct.pack(">HH", 1, size) + packet


class StreamTests(unittest.TestCase):
    def test_full_frames_and_heartbeat(self):
        subs = {123:{"symbol":"NFO:BANKNIFTY26OCT56000CE"}, 99:{"symbol":"NSE:NIFTY BANK"}}
        self.assertEqual(decode_frame(b"\x00", subs, NOW), [])
        option = decode_frame(frame(), subs, NOW)[0]
        self.assertEqual(option["asks"], [{"quantity":30,"price":501}])
        self.assertEqual(option["bids"][0]["price"], 499)
        self.assertEqual(decode_frame(frame(token=99,size=32), subs, NOW)[0]["last_price"],500)

    def test_malformed_stale_and_unknown_packets_block(self):
        subs = {123:{"symbol":"NFO:BANKNIFTY26OCT56000CE"}}
        for raw in [b"", frame()[:-1], frame()+b"x", frame(token=8), frame(size=32), frame(at=NOW-timedelta(seconds=6))]:
            with self.subTest(raw=raw[:5]), self.assertRaises(ValueError):
                decode_frame(raw, subs, NOW)

    def test_quote_freshness_and_disconnection(self):
        stream = MarketStream(VerifiedCalendar(), now=lambda: NOW)
        stream.state = "connected"
        stream.quotes[123] = quote()
        self.assertEqual(stream.quote(123)["token"],123)
        stream.now = lambda: NOW+timedelta(seconds=6)
        with self.assertRaises(ValueError): stream.quote(123)
        stream.now = lambda: NOW
        stream.stop()
        with self.assertRaises(ValueError): stream.quote(123)

    def test_reconnect_bounded_and_secrets_not_reported(self):
        stream = MarketStream(VerifiedCalendar(), now=lambda:NOW)
        client = Mock()
        client.open_stream.side_effect = RuntimeError("access_token=secret")
        stream.stop_event = Mock(is_set=Mock(return_value=False),wait=Mock(return_value=False))
        stream._run(client, lambda: True)
        self.assertEqual(client.open_stream.call_count,4)
        self.assertEqual(stream.status()["retries"],3)
        self.assertEqual(stream.status()["state"],"blocked")
        self.assertNotIn("secret",json.dumps(stream.status()))

    def test_safe_broker_stream_failure_is_preserved_without_secrets(self):
        stream = MarketStream(VerifiedCalendar(), now=lambda:NOW)
        client = Mock()
        client.open_stream.side_effect = KiteNetworkError("The price stream timed out before Zerodha replied. Check your network or try again shortly.")
        stream.stop_event = Mock(is_set=Mock(return_value=False),wait=Mock(return_value=False))
        stream._run(client, lambda: True)
        status = stream.status()
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["last_failure"], "The price stream timed out before Zerodha replied. Check your network or try again shortly.")
        self.assertIn("timed out", status["message"])

    def test_broker_subscription_refusal_has_a_safe_actionable_message(self):
        stream = MarketStream(VerifiedCalendar(), now=lambda:NOW)
        stream.subscriptions = {123:{"symbol":"NFO:BANKNIFTY26OCT56000CE"}}
        sock = Mock()
        sock.recv.return_value = '{"type":"error","data":"access_token=private"}'
        client = Mock(open_stream=Mock(return_value=sock))
        stream.stop_event = Mock(is_set=Mock(return_value=False),wait=Mock(return_value=False))
        stream._run(client, lambda: True)
        status = stream.status()
        self.assertEqual(status["state"], "blocked")
        self.assertIn("refused the price subscription", status["last_failure"])
        self.assertNotIn("private", json.dumps(status))

    def test_resubscribe_in_full_mode_after_disconnect(self):
        stream = MarketStream(VerifiedCalendar(), now=lambda:NOW)
        stream.subscriptions = {123:{"symbol":"NFO:BANKNIFTY26OCT56000CE"}}
        sockets = [Mock(), Mock(), Mock(), Mock()]
        for sock in sockets: sock.recv.side_effect = OSError("gone")
        client = Mock(open_stream=Mock(side_effect=sockets))
        stream.stop_event = Mock(is_set=Mock(return_value=False),wait=Mock(return_value=False))
        stream._run(client, lambda:True)
        for sock in sockets:
            self.assertEqual([json.loads(c.args[0]) for c in sock.send.call_args_list], [{"a":"subscribe","v":[123]},{"a":"mode","v":["full",[123]]}])

    def test_hour_requires_all_minutes_and_no_disconnect(self):
        candles = ObservedCandles(VerifiedCalendar())
        start = NOW.replace(hour=10,minute=15)
        candles.connection(start-timedelta(seconds=1))
        for seconds in range(0,3601):
            at = start+timedelta(seconds=seconds)
            candles.add(quote(at, symbol="NSE:NIFTY BANK", last_price=56000+seconds/10))
        self.assertEqual(candles.hour(start,start+timedelta(hours=1))["open"],56000)
        candles.invalid.add(start+timedelta(minutes=30))
        with self.assertRaises(ValueError): candles.hour(start,start+timedelta(hours=1))

    def test_session_fragment_and_midminute_start_not_complete(self):
        candles = ObservedCandles(VerifiedCalendar())
        with self.assertRaises(ValueError): candles.hour(NOW.replace(hour=15,minute=15),NOW.replace(hour=17))
        start = NOW.replace(hour=10,minute=15)
        candles.connection(start+timedelta(seconds=20))
        for seconds in range(20,3601): candles.add(quote(start+timedelta(seconds=seconds),symbol="NSE:NIFTY BANK"))
        with self.assertRaises(ValueError): candles.hour(start,start+timedelta(hours=1))

    def test_socket_uses_tls_fixed_host_and_no_redirects(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/".stream-deps"))
        sock = Mock(connected=True,getstatus=Mock(return_value=101))
        with patch("websocket.create_connection",return_value=sock) as create:
            self.assertIs(KiteClient("examplekey", "exampletoken").open_stream(),sock)
        self.assertTrue(create.call_args.args[0].startswith("wss://ws.kite.trade?"))
        self.assertEqual(create.call_args.kwargs["redirect_limit"],0)
        self.assertNotIn("sslopt",create.call_args.kwargs)
        sock.getstatus.return_value = 302
        with patch("websocket.create_connection",return_value=sock),self.assertRaises(KiteNetworkError):
            KiteClient("examplekey", "exampletoken").open_stream()


class CalendarTests(unittest.TestCase):
    def setUp(self): self.calendar = VerifiedCalendar()

    def test_verified_holidays_and_settlement_only_date(self):
        self.assertEqual(self.calendar.windows(date(2026,1,15)),[])
        self.assertEqual(self.calendar.windows(date(2026,10,2)),[])
        self.assertTrue(self.calendar.windows(date(2026,4,1)))

    def test_special_sunday_and_unknown_muhurat(self):
        self.assertTrue(self.calendar.windows(date(2026,2,1)))
        with self.assertRaises(ValueError): self.calendar.windows(date(2026,11,8))
        self.assertFalse(self.calendar.status(datetime(2026,11,8,12,tzinfo=IST))["verified_today"])

    def test_unknown_year_and_naive_time_block(self):
        with self.assertRaises(ValueError): self.calendar.windows(date(2027,1,1))
        with self.assertRaises(ValueError): self.calendar.is_open(datetime(2026,9,30,12))

    def test_expiry_and_24_hour_selection_boundary(self):
        contracts = [{"expiry":"2026-10-27"},{"expiry":"2026-11-23"}]
        self.assertEqual(self.calendar.expiry(2026,11),date(2026,11,23))
        at = datetime(2026,10,26,15,30,tzinfo=IST)
        self.assertEqual(self.calendar.select_expiry(at,contracts),"2026-10-27")
        self.assertEqual(self.calendar.select_expiry(at+timedelta(seconds=1),contracts),"2026-11-23")
        with self.assertRaises(ValueError): self.calendar.select_expiry(at,[{"expiry":"2026-10-29"}])

    def test_rollover_uses_verified_expiry_week(self):
        self.assertEqual(self.calendar.rollover(date(2026,10,27)),datetime(2026,10,26,15,15,tzinfo=IST))

    def test_historical_split_sessions_preserved(self):
        self.assertEqual(len(session_windows(date(2024,3,2))),2)
        self.assertEqual(len(session_windows(date(2024,5,18))),2)
        self.assertTrue(session_windows(date(2024,1,20)))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.kite = Mock()
        self.runtime = PaperRuntime(self.kite,Path(self.temp.name)/"test.db",now=lambda:NOW)

    def test_unsigned_session_records_blocker_not_pass(self):
        self.kite._client.side_effect = ValueError("Sign in first")
        result = self.runtime.check()
        self.assertEqual(result["status"],"blocked")
        self.assertEqual(result["actual_broker_orders"],0)
        self.assertEqual(result["evidence"]["orders"],[])

    def test_stale_retry_is_bounded_and_never_resubmits(self):
        contract = {"tradingsymbol":"BANKNIFTY26OCT56000CE","instrument_token":123,"expiry":"2026-10-27","lot_size":30}
        self.runtime.client = self.kite._client.return_value
        self.runtime.stream.quote = Mock(side_effect=ValueError("Stale"))
        with patch("paper_runtime.time.sleep"), self.assertRaisesRegex(ValueError,"three retries"):
            self.runtime._fill("test","leg",contract,"BUY")
        self.assertEqual(self.runtime.stream.quote.call_count,4)
        orders = self.runtime.execution.status()["orders"]
        self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]["status"],"CANCELLED")

    def test_retained_positions_block_new_test_and_can_close_on_fresh_quotes(self):
        contract = {"tradingsymbol":"BANKNIFTY26OCT56000CE","instrument_token":123,"expiry":"2026-10-27","lot_size":30}
        self.runtime.client = self.kite._client.return_value
        self.runtime.contracts = [contract]
        self.runtime.stream.quote = Mock(return_value=quote())
        self.runtime._fill("test","entry",contract,"BUY")
        result = self.runtime.check()
        self.assertEqual(result["status"],"blocked")
        self.assertIn("retained",result["message"])
        self.runtime.execution.pause()
        self.runtime.close_retained()
        self.assertTrue(self.runtime.execution.status()["paused"])
        self.assertTrue(all(p["quantity"] == 0 for p in self.runtime.execution.status()["positions"]))

    def test_complete_ce_pe_acceptance_path_with_synthetic_quotes(self):
        self.runtime.client = self.kite._client.return_value
        self.runtime.index_token = 99
        self.runtime.expiry = "2026-10-27"
        contracts = []
        for kind, strikes in (("CE",[55000,56500,57000]),("PE",[57000,55500,55000])):
            for strike in strikes:
                contracts.append({"tradingsymbol":f"BANKNIFTY26OCT{strike}{kind}","instrument_token":len(contracts)+123,
                                  "expiry":"2026-10-27","lot_size":30,"strike":strike,"instrument_type":kind})
        self.runtime.contracts = contracts
        def get_quote(token):
            if token == 99: return quote(token=99,symbol="NSE:NIFTY BANK",last_price=56000)
            c = next(c for c in contracts if c["instrument_token"] == token)
            return quote(token=token,symbol="NFO:"+c["tradingsymbol"])
        self.runtime.stream.quote = get_quote
        with patch("paper_runtime.time.sleep"):
            result = self.runtime.check()
        self.assertEqual(result["status"],"synthetic_passed",result["message"])
        self.assertEqual(result["source"],"synthetic_test")
        self.assertEqual(len(result["evidence"]["orders"]),12)
        self.assertTrue(all(p["quantity"] == 0 for p in result["evidence"]["positions"]))
        self.assertEqual(result["selection"]["replacement_distance"],1000)
        self.assertEqual(result["selection"]["hedge_target_premium"],500)

    def test_strategy_runner_selects_current_entry_and_next_month_rollover_hedge(self):
        self.runtime.client = self.kite._client.return_value
        self.runtime.index_token = 99
        self.runtime.expiry, self.runtime.next_expiry = "2026-10-27", "2026-11-23"
        contracts = []
        premiums = {
            ("2026-10-27", "CE", 54000): 1000, ("2026-10-27", "CE", 55000): 500,
            ("2026-10-27", "PE", 55000): 1000, ("2026-10-27", "PE", 54000): 500,
            ("2026-11-23", "CE", 55000): 500, ("2026-11-23", "PE", 54000): 500,
        }
        for (expiry, kind, strike), premium in premiums.items():
            contracts.append({"tradingsymbol": f"BANKNIFTY26{'OCT' if expiry.endswith('10-27') else 'NOV'}{strike}{kind}",
                              "instrument_token": len(contracts) + 100, "expiry": expiry, "lot_size": 30,
                              "strike": strike, "instrument_type": kind, "premium": premium})
        self.runtime.contracts = contracts
        def streamed(token):
            if token == 99:
                return quote(token=99, symbol="NSE:NIFTY BANK", last_price=54500, source="zerodha_stream")
            contract = next(item for item in contracts if item["instrument_token"] == token)
            return quote(token=token, symbol="NFO:" + contract["tradingsymbol"], last_price=contract["premium"], source="zerodha_stream")
        self.runtime.stream.quote = streamed
        ce = self.runtime.strategy_entry_selection("CE", NOW)
        pe = self.runtime.strategy_entry_selection("PE", NOW)
        self.assertEqual((ce["expiry_offset"], ce["short_strike"], ce["hedge_strike"]), (0, 54000, 55000))
        self.assertEqual((pe["expiry_offset"], pe["short_strike"], pe["hedge_strike"]), (0, 55000, 54000))
        next_hedge = self.runtime.strategy_replacement_selection("CE", NOW, {
            "short_strike": ce["short_strike"], "hedge_symbol": ce["hedge_symbol"], "hedge_expiry": ce["expiry"]
        }, rollover=True)
        self.assertEqual((next_hedge["expiry"], next_hedge["expiry_offset"], next_hedge["hedge_strike"]), ("2026-11-23", 1, 55000))


if __name__ == "__main__": unittest.main()
