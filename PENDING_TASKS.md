# Completion checklist

Scope: BANK NIFTY, private local workspace. Broker orders remain disabled. A paper fill is an estimate, not an exchange execution.

Latest pricing update: recorded option observations now take precedence over estimated premiums, with per-price and per-leg source labels. New backtests read the existing engine's saved option history without modifying it; future accepted option quotes are archived for reuse. Paper open marks use recorded quotes first, then the saved-entry premium model with fresh spot. Missing quotes never become fills. Saved September and synthetic fallback/restart/rollover tests passed; fresh market-hours acceptance remains pending. The existing local streaming-library permission error still needs resolution before that acceptance.

## 1. Order execution service

- [x] Save paper order requests, fills, positions and event history across restarts.
- [x] Prevent duplicate submissions and keep each strategy separate.
- [x] Handle partial fills, cancellation, rejection, uncertain restarts and three bounded retries.
- [x] Buy the new hedge completely before releasing the old hedge.
- [x] Add an account-wide pause, restart reconciliation and retained-position recovery.
- [x] Test failures, recovery, concurrency and position accounting.
- [x] Leave real broker submission locked; this task does not approve live trades.
- [ ] Implement and validate an actual broker-order adapter, margin checks and broker reconciliation before any future live deployment. The completed service is paper-only.

## 2. Streaming market data

- [x] Implement authenticated server-side Zerodha spot and option-depth transport; install the pinned dependency.
- [x] Validate packets, exchange timestamps, subscription identity and quote freshness.
- [x] Reconnect with three retries, resubscribe, and block use of stale prices.
- [x] Build observed minute/hour candles without inventing missing prices.
- [x] Show stream health and timestamps in Market data.
- [ ] Record a real market-hours connection test, or name its actual blocker.

## 3. Verified exchange calendar

- [x] Keep official sources, covered dates and review date together.
- [x] Distinguish NSE trading holidays from settlement holidays.
- [x] Preserve special/split sessions and actual monthly-expiry rules.
- [x] Reject unsupported dates and special sessions with unverified hours.
- [x] Check listed contract expiries and lot sizes before paper execution.
- [x] Test holidays, month/year boundaries, special sessions and rollovers.
- [ ] Add the official 8 November Muhurat hours and 2027 calendar when verified; these dates are blocked, not guessed.

## 4. Real-quote paper tests

- [x] Implement listed-contract selection using fresh quotes at 500-point strikes; disclose excluded stale/missing candidates.
- [x] Use buy offers / sell bids and available depth, never assume a fill at last price.
- [x] Save quote evidence, paper fills, costs and results separately from backtests.
- [x] Test the complete CE/PE paper round trip, partial liquidity, stale prices and buy-before-sell replacement with synthetic data.
- [x] Verify browser controls, safe failure without login, pause/resume and phone layout without sending broker orders.
- [ ] Run the full path with actual market-hours Zerodha quotes after reconnection. Do not mark synthetic tests as real acceptance.
- [x] Separate passing connectivity/round-trip checks from multi-session strategy validation.
- [x] Store validated Tradetron paper entry, exit and hedge-replacement events locally; never mistake an API acknowledgement for an execution or retry an uncertain request.
- [x] Keep a protected Tradetron link, signal counters and paper outbox per saved deployment; provide a one-time local migration from the older app-wide link.
- [x] Record deployment lifecycle states without claiming that linking starts a runner or enables live trading.
- [x] Build the paper-only 09:15-anchored BANKNIFTY signal producer: fixed close-based 500/1,000-point risk, independent CE/PE state, 500-point strike selection, next-month hedge rollover and duplicate-event protection.
- [x] Keep every Tradetron runtime update fail-closed until its matching paper execution is manually confirmed; an API acknowledgement is never treated as a fill.
- [ ] Connect and validate an autonomous strategy runner across complete sessions, rollover and reconnects before declaring deployment readiness.

## Evidence from this implementation

- 320 Python tests passed, including 41 added service/API regressions.
- 40 interface checks passed, including five added paper-screen checks.
- Browser checked pause/resume, blocked stream start without credentials, linked calendar sources and 390-pixel layout with no horizontal page overflow. No browser errors were reported.
- Original audit reproductions passed, including 5,136 premium comparisons with zero mismatches. The existing backtest strategy was not changed.
- Fixed an observed Windows issue where two server processes could share port 8765. A regression now requires a single listener.
- Reconnection verified on 30 September 2026 at about 20:46 IST. Real Zerodha access returned 36,330 instruments, including 923 BANKNIFTY contracts, listed lot size 30 and monthly expiries matching 27 October, 23 November and 29 December. A spot snapshot returned 54,633.05 with exchange timestamp 17:35:05 IST; it was several hours old and was not used for a fill.
- Actual blocker is now market closure, not login. The stream-start endpoint correctly rejected the request with "The verified exchange session is closed." Paper fills and positions remained zero. Fresh-stream and real-quote acceptance remain pending until an open session with a valid login; no broker order has been sent.

See [PAPER_SERVICES.md](PAPER_SERVICES.md) for operating steps, sources and limitations. The four workstreams are implemented to the paper-acceptance boundary, not all four independently market-validated or live-ready.
