# Shared historical option model

Pricing policy: `recorded_first_estimate_fallback_v1`; fallback model: `banded-delta-lower-hedge-v1`. Revised 30 September 2026. New app runs prefer recorded option observations, with explicitly labelled model estimates for missing prices. Backtest/paper P&L is not actual broker profit, executable fills, margin approval or guaranteed hedge protection. Live deployment remains unavailable.

## Recorded prices first, estimates when missing

`option_prices.py` supplies a shared, versioned resolver and persistent archive for future strategy adapters. `simulate_options(..., option_prices=book)` opts into the policy; old audit scripts intentionally omit the book to reproduce model-only assumptions. Every entry, exit, open mark and charge-bearing order retains its price basis and observation source. A leg is labelled **recorded**, **mixed** or **estimated**, and the report reconciles each group's closed/open net contribution to the total. Even fully recorded prices are backtest proxies, never proof of an exchange fill.

- The main app reads the existing `buildweek/algo/.local/history.sqlite3` cache without changing it. `--option-history PATH` selects a different compatible cache. Missing/unreadable caches produce a visible note and estimated prices. No expired-option download is promised or silently attempted.
- Accepted future option-stream quotes are saved in `data/private-algo-options.sqlite3` with full contract identity and source timestamps. They become available to later backtests. Archive-write errors appear in stream details. The archive has no automatic deletion policy; it will grow while recording.
- Match underlying, expiry, strike and CE/PE together, never token or symbol alone. An observation must be available at the valuation time. Fresh saved bid/ask quotes (at most five seconds old, not future-dated or received later) take precedence. Otherwise use the exact minute open or completed-minute close, with the candle-proxy limitation visible. No interpolation, forward filling, different expiry, nearest-time substitution or future candle close is used.
- Candle prices must be finite, positive, internally consistent, in-session and accompanied by positive volume. Zero-volume records are not treated as evidence of an executable price. Conflicting OHLC revisions are excluded rather than choosing the profitable revision. Rejection counts count source rows, including overlaps between downloads.
- Selling contract selection uses the available entry-time ATM/ITM contracts at 500-point strikes, nearest to the ₹1,000 premium target (LTP for saved quotes; opening price for minute history). Incomplete chain coverage remains disclosed. If no contract can be identified at entry, that selling leg stays model-only; a later convenient contract is never substituted.
- Hedge strikes and spot replacement distances retain their existing rules. Recorded premiums affect P&L and charges only. A known hedge contract may have an estimated entry and recorded exit, or vice versa. Fallback movement/decay starts from the leg's own entry premium, including a recorded premium when available.
- Intraminute exits use the selected strategy's declared valuation time. The boundary mode can use the just-completed option candle as a proxy; the review mode cannot use that candle's future close at its earlier minute-start reference and falls back unless an eligible recorded quote exists. Exact trigger-second execution remains unknown.
- Quantity assumptions, dated fees, closed profit, open estimates and all spot strategy decisions remain separate and unchanged. Rerun older results to apply this pricing policy.

Run `python validation/check_recorded_prices.py` for an offline check of saved 1–23 September prices; `--preview` serves an isolated browser report. It compares the same current spot ledger against recorded-first and model-only pricing, asserts unchanged trade/hedge events, and saves source/coverage evidence in `validation/recorded-prices-check.json`. This is not a new broker download or a live paper acceptance.

## Named BANKNIFTY strategy settings

The app defaults to `hour_close_boundary_uncertain_v2`: a complete 09:15–10:15 bar enters at the supplied 10:15 opening price, but its fixed stop and target are calculated from that completed hour's close (the 10:14 minute's close). Intraminute exits carry both their minute-start event phase and completed-minute valuation reference. Both limits touched means an uncertain position, with no exact exit price or P&L. The option calculation stops before that ambiguous minute and reports only the prior checked portion; it does not invent subsequent cash results.

`hour_close_review_20s_stop_first_v2` retains the separately labelled 20-second review, 10:16 entry and assumed stop-first convention, using exactly the same signal-hour close for risk. A bullish trade has stop = close − 500 and target = close + 1,000; bearish has stop = close + 500 and target = close − 1,000. An entry opening price at or beyond either level is skipped, not entered and immediately closed. Later hourly closes and monthly rollovers never move an existing trade's levels. Actual entry, exit, spot P&L and option-premium movement still use execution prices; a 1,000-point target from the reference does not promise 1,000 points of profit from a delayed entry. Independent hedge replacement triggers remain unchanged.

