# 3R strategy optimization results

**Research run:** [GitHub Actions run 40173394780](https://github.com/mostafasp209-ship-it/crypto-signal-bot/actions/runs/40173394780)  
**Full artifact:** [JSON summary, all 360 candidates, and report](https://github.com/mostafasp209-ship-it/crypto-signal-bot/actions/runs/40173394780/artifacts/12591760806)

## Scope

- Period: 2025-10-25 07:35 UTC through 2026-10-25 07:35 UTC
- 5-minute Binance.US spot candles for BTC/USDT, ETH/USDT, SOL/USDT
- 360 candidate combinations across pullback, breakout, support-reversal, baseline, and higher-timeframe-confirmed variants
- Target fixed at 3R; stop distances tested at 1.0, 1.25, and 1.5 ATR
- Chronological split: first 70% for training, final 30% held out
- Fees: 0.1% per side; modeled adverse slippage: 0.02% per side
- Entries at next candle open; if stop and target touch in one candle, stop is assumed first

## Result

**Zero of 360 candidates passed the training requirement** of at least 30 trades, profit factor above 1, and positive expectancy.

The best-looking held-out candidate by expectancy was a 20-bar breakout with ATR/price at least 0.15%, 1.5 ATR stop, and 3R target:

| Metric | Held-out test |
|---|---:|
| Trades | 86 |
| Net win rate | 36.04% |
| Net PnL after modeled costs | -$20.43 |
| Profit factor | 0.683 |
| Expectancy | -0.343 R/trade |
| Maximum drawdown | $35.22 |

Adding completed-hour trend confirmation did not solve the problem: the comparable breakout variant had 56 held-out trades, 33.93% net win rate, -$15.94 net PnL, PF 0.641, and -0.409 R expectancy.

## Decision

Do **not** promote any candidate to the live bot. None met even the basic profitability threshold on training data, and the held-out candidates were negative. This does not achieve the requested 70% win rate at 3R. The next research iteration should focus on better entry/exit structure and more realistic cost/fill assumptions, with a new untouched holdout period; it should not tune directly against this test set.

This is a research report, not a claim of profitable strategy performance. No live trading logic was changed.
