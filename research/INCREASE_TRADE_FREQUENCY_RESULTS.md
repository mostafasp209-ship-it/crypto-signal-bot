# Trade-frequency optimization: first evidence

Date: 2026-10-26

## Safety
Research only. The live bot and `bot.py` were not changed. Nothing is auto-deployed.

## Important distinction
The earlier single-grid #1 (EMA20/EMA50 cross, EMA100 trend filter, RSI >= 55, MACD confirmation, ATR stop 1x, fixed 3R) had only 13 holdout trades, 61.54% wins, +1.2843R expectancy and PF 3.9235. That is a very small sample selected from a large parameter grid; it is highly vulnerable to selection bias and is not a trustworthy baseline for a production claim.

A separate, more robust family lab uses chronological train/validation/test splits and evaluates aggregated results over eight spot pairs. Its top EMA20/EMA50-cross candidate (ATR 2x, fixed 3R) produced:
- Train: 265 trades, 24.91% win rate, -0.0557R expectancy
- Validation: 103 trades, 37.86% win rate, +0.4528R expectancy
- Test: 91 trades, 34.07% win rate, +0.2826R expectancy

## Frequency vs win-rate trade-off from the existing family lab

| Candidate | Test trades | Test win rate | Test expectancy |
|---|---:|---:|---:|
| EMA20/50 cross, ATR 2x, fixed 3R | 91 | 34.07% | +0.2826R |
| EMA20/50 cross, ATR 2x, fixed 2R | 95 | 40.00% | +0.1208R |
| EMA20/50 cross, ATR 1.5x, fixed 3R | 96 | 32.29% | +0.1848R |
| EMA20/50 cross, ATR 1.5x, fixed 2R | 97 | 39.18% | +0.0690R |

The fixed-2R variant increased test win rate by about 5.9 percentage points and test trades by 4, but reduced expectancy by about 57%. It is a candidate for further validation, not a confirmed improvement. Lowering stop distance to 1.5x ATR did not preserve the 3R candidate's win rate.

## Next controlled experiment
Use the cached OHLCV data and keep the exact same fees/slippage, entry timing, stop-first candle rule, symbols, and chronological splits. Compare small changes one at a time: RSI thresholds 55/50/45, MACD filter on/off, EMA trend filter 50/100/150, ATR stop 1.0/1.25/1.5/2.0, and fixed target 2R/2.5R/3R. Select on validation only, then evaluate the frozen candidate on a fresh forward period. Report total and per-symbol trades, win rate, expectancy, profit factor, and drawdown.

## Decision
No candidate is promoted to the live bot yet. The initial evidence shows a real trade-off: the more frequent / higher-win-rate 2R variant has lower expectancy. Continue testing before choosing; do not claim that win rate can be held constant while frequency rises unless a new out-of-sample test supports it.
