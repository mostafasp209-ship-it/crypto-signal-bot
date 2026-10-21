# Backtest Audit Skill

## Checklist
- Confirm all signals use closed candles only.
- Confirm indicator calculations are causal and do not use future rows.
- Confirm higher-timeframe values are aligned to the last fully closed candle.
- Account for entry/exit fees, spread, slippage, partial take-profits, stop losses, expiry, and unfilled orders.
- Check for duplicate signals and overlapping positions.
- Check that realized PnL reconciles with position size, partial exits, fees, and remaining fraction.
- Split results chronologically; never randomly shuffle time-series rows.
- Report drawdown, expectancy, profit factor, trade count, and net return; disclose missing assumptions.
- Add a regression test for each confirmed bug.
- Do not tune thresholds on the final out-of-sample data.
- Never interpret a small sample or live signal log as proof of a profitable strategy.

## Required output
Summarize findings as confirmed bug, possible issue, or improvement idea. Include evidence, impact, test, and proposed patch. Do not modify live trading behavior during an audit-only task.
