# Increasing Trade Frequency of the Current #1 Research Candidate

## Baseline (existing holdout result; exploratory, not a guarantee)
- Entry family: EMA20/EMA50 bullish cross
- Trend filter: close above EMA100
- RSI minimum: 55
- MACD confirmation: enabled
- Volume filter: none
- ADX filter: none
- Stochastic filter: disabled
- Stop: 1.0 x ATR(14)
- Fixed target: 3R
- Holdout trades: 13
- Holdout win rate: 61.54%
- Net expectancy: +1.2843R/trade
- Profit factor: 3.9235
- Max drawdown: 2.298R

These results came from a large grid search, so they are susceptible to selection bias. The holdout has only 13 trades and must not be treated as definitive.

## Objective
Find a version with materially more trades while keeping win rate at or above the baseline where possible, and preserving positive net expectancy after costs. Do not optimize win rate alone.

## Candidate changes to test one at a time and as a small controlled factorial
1. Lower RSI minimum from 55 to 50, then 45.
2. Disable MACD confirmation.
3. Compare EMA100 trend filter with EMA50 and EMA150.
4. Test ATR stop multipliers 1.0, 1.25, and 1.5 while keeping the 3R target fixed.
5. Test combinations of the above only after measuring each individual change.
6. Keep volume, ADX, and stochastic filters off initially; add each only if out-of-sample results justify it.

## Evaluation protocol
- Use closed 4h candles only; signal on close, enter next candle open.
- Preserve 0.10% fee per side and 0.02% adverse slippage per side.
- If stop and target touch in the same candle, count stop first.
- Keep chronological splits. Use the existing holdout only as exploratory evidence; make a new walk-forward/forward holdout for final confirmation.
- Report train/validation/test trades, win rate, net expectancy in R, profit factor, max drawdown in R, and return by symbol and market regime.
- Compare candidates against the baseline at matched costs.
- Prefer a candidate with more trades only if net expectancy and drawdown remain acceptable; if no candidate preserves win rate, report the tradeoff rather than claiming success.
- Do not change live strategy or deploy any candidate automatically. Require explicit approval and paper-trading validation first.

## Acceptance target
Primary target: more than 13 holdout trades with win rate >= 61.54%, positive net expectancy, and profit factor > 1.5. This is an evaluation target, not a promise that such a candidate exists.
