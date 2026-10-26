# Comprehensive strategy research plan

## Goal
Find robust, deployable Binance Spot long-only strategies for the bot. This is a comparative research process, not a claim that a universal "best strategy" exists.

## Execution order
1. Finish the currently running V3 parameter search first. Do not cancel it or change its inputs mid-run.
2. Run a separate strategy-family lab on the same 4h universe and a common cost model.
3. Compare candidates with the current live V3 baseline and buy-and-hold; never select using the final holdout.
4. Re-run shortlisted candidates using walk-forward windows and per-symbol/per-year breakdowns.
5. Paper trade the winner before any live deployment proposal.

## Candidate families
- Breakout + volume / trend confirmation
- EMA pullback in an established trend
- EMA trend crossover / alignment
- MACD momentum crossover
- Bollinger Band breakout
- RSI mean reversion (only in a defined non-crashing regime)
- Bollinger mean reversion (only in a defined non-crashing regime)
- Donchian breakout / retest variants
- Support rejection and failed-breakdown reclaim (where levels are defined using past candles only)
- Ichimoku trend breakout
- Buy-and-hold benchmark

## Fair-test rules
- Closed candles only; signal at candle close, earliest entry at next candle open.
- No look-ahead in rolling highs/lows, support/resistance, or indicator calculations.
- Fees and adverse slippage on entry and exit; conservative stop-first fills when OHLC bars touch both stop and target.
- Chronological train/validation/test split; final test remains untouched until candidate selection is frozen.
- Report net expectancy in R, profit factor, max drawdown, trade count, win rate, net return, exposure, and results by symbol and year.
- Penalize tiny samples, single-symbol dependence, parameter cliffs, and strategies that only work in one market regime.
- Save incremental checkpoints so a runner timeout does not destroy completed work.

## Speed and reliability
- Reuse downloaded candle data within each run and cache it between runs when safe.
- Start with coarse parameter grids, then refine only robust finalists.
- Checkpoint every batch and write files atomically.
- Keep research separate from `bot.py` and never alter the live strategy automatically.

## Promotion gate
No candidate is deployed merely because it ranks first. It must beat the baseline after costs, show adequate trade count and cross-symbol/year robustness, pass walk-forward validation, and complete a paper-trading period. Any live switch requires explicit approval.
