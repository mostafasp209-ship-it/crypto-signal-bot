# Strategy Research Skill

## Purpose
Research changes to the spot signal bot without changing live trading behavior until evidence supports the change.

## Workflow
1. Read the current entry, exit, sizing, and signal lifecycle code before proposing edits.
2. Write each candidate setup as explicit, deterministic rules using only information available at signal time.
3. Keep the currently forming candle excluded. Any higher-timeframe feature must use a fully closed candle.
4. Define the hypothesis, expected mechanism, failure mode, and metrics before running tests.
5. Compare the candidate against the current baseline on identical symbols, timestamps, fees, and slippage assumptions.
6. Separate training/development periods from chronological validation and final out-of-sample periods.
7. Report trade count, net PnL after fees, win rate, profit factor, maximum drawdown, expectancy, and performance by market regime.
8. Do not accept a strategy solely because win rate or one backtest period improves.
9. Do not enable live trading or change risk parameters automatically.
10. If the dataset, fill model, or sample size is insufficient, say so and do not claim the strategy is profitable.

## Required output
- Hypothesis and exact rules
- Files/functions affected
- Data requirements and assumptions
- Baseline and candidate results
- Tests run and failures
- Recommendation: reject, investigate further, or paper-test

## Safety constraints
- No look-ahead bias.
- Include trading fees and realistic slippage.
- Avoid repeated parameter search on the final holdout period.
- Never commit exchange credentials or Telegram tokens.