The old `hour_boundary_uncertain_v1` and `review_20s_stop_first_v1` execution-price risk rules remain available to explicit low-level callers for historical audit reproduction. Low-level legacy defaults are retained for compatibility, but new UI/API runs accept only the two v2 modes. Mixed-mode session ledgers are rejected. Old saved results are labelled legacy and require a rerun; they are never silently relabelled. The earlier reference-engine profit comparison verifies v1, not this subsequently requested v2 risk change.

Both modes retain fixed 500-point spot stops, 1,000-point targets, a uniform 1,000-point signal grid, no weekly entry cap, one active position per direction and independent opposite directions. The final 15-minute fragment cannot signal; the completed 14:15–15:15 hour may enter at 15:15. Special-session closing signals do not carry forward.

For boundary-mode reference parity, a direction already open just before a boundary does not take that boundary's signal, even if it gaps out at the opening price. A prior-minute completed exit permits the next hourly-boundary signal. Rollover never resets the parent spot levels.

## Reuse across strategies

`option_model.simulate_options` consumes a strategy's spot entry/exit ledger and checked minute candles. It neither generates signals nor changes the parent strategy's stops, targets, entry rules or trade limits. The current BANK NIFTY replay calls it after producing the ledger under the selected named timing rule. Future algorithms should provide the same adapter input instead of duplicating the pricing, hedge or charge calculations.

- `backtest_config.py`: versioned instrument-specific assumptions. BANKNIFTY is the only registered profile. NIFTY is explicitly rejected until its own settings and calendar exist. Quantity overrides are validated separately.
- `option_model.py`: marginal delta bands, elapsed-time decay, selling positions, independent hedge lifecycle, rollover sequencing and reconciliation.
- `backtest_calendar.py`: explicit BANKNIFTY monthly expiry calendar and regular-session holidays, currently 2021–2026.
- `market_sessions.py`: normal trading windows plus verified historical Saturday, Sunday, split and Muhurat sessions. Signals, position monitoring, downloads and replacement timing share this calendar.
- `backtest_fees.py`: dated Zerodha/NSE resident-account fee estimates, reviewed on 30 September 2026. Missing or overlapping fee periods stop the simulation. The latest published rates remain effective until explicitly updated; new revisions are not downloaded automatically. The report discloses this review date and limitation.
- `app.py`: provider download/error handling and strategy adapter. API responses contain independent `range`, `spot_proxy` and `option_model` sections.

The optional calendar and fees dependencies allow an explicitly configured instrument/account to reuse the engine. To add a new profile, supply every pricing/hedge/quantity setting, an appropriate calendar with `initial_expiry`, `next_expiry`, `rollover` and `version`, and a fee schedule with `rates`, `calculate` and `version`. The current runner supports BANKNIFTY's configured historical sessions. Other instruments need their own session calendar and tests, not silent reuse.

### Adapter contract

Candles must be validated, unique one-minute OHLC dictionaries with positive prices and time-zone-aware `timestamp_ist`. The strategy adapter identifies `last_session`, `status`, `blocked_from`, `starting_state` and `events`. Its `session_calendar_version` enables the shared special-session schedule in the option layer. The current app sets `missing_minute_policy: skip` and records `skipped_minute_count`, `data_gaps` and `approximate_due_to_missing_minutes`; future strategy adapters reuse this contract. Low-level callers retain strict defaults unless they opt into `ignore_missing_minutes=True` with complete-hour signal checks. The option layer never consumes candles beyond the accepted position-history prefix. Duplicates, invalid prices and unknown session hours still block calculation.

An entry supplies `type: proxy_entry`, a unique `position_id`, `side`, `at`, `spot` and `intended_short_option_side` (`PE` for bull, `CE` for bear). An exit supplies `type: proxy_exit`, the same parent ID, `at`, `spot`, `reason` and `price_basis` (`minute_open_gap` or `threshold_touched_within_minute`). Its optional `valuation_at` is constrained to that minute; opening gaps cannot use a later valuation time. Comparison intraminute exits use minute end for decay, not an invented exact execution second. Uncertain exits use `proxy_uncertain`, with no price.

The entire event stream is checked before any model orders: exact supplied minute timestamps, chronological opening/entry/intraminute phases, unique parent IDs, valid direction/type, one active parent per direction, matched exits, and agreement with supplied position statuses. Unknown references, duplicate closes, missing events or unmatched timestamps reject the calculation. Supplied/processed event counts are returned, and a complete result requires every event processed. Same-minute entry/exit pairs are supported. The first session starts flat. Spot-only carry-in is rejected: genuine option carry-in requires each leg's original premium, time, expiry and charges.

