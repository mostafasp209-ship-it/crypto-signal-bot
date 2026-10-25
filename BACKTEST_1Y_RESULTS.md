# One-year spot backtest — current bot strategy

**Run:** [GitHub Actions run](https://github.com/mostafasp209-ship-it/crypto-signal-bot/actions/runs/40172443600)  
**Artifact:** [Download detailed summary and trade ledger](https://github.com/mostafasp209-ship-it/crypto-signal-bot/actions/runs/40172443600/artifacts/12590657998)

- Period: 2025-10-25 07:20 UTC to 2026-10-25 07:20 UTC
- Data: Binance.US spot OHLCV, 5-minute candles
- Symbols: BTC/USDT, ETH/USDT, SOL/USDT
- Trades: 616 (138 net profitable, 478 net unprofitable)
- Net win rate: 22.40%
- Gross PnL: +$5.10
- Fees at 0.1% per side: $246.41
- Net PnL after fees: -$241.30
- Profit factor: 0.168
- Expectancy: -$0.392 per trade; -1.778 R per trade
- Maximum realized-equity drawdown: $242.97
- Exit reasons: 361 stop-loss exits; 255 target exits

## Per-symbol results

| Symbol | Trades | Net win rate | Net PnL | Profit factor |
|---|---:|---:|---:|---:|
| BTC/USDT | 194 | 19.59% | -$74.95 | 0.130 |
| ETH/USDT | 199 | 26.63% | -$70.70 | 0.219 |
| SOL/USDT | 223 | 21.08% | -$95.65 | 0.158 |

## Method

- Entry at the next candle open after a closed-candle signal.
- The current strategy rules are reproduced: EMA20/EMA50 trend, RSI(7), Stochastic(5,3), ATR(14) above 0.8 times its 50-period mean, and UTC session filters.
- Stop = 1 ATR; target = 1.5 ATR; expiry = 48 hours.
- Position sizing = min($10 / stop distance, $200 / entry notional).
- 0.1% fee on entry and exit; no extra slippage model.
- If stop and target are both inside the same candle, stop is assumed first.
- One open trade per symbol; positions across symbols may overlap.
- Spot BUY signals are tested. SELL signals are counted but not treated as shorts.

## Interpretation

This is a negative result for the current reference strategy on these three spot pairs. A target touch is not necessarily a net-winning trade: on 5-minute candles, fees can exceed the 1.5 ATR target move when ATR is small relative to price. Do not promote this setup to live use based on these results. This is a historical simulation, not a guarantee of future performance.
