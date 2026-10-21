# Repository Agent Instructions

## Mission
Improve the crypto signal bot through small, evidence-based changes. Prioritize correctness, observability, reproducibility, and risk controls over signal volume.

## Required workflow
1. Investigate the current code and state/data flow before proposing a fix.
2. Write a short plan that names files, risks, and tests before non-trivial changes.
3. Make the smallest coherent change; avoid unrelated refactors.
4. Add or update regression tests for every confirmed bug.
5. Run compile checks and tests; report exact commands and results.
6. Review the final diff for unintended changes, secrets, weakened tests, and risk-control changes.
7. Use a pull request for reviewable changes. Do not merge solely because code compiles.
8. After merge, distinguish CI success from a successful live bot run and Telegram delivery.

## Trading-specific constraints
- Never claim profitability based on signal logs or a single backtest.
- Never use future or still-forming candle data to make a past decision.
- Backtests must disclose fees, slippage, fill assumptions, partial exits, and sample size.
- Keep a chronological out-of-sample period untouched during parameter tuning.
- Do not automatically enable live trading, increase risk, change order sizing, or loosen stop-loss rules.
- Preserve signal history unless an explicit versioned reset is requested.
- Never commit API keys, Telegram tokens, exchange credentials, or private user data.

## Review checklist
- Entry/exit and signal state transitions
- Duplicate suppression and concurrency
- API failures, rate limits, stale/missing market data
- Position sizing, stop loss, targets, fees and PnL reconciliation
- State persistence and backward compatibility
- Test coverage for changed behavior
- CI status and operational observability

## Expected report
Summarize: what changed, why, files affected, tests run, known limitations, and whether live behavior changed.