Use date objects for `assumed_holidays`; dates recorded on the spot ledger also propagate automatically. `status` describes execution completion; `validation_status` distinguishes `validated`, `approximate`, `uncertain` (spot) and `incomplete`. A completed calculation can still be approximate. Missing scheduled whole days are not verified holidays. Continuing under the assumed-holiday policy carries explicit dated uncertainty and warnings into both spot and option reports, even when zero individual minutes are missing. Known configured calendar closures remain distinct. Missing minutes are never interpolated. A cancellation callback is checked each session and propagated.

## Assumptions and sequence

At every sold-option opening, including a rollover, the replay first checks all eligible currently listed BANKNIFTY ATM/ITM strikes that are exact multiples of 500 at the entry minute. It selects the recorded price closest to ₹1,000 and retains that exact contract. If no exact candle is available, the opening remains an explicitly estimated ₹1,000 premium and no strike is invented. Known hedge strikes use their own exact contract candles first. A current Kite catalogue cannot identify a contract that has already expired; the replay may use a previously saved exact candle, but otherwise labels that leg estimated rather than guessing or reusing a token. A position's CE movement is current spot minus its own entry; PE movement is entry minus current spot. Each successive 200-point band uses its own multiplier: positive movement starts at 0.50 and increases 0.05 per band to 1.00; negative movement starts at 0.50 and decreases 0.05 to zero. Only net displacement matters.

Premium = max(0, entry premium + signed band contributions − daily decay × elapsed minutes / 1,440), rounded to two decimals at the final premium. Sold decay is 20 and hedge decay 30 premium points per calendar day, including overnight/weekend time. There is no artificial selling-loss cap. Gross money result is signed premium difference × units; charges are then subtracted once.

Both independent hedges start at the first checked minute and continue even without a selling trade. This implements the latest request to maintain hedges throughout the test. They start in the current monthly cycle, or the following one if its scheduled rollover has already occurred.

- CE strike: ceil(spot / 1,000) × 1,000 + 1,000.
- PE strike: floor(spot / 1,000) × 1,000 − 1,000.
- Hedge entry premium: floor(spot / 10,000) × 100, without an added ₹100 or a ₹600 cap.
- Favourable replacement distance: twice that hedge's own entry premium, measured in spot points, not an option-premium doubling target.

A completed one-minute candle confirms a high/low trigger. The old hedge is valued at its trigger level at candle confirmation, including gap-through candles; this is a simulation convention, not a fill promise. Only that side is replaced at the next trading minute's open, with recalculated terms. A trigger before a scheduled break or at session end queues the next configured trading minute. Under the skip policy, a missing replacement price delays the replacement to the next available minute open; both scheduled and actual times are disclosed. A pending end-of-test replacement is displayed without inventing an order. The separate unimplemented live buy-first safety plan is unchanged; historical simulation follows the newly supplied close-then-open sequence.

Monthly rollover is Monday of the actual expiry week at 15:15 IST, or the first trading date between that Monday and expiry. Process opening-gap spot exits first; those positions are not reopened. Roll surviving sold legs and both hedges at the minute's opening price, retaining parent IDs and original spot stops/targets. Then process entries and intraminute spot exits against the current option legs. A later intraminute target cannot cancel an earlier rollover. Scheduled rollover suppresses a conflicting old-hedge trigger confirmed exactly at the rollover opening, but new hedges are monitored during that rollover minute and can close at 15:16 with replacement at its available open. Under the skip policy, missing rollover prices delay the action until the next available open, with both times disclosed, never beyond expiry. Strict callers stop instead.

## Quantity, results and fees

The UI exposes selling lots, CE hedge lots, PE hedge lots and comparison units per lot. Default hedged comparison is 4 / 4 / 4 lots at 30 units; the unhedged comparison is 1 / 0 / 0. Thirty is a fixed user modelling assumption, not the historical contract lot size on every date. Model quantities are independent of the saved draft's planning fields and displayed in each report. No automatic four-times margin claim is made.

All lots of one leg/action form one modelled order. Brokerage is ₹20 per order, not per lot; exchange quantity-freeze splitting is not simulated. Open records receive entry charges only. Closed records receive their entry and exit charges. No end-of-test liquidation is invented.

