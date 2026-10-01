"""Synthetic snapshot tests: reconstruction never becomes executable inventory."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock

from app import KiteState
from backtest_config import model_config
from engine import IST
from paper_replay import PaperReplay, project_result, request_settings
from strategy_config import CLOSE_BOUNDARY, CLOSE_REVIEW, strategy_config


def result(day="2026-09-23", clock="15:30"):
    at=f"{day}T{clock}:00+05:30"
    leg={"position_id":"fixture", "status":"open", "quantity":120, "mark_at":at,
         "option_type":"PE", "direction":"SELL", "entry_premium":1000, "mark_premium":500,
         "entry_pricing":{"basis":"recorded"}, "mark_pricing":{"basis":"estimated"},
         "stop_spot":55500, "target_spot":57000, "risk_reference_spot":56000}
    return {"range":{"start_date":"2026-09-01","end_date":day},
            "spot_proxy":{"status":"complete", "uncertain_positions":[]},
            "option_model":{"status":"complete", "as_of":at, "strategy":strategy_config(CLOSE_BOUNDARY),
                            "config":model_config(), "event_validation":{"all_processed":True},
                            "ledgers":{"selling":[leg,{"status":"closed"}],"ce_hedge":[],"pe_hedge":[]},
                            "combined":{"open_net":60000}, "validation_status":"approximate",
                            "warnings":["Missing minute prices were skipped"],
                            "pending_replacements":[{"option_type":"PE","due_at":"2026-09-24T09:15:00+05:30"}]}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"replay.sqlite3"
        self.store=PaperReplay(self.path)

    def test_preserves_prices_risk_warnings_and_pending_replacement(self):
        r=result();before=deepcopy(r)
        s=project_result(r,"synthetic fixture")
        self.assertEqual(r,before)
        self.assertFalse(s["is_execution"])
        self.assertEqual(len(s["legs"]),1)
        self.assertEqual(s["legs"][0]["stop_spot"],55500)
        self.assertEqual(s["legs"][0]["target_spot"],57000)
        self.assertEqual(s["legs"][0]["entry_pricing"]["basis"],"recorded")
        self.assertEqual(s["legs"][0]["mark_pricing"]["basis"],"estimated")
        self.assertEqual(s["warnings"],before["option_model"]["warnings"])
        self.assertEqual(len(s["pending_replacements"]),1)

    def test_persistence_does_not_create_an_execution_table(self):
        self.store.capture(result())
        self.assertEqual(PaperReplay(self.path).snapshot(),self.store.snapshot())
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[("replay_snapshot",)])

    def test_older_result_cannot_replace_newer_snapshot(self):
        self.store.capture(result("2026-09-24"))
        self.assertFalse(self.store.capture(result()))
        self.assertEqual(self.store.snapshot()["end_date"],"2026-09-24")

    def test_other_research_settings_never_replace_snapshot(self):
        self.store.capture(result());before=self.store.snapshot()
        for change in ("start","timing","quantity"):
            r=result()
            if change=="start":r["range"]["start_date"]="2026-08-01"
            if change=="timing":r["option_model"]["strategy"]=strategy_config(CLOSE_REVIEW)
            if change=="quantity":r["option_model"]["config"]["sell_lots"]=1
            with self.subTest(change=change):
                self.assertFalse(self.store.capture_matching(r))
                with self.assertRaises(ValueError):project_result(r,"fixture")
                self.assertEqual(self.store.snapshot(),before)

    def test_incomplete_uncertain_and_unmatched_results_rejected(self):
        for change in ("options","spot","uncertain","events"):
            r=result()
            if change=="options":r["option_model"]["status"]="partial"
            if change=="spot":r["spot_proxy"]["status"]="partial"
            if change=="uncertain":r["spot_proxy"]["uncertain_positions"]=[{}]
            if change=="events":r["option_model"]["event_validation"]["all_processed"]=False
            with self.subTest(change=change), self.assertRaises(ValueError):self.store.capture(r)
        self.assertIsNone(self.store.snapshot())

    def test_rejects_inconsistent_leg_valuation_and_quantity(self):
        for key,value in (("mark_at","2026-09-01T09:15:00+05:30"),("quantity",0),("quantity",True)):
            r=result();r["option_model"]["ledgers"]["selling"][0][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):self.store.capture(r)

    def test_altered_premium_rules_are_not_silently_accepted(self):
        r=result();r["option_model"]["config"]["hedge_premium_step"]="200"
        with self.assertRaises(ValueError):self.store.capture(r)

    def test_rejects_future_and_naive_valuation(self):
        for stamp in ("2026-09-24T15:30:00+05:30","2026-09-23T15:30:00"):
            r=result();r["option_model"]["as_of"]=stamp
            with self.assertRaises(ValueError):self.store.capture(r)

    def test_no_data_is_unavailable_not_flat(self):
        s=self.store.status(datetime(2026,9,30,18,tzinfo=IST))
        self.assertIsNone(s["snapshot"])
        self.assertFalse(s["through_latest_close"])

    def test_old_snapshot_is_not_current(self):
        self.store.capture(result())
        s=self.store.status(datetime(2026,9,30,18,tzinfo=IST))
        self.assertFalse(s["through_latest_close"])
        self.assertEqual(s["latest_completed_date"],"2026-09-30")

    def test_missing_final_minute_never_claims_through_close(self):
        now=datetime(2026,9,23,18,tzinfo=IST)
        self.store.capture(result(clock="15:29"))
        self.assertFalse(self.store.status(now)["through_latest_close"])
        self.store.capture(result())
        self.assertTrue(self.store.status(now)["through_latest_close"])
        self.assertIn("not intraday",self.store.status(now)["note"])

    def test_completed_job_persists_matching_result(self):
        kite=KiteState();kite.paper_replay=self.store;kite.replay_spot_range=Mock(return_value=result())
        job={};kite._run_replay_job(request_settings(),job,Event())
        self.assertEqual(job["status"],"complete")
        self.assertIsNotNone(self.store.snapshot())

    def test_snapshot_failure_keeps_research_result_and_previous_snapshot(self):
        self.store.capture(result());before=self.store.snapshot()
        r=result();r["spot_proxy"]["uncertain_positions"]=[{}]
        kite=KiteState();kite.paper_replay=self.store;kite.replay_spot_range=Mock(return_value=r)
        job={};kite._run_replay_job(request_settings(),job,Event())
        self.assertEqual(job["status"],"complete")
        self.assertIn("not updated",job["paper_replay_warning"])
        self.assertEqual(self.store.snapshot(),before)

    def test_cancelled_job_never_replaces_snapshot(self):
        kite=KiteState();kite.paper_replay=self.store;kite.replay_spot_range=Mock(return_value=result())
        job={};cancel=Event();cancel.set();kite._run_replay_job(request_settings(),job,cancel)
        self.assertEqual(job["status"],"cancelled")
        self.assertIsNone(self.store.snapshot())
