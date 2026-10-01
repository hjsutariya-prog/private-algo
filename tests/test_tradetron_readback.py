from pathlib import Path
import tempfile
import unittest

from tradetron_readback import TradetronReadbackStore, parse_export


HEADER = "id,broker,order_id,exchange,Instrument,underlying,expiry,instrument_type,execution_type,strike,option_type,txn_type,condition_type,condition_category,entry_date,entry_time,quantity,quantity_pending,price,underlying_price,atm_strike,amount,type_of_position,itm_otm,condition_parent_id,condition_id,leg_id,run_counter,cmd,created_at,product\n"
ROWS = (
    "1,TT Paper,one,NFO,BANKNIFTY 27OCT2026 PE 54500,BANKNIFTY,27-OCT-2026,OPT,Paper,54500,PE,SELL,,,01-Oct-2026,10:15,120,0,777.3,,,,,,,,1,,\n"
    "2,TT Paper,two,NFO,BANKNIFTY 27OCT2026 PE 53500,BANKNIFTY,27-OCT-2026,OPT,Paper,53500,PE,BUY,,,01-Oct-2026,10:15,120,0,201.5,,,,,,,,1,,\n"
)


class TradetronReadbackTests(unittest.TestCase):
    def test_official_export_rows_are_netted_without_claiming_a_fill_or_pnl(self):
        rows = parse_export(HEADER + ROWS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["txn_type"], "SELL")
        with tempfile.TemporaryDirectory() as folder:
            status = TradetronReadbackStore(Path(folder) / "readback.sqlite3").import_csv(HEADER + ROWS)
        self.assertEqual(status["rows"], 2)
        self.assertEqual({item["net_quantity"] for item in status["net_positions"]}, {-120, 120})
        self.assertIn("does not include a live option mark", " ".join(status["limitations"]))

    def test_header_only_export_is_retained_as_a_real_zero_position_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TradetronReadbackStore(Path(folder) / "readback.sqlite3")
            imported = store.import_csv(HEADER)
            latest = store.status()
        self.assertEqual(imported["rows"], 0)
        self.assertEqual(latest["status"], "imported")
        self.assertEqual(latest["net_positions"], [])

    def test_wrong_or_incomplete_csv_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Tradetron Download data CSV"):
            parse_export("symbol,quantity\nBANKNIFTY,30\n")
        with self.assertRaisesRegex(ValueError, "invalid option"):
            parse_export(HEADER + "1,,,,BAD,,,,,,XX,SELL,,,,,30,0,1,,,,,,,,,,,\n")


if __name__ == "__main__":
    unittest.main()
