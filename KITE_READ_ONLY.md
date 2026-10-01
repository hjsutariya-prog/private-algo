# Read-only Kite adapter

`kite_client.py` contains no order placement, cancellation, modification, margin,
portfolio, or account-setting methods. A login token can grant wider permissions
at Zerodha; this adapter restricts what this local app can request. “Read-only”
is an application restriction, not a claim that the broker issued a read-only
token. Real account credentials have not been used in its tests.

## Manual login and secrets

The user signs in on Zerodha's own HTTPS page. The app's registered callback
receives a short-lived request token, which the local server exchanges using a
SHA-256 checksum. Keep API secrets and access tokens on the server, never in
browser responses, URLs, files, console output, or logs. The adapter stores only
the access token in memory and does not retain the API secret. Its session-return
dictionary contains an access token and is **server-only**.

Use a fresh one-time `state` value in `login_url({"state": value})`, then compare
that value at the callback before exchanging the request token. The callback
route, its registered URL, browser request checks, and secret storage are the
parent app's responsibility. The adapter does not automate credentials or 2FA.

Kite's documentation says an access token normally expires at 06:00 the next day
and can become invalid earlier. A request token lasts only minutes. The adapter
does not invent a refresh token or silently log in again. A `KiteAuthError` means
the parent app must clear its own session and ask the user to sign in again.
[Official login and token documentation](https://kite.trade/docs/connect/v3/user/)

## Public interface and return shapes

Constructor: `KiteClient(api_key, access_token=None, *, opener=None)`. The optional
opener is for mocked tests. Keep one shared client per local account, rather than
creating a new client on every browser refresh.

- `login_url(redirect_params=None)` returns the public Zerodha login URL; no HTTP
  call occurs. `redirect_params` is an optional string-to-string dictionary.
- `exchange_request_token(request_token, api_secret)` returns `access_token`,
  `user_id`, `user_shortname`, and `login_time`. Only this method makes a POST, to
  `/session/token`; its return value must not be sent to the browser.
- `profile()` returns only `user_id`, `user_shortname`, and `broker`.
- `clear_access_token()` clears this client's in-memory authentication.
- `instruments(exchange="NFO")` accepts `NSE` or `NFO` and returns a list of
  dictionaries. Text fields are `tradingsymbol`, `name`, `instrument_type`,
  `segment`, and `exchange`. `instrument_token`, `exchange_token`, and `lot_size`
  are integers. `tick_size` and `last_price` are floats. `strike` is a float or
  `None`; `expiry` is an ISO date string or `None`. The catalogue supplies lot size;
  no current BANKNIFTY lot size is hardcoded into the adapter.
- `quotes(symbols)` accepts 1–500 distinct `NSE:SYMBOL`/`NFO:SYMBOL` strings and
  returns a symbol-keyed map. Every result contains integer `instrument_token`,
  positive finite `last_price`, original `timestamp`, and normalized
  `timestamp_ist`. Optional `ohlc` contains numeric `open`, `high`, `low`, `close`.
- `banknifty_spot()` returns the single quote for `NSE:NIFTY BANK`, not a futures
  or option quote. It has the same fields as one `quotes()` result.

## Data limitations and safe failures

The instrument CSV is a daily catalogue, not a source of current prices. Fetch
and store it once daily. Identify contracts by exchange plus trading symbol;
numeric derivative tokens may be reused. Listing does not prove that a strike is
permitted for a particular account, or label a contract as the desired monthly
expiry. [Official instrument documentation](https://kite.trade/docs/connect/v3/market-quotes/#instruments)

Full quotes are snapshots. Missing symbols, absent/invalid timestamps, invalid
identifiers, and nonpositive/nonfinite prices cause a safe error; zero or an old
cached price is never substituted by the adapter. It does not replace quote time
with receipt time. The parent app must display the supplied timestamp and judge
staleness. For Indian exchange quotes, timezone-less timestamps are interpreted
as India time. Index quotes need not have trade volume or order-book depth.

Quote OHLC is **daily**: `close` is the previous trading day's close. It is not a
completed hourly candle and must not feed the hourly entry strategy. Real-time
streaming is now a separate server-only connection in `market_stream.py`; see
[PAPER_SERVICES.md](PAPER_SERVICES.md). It is read-only and cannot place orders.
[Official quote documentation](https://kite.trade/docs/connect/v3/market-quotes/#market-quotes)

`KiteAuthError`, `KiteRateLimitError`, `KiteDataError`, and `KiteNetworkError` all
derive from `KiteError`. Their messages never include raw provider bodies or
credential values. There are no automatic HTTP retries. The separate stream has
an initial attempt plus at most three reconnections. HTTP redirects are blocked;
response sizes and decompression are bounded. Only exact allowed paths can be
requested. The client refuses quote calls less than one second apart; this is a
per-instance guard, not a process-wide rate limiter. The parent must also avoid
refreshing the large instrument catalogue repeatedly.
[Official errors and limits](https://kite.trade/docs/connect/v3/exceptions/)

Tests use invented responses through a fake HTTP opener. They verify contracts,
validation, error handling, and the absence of order methods; they do not verify
the user's login, data subscription, live feed, market calendar, or trading.
