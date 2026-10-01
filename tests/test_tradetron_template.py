import json
import unittest

from tradetron_template import BANKNIFTY_LOT_UNITS, build_template


class TradetronPortalTemplateTests(unittest.TestCase):
    def test_template_is_a_paper_only_two_direction_executor(self):
        template = build_template()
        self.assertEqual(template["_format"], "tt-strategy/v2")
        self.assertIn("PAPER ONLY", template["name"])
        self.assertEqual(template["priceExecution"], "Market Price")
        self.assertEqual(template["exitShortsFirst"], 1)
        self.assertEqual(template["rolloverExpiryPeriod"], "Dont Rollover")
        self.assertEqual([item["name"] for item in template["sets"][:2]], [
            "CE · portal controlled · PAPER ONLY", "PE · portal controlled · PAPER ONLY",
        ])

    def test_each_direction_keeps_entry_exit_and_replacement_separate(self):
        template = build_template()
        for direction, item in zip(("ce", "pe"), template["sets"][:2]):
            entry, replacement, exit_block = item["conditions"]
            self.assertEqual([entry["type"], replacement["type"], exit_block["type"]], [
                "Entry", "Repair Continuous", "Exit",
            ])
            self.assertEqual([leg["buySell"] for leg in entry["legs"]], ["B", "S"])
            self.assertEqual([leg["buySell"] for leg in replacement["legs"]], ["B", "S"])
            self.assertEqual(entry["legs"][0]["optionType"], direction.upper())
            for block in (entry, replacement):
                self.assertTrue(all(leg["qtyType"] == "Qty" for leg in block["legs"]))
                self.assertTrue(all(leg["qty"] == str(BANKNIFTY_LOT_UNITS) for leg in block["legs"]))
            self.assertIn(f"pa_{direction}_entry_seq", entry["conditionJson"])
            self.assertIn(f"pa_{direction}_exit_seq", exit_block["conditionJson"])
            self.assertIn(f"pa_{direction}_hedge_seq", replacement["conditionJson"])

    def test_template_is_serialisable_and_never_contains_a_secret(self):
        encoded = json.dumps(build_template())
        self.assertIn("pa_ce_entry_seq", encoded)
        self.assertIn("pa_pe_hedge_seq", encoded)
        self.assertNotIn("auth-token", encoded)
        self.assertNotIn("api.tradetron.tech", encoded)

    def test_import_fields_include_their_textual_formula_as_tradetron_requires(self):
        template = build_template()
        self.assertEqual(template["variables"][0]["value"], "( tt_Number ( '0' ) )")
        entry = template["sets"][0]["conditions"][0]
        self.assertIn("get_runtime", entry["conditionValue"])
        self.assertIn("tt_expiry_offset", entry["legs"][0]["expiry"])
        self.assertIn("get_runtime", entry["legs"][0]["strike"])
        condition = json.loads(entry["conditionJson"])
        self.assertEqual(condition["operands"][0]["elements"][0]["name"], "Get Runtime Number")

    def test_replacement_tracks_old_and_new_months_without_a_global_exit(self):
        template = build_template()
        for direction, item in zip(("ce", "pe"), template["sets"][:2]):
            replacement = item["conditions"][1]
            self.assertIn(f"pa_{direction}_expiry_offset", replacement["legs"][0]["expiryJson"])
            self.assertIn(f"pa_{direction}_previous_expiry_offset", replacement["legs"][1]["expiryJson"])
        placeholder, universal_exit = template["sets"][2]["conditions"]
        self.assertEqual(placeholder["type"], "Repair Once")
        self.assertIn("builder-basic_rule", universal_exit["conditionJson"])

