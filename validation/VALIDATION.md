# BANKNIFTY backtesting engine — implementation and validation

30 September 2026. Scope: the revised hourly BANKNIFTY spot strategy and shared modelled-option layer. No live orders, broker requests, credentials, existing-engine edits or changes to saved strategies were involved.

Historical scope: this report records the v1 execution-price risk comparison. The user subsequently requested fixed signal-hour close references for SL/target. New app runs use the separately versioned v2 rules documented in `../BACKTEST_MODEL.md`; the v1 profit-match verdict below does not claim parity for that changed strategy. Old reference scripts deliberately retain v1 settings.

## Verdict

**Conditional yes for replacing the current BANKNIFTY research backtester.** The confirmed audit defects are fixed. The selected reference-comparison mode matches every spot trade in the tested cached history, and every remaining option-event and cash difference is explained below. Use the named strategy setting and respect each result's quality warning.

This is not an unconditional five-year certification, proof of actual option returns, or approval for live trading. Keep the old engine and these comparison scripts as a regression reference. No replacement claim is made for the old app's other strategies or services.

Changes are saved on disk. The existing app process at port 8765 was deliberately not restarted: its current login and in-memory report were preserved. Restart the app to load the new engine, then sign in and rerun the report. Old results are not rewritten; refreshed UI labels unversioned results as legacy. The isolated browser preview was stopped after checking.

## What changed

1. **Rollover-minute monitoring (F1).** A conflicting old hedge trigger confirmed at the scheduled rollover yields to that rollover. New hedges opened at 15:15 are monitored through 15:15–15:16; a trigger closes and replaces the appropriate side at 15:16. CE and PE remain independent.
2. **Event ordering (F2).** Opening-gap exits happen first. Surviving option legs roll at the opening spot price. Entries and later intraminute stop/target exits then act on the current legs. Original parent spot stops and targets never reset. Comparison-mode intraminute decay uses the completed-minute time, matching the reference; it is not a claimed exact fill time.
3. **Whole-day quality (F3).** Configured closures and unverified assumed holidays are distinct. The latter propagate to both option and spot warnings and `validation_status=approximate`, even with zero missing individual minutes. Strict empty-day policy blocks instead. Missing-minute continuation remains approximate, without interpolated prices or invented fills.
4. **Event validation (F4).** Before any model order, validate exact timestamp membership, parent identities, direction/type, chronological phases, duplicate/unknown entries and exits, one active position per direction and agreement with supplied position statuses. A complete result requires every supplied strategy transition to be processed; supplied/processed counts are visible. Invalid streams are rejected rather than silently dropped.
5. **Explicit strategy versions.** App default `hour_boundary_uncertain_v1` enters at 10:15 for a completed 09:15–10:15 hour. `review_20s_stop_first_v1` is a separately named 10:16-entry alternative. Low-level omitted-mode calls retain the labelled legacy setting for compatibility. Mixed-mode session ledgers are rejected. Stop-first collision runs are explicitly approximate.
6. **Uncertain collisions.** Comparison mode records both-touched exits without an exact price or P&L; these are not normal closed trades. The option calculation stops before that ambiguous minute and reports only the prior checked portion. The uncertain spot records remain visible.

Preserved: fixed 500/1,000 spot distances, no weekly cap, independent directions, lower hedge premium `floor(spot / 10000) * 100`, marginal premium movement bands, calendar-time decay, special-session and split-session calendars, session-end replacement queues, grouped dated charges and separate closed/open/combined values. Modelled premiums remain estimates, not actual option quotes. The 15:15–15:30 fragment cannot form an hourly signal; the preceding full hour can enter at 15:15.

## Tests and actual results

- New application: **277 Python tests passed**, including 25 added audit regressions. Three pre-existing expectations were updated for corrected rollover order, explicit boundary-mode default and the holiday-count field; the UI request-shape test now checks the strategy selector.
- Report presentation: **24 Node tests passed**, including visible whole-day warnings, uncertain positions, mode labels and legacy-result labels.
- Existing engine's focused suites: **38 tests passed** (`test_thousand_levels_hourly`, `test_short_option_hedge_model`, `test_short_option_rollover`).
- Adapted original audit reproductions: **all assertions passed** against current application source. **5,136 independent premium calculations matched** the old pricing arithmetic exactly.
- Cached comparisons: event and financial reconciliation assertions passed for both windows below. No unexplained event or money difference remained.
- Browser: isolated synthetic preview showed the default boundary setting, dated whole-day warnings on spot and option panels, event counts, separate closed/open totals, rollover then target on the newly rolled selling leg, and no browser errors. Switching the input setting did not relabel the existing result. Desktop width had no page-wide horizontal overflow. No fresh production broker replay or mobile-layout recheck was performed in this task.

Test limitations are recorded, not hidden: an earlier full new-app run encountered two Windows connection resets in existing HTTP origin-rejection tests. Both passed on immediate focused rerun and the final complete suite passed; no security guard was weakened. The repeat runner retains that failed attempt in `verification-run.json`. The old app's broader suite was attempted but did not complete: its Python 3.11 binary package is incompatible with the bundled 3.12 runtime; using compatible shared packages then reached a dashboard import that attempted a write to the protected old database. Excluding that dashboard subsequently stalled and was interrupted. Those broader legacy-app tests are **not claimed as passing**. The relevant pure engine suites ran successfully without changing the old environment.

## Identical cached-candle comparison

