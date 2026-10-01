"""Recorded-option replay tests use only invented Kite candles and contracts."""
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from historical_options import banknifty_catalog, collect_historical_option_prices
from option_model import simulate_options
from option_prices import OptionPriceArchive
from tests.test_spot_proxy import make_day


IST = "+05:30"


def at(clock):
    return f"2026-09-24T{clock}{IST}"


def entry():
    return {"type": "proxy_entry", "at": at("10:16:00"), "position_id": "bull-1", "side": "bull",
            "spot": 56000, "intended_short_option_side": "PE"}


def ledger():
    return {"status": "complete", "last_session": "2026-09-24", "events": [entry()]}


class FakeOptionHistoryClient:
    def __init__(self, *, include_catalog=True):
        self.calls = []
        self.include_catalog = include_catalog
        self.contracts = {
            101: ("CE", 57000, 550, 520),
            102: ("PE", 55000, 450, 430),
            103: ("PE", 56000, 920, 900),
            104: ("PE", 56500, 1020, 1000),
            # This deliberately closer premium must be ignored: the strategy
            # permits only strikes that are exact multiples of 500.
            105: ("PE", 56250, 1005, 990),
        }

    def instruments(self, exchange):
        assert exchange == "NFO"
        if not self.include_catalog:
            return []
        return [{"exchange": "NFO", "name": "BANKNIFTY", "tradingsymbol": f"BANKNIFTYTEST{token}",
                 "instrument_type": kind, "expiry": "2026-09-29", "strike": strike,
                 "instrument_token": token}
                for token, (kind, strike, _, _) in self.contracts.items()]

    def historical_minutes(self, token, start, end):
        self.calls.append((token, start, end))
        kind, strike, entry_price, close_price = self.contracts[token]
        if start.hour == 10 and start.minute == 16:
            return [{"timestamp_ist": at("10:16:00"), "open": entry_price, "high": entry_price,
                     "low": entry_price, "close": entry_price, "volume": 10}]
        rows = []
        for clock, price in (("09:15:00", entry_price), ("10:16:00", entry_price), ("15:29:00", close_price)):
            stamp = datetime.fromisoformat(at(clock))
            if start <= stamp <= end:
                rows.append({"timestamp_ist": stamp.isoformat(), "open": price, "high": price,
                             "low": price, "close": price, "volume": 10})
        return rows


class HistoricalOptionDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = OptionPriceArchive(Path(self.temp.name) / "options.sqlite3")
        self.rows = make_day(date(2026, 9, 24), base=56000)
        self.client = FakeOptionHistoryClient()

    def test_catalog_keeps_exact_contract_identity_and_ignores_invalid_rows(self):
        catalog = banknifty_catalog(self.client.instruments("NFO") + [{"exchange": "NFO", "name": "BANKNIFTY", "tradingsymbol": "BAD", "instrument_type": "PE", "expiry": "bad", "strike": 56000, "instrument_token": 1}])
        self.assertEqual(len(catalog), 5)
        self.assertEqual(catalog[("BANKNIFTY", "2026-09-29", "PE", 56500.0)]["instrument_token"], 104)

    def test_download_selects_closest_recorded_short_and_exact_hedges(self):
        preliminary = simulate_options(self.rows, ledger())
        book, summary = collect_historical_option_prices(self.client, self.archive, self.rows, ledger(),
                                                          preliminary["config"], date(2026, 9, 24), date(2026, 9, 24))
        result = simulate_options(self.rows, ledger(), config=preliminary["config"], option_prices=book)
        short = result["ledgers"]["selling"][0]
        ce_hedge = result["ledgers"]["ce_hedge"][0]
        pe_hedge = result["ledgers"]["pe_hedge"][0]
        self.assertEqual((short["strike"], short["entry_premium"], short["entry_pricing"]["basis"]), (56500.0, 1020.0, "recorded"))
        self.assertNotIn(105, [token for token, _, _ in self.client.calls])
        self.assertEqual(short["mark_pricing"]["basis"], "recorded")
        self.assertEqual((ce_hedge["strike"], ce_hedge["entry_premium"], ce_hedge["entry_pricing"]["basis"]), (57000.0, 550.0, "recorded"))
        self.assertEqual((pe_hedge["strike"], pe_hedge["entry_premium"], pe_hedge["entry_pricing"]["basis"]), (55000.0, 450.0, "recorded"))
        self.assertEqual(summary["candidate_contracts_checked"], 2)
        self.assertEqual(summary["lifecycle_contracts_checked"], 3)
        self.assertGreater(summary["candidate_candles_saved"], 0)
        self.assertGreater(summary["lifecycle_candles_saved"], 0)
        self.assertTrue(any(token == 104 and start.hour == 10 for token, start, _ in self.client.calls))

    def test_unlisted_expired_contracts_are_estimated_not_guessed(self):
        client = FakeOptionHistoryClient(include_catalog=False)
        preliminary = simulate_options(self.rows, ledger())
        book, summary = collect_historical_option_prices(client, self.archive, self.rows, ledger(),
                                                          preliminary["config"], date(2026, 9, 24), date(2026, 9, 24))
        result = simulate_options(self.rows, ledger(), config=preliminary["config"], option_prices=book)
        self.assertEqual(result["ledgers"]["selling"][0]["entry_pricing"]["basis"], "estimated")
        self.assertEqual(result["ledgers"]["ce_hedge"][0]["entry_pricing"]["basis"], "estimated")
        self.assertFalse(client.calls)
        self.assertGreater(summary["unavailable_contracts"], 0)
        self.assertTrue(any("current catalogue" in note for note in summary["notes"]))

    def test_zero_volume_candle_does_not_become_an_actual_premium(self):
        class ZeroVolume(FakeOptionHistoryClient):
            def historical_minutes(self, token, start, end):
                rows = super().historical_minutes(token, start, end)
                for row in rows:
                    if token == 104 and row["timestamp_ist"] == at("10:16:00"):
                        row["volume"] = 0
                return rows
        client = ZeroVolume()
        preliminary = simulate_options(self.rows, ledger())
        book, _ = collect_historical_option_prices(client, self.archive, self.rows, ledger(), preliminary["config"],
                                                    date(2026, 9, 24), date(2026, 9, 24))
        result = simulate_options(self.rows, ledger(), config=preliminary["config"], option_prices=book)
        self.assertEqual(result["ledgers"]["selling"][0]["strike"], 56000.0)
        self.assertEqual(result["ledgers"]["selling"][0]["entry_pricing"]["basis"], "recorded")

    def test_out_of_session_candle_is_excluded_without_stopping_replay(self):
        class OutOfSession(FakeOptionHistoryClient):
            def historical_minutes(self, token, start, end):
                rows = super().historical_minutes(token, start, end)
                if token == 104 and start.hour == 10 and start.minute == 16:
                    rows.append({"timestamp_ist": at("08:59:00"), "open": 1010, "high": 1010,
                                 "low": 1010, "close": 1010, "volume": 10})
                return rows
        client = OutOfSession()
        preliminary = simulate_options(self.rows, ledger())
        book, summary = collect_historical_option_prices(client, self.archive, self.rows, ledger(), preliminary["config"],
                                                          date(2026, 9, 24), date(2026, 9, 24))
        result = simulate_options(self.rows, ledger(), config=preliminary["config"], option_prices=book)
        self.assertEqual(result["ledgers"]["selling"][0]["entry_pricing"]["basis"], "recorded")
        self.assertEqual(summary["rejected_candles"], 1)
        self.assertEqual(summary["rejected_reasons"], {"Option candle is outside its exchange session.": 1})
        self.assertTrue(any("exchange-session checks" in note for note in summary["notes"]))


if __name__ == "__main__":
    unittest.main()
