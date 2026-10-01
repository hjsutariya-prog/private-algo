"""BANKNIFTY monthly expiries, derived from date-bounded NSE rules/holidays.

This is separate from the data-provider's empty-day policy. Empty data cannot
change the exchange's expiry date. Unsupported years fail closed.
Sources and exceptional holidays are recorded in BACKTEST_MODEL.md.
"""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from engine import IST

HOLIDAYS = {
    2021: "01-26 03-11 03-29 04-02 04-14 04-21 05-13 07-21 08-19 09-10 10-15 11-04 11-05 11-19",
    2022: "01-26 03-01 03-18 04-14 04-15 05-03 08-09 08-15 08-31 10-05 10-24 10-26 11-08",
    2023: "01-26 03-07 03-30 04-04 04-07 04-14 05-01 06-29 08-15 09-19 10-02 10-24 11-14 11-27 12-25",
    2024: "01-22 01-26 03-08 03-25 03-29 04-11 04-17 05-01 05-20 06-17 07-17 08-15 10-02 11-01 11-15 11-20 12-25",
    2025: "02-26 03-14 03-31 04-10 04-14 04-18 05-01 08-15 08-27 10-02 10-21 10-22 11-05 12-25",
    2026: "01-15 01-26 03-03 03-26 03-31 04-03 04-14 05-01 05-28 06-26 09-14 10-02 10-20 11-10 11-24 12-25",
}
HOLIDAY_DATES = {date.fromisoformat(f"{year}-{day}") for year, values in HOLIDAYS.items() for day in values.split()}


class CalendarUnavailable(ValueError):
    pass


class MonthlyCalendar:
    version = "nse-banknifty-monthly-2021-2026-v1"

    def __init__(self, instrument="BANKNIFTY", assumed_holidays=()):
        if instrument != "BANKNIFTY":
            raise CalendarUnavailable("An explicit expiry calendar is required for this instrument.")
        self.assumed_holidays = set(assumed_holidays)

    def expiry(self, year, month):
        if year not in HOLIDAYS:
            raise CalendarUnavailable(f"Verified monthly expiry calendar is unavailable for {year}.")
        anchor = date(year, month, 1)
        weekday = 3 if anchor < date(2024, 3, 1) or date(2025, 1, 1) <= anchor < date(2025, 9, 1) else 2 if anchor < date(2025, 1, 1) else 1
        expiry = date(year, month, monthrange(year, month)[1])
        expiry -= timedelta(days=(expiry.weekday() - weekday) % 7)
        while expiry.weekday() >= 5 or expiry in HOLIDAY_DATES:
            expiry -= timedelta(days=1)
        return expiry

    def next_expiry(self, expiry):
        year, month = (expiry.year + 1, 1) if expiry.month == 12 else (expiry.year, expiry.month + 1)
        return self.expiry(year, month)

    def rollover(self, expiry):
        chosen = expiry - timedelta(days=expiry.weekday())
        while chosen <= expiry and (chosen.weekday() >= 5 or chosen in HOLIDAY_DATES or chosen in self.assumed_holidays):
            chosen += timedelta(days=1)
        if chosen > expiry:
            raise CalendarUnavailable(f"No rollover session from Monday through {expiry}; prices/holiday assumptions need review.")
        return datetime.combine(chosen, time(15, 15), IST)

    def initial_expiry(self, stamp):
        expiry = self.expiry(stamp.year, stamp.month)
        if stamp >= self.rollover(expiry):
            expiry = self.next_expiry(expiry)
        return expiry
