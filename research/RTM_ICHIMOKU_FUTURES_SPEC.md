# RTM–Ichimoku Futures Research V1

**Status:** Research specification only. Not a live strategy and not yet backtested.
**Target market:** Binance USDⓈ-M perpetual futures, USDT-margined, long and short.
**Research branch:** `research/rtm-ichimoku-futures-v1`

## User objectives and interpretation

- Net win rate target: at least 60% of fully resolved trades.
- Fixed initial reward:risk: 3:1 (TP = 3 × initial structural risk).
- Frequency target: at least one qualified signal per calendar day on average across the complete liquid-symbol universe, not one forced trade every day and not one per symbol.
- Both long and short trades.
- Preserve the live bot unchanged until research and paper-trading gates pass.

These are aspirational acceptance criteria, not assumed achievable results. A 60% win rate at +3R / -1R implies +1.4R gross expectancy per trade before fees and slippage, an exceptionally demanding edge. Never relax the 3R target or misclassify timeouts/partial outcomes to manufacture the target win rate.

## Important repository finding

The current `STRATEGY_V5.md` describes a forward-test bot configuration with a 2R default target. Its cited family research reports 34.1% win rate for a 3R EMA20/50 configuration (91 trades) and 40.0% for 2R (95 trades). The existing `research/comprehensive_strategy_lab.py` downloads Binance **Spot** candles and tests long-only candidates. It is not a valid test of the requested long/short USDⓈ-M futures system. Do not reuse its results as futures evidence.

The repository post-mortem records 22 closed trades, 18.2% wins, -$206.88 realized PnL, and severe stop slippage concentrated in illiquid/exotic symbols. The liquidity and symbol safety filters must remain mandatory.

## Candidate architecture (research hypotheses)

Use a modular scorecard; no single indicator is a standalone entry signal.

1. **Regime (4h, closed candles only):** Ichimoku cloud position and cloud slope, Tenkan/Kijun relationship, EMA 200 slope, ADX and ATR percentile. Permit trend continuation and separately label range regimes; do not mix their results.
2. **Location (1h):** objective RTM-style base-and-impulse zones, freshness/touch count, prior day/week high-low, confirmed swing levels, equal highs/lows. Every pivot/zone must be timestamped when it became knowable; no retrospective pivot leakage.
3. **Liquidity/psychology (15m):** sweep beyond a confirmed swing/equal high-low followed by close back through the level; displacement relative to ATR; relative volume; optional funding and open-interest changes only when timestamped historical observations are available.
4. **Trigger (15m):** post-sweep CHoCH/BOS confirmed on candle close, then next-bar execution or a predeclared retest entry. No same-close fills.
5. **Risk:** structural invalidation beyond sweep/zone plus a volatility floor/cap. Reject trades when stop distance, fee-adjusted 3R target, or next opposing level makes the setup infeasible.
6. **Exit:** one full-position TP at exactly +3 initial R and SL at -1 initial R for the primary experiment. No breakeven moves, trailing stops, scaling, or time-exit wins in this primary comparison. Separate experiments may test management changes, each reported independently.
7. **Safety:** liquid USDT-M perpetuals only; exclude leveraged tokens, delisted/low-price contracts, inadequate quote volume, and contracts with insufficient history. Keep existing bot safety filters.

## Baselines and ablations

Compare, on the same symbols, dates, costs and fill assumptions:
- Current V5 baseline adapted only where its spot/futures semantics are valid.
- Ichimoku + EMA regime filter alone.
- RTM zone + first retest.
- Liquidity sweep + reclaim + displacement.
- RTM + sweep + CHoCH.
- Full RTM + liquidity + Ichimoku scorecard.
- Long-only, short-only and combined results.

Then ablate one feature at a time (Ichimoku, zone freshness, sweep, volume, ADX, funding/OI) to detect redundant filters and overfitting. Do not add indicators merely to raise in-sample win rate.

## Data and backtest protocol

- Binance USDⓈ-M perpetual OHLCV, with 4h/1h/15m bars aligned to UTC and only fully closed candles used.
- Build 4h and 1h features from their own completed bars; map them backward to 15m timestamps using as-of joins. Never forward-fill future values.
- Prefer mark-price candles for stop-trigger sensitivity checks; use contract OHLCV for signal formation. Record basis/mark discrepancies.
- Historical funding and open-interest data are included only if the timestamped history is complete; otherwise explicitly run a no-funding primary test and a separately labelled subset test.
- Entry at next 15m open with adverse slippage. Include taker fee on both sides; run base and stressed cost scenarios. Include funding cashflows when available.
- If SL and TP are both touched in one bar, count SL first. Gaps through stops fill at the worse opening price plus adverse slippage.
- Enforce one position per symbol and a portfolio-level concurrent-position cap; prevent overlapping duplicate signals.
- Report resolved wins, losses, unresolved/timeouts, and invalidated setups separately. A timeout is not a win. Do not count TP1 as a 3R win.
- Chronological walk-forward: train / validation / untouched final test; roll multiple windows. Select parameters using validation only, freeze them before each test window.
- Include per-symbol, per-side, per-year, volatility-regime and trend/range breakdowns; bootstrap confidence intervals for win rate and expectancy; sensitivity to doubled costs.
- Correct for multiple testing / parameter search. Penalize candidates whose performance is concentrated in a few symbols or one regime.

## Promotion gates

A candidate is not promoted just for reaching 60% in a small sample. Minimum gates:
- At least 200 fully resolved out-of-sample trades overall, with at least 75 per side before claiming both directions are validated.
- Net 3R target win rate >=60% in the untouched walk-forward aggregate, with confidence interval disclosed.
- Positive net expectancy after fees, slippage and funding; profit factor >2 as a secondary guardrail.
- Robustness across symbols, years, sides and stressed costs; no single-symbol dependence.
- Average qualified signal frequency >=1/day across the universe, reported alongside the distribution of days with zero signals.
- Paper-trading validation for at least 50 resolved trades after code freeze.
- Human review and explicit approval before any live strategy switch.

## Deliverables

1. Reproducible data manifest and cached candle data identifiers.
2. Unit tests for no-lookahead, bar alignment, stop/target tie handling, gaps, fees, funding and duplicate suppression.
3. Candidate comparison CSV/JSON with full trade ledger.
4. Walk-forward report and per-symbol/per-side diagnostics.
5. Only after evidence: a separate implementation PR for the bot's alert logic. This research branch must not change live trading behavior.
