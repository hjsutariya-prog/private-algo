# Paper execution and market data

Updated 30 September 2026. No real broker orders can be submitted. Historical and forward paper valuations now share a recorded-first, estimated-fallback pricing policy; fills and strategy decisions are not fabricated from estimates.

## What is implemented

1. `paper_execution.py` saves intents, fills, net positions, quote evidence and events to a separate SQLite ledger. Stable request keys and database transactions prevent duplicate fills or submissions, including concurrent requests. Partial depth is consumed once per quote and side across strategies. Cancelled/rejected orders cannot restart. Interrupted requests become uncertain after restart and require reconciliation. Open exposure also pauses the service after restart.
2. `market_stream.py` decodes Kite full-mode index and option packets, validates instrument identity, timestamps and depth, and maintains a fresh quote book. `KiteClient.open_stream()` opens only the fixed encrypted Zerodha WebSocket address, with normal certificate checks and redirects disabled. Authenticated URLs and raw handshake errors are never logged or returned to the browser. Credentials remain in memory.
3. `verified_calendar.py` applies a date-bounded forward calendar for 2026, using the official sources below. Unsupported years and unverified special hours block paper actions rather than becoming guessed trading days or holidays. Contract expiry must match the listed monthly expiry; new selection needs at least 24 hours to the expiry session close. Lot sizes come from the day's contract list.
4. `paper_runtime.py` wires these services into an isolated one-lot acceptance lab. Each CE/PE sequence buys a hedge, sells an ATM/ITM option, fully buys its replacement hedge before closing the old hedge, then closes both remaining legs. This is an intentionally forced order-path test, not a spot signal or hedge-target trading decision.

## Use the dashboard

1. Connect Zerodha in **Market data**, then choose **Start price stream**. The current contract list is downloaded if needed. The stream subscribes to BANKNIFTY spot and 500-point strikes for the selected monthly expiry.
2. In **Safety checks**, choose **Run paper test**. Prices must be fresh, the verified exchange session must be open, and no previous test position may remain. Each leg receives an initial fill attempt plus at most three retries under the same order identity.
3. Inspect the result and download its evidence. A failed or partial test keeps its actual paper fills; it does not invent a closing price. If needed, reconnect prices and select **Close remaining paper positions**. Recovery closes only retained test exposure on available quotes, then leaves the service paused. **Check ledger & resume** verifies the stored fills before allowing further tests.

Pausing blocks new fills but does not flatten positions. Stopping prices clears the current quote book. Refreshing the page does not restart a test. Restarting the server clears Zerodha credentials and the stream, while preserving paper evidence.

## Fill and selection assumptions

- These are simulated fills at available offers for buys and bids for sells, not trades at the last price. No spread cap is imposed. No exchange queue priority, network latency, real margin or broker acceptance is promised.
- Selection uses LTP closest to ₹1,000 for ATM/ITM shorts and closest to `floor(spot / 10,000) × ₹100` for OTM hedges, among current fresh two-sided quotes at 500-point strikes. Stale/missing candidates are counted and excluded, so the lab does not claim the closest premium across an incompletely quoted chain. The selection evidence records this limitation.
- Each leg is one listed lot. A partial simulated fill can contain fewer units; recovery closes the actual remaining units rather than inventing a full lot. Live broker lot/freeze-quantity constraints are not implemented by this paper simulator.
- A quote is usable only when both exchange time and receipt time are within five seconds (one second of future clock tolerance). The quote must be received after its order request. Re-reading unchanged depth cannot manufacture additional liquidity.
- Depth is evidence, not a guarantee of fill availability. Stored order and strategy identities isolate CE/PE legs and acceptance runs.
- Fees are estimated once per grouped filled order using the existing dated fee schedule. The valuation splits closed profit from remaining open positions using first-in, first-out quantities, allocating entry/exit fees once. The older all-flat acceptance totals remain in exported evidence for compatibility.
- Open positions use fresh, exact-contract option bid/ask marks first. When option quotes are absent, the saved fill premium, saved entry spot and fresh BANKNIFTY spot feed the existing movement/decay model; the result is labelled estimated/mixed. Entry references are saved alongside fills and survive restarts. If both option and fresh spot/reference data are unavailable, or the contract has expired, open/combined values remain unknown rather than inventing a current price.
- Estimated valuations never become fills, never close positions, never consume depth and never make a failed real-quote acceptance pass. Orders and recovery still require fresh real quotes. Closed profit is based on saved paper fills, not retroactively rewritten from the premium model.
- Accepted real option quotes are archived locally for later backtests, with full contract and time metadata. Only subscribed contracts and observed times are covered; this is not a complete exchange data feed. Saving failures are visible in stream details.
- Observed spot candles reject incomplete minutes, connection gaps and the final 15-minute fragment. Observed ticks are not exchange-verified OHLC. No autonomous signal loop consumes these candles yet.

## Calendar sources and boundaries

- [NSE 2026 F&O holidays](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf).
- [Additional 15 January closure](https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf).
- [1 February 2026 F&O Sunday session](https://nsearchives.nseindia.com/content/circulars/FAOP72352.pdf).
- [BANKNIFTY Tuesday expiry transition](https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf) and [current contract specification](https://www.nseindia.com/static/products-services/equity-derivatives-banknifty).
- Historical special and split sessions remain documented in [BACKTEST_MODEL.md](BACKTEST_MODEL.md); they are not reinterpreted by the new forward calendar.

The holiday circular announces 8 November 2026 Muhurat trading but says the hours will be notified separately. Until those hours are verified, that date blocks forward paper execution. The current forward calendar does not cover 2027. Later circulars can revise a reviewed schedule; automatic circular ingestion is not implemented. Settlement-only holidays do not close this trading calendar. The historical assumed-holiday policy remains visibly approximate and does not apply to forward execution.

## Verification and remaining work

The automated tests use synthetic packets and quotes in temporary databases. A synthetic acceptance is explicitly recorded as `synthetic_passed`, never as real-quote acceptance. The dashboard exposes the separate real-quote acceptance status.

Live broker submission, real margin checks and full autonomous multi-session strategy execution remain unavailable. Passing the acceptance lab is not sufficient to remove these restrictions. A fresh Zerodha sign-in and streaming entitlement are required to verify this connection against actual market-hours quotes; a previous historical-data login does not prove that test passed.