The three ledgers reconcile independently to closed gross, closed charges, closed net, open gross estimate, open entry charges and open net estimate. Combined net = closed net + open net; it is not realised cash. Hedge profit is never repeatedly allocated to selling trades. Records include parent references, entry/exit or mark times/spots/premiums, strike where defined, expiry, lots/units, signed movement, premium movement, decay, charges and reasons. Order history contains each fee component; rollover history records both expiries. The spot chart remains closed spot points only, not rupee equity or drawdown.

Fees are percentages of option-premium turnover unless stated otherwise. Exchange and IPFT are separate here; they are not charged again as a combined exchange rate. Period starts are inclusive:

- Before 1 Apr 2023: exchange 0.053%, IPFT ₹0.01/crore, sell STT 0.05%.
- From 1 Apr 2023: exchange 0.05%, IPFT ₹50/crore, sell STT 0.0625%.
- From 1 Apr 2024: exchange 0.0495%; other preceding rates unchanged.
- From 1 Oct 2024: exchange 0.03503%, IPFT ₹50/crore, sell STT 0.1%.
- From 1 Mar 2026: exchange ₹3,552.99/crore and IPFT ₹0.01/crore (combined 0.03553%).
- From 1 Apr 2026: sell STT 0.15%.

SEBI is ₹10/crore, buy-side stamp duty 0.003%, GST 18% on brokerage + exchange + IPFT + SEBI. Components are rounded to paise per order. Actual contract-note daily rounding, different account classifications, broker extras, exercise and physical settlement are outside this model. Calendar/fee versions are included in results so assumptions can be audited and refreshed.

### Primary fee references

