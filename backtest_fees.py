"""Date-versioned resident Zerodha/NSE option fee estimates, per grouped order.

No account-level, debit-balance, exercise or special broker charges. We roll
before expiry, not exercise. Per-order paise rounding differs from contract-note
daily rounding. See BACKTEST_MODEL.md for source URLs and effective dates.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from backtest_config import decimal

FEE_VERSION = "zerodha-nse-options-reviewed-2026-09-30-v1"
# Start (inclusive), end (exclusive), exchange ratio, IPFT ratio, sell STT ratio.
FEE_PERIODS = [
    ("2021-01-01", "2023-04-01", "0.00053", "0.000000001", "0.0005"),
    ("2023-04-01", "2024-04-01", "0.00050", "0.000005", "0.000625"),
    ("2024-04-01", "2024-10-01", "0.000495", "0.000005", "0.000625"),
    ("2024-10-01", "2026-03-01", "0.0003503", "0.000005", "0.001"),
    ("2026-03-01", "2026-04-01", "0.000355299", "0.000000001", "0.001"),
    ("2026-04-01", None, "0.000355299", "0.000000001", "0.0015"),
]
COMPONENTS = ("brokerage", "exchange", "ipft", "sebi", "gst", "stamp_duty", "stt")


def rounded(value):
    return decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class FeeSchedule:
    version = FEE_VERSION

    def __init__(self, periods=None, brokerage="20", sebi="0.000001", gst="0.18", stamp="0.00003"):
        self.periods = FEE_PERIODS if periods is None else periods
        self.brokerage, self.sebi, self.gst, self.stamp = map(decimal, (brokerage, sebi, gst, stamp))
        if min(self.brokerage, self.sebi, self.gst, self.stamp) < 0:
            raise ValueError("Fees cannot be negative.")

    def rates(self, day):
        matches = [row for row in self.periods if date.fromisoformat(row[0]) <= day and (row[1] is None or day < date.fromisoformat(row[1]))]
        if len(matches) != 1:
            raise ValueError(f"No unique date-appropriate fee configuration for {day}.")
        return matches[0]

    def calculate(self, stamp, side, premium, units):
        if side not in ("BUY", "SELL") or type(units) is not int or units < 1 or decimal(premium) < 0:
            raise ValueError("A fee calculation needs a valid side, premium and positive integer units.")
        row = self.rates(stamp.date())
        turnover = decimal(premium) * units
        exchange, ipft, stt = map(decimal, row[2:])
        raw = {"brokerage": self.brokerage, "exchange": turnover * exchange, "ipft": turnover * ipft,
               "sebi": turnover * self.sebi, "stamp_duty": turnover * self.stamp if side == "BUY" else Decimal(0),
               "stt": turnover * stt if side == "SELL" else Decimal(0)}
        raw["gst"] = (raw["brokerage"] + raw["exchange"] + raw["ipft"] + raw["sebi"]) * self.gst
        charges = {key: float(rounded(raw[key])) for key in COMPONENTS}
        charges["total"] = float(sum((rounded(raw[key]) for key in COMPONENTS), Decimal(0)))
        return {**charges, "schedule": self.version, "effective_from": row[0], "turnover": float(rounded(turnover))}