Both engines receive identical, sorted candle records, start flat, use fixed 500/1,000 distances, a uniform 1,000-point signal grid and no weekly cap. The reference is loaded in memory with the exact two weekly-cap removals and lower hedge formula from `scripts/compare_2025_old_rules_no_weekly.py`; its files are unchanged. Older percentage, banded and higher-hedge defaults are not used.

Calendar alignment is explicit: both receive special-session candles present in the cache, including the regular-length February 2025/2026 weekend sessions. The original comparison report's upstream weekday-only signal filtering is not applied to one side alone. The retained new session calendar additionally recognises the October 2025 special hour below; no trade is created at its close.

The old SQLite cache is opened `mode=ro`. Overlapping downloads contained one differing candle revision, 18 September 2026 at 09:15. The newest stored batch is selected consistently for both engines **before** running either. Earlier and later values, batch hashes and a canonical candle digest are saved in `comparison-results.json`. This is an explicit cache-revision policy, not proof of which broker revision is correct. No candle was interpolated or changed to force agreement.

### 1–23 September 2026

- 5,999 minutes; 10/10 signals and 4/4 spot trades match, including entry/stop/target, exit bar, valuation time, prices and state. Three closed; one open.
- All 7/7 option legs match; one hedge replacement, no rollover within this window.
- Both marked at 23 September 15:29 using the last supplied 15:28 candle's close, 56,542.25.
- Aligned four-lot grouped-fee results match to the paise: closed net **₹47,264.05**, open estimate **−₹54,734.52**, combined **−₹7,470.47**. Every aligned financial difference is **₹0.00**.
- The original one-lot-charge-times-four headline was ₹46,697.64 closed net. The ₹566.41 increase after alignment is charges, not different trading logic. Its old final mark also used minute start rather than completed-minute end.
- The absent 23 September 15:29 candle keeps this run **approximate**.

### 1 January 2025–22 September 2026

- **160,184 identical minutes; 175/175 spot trades match**, with 174 closed and one open. **349/349 supplied strategy events processed.**
- **437 shared signals match.** The new calendar recognises one additional complete-hour bearish signal on 21 October 2025 at 14:45, the special-session close. It is skipped because the market is closed, so it creates no extra trade.
- **20 monthly rollover dates**, all rollover option legs, and **52 hedge replacements** reconcile. Of **293 option legs**, **292 match exactly**; the remaining difference is the session-end improvement described next. Selling has 199 legs; independent hedges have 94.
- The CE hedge opened 27 July 2026 at 15:15 hits its trigger in the final 3 August candle. The new engine closes at **3 August 15:30** and opens its replacement at **4 August 09:15**. The old engine postpones that same close until 4 August 09:15. The new engine correctly avoids an extra 1,065 minutes of decay on the already-triggered hedge.
- That single leg's exit premium is **₹889.69 new versus ₹867.50 old**. At 120 units, gross differs by **₹2,662.80**; dated grouped charges differ by **₹5.11**; net differs by **₹2,657.69**. The per-leg bridge exactly equals the whole-run difference. No other aligned cash difference remains, and no assumption was changed to erase this improvement.
- Common final mark: **22 September 2026, 15:30, spot 56,215.55**. New closed net **₹16,99,648.67**, open estimate **−₹54,886.92**, combined **₹16,44,761.75**. Aligned reference closed net **₹16,96,990.98**, same open estimate, combined **₹16,42,104.06**.
- The raw legacy headline closed net was ₹16,55,929.88. Replacing multiplied one-lot fees with the shared dated grouped-order fees accounts for **₹41,061.10**; the remaining **₹2,657.69** is the session-end hedge event. Combined totals must never be compared to that closed-only headline.
- The absent **15 December 2025 15:15** minute keeps this run **approximate**. Agreement between engines cannot recover a missed trigger inside that minute.

## Remaining limits

- This is approximately 21 months of cached real spot evidence, not all five years. January 2024 and split-session boundaries have synthetic regression coverage, not a new full historical reconciliation here.
- Minute candles cannot reveal exact intraminute ordering or executable option fills. Assumed holidays and skipped minutes can conceal real trades, stops or triggers; warnings must not be treated as cosmetic.
- Prices, volatility, bid/ask spreads, slippage, margin, historical actual lot sizes and broker execution are outside the premium model. Thirty units per lot is a fixed comparison assumption.
- Existing dated fee settings and the explicit 2021–2026 calendar were preserved, not independently recertified from every exchange publication during this coding task. Unsupported years still fail closed; future calendar/fee changes require maintenance.
- Spot-only carried-in exposure cannot establish an option's historical premium/time/expiry; unsupported option carry-in remains rejected. Both comparisons deliberately start flat.
- The reference engine's broader app suite was not fully runnable in this environment, as documented above. No claim is made about unrelated NIFTY, two-minute strategies, live services or the rest of the old app.

## Reproduce and inspect

From `private-algo`, run `python -B validation/run_checks.py` with the bundled Python runtime. The runner records actual commands, output, exit codes and prior attempts. Local paths for the old reference and bundled Node are explicit in the script; no network or broker session is needed.

- `verification-run.json`: final test run and earlier attempt.
- `audit_reproductions.py` / `audit-reproductions.json`: original audit cases, adapted to current source, with fixed-behaviour assertions and source hashes.
- `compare_engines.py` / `comparison-results.json`: candle provenance, rule settings, all remaining event differences, full option legs, aligned cash totals and assertions.
- `tests/test_engine_validation.py`: targeted regression cases.
- `BACKTEST_MODEL.md`: current adapter contract, strategy names, ordering and assumptions.