- [Zerodha: April 2023 exchange/STT changes](https://zerodha.com/marketintel/bulletin/347967/revision-in-transaction-charges-and-stt-from-1st-april-2023), [NSE: April 2023 IPFT](https://nsearchives.nseindia.com/content/circulars/FA56129.pdf).
- [Zerodha: April 2024 exchange changes](https://zerodha.com/marketintel/bulletin/373933/revision-in-nse-transaction-charges-from-april-1-2024), [October 2024 exchange/STT changes](https://zerodha.com/marketintel/bulletin/391488/revision-in-transactions-charges-from-1st-october-2024).
- [NSE: March 2026 exchange/IPFT realignment](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf), [April 2026 STT revision](https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf).
- [Zerodha charges](https://zerodha.com/charges), [IPFT explanation](https://support.zerodha.com/category/account-opening/resident-individual/ri-charges/articles/ipft-charges).

## Calendar and empty-day policy

BANKNIFTY monthly expiry uses last Thursday before March 2024, last Wednesday March–December 2024, last Thursday January–August 2025, and last Tuesday from September 2025; exchange holidays move expiry backward to the preceding trading day. Historical announced contract-code transitions are not actual strike selection in this price model.

Sources: [March 2024 revision](https://nsearchives.nseindia.com/content/circulars/FAOP60011.pdf), [January 2025 revision](https://nsearchives.nseindia.com/content/circulars/FAOP65336.pdf), [September 2025 revision](https://nsearchives.nseindia.com/content/circulars/FAOP68685.pdf), [transition clarification](https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf).

Regular-session holiday sources: [2021](https://archives.nseindia.com/content/circulars/FAOP46625.pdf), [2022](https://archives.nseindia.com/content/circulars/FAOP50561.pdf), [2023](https://archives.nseindia.com/content/circulars/FAOP54759.pdf), [2024](https://nsearchives.nseindia.com/content/circulars/FAOP59723.pdf), [2025](https://nsearchives.nseindia.com/content/circulars/FAOP65588.pdf), [2026](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf).

Revisions incorporated: [29 June 2023 rather than 28 June](https://zerodha.com/marketintel/bulletin/353722/trading-holiday-on-account-of-bakri-id), [22 January 2024](https://zerodha.com/marketintel/bulletin/369149/equity-fo-and-currency-market-holiday-on-monday-january-22-2024), [20 May 2024](https://nsearchives.nseindia.com/content/circulars/FAOP61517.pdf), [20 November 2024](https://nsearchives.nseindia.com/content/circulars/FAOP64959.pdf), [15 January 2026](https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf).

The user's data policy is separate: after **successful** Zerodha downloads, an entire expected trading day with zero candles is an **assumed holiday**, including a configured weekend session. It permits position continuity and can move that replay's rollover forward, but cannot rewrite the actual expiry date. Provider/network/authentication failures abort; partially populated days and duplicate minutes are not holidays. The last checked prefix is reported if continuity breaks. This assumption can mask a provider's silent omission of an entire trading day; it is not proof of an exchange closure.

### Complete hours and special sessions

Each declared trading period is split into non-overlapping hours anchored at its opening. Exactly 60 unique, consecutive one-minute candles are required for an entry candle. A 45-minute period is ignored for entries; it cannot be joined to a later period across a scheduled break. One incomplete hour does not hide other complete hours on the same date. The selected named strategy determines the boundary entry or 20-second review and next-full-minute entry. **If that minute falls outside the session, skip the signal rather than queueing an entry for the next session**, as requested by the user.

Short periods still monitor existing stops, targets and hedge triggers. Under the user's latest rule, missing minutes are skipped and backtesting continues through available prices. No candle is filled in or interpolated; stops/targets are checked at the next observed open and within subsequent available candles. Triggers during a missing minute may be missed, so every affected report is labelled approximate and lists the skipped minutes. An hour with fewer than 60 valid minutes still cannot create an entry. If the scheduled entry minute itself is missing, that entry is skipped rather than moved. Trailing gaps are marked at the last actual candle close, not a fictional session-close price. Elapsed-time option decay includes gaps. Unconfigured special dates may show complete observed hours, but position tracking still stops because sparse prices cannot establish the full trading schedule.

Verified exceptions in the 2021–2026 calendar (normal market hours, excluding pre-open and mock sessions):

- 20 January 2024, 1 February 2025 and 1 February 2026: 09:15–15:30. Sources: [Zerodha January 2024 announcement](https://zerodha.com/marketintel/bulletin/369143/equity-and-fo-markets-open-on-january-20-2024), [NSE February 2025](https://nsearchives.nseindia.com/content/circulars/FAOP65730.pdf), [NSE February 2026](https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf).
- 2 March and 18 May 2024: 09:15–10:00, then 11:30–12:30. Only the second period supplies a full entry candle, and its closing signal is skipped. Sources: [NSE March split session](https://nsearchives.nseindia.com/content/circulars/MSD60677.pdf), [NSE May split session](https://nsearchives.nseindia.com/content/circulars/MSD61893.pdf).
- Muhurat: 4 November 2021, 24 October 2022 and 12 November 2023, 18:15–19:15; 1 November 2024, 18:00–19:00; 21 October 2025, 13:45–14:45. Sources: Zerodha announcements for [2021](https://zerodha.com/marketintel/bulletin/308082/muhurat-trading-session-on-account-of-diwali-5), [2022](https://zerodha.com/marketintel/bulletin/334061/muhurat-trading-session-on-account-of-diwali-6), [2023](https://zerodha.com/marketintel/bulletin/363312/muhurat-trading-session-on-account-of-diwali-7), [2024](https://zerodha.com/z-connect/updates/muhurat-trading-session-on-friday-november-1-2024), and [NSE 2025](https://nsearchives.nseindia.com/content/circulars/FAOP70320.pdf). All are checked as one complete hour, but closing signals do not open trades.

Session-calendar review date: 30 September 2026. Future unannounced special hours must be added explicitly. Date-specific promotional brokerage rebates are not modelled; the fee estimate retains the standard per-order brokerage. The old January 2024 cutoff can now be replayed with the configured Saturday session, but a fresh real-data replay is still required. A five-year requested window is not a claim of complete five-year coverage.

## Checks

The supplied PE example gives movement −190, decay 20, premium ₹790 and seller profit 210 per unit. Spot 56,000 produces ₹500 hedges and spot triggers 57,000/55,000. Tests cover positive/negative band boundaries and caps, floor/rounding, weekend decay, independent side replacement, pending replacements, both directions, monthly reset and exit precedence, holiday rollover, missing prices/configuration, quantity overrides and reconciliation.

Tests also cover 59 versus 60 minutes, duplicate prices, independently valid hours, split periods, Saturday entries, Sunday date selection, evening download bounds, short-period stops, hedge replacement across a scheduled break, and skipped closing signals.

Run `python -m unittest discover -s tests` and `node --test tests/report_ui.test.cjs tests/workspace_ui.test.cjs tests/paper_ui.test.cjs`. The isolated browser fixture runs with `python -m tests.preview_options` at port 8766; add `--special` for the special-session report, `--validation` for missing-day warnings and rollover-minute monitoring, or `--hour-close` for a 53,700 signal close, 54,100 delayed entry, 53,200 stop and 54,700 target. It uses a temporary database and explicitly labelled invented prices, never the user's saved strategies or real Kite credentials. No synthetic result proves profitability.
