# Crypto Signal Bot V2 — Engineering Roadmap

**Status:** In development on `hardening/risk-liquidity-entry-guards`  
**Release policy:** V2 is not promoted to live execution by CI or a backtest alone.

## Product objective

Evolve the V1 signal-and-paper-monitoring bot into a modular, observable and reproducible research-to-paper-trading system. V2 prioritizes capital preservation, data integrity, transparent evidence and maintainability over raw signal count.

## V1 baseline and compatibility

- Keep the existing USDT-linear perpetual market scope unless an explicit product decision changes it.
- Preserve the existing `signals.json` history and legacy signal fields.
- Keep the current signal-only behavior; no exchange order placement is introduced by this roadmap.
- Keep Telegram panel commands and environment-variable names compatible while modules are extracted.
- Do not silently change live strategy thresholds during engineering refactors.

## Target architecture

| Module | Responsibility |
|---|---|
| `config.py` | Central runtime and strategy configuration |
| `exchange.py` | CCXT clients, market discovery and exchange adapters |
| `market_data.py` | OHLCV schema, ordering, validation, closed-candle policy and freshness |
| `indicators.py` | Deterministic indicator calculations |
| `strategy.py` | Strategy families and deterministic setup rules |
| `risk.py` | Stop validation, fee/slippage-aware sizing and exposure limits |
| `trade_manager.py` | Pure TP/SL/expiry state transitions |
| `storage.py` | Versioned, validated, atomic persistence and migration |
| `notifications.py` | Telegram transport, retries and delivery telemetry |
| `observability.py` | Structured run metrics, health and diagnostic events |
| `bot.py` | CLI and orchestration only |
| `panel.py` | Telegram control plane only |

Modules are extracted one boundary at a time. Existing imports remain as compatibility facades until downstream callers and tests migrate.

## Delivery phases

### Phase 1 — Safety and code foundations
- Centralize configuration and exchange access.
- Validate OHLCV rows, sort timestamps, remove duplicates and reject impossible/non-finite bars.
- Fail closed on corrupt signal state; never silently reset trade history.
- Expand compile, import and regression-test coverage.

### Phase 2 — Domain separation
- Extract indicator calculations, strategy evaluation, risk/sizing and trade lifecycle into pure modules.
- Keep state transitions deterministic and replayable.
- Add stable signal IDs, idempotent signal creation and explicit schema-version migration.
- Serialize all writers or move to a transactional store only after a tested export/import path exists.

### Phase 3 — Strategy research engine
- Compare the current production baseline with trend, breakout, pullback and mean-reversion families on identical symbols and candles.
- Use next-bar entries, closed candles only, conservative stop-first ambiguity, fees, spread and adverse slippage.
- Split chronologically into development, validation, walk-forward and untouched final holdout periods.
- Report sample count, expectancy in R, profit factor, drawdown, net return, exposure and regime/symbol/year breakdowns.
- Do not tune on the final holdout or promote a family from win rate alone.

### Phase 4 — Operations and Telegram V2
- Structured run summaries: symbols scanned, venues healthy, data failures, rejected candidates, signals emitted and notification delivery.
- Health/status screen with last successful scan, active signals, stale data and exchange availability.
- Explainable signal cards: setup, score components, entry, stop, targets, net RR and invalidation reason.
- Admin controls for pause/resume and paper mode; all sensitive controls audited.

### Phase 5 — Paper release gate
- At least 50 closed forward-test trades, with the sample size and market regimes disclosed.
- Positive net expectancy after realistic costs, no unacceptable drawdown, and evidence across more than one symbol/regime.
- Compare paper fills to historical replay; quantify stop slippage and missed/late polling.
- Verify Telegram delivery and scheduled workflow behavior independently from CI.
- Keep a rollback path to the last V1 release and preserve all historical records.

### Phase 6 — Optional exchange execution (separate project gate)
- Only after explicit approval and a dedicated design review.
- Exchange testnet first; order idempotency, partial fills, reduce-only exits, exchange-native protective stops, reconciliation, rate limits and kill switch.
- API keys with minimum permissions; withdrawal permissions must remain disabled.
- No automatic switch from paper to live.

## Acceptance criteria

- Every strategy input candle is validated and closed.
- No look-ahead or cross-split leakage.
- Corrupt state cannot be overwritten with an empty state.
- Re-running a scan cannot create duplicate signals for the same symbol/setup/candle.
- State transitions and PnL reconcile under partial exits and fees.
- Unit, integration, replay and workflow checks pass.
- CI status, paper-trading status and actual notification delivery are reported separately.

## Current scope delivered

The current hardening branch has extracted configuration, exchange access, shared numeric parsing, OHLCV processing and indicator calculation. The latest changes add deterministic OHLCV validation and regression tests. This is an incremental V2 foundation, not yet a complete V2 release.
