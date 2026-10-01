"""Fail-closed calendar for forward paper execution, not the backtest gap policy."""
from datetime import date, datetime, time, timedelta
from backtest_calendar import MonthlyCalendar, HOLIDAY_DATES, CalendarUnavailable
from engine import IST
from market_sessions import session_windows, SPECIAL_SESSIONS

REVIEWED = "2026-09-30"
VERSION = "nse-fo-forward-2026-reviewed-2026-09-30"
SOURCES = [
    {"title": "2026 F&O trading holidays", "url": "https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf"},
    {"title": "15 January additional trading holiday", "url": "https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf"},
    {"title": "1 February 2026 F&O special session", "url": "https://nsearchives.nseindia.com/content/circulars/FAOP72352.pdf"},
    {"title": "BANKNIFTY expiry transition", "url": "https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf"},
    {"title": "Current BANKNIFTY contract specification", "url": "https://www.nseindia.com/static/products-services/equity-derivatives-banknifty"},
]
PENDING_HOURS = {date(2026, 11, 8): "Muhurat session hours have not been verified; execution is blocked for this date."}


class VerifiedCalendar:
    version = VERSION

    def windows(self, day):
        if type(day) is not date or day.year != 2026:
            raise CalendarUnavailable("Forward paper calendar is verified for 2026 only; update official sources before another year.")
        if day in PENDING_HOURS:
            raise CalendarUnavailable(PENDING_HOURS[day])
        return session_windows(day)

    def is_open(self, stamp):
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError("A timezone-aware time is required.")
        local = stamp.astimezone(IST)
        return any(start <= local < end for start, end in self.windows(local.date()))

    def expiry(self, year, month):
        if year != 2026:
            raise CalendarUnavailable("Contract expiry is outside the verified forward calendar.")
        return MonthlyCalendar().expiry(year, month)

    def select_expiry(self, stamp, contracts):
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError("A timezone-aware time is required.")
        local = stamp.astimezone(IST)
        self.windows(local.date())
        # A day means 24 hours until the scheduled expiry-session close.
        listed = sorted({date.fromisoformat(c["expiry"]) for c in contracts})
        for expiry in listed:
            if expiry.year != 2026:
                continue
            if expiry != self.expiry(expiry.year, expiry.month):
                continue
            windows = self.windows(expiry)
            if windows and windows[-1][1] - local >= timedelta(days=1):
                return expiry.isoformat()
        raise CalendarUnavailable("No listed monthly contract with at least 24 hours remaining in the verified calendar.")

    def rollover(self, expiry):
        if expiry != self.expiry(expiry.year, expiry.month):
            raise CalendarUnavailable("The listed expiry does not match the verified monthly calendar.")
        monday = expiry - timedelta(days=expiry.weekday())
        for offset in range((expiry - monday).days + 1):
            day = monday + timedelta(days=offset)
            for start, end in self.windows(day):
                cutoff = datetime.combine(day, time(15, 15), IST)
                if start <= cutoff < end:
                    return cutoff
        raise CalendarUnavailable("No verified 15:15 rollover window in expiry week; do not guess a special-session time.")

    def status(self, now):
        day = now.astimezone(IST).date()
        try:
            windows = self.windows(day)
            verified = True
            message = "Today's session is verified." if windows else "Verified closed day."
        except CalendarUnavailable as exc:
            windows, verified, message = [], False, str(exc)
        return {"version": self.version, "reviewed_at": REVIEWED, "covered_year": 2026,
                "verified_today": verified, "open_now": any(a <= now < b for a, b in windows),
                "sessions": [[a.isoformat(), b.isoformat()] for a, b in windows], "message": message,
                "sources": SOURCES, "pending_dates": {d.isoformat(): v for d, v in PENDING_HOURS.items()},
                "limitation": "Published schedule as reviewed, not a guarantee against later exchange circulars. Unsupported dates block paper execution."}
