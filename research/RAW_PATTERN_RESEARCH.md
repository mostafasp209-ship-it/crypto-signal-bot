# Raw Market Pattern Research (research-only)

This experiment is isolated from the live signal engine. It does not edit signals, place orders, or deploy a strategy.

## Design
- Binance USDT perpetual 4-hour OHLCV, from 2021-01-01, for BTC, ETH, BNB, SOL, XRP, ADA, DOGE and LINK.
- Features are derived from raw candle geometry, volatility, volume, prior swing structure and trend context. RSI/EMA are optional explanatory features, not entry rules.
- Directional labels ask whether a 3R target is reached before a 1R stop within 30 bars. Entry is the next candle open; same-bar stop/target ambiguity resolves conservatively against the trade.
- Chronological 60/20/20 train/validation/test. Threshold selection uses validation only; final metrics are test-only.
- Portfolio simulation uses $100 starting equity, 2% of current equity risk, 10x notional cap, 0.04% fee per side and 0.02% slippage per side. One open position per symbol/direction stream.
- Per-symbol, per-direction trades and summary JSON are uploaded as an Actions artifact.

## Important limitations
This is a first research baseline, not a deployable strategy. Funding payments and exact exchange maintenance-margin/liquidation rules are not modeled. The leverage cap is a notional cap, not a full margin engine. Cross-symbol portfolio concurrency is not combined into one shared equity curve yet. Threshold optimization can still overfit; use multiple walk-forward folds and a final untouched period before any live use. A high win rate is not guaranteed; 3R with realistic costs is a demanding target.

## Reproducibility
Run the GitHub Actions workflow named **Raw Market Pattern Research** manually, or open a PR touching this script/workflow to run it. Inspect the JSON lines in the Actions log and download the `raw-market-pattern-research` artifact. No live strategy files are changed.
