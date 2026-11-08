# Strategy V2 Futures Research

The live signal builder currently emits 4h USDT-linear perpetual signals from RSI(14) recovery through 30 for longs / drop through 70 for shorts, a Stochastic(14,3) cross and a 1.12x volume condition. The separate V3 breakout gate is not the current build_signal path. Older reports based on Binance Spot long-only data are not evidence for this futures bot.

## Fixed candidate families
- trend_breakout_20: 20-bar Donchian close breakout, volume expansion, ADX, 4h EMA200/EMA50 and last fully closed daily EMA50 regime.
- trend_pullback_ema20: trend-aligned EMA20 reclaim with candle direction and RSI zone.
- trend_macd_cross: MACD cross aligned with 4h and daily trend.
- range_mean_reversion: RSI/Bollinger re-entry only when ADX indicates a weak/ranging market.

All candidates test long and short symmetrically with the same fixed 1.5 ATR stop, 2R target and 48-bar maximum hold. Signal close -> next 4h open. Fees and adverse slippage are charged on both sides; ambiguous same-bar stop/target assumes stop first. There is one position per symbol and family. Chronological 60/20/20 split; candidate ranking uses validation median per-symbol expectancy, not holdout.

## Limitations
Historical funding, spread dynamics, order-book impact, partial fills, liquidation and maintenance margin are not modeled. This is a research simulation, not an exchange execution backtest. Funding is required before live promotion.

## Promotion policy
No candidate automatically replaces the current strategy. Require positive net expectancy after costs across multiple symbols in validation and untouched holdout, sufficient sample size, acceptable drawdown and a paper-forward period. Production changes require separate review and explicit approval.
