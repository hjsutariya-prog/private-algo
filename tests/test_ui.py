"""Static UI safety/structure checks, not browser or trading verification."""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import ASSETS  # noqa: E402


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


class OfflineInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.page = Page(cls.html)
        cls.by_id = {attrs["id"]: (tag, attrs) for tag, attrs in cls.page.elements if "id" in attrs}
        cls.visible_copy = " ".join(" ".join(cls.page.text).split()).casefold()

    def test_html_ids_are_unique(self):
        counts = Counter(attrs["id"] for _, attrs in self.page.elements if "id" in attrs)
        self.assertTrue(counts)
        self.assertEqual({key: value for key, value in counts.items() if value > 1}, {})

    def test_trading_desk_preserves_paper_live_separation_and_disabled_recovery(self):
        desk = (ROOT / "static" / "trading-desk.js").read_text(encoding="utf-8")
        for name in ("desk-attention", "desk-risk", "desk-open", "position-groups", "desk-activity", "recent-activity"):
            self.assertIn(name, self.by_id)
        self.assertIn("disabled", self.by_id["paper-close-retained"][1])
        self.assertNotIn('paperAction("/api/paper/close-retained"', self.js)
        self.assertNotRegex(desk, r"\bfetch\s*\(|XMLHttpRequest|innerHTML\s*=|insertAdjacentHTML")
        for label in ('text("desk-execution"', 'text("desk-prices"', 'text("desk-protection"'):
            self.assertIn(label, desk)
        self.assertIn('if(key === lastKey)return', desk)
        self.assertIn('focus({preventScroll:true})', desk)

    def test_literal_javascript_id_references_exist(self):
        references = set(re.findall(r'\$\(["\']([^"\']+)["\']\)', self.js))
        references.update(re.findall(r'showError\(["\']([^"\']+)["\']', self.js))
        self.assertGreater(len(references), 20)
        self.assertEqual(references - self.by_id.keys(), set())

    def test_internal_navigation_and_labels_point_to_existing_ids(self):
        for _, attrs in self.page.elements:
            targets = []
            if attrs.get("href", "").startswith("#"):
                targets.append(attrs["href"][1:])
            if "for" in attrs:
                targets.append(attrs["for"])
            for relationship in ("aria-labelledby", "aria-describedby"):
                targets.extend(attrs.get(relationship, "").split())
            for target in targets:
                with self.subTest(target=target):
                    self.assertIn(target, self.by_id)

    def test_css_and_javascript_are_served_local_assets(self):
        assets = []
        for tag, attrs in self.page.elements:
            if tag == "script":
                self.assertIn("src", attrs, "Inline scripts would violate the local app's security policy.")
                assets.append(attrs["src"])
                self.assertIn("defer", attrs)
            if tag == "link" and attrs.get("rel") == "stylesheet":
                assets.append(attrs["href"])
        self.assertEqual(len(assets), 3)
        for url in assets:
            with self.subTest(url=url):
                parts = urlsplit(url)
                self.assertFalse(parts.scheme or parts.netloc)
                self.assertTrue(parts.path.startswith("/"))
                self.assertIn(parts.path, ASSETS)
                self.assertTrue((ROOT / "static" / ASSETS[parts.path][0]).is_file())

    def test_primary_navigation_is_compact_and_keeps_core_workflows(self):
        links = [attrs for _, attrs in self.page.elements if "nav-item" in attrs.get("class", "").split()]
        self.assertEqual([link["href"] for link in links], [
            "#main", "#strategies-heading", "#deployments", "#paper-trading",
            "#positions-orders", "#research", "#kite-connection",
        ])
        for name in ("strategy-count", "check-count", "check-count-note", "broker-status"):
            self.assertIn(name, self.by_id)
        self.assertNotIn('href="#activity"', self.html)

    def test_offline_and_no_real_money_warnings_remain(self):
        desk = (ROOT / "static" / "trading-desk.js").read_text(encoding="utf-8")
        self.assertIn("live execution unavailable", desk.casefold())
        for warning in (
            "market data only",
            "example data", "saved drafts",
            "No live orders", "No broker orders", "No market prices or real orders",
        ):
            with self.subTest(warning=warning):
                self.assertIn(warning.casefold(), self.visible_copy)

    def test_limits_of_editing_and_planning_capital_remain_clear(self):
        for phrase in (
            "rule editing is not available for this template",
            "planned capital", "not a margin check", "not a funds check",
        ):
            self.assertIn(phrase.casefold(), self.visible_copy)

    def test_critical_controls_and_forms_remain(self):
        for name in ("create-strategy", "create-first-strategy", "edit-strategy", "close-dialog", "cancel-dialog", "save-strategy", "run-check"):
            self.assertEqual(self.by_id[name][0], "button")
        self.assertEqual(self.by_id["strategy-dialog"][0], "dialog")
        for form in ("strategy-form", "scenario-form"):
            self.assertEqual(self.by_id[form][0], "form")
            self.assertFalse(self.by_id[form][1].get("action"))
            pattern = rf'\$\("{form}"\)\.addEventListener\("submit",\s*async\s*\(event\)\s*=>\s*\{{\s*event\.preventDefault\(\)'
            self.assertRegex(self.js, pattern)

    def test_buttons_have_explicit_nonaccidental_submit_types(self):
        for tag, attrs in self.page.elements:
            if tag != "button":
                continue
            with self.subTest(button=attrs.get("id")):
                expected = "submit" if attrs.get("id") in {"save-strategy", "run-check", "kite-save-config", "tradetron-save-config", "replay-submit"} else "button"
                self.assertEqual(attrs.get("type"), expected)

    def test_strategy_form_has_required_bounded_inputs(self):
        name = self.by_id["strategy-name"][1]
        self.assertIn("required", name)
        self.assertEqual(name.get("maxlength"), "80")
        for name, maximum in (("strategy-lots", "100"),):
            attrs = self.by_id[name][1]
            with self.subTest(field=name):
                self.assertEqual(attrs.get("type"), "number")
                self.assertEqual(attrs.get("min"), "1")
                self.assertEqual(attrs.get("max"), maximum)
                self.assertEqual(attrs.get("step"), "1")
                self.assertIn("required", attrs)
        multiplier = self.by_id["strategy-multiplier"][1]
        self.assertEqual((multiplier.get("type"), multiplier.get("min"), multiplier.get("max"), multiplier.get("value")), ("number", "1", "100", "1"))
        self.assertEqual(multiplier.get("aria-describedby"), "strategy-multiplier-note")
        self.assertIn('id="strategy-multiplier-note"', self.html)
        capital = self.by_id["strategy-capital"][1]
        self.assertEqual(capital["type"], "text")
        self.assertEqual(capital["inputmode"], "numeric")
        self.assertIn("required", capital)
        self.assertIn('capital: parseCapital($("strategy-capital").value)', self.js)
        self.assertIn("payload.capital > 1000000000", self.js)
        self.assertIn("Number.isSafeInteger(payload.lots)", self.js)
        self.assertIn("Number.isSafeInteger(payload.capital)", self.js)

    def test_run_controls_start_disabled_and_requests_are_guarded(self):
        self.assertIn("disabled", self.by_id["scenario-select"][1])
        self.assertIn("disabled", self.by_id["run-check"][1])
        self.assertIn('headers["X-Local-Request"] = "PrivateAlgoDesk"', self.js)
        self.assertIn('credentials: "same-origin"', self.js)
        self.assertIn('body.mode !== "offline" || body.live_enabled !== false', self.js)
        self.assertIn('if (saving || !$("strategy-form").reportValidity()) return;', self.js)
        self.assertIn("if (running || !strategy || !scenarioId) return;", self.js)

    def test_past_data_replay_separates_spot_points_from_option_pnl(self):
        rendered_or_dynamic = self.visible_copy + " " + self.js.casefold()
        for phrase in (
            "spot points are not rupee p&l",
            "not actual option quotes or fills",
            "complete 60-minute candles", "short periods", "missing minutes",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.casefold(), rendered_or_dynamic)
        self.assertIn('api("/api/replay/spot-range/jobs"', self.js)
        self.assertIn('JSON.stringify({ start_date: startDate, model_config: quantity, strategy_mode: $("strategy-mode").value, return_base: returnBase })', self.js)
        self.assertIn('id="strategy-mode"', self.html)
        self.assertIn('hour_close_boundary_uncertain_v2', self.html)
        self.assertIn('hour_close_review_20s_stop_first_v2', self.html)
        self.assertNotIn('value="hour_boundary_uncertain_v1"', self.html)
        self.assertIn('$("replay-date").addEventListener("change"', self.js)
        self.assertIn('clearSpotRangeResult();', self.js)
        self.assertIn('"incomplete", "no_price_data"', self.js)
        self.assertIn('spot_points_proxy', self.js)
        self.assertIn('Monday 15:15 expiry-week opening', self.js)
        self.assertNotIn('api("/api/replay/spot-signals"', self.js)

    def test_option_quantity_settings_are_bounded_and_explicit(self):
        for name, low, high, default in (("model-sell-lots", "1", "100", "4"), ("model-ce-lots", "0", "100", "4"), ("model-pe-lots", "0", "100", "4"), ("model-units", "1", "10000", "30")):
            attrs = self.by_id[name][1]
            self.assertEqual((attrs["min"], attrs["max"], attrs["value"]), (low, high, default))
        self.assertIn("fixed", self.visible_copy)
        self.assertIn("assumed holiday", self.visible_copy)

    def test_every_navigation_destination_has_a_separate_workspace_view(self):
        views = [attrs for _, attrs in self.page.elements if "data-view" in attrs]
        self.assertEqual({view["data-view"] for view in views}, {"overview", "deployments", "strategies", "backtests", "market-data", "safety-checks", "paper-trading", "research", "positions"})
        self.assertEqual([view["data-view"] for view in views if "hidden" not in view], ["overview"])
        links = [attrs for _, attrs in self.page.elements if "nav-item" in attrs.get("class", "").split()]
        self.assertEqual(len(links), 7)
        self.assertEqual(len({link["href"] for link in links}), 7)
        self.assertIn('href="#past-data-check"', self.html)
        self.assertIn('id="page-location"', self.html)
        self.assertIn('id="page-title"', self.html)

    def test_backtest_and_draft_quantities_are_not_implied_to_be_linked(self):
        self.assertIn("saved drafts are not deployed", self.visible_copy)
        self.assertIn("these quantities apply only to this run", self.visible_copy)
        self.assertIn("settings changed. run again", self.visible_copy)
        self.assertIn("check-strategy-select", self.by_id)

    def test_rendering_does_not_insert_untrusted_html_or_inline_handlers(self):
        self.assertNotRegex(self.js, r"\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(|\beval\s*\(")
        self.assertIn("node.textContent = String(text)", self.js)
        for _, attrs in self.page.elements:
            self.assertFalse([key for key in attrs if key.lower().startswith("on")])

    def test_model_source_summary_and_ownership_are_not_collapsed(self):
        self.assertIn("model simulator", self.visible_copy)
        self.assertIn("these quantities apply only to this run", self.visible_copy)
        self.assertIn("saved option quotes or candles first", self.visible_copy)
        self.assertIn("not a broker margin check or historical contract sizes", self.visible_copy)
        for identity in ("run-brief", "run-date-coverage", "run-quantity-summary"):
            if identity == "run-brief":
                self.assertIn('class="run-brief"', self.html)
            else:
                self.assertIn(identity, self.by_id)
        self.assertIn('id="replay-submit"', self.html)

    def test_navigation_and_draft_cards_match_single_detail_navigation(self):
        self.assertEqual(self.by_id["mobile-menu"][1]["aria-controls"], "workspace-nav")
        self.assertIn('href="#strategies-heading">← Back to strategy drafts', self.html)
        self.assertNotIn('card.setAttribute("aria-pressed"', self.js)
        self.assertIn('name.href = "#strategy-detail"', self.js)
        self.assertIn('openStrategyDetail(strategy)', self.js)
        self.assertIn("Example scenario passed", self.js)


if __name__ == "__main__":
    unittest.main()
