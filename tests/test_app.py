"""Local API and persistence checks; no connections to any broker."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import LocalServer
from deployment_runtime import DeploymentRuntimeRegistry, MIGRATION_CONFIRMATION
from scenarios import CATALOG, run_scenario
from store import Store
from tradetron_link import TradetronLinkState, TradetronLinkStore


class TestOnlyProtection:
    """Avoids accessing the real Windows user profile from the test runtime."""
    available = True

    def protect(self, value):
        return b"test-only:" + value[::-1]

    def unprotect(self, value):
        if not value.startswith(b"test-only:"):
            raise ValueError()
        return value[10:][::-1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)

    def test_multiple_drafts_and_edits_are_isolated(self):
        first = self.store.list_strategies()[0]
        second = self.store.create_strategy({"name": "Second draft", "lots": 2, "capital": 200000})
        self.store.update_strategy(second["id"], {"lots": 3})
        self.assertEqual(self.store.get_strategy(first["id"])["lots"], 1)
        self.assertEqual(self.store.get_strategy(first["id"])["multiplier"], 1)
        self.assertEqual(self.store.get_strategy(second["id"])["lots"], 3)

    def test_example_evidence_retains_the_saved_draft_version_and_settings(self):
        original = self.store.list_strategies()[0]
        saved = self.store.add_run(original, run_scenario("hedge_full_fill", original["effective_lots"]))
        changed = self.store.update_strategy(original["id"], {"lots": 2})
        self.assertNotEqual(original["version"], changed["version"])
        reopened = Store(self.path)
        evidence = reopened.get_run(saved["id"])
        self.assertEqual(evidence["strategy_version"], original["version"])
        self.assertEqual(evidence["strategy_snapshot"]["lots"], original["lots"])
        self.assertEqual(evidence["strategy_snapshot"]["multiplier"], original["multiplier"])
        self.assertEqual(reopened.get_strategy(original["id"])["version"], changed["version"])
        self.assertIn("rules", evidence["strategy_snapshot"])

    def test_drafts_survive_restart_without_duplicate_seed(self):
        self.store.create_strategy({"name": "Saved draft", "lots": 1, "capital": 500000})
        reopened = Store(self.path)
        self.assertEqual(len(reopened.list_strategies()), 2)
        self.assertEqual(reopened.list_strategies()[1]["name"], "Saved draft")

    def test_paper_deployment_freezes_one_draft_version_and_stays_unlinked(self):
        draft = self.store.list_strategies()[0]
        deployment = self.store.create_deployment({"strategy_id": draft["id"]})
        self.assertEqual((deployment["mode"], deployment["state"], deployment["strategy_version"]), ("paper_only", "not_linked", draft["version"]))
        self.assertFalse(deployment["executor"]["linked"])
        with self.assertRaises(ValueError):
            self.store.create_deployment({"strategy_id": draft["id"]})
        changed = self.store.update_strategy(draft["id"], {"lots": 2})
        self.assertNotEqual(deployment["strategy_version"], changed["version"])
        self.assertEqual(self.store.get_deployment(deployment["id"])["lots"], 1)
        self.assertEqual(self.store.create_deployment({"strategy_id": draft["id"]})["strategy_version"], changed["version"])

    def test_paper_deployment_lifecycle_rejects_unknown_states(self):
        deployment = self.store.create_deployment({"strategy_id": self.store.list_strategies()[0]["id"]})
        ready = self.store.set_deployment_state(deployment["id"], "paper_ready")
        self.assertEqual(ready["state"], "paper_ready")
        with self.assertRaises(ValueError):
            self.store.set_deployment_state(deployment["id"], "live_running")

    def test_no_live_status_or_credentials_can_be_saved(self):
        draft = self.store.list_strategies()[0]
        for body in [{"status": "live"}, {"api_secret": "not-a-real-key"}, {"live_enabled": True}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.store.update_strategy(draft["id"], body)
        self.assertEqual(self.store.get_strategy(draft["id"])["status"], "draft")

    def test_invalid_inputs_rejected(self):
        for patch in [{"name": " "}, {"name": "x" * 81}, {"name": "x\ny"}, {"lots": 0}, {"lots": True}, {"lots": 1.5}, {"multiplier": 0}, {"multiplier": True}, {"multiplier": 1.5}, {"capital": float("nan")}, {"capital": -1}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.store.create_strategy(dict({"name": "Draft", "lots": 4, "capital": 1000000}, **patch))

    def test_multiplier_preserves_effective_lots_and_saved_history(self):
        first = self.store.list_strategies()[0]
        changed = self.store.update_strategy(first["id"], {"lots": 1, "multiplier": 4})
        self.assertEqual((changed["lots"], changed["multiplier"], changed["effective_lots"]), (1, 4, 4))
        result = self.store.add_run(changed, run_scenario("hedge_full_fill", changed["effective_lots"]))
        self.assertIn("4 test lots", result["events"][0]["detail"])
        self.assertEqual(result["strategy_snapshot"]["effective_lots"], 4)

    def test_names_are_stored_as_text_not_sql(self):
        name = "<script>alert(1)</script> '; DROP TABLE strategies; --"
        made = self.store.create_strategy({"name": name, "lots": 1, "capital": 10000})
        self.assertEqual(made["name"], name)
        self.assertEqual(len(self.store.list_strategies()), 2)

    def test_run_history_is_saved_with_original_size(self):
        strategy = self.store.list_strategies()[0]
        result = self.store.add_run(strategy, run_scenario("hedge_full_fill", strategy["effective_lots"]))
        self.store.update_strategy(strategy["id"], {"lots": 9})
        reopened = Store(self.path)
        self.assertEqual(reopened.get_run(result["id"])["strategy_lots"], 1)
        self.assertEqual(len(reopened.recent_runs()), 1)

    def test_backtest_history_survives_restart_with_its_original_result(self):
        result = {"range": {"start_date": "2026-09-01", "end_date": "2026-09-30", "total_signals": 12},
                  "spot_proxy": {"strategy": {"id": "hour_close_boundary_uncertain_v2"}, "validation_status": "validated"},
                  "option_model": {"validation_status": "validated", "combined": {"total_net": 1234.5}}}
        saved = self.store.add_backtest(result)
        reopened = Store(self.path)
        self.assertEqual(reopened.recent_backtests()[0]["id"], saved["id"])
        self.assertEqual(reopened.recent_backtests()[0]["combined_net"], 1234.5)
        self.assertEqual(reopened.get_backtest(saved["id"])["result"], result)


class ScenarioTests(unittest.TestCase):
    def test_every_scenario_uses_test_data_and_passes(self):
        for spec in CATALOG:
            for lots in (1, 4, 100):
                with self.subTest(scenario=spec["id"], lots=lots):
                    result = run_scenario(spec["id"], lots)
                    self.assertTrue(result["test_data"])
                    self.assertEqual(result["result"], "passed")
                    self.assertNotIn("pnl", result)

    def test_unknown_scenario_rejected(self):
        with self.assertRaises(ValueError):
            run_scenario("execute-live", 4)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = LocalServer(("127.0.0.1", 0), Store(Path(cls.temp.name) / "api.sqlite3"))
        # The bundled test runtime cannot access the signed-in desktop user's
        # DPAPI profile. The production server still uses WindowsProtection.
        cls.server.tradetron = TradetronLinkState(TradetronLinkStore(
            Path(cls.temp.name) / "api-tradetron.dpapi", TestOnlyProtection()))
        cls.server.deployment_runtimes = DeploymentRuntimeRegistry(cls.server.store, Path(cls.temp.name), cls.server.tradetron)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.temp.cleanup()

    def request(self, method, path, body=None, extra=None):
        headers = {"Content-Type": "application/json", "X-Local-Request": "PrivateAlgoDesk"}
        headers.update(extra or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_state_is_explicitly_offline(self):
        status, state, headers = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(state["live_enabled"])
        self.assertFalse(state["broker"]["connected"])
        self.assertTrue(all(not item["ready"] for item in state["strategies"][0]["readiness"]))
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_live_endpoint_does_not_exist(self):
        status, _, _ = self.request("POST", "/api/orders", {"side": "BUY"})
        self.assertEqual(status, 404)

    def test_paper_deployment_api_is_versioned_and_never_creates_an_order(self):
        _, state, _ = self.request("GET", "/api/state")
        strategy = state["strategies"][0]
        status, created, _ = self.request("POST", "/api/deployments", {"strategy_id": strategy["id"]})
        self.assertEqual(status, 201)
        deployment = created["deployment"]
        self.assertEqual((deployment["mode"], deployment["state"], deployment["strategy_version"]),
                         ("paper_only", "not_linked", strategy["version"]))
        self.assertFalse(deployment["executor"]["linked"])
        status, saved, _ = self.request("GET", "/api/deployments")
        self.assertEqual(status, 200)
        self.assertEqual(saved["deployments"][0]["id"], deployment["id"])
        status, _, _ = self.request("POST", "/api/deployments", {"strategy_id": strategy["id"]})
        self.assertEqual(status, 400)

    def test_each_paper_deployment_has_an_isolated_executor_and_outbox(self):
        first = self.request("POST", "/api/strategies", {"name": "Executor isolation A", "lots": 1, "capital": 100000})[1]["strategy"]
        second = self.request("POST", "/api/strategies", {"name": "Executor isolation B", "lots": 1, "capital": 100000})[1]["strategy"]
        first_deployment = self.request("POST", "/api/deployments", {"strategy_id": first["id"]})[1]["deployment"]
        second_deployment = self.request("POST", "/api/deployments", {"strategy_id": second["id"]})[1]["deployment"]
        fixture_token = "22222222-2222-4222-8222-222222222222"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        status, linked, _ = self.request("POST", f"/api/deployments/{first_deployment['id']}/tradetron/link", {
            "strategy_id": "999056854", "api_link": fixture_link, "remember": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(linked["deployment"]["state"], "linked_unverified")
        self.assertTrue(linked["deployment"]["executor"]["linked"])
        self.assertEqual(linked["deployment"]["executor"]["strategy_id"], "999056854")
        self.assertEqual(linked["deployment"]["runner"]["mode"], "paper_only")
        self.assertFalse(linked["deployment"]["runner"]["armed"])
        self.assertEqual(linked["deployment"]["runner"]["automatic_runner"], "unavailable")
        self.assertEqual(linked["deployment"]["bridge"], {
            "signal_lots": 1,
            "tradetron_multiplier": "managed_in_tradetron",
            "sends_quantity": False,
        })
        self.assertNotIn(fixture_token, json.dumps(linked))
        status, saved, _ = self.request("GET", "/api/deployments")
        self.assertEqual(status, 200)
        deployments = {item["id"]: item for item in saved["deployments"]}
        self.assertTrue(deployments[first_deployment["id"]]["executor"]["linked"])
        self.assertFalse(deployments[second_deployment["id"]]["executor"]["linked"])
        first_runner = self.server.deployment_runtimes._item(first_deployment["id"])[1]
        second_runner = self.server.deployment_runtimes._item(second_deployment["id"])[1]
        self.assertNotEqual(first_runner.store.path, second_runner.store.path)
        self.assertEqual(first_runner.status()["pending_events"], 0)
        self.assertEqual(second_runner.status()["pending_events"], 0)
        status, removed, _ = self.request("POST", f"/api/deployments/{first_deployment['id']}/tradetron/disconnect", {})
        self.assertEqual(status, 200)
        self.assertEqual(removed["deployment"]["state"], "not_linked")
        self.assertFalse(removed["deployment"]["executor"]["linked"])

    def test_legacy_link_can_only_move_once_into_one_unlinked_deployment(self):
        self.server.tradetron.clear()
        strategy = self.request("POST", "/api/strategies", {"name": "Legacy migration target", "lots": 1, "capital": 100000})[1]["strategy"]
        deployment = self.request("POST", "/api/deployments", {"strategy_id": strategy["id"]})[1]["deployment"]
        fixture_token = "33333333-3333-4333-8333-333333333333"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        self.server.tradetron.configure({"strategy_id": "999056854", "api_link": fixture_link, "remember": True})
        status, rejected, _ = self.request("POST", f"/api/deployments/{deployment['id']}/tradetron/migrate-legacy", {"confirmation": "wrong"})
        self.assertEqual(status, 400)
        self.assertIn("exact local confirmation", rejected["error"])
        self.assertTrue(self.server.tradetron.status()["linked"])
        status, moved, _ = self.request("POST", f"/api/deployments/{deployment['id']}/tradetron/migrate-legacy", {"confirmation": MIGRATION_CONFIRMATION})
        self.assertEqual(status, 200)
        self.assertTrue(moved["deployment"]["executor"]["linked"])
        self.assertEqual(moved["deployment"]["state"], "linked_unverified")
        self.assertFalse(self.server.tradetron.status()["linked"])
        self.assertNotIn(fixture_token, json.dumps(moved))
        status, repeated, _ = self.request("POST", f"/api/deployments/{deployment['id']}/tradetron/migrate-legacy", {"confirmation": MIGRATION_CONFIRMATION})
        self.assertEqual(status, 400)
        self.assertIn("unlinked", repeated["error"])

    def test_deployment_paper_bootstrap_uses_only_its_isolated_link(self):
        strategy = self.request("POST", "/api/strategies", {"name": "Deployment bootstrap", "lots": 1, "capital": 100000})[1]["strategy"]
        deployment = self.request("POST", "/api/deployments", {"strategy_id": strategy["id"]})[1]["deployment"]
        fixture_token = "44444444-4444-4444-8444-444444444444"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        self.request("POST", f"/api/deployments/{deployment['id']}/tradetron/link", {
            "strategy_id": "999056854", "api_link": fixture_link, "remember": True,
        })
        with patch("deployment_runtime.dispatch_snapshot_entries", return_value={"mode": "paper_bootstrap_only"}) as dispatch, \
                patch.object(self.server.paper_replay, "snapshot", return_value={"kind": "historical_paper_replay"}):
            status, result, _ = self.request("POST", f"/api/deployments/{deployment['id']}/tradetron/paper-bootstrap", {
                "confirmation": "paper-bootstrap-four-legs"
            })
        self.assertEqual(status, 200)
        self.assertEqual(result["delivery"]["mode"], "paper_bootstrap_only")
        self.assertEqual(result["deployment"]["state"], "paper_ready")
        self.assertEqual(result["deployment"]["executor"]["delivery"], "verified")
        self.assertEqual(dispatch.call_args.args[0]["strategy_id"], "999056854")
        self.assertNotIn(fixture_token, json.dumps(result))

    def test_tradetron_link_exposes_locked_signal_contract_and_cannot_send_orders(self):
        fixture_token = "11111111-1111-4111-8111-111111111111"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        self.server.tradetron.clear()
        status, initial, _ = self.request("GET", "/api/tradetron/status")
        self.assertEqual(status, 200)
        self.assertFalse(initial["status"]["linked"])
        self.assertEqual(initial["runner"]["mode"], "paper_only")
        self.assertFalse(initial["runner"]["armed"])
        self.assertEqual(initial["runner"]["automatic_runner"], "unavailable")
        status, linked, _ = self.request("POST", "/api/tradetron/link", {
            "strategy_id": "999056202", "api_link": fixture_link, "remember": True,
        })
        self.assertEqual(status, 200)
        self.assertTrue(linked["status"]["linked"])
        self.assertEqual(linked["status"]["strategy_id"], "999056202")
        self.assertEqual(linked["status"]["delivery"], "unverified")
        self.assertTrue(linked["status"]["signal_contract"]["signals_locked"])
        self.assertNotIn(fixture_token, json.dumps(linked))
        self.assertNotIn("api_buy", json.dumps(linked))
        status, _, _ = self.request("POST", "/api/tradetron/send", {"side": "BUY"})
        self.assertEqual(status, 404)
        status, cleared, _ = self.request("POST", "/api/tradetron/disconnect", {})
        self.assertEqual(status, 200)
        self.assertFalse(cleared["status"]["linked"])

    def test_paper_bootstrap_accepts_only_its_exact_local_confirmation_and_target(self):
        fixture_token = "11111111-1111-4111-8111-111111111111"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        status, _, _ = self.request("POST", "/api/tradetron/link", {
            "strategy_id": "999056854", "api_link": fixture_link, "remember": True,
        })
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/tradetron/paper-bootstrap", {"confirmation": "wrong"})
        self.assertEqual(status, 400)
        with patch("app.dispatch_snapshot_entries", return_value={
            "mode": "paper_bootstrap_only", "strategy_id": "999056854", "entries": []
        }) as dispatch, patch.object(self.server.paper_replay, "snapshot", return_value={"kind": "historical_paper_replay"}):
            status, body, _ = self.request("POST", "/api/tradetron/paper-bootstrap", {
                "confirmation": "paper-bootstrap-four-legs"
            })
        self.assertEqual(status, 200)
        self.assertEqual(body["delivery"]["mode"], "paper_bootstrap_only")
        self.assertEqual(dispatch.call_args.args[0]["strategy_id"], "999056854")
        self.assertNotIn(fixture_token, json.dumps(body))

    def test_paper_bootstrap_has_no_second_trigger_after_the_directional_json_payload(self):
        fixture_token = "11111111-1111-4111-8111-111111111111"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        status, _, _ = self.request("POST", "/api/tradetron/link", {
            "strategy_id": "999056854", "api_link": fixture_link, "remember": True,
        })
        self.assertEqual(status, 200)
        status, body, _ = self.request("POST", "/api/tradetron/paper-bootstrap/trigger", {
            "confirmation": "paper-bootstrap-four-legs"
        })
        self.assertEqual(status, 400)
        self.assertIn("directional JSON", body["error"])
        self.assertNotIn(fixture_token, json.dumps(body))

    def test_paper_bootstrap_initialisation_is_owned_by_the_template(self):
        fixture_token = "11111111-1111-4111-8111-111111111111"
        fixture_link = "https://api.tradetron.tech/api?auth-token=" + fixture_token + "&key=api_buy&value=1"
        status, _, _ = self.request("POST", "/api/tradetron/link", {
            "strategy_id": "999056854", "api_link": fixture_link, "remember": True,
        })
        self.assertEqual(status, 200)
        status, body, _ = self.request("POST", "/api/tradetron/paper-bootstrap/initialise", {
            "confirmation": "paper-bootstrap-four-legs"
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["initialisation"], {
            "managed_by": "Tradetron template", "values": {"ce_entry_seen": 0, "pe_entry_seen": 0}
        })
        self.assertNotIn(fixture_token, json.dumps(body))

    def test_paper_controls_cannot_accept_arbitrary_order_or_quote(self):
        for route in ("stream/start", "check", "resume", "close-retained"):
            status, _, _ = self.request("POST", "/api/paper/"+route, {"price": 500, "side": "BUY"})
            self.assertEqual(status, 400)

    def test_paper_state_has_no_secrets_and_no_live_authority(self):
        status, body, _ = self.request("GET", "/api/paper/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["paper"]["live_enabled"])
        self.assertEqual(body["stream"]["state"], "stopped")
        for secret in ("api_key", "api_secret", "access_token"):
            self.assertNotIn(secret, json.dumps(body))

    def test_september_replay_is_separate_and_needs_connection(self):
        status,body,_=self.request("GET","/api/paper/status")
        self.assertEqual(status,200)
        self.assertEqual(body["historical_replay"]["settings"]["start_date"],"2026-09-01")
        self.assertIsNone(body["historical_replay"]["snapshot"])
        before=self.server.paper.execution.status()
        status,reply,_=self.request("POST","/api/paper/replay/refresh",{})
        self.assertEqual(status,400)
        self.assertIn("Sign in",reply["error"])
        self.assertEqual(self.server.paper.execution.status(),before)
        status,_,_=self.request("POST","/api/paper/replay/refresh",{"quantity":1})
        self.assertEqual(status,400)
        status,_,_=self.request("POST","/api/paper/replay/refresh",{}, {"Origin":"https://evil.example"})
        self.assertEqual(status,403)

    def test_paper_check_without_connection_is_saved_as_blocked(self):
        status, body, _ = self.request("POST", "/api/paper/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["check"]["status"], "blocked")
        self.assertEqual(body["check"]["actual_broker_orders"], 0)
        self.assertEqual(body["check"]["evidence"]["orders"], [])

    def test_paper_controls_require_same_origin_and_local_header(self):
        for extra in ({"Origin":"https://evil.example"},{"X-Local-Request":""}):
            status, _, _ = self.request("POST", "/api/paper/pause", {}, extra)
            self.assertEqual(status, 403)

    def test_only_one_local_server_can_bind_the_address(self):
        before = self.server.paper.execution.status()["paused"]
        with self.assertRaises(OSError):
            LocalServer(("127.0.0.1",self.port),self.server.store)
        self.assertEqual(self.server.paper.execution.status()["paused"],before)

    def test_unknown_host_is_blocked(self):
        status, _, _ = self.request("GET", "/api/state", extra={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_external_origin_is_blocked(self):
        status, _, _ = self.request("POST", "/api/strategies", {"name": "x", "lots": 1, "capital": 1}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_local_request_header_is_required_for_changes(self):
        status, _, _ = self.request("POST", "/api/strategies", {"name": "x", "lots": 1, "capital": 1}, {"X-Local-Request": ""})
        self.assertEqual(status, 403)

    def test_scenarios_cannot_enable_live_trading(self):
        _, state, _ = self.request("GET", "/api/state")
        strategy = state["strategies"][0]
        status, result, _ = self.request("POST", "/api/scenarios/run", {"strategy_id": strategy["id"], "scenario_id": "hedge_full_fill"})
        self.assertEqual(status, 201)
        self.assertTrue(result["run"]["test_data"])
        _, after, _ = self.request("GET", "/api/state")
        self.assertFalse(after["live_enabled"])

    def test_path_traversal_cannot_read_local_files(self):
        status, _, _ = self.request("GET", "/../store.py")
        self.assertEqual(status, 404)

    def test_invalid_patch_fails_without_changing_draft(self):
        _, state, _ = self.request("GET", "/api/state")
        draft = state["strategies"][0]
        status, _, _ = self.request("PATCH", f"/api/strategies/{draft['id']}", {"status": "live"})
        self.assertEqual(status, 400)

    def test_saved_backtests_have_read_only_history_routes(self):
        result = {"range": {"start_date": "2026-09-01", "end_date": "2026-09-30", "total_signals": 12},
                  "spot_proxy": {"strategy": {"id": "hour_close_boundary_uncertain_v2"}, "validation_status": "validated"},
                  "option_model": {"validation_status": "validated", "combined": {"total_net": 1234.5}}}
        saved = self.server.store.add_backtest(result)
        status, listed, _ = self.request("GET", "/api/backtests")
        self.assertEqual(status, 200)
        self.assertEqual(listed["backtests"][0]["id"], saved["id"])
        status, opened, _ = self.request("GET", f"/api/backtests/{saved['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(opened["backtest"]["result"], result)


if __name__ == "__main__":
    unittest.main()
