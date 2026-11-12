# Critical Safety-Gate Audit — 2026-11-05

## Scope
Research branch only. This change does not alter main and is not deployed to the signal bot.

## Confirmed defects in main
- safety_filters.py existed but was not imported or called by bot.py.
- scan_symbols() ranked markets by 24h quote volume but admitted every symbol, including markets below the documented $5M floor.
- If ticker retrieval failed, scanning fell back to the unfiltered market list.
- build_signal() accepted stop distances up to 3.8%, despite the post-mortem's documented 2.5% hard cap.
- STRATEGY_V5.md describes EMA20/50 + 2R, while executable bot.py creates REFERENCE_RSI_STOCH_ATR_RR15 signals using RSI recovery + Stochastic cross + volume and a 1.5R target.

## Changes in this branch
- Apply a hard $5M 24h quote-volume gate and reject symbols on the existing blacklist.
- Fail closed when ticker/quote-volume data is missing; do not bypass liquidity controls.
- Enforce the $0.01 minimum entry price and 2.5% maximum stop-distance cap.
- Add regression tests for low-liquidity, blacklisted, missing-volume, micro-price, and excessive-risk cases.

## Strategy conclusion
These are execution/universe safety corrections, not evidence of a profitable entry edge. The live RSI/Stochastic entry and 1.5R target remain unchanged. The requested 3R / 60% win-rate objective remains unproven; no parameter tuning or live promotion is performed here.

## Validation required
Run the repository's complete unit test suite and compile checks in GitHub Actions. Then run a separate historical futures test with actual fee, slippage, funding, stop-gap and walk-forward assumptions before changing entry logic.
