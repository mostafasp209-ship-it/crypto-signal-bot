# Strategy V5 — Hybrid EMA Cross + RSI/Stoch

**Date:** 2026-11-01  
**Status:** Forward-test active (paper / alert mode recommended until >=50 live trades)

## What changed

| Item | Before | After V5 |
|------|--------|----------|
| Primary entry | RSI recovery + Stoch only (0 live signals) | **EMA20/50 cross** + price above EMA100 |
| Secondary entry | — | RSI recovery (35) + Stoch cross (relaxed) |
| Stop | 1.32xATR or swing | **2.0xATR** (env: ATR_STOP_MULT) |
| Target | 1.5R fixed | **2.0R** default (env: TARGET_RR) |
| Universe | All USDT-M perps | **Min $5M quote volume** + blacklist + min price $0.01 |
| Max risk/unit | 3.8% | **2.5%** hard cap |

## Research basis (your own family lab)

| Config | Test trades | Win rate | Expectancy |
|--------|------------:|---------:|-----------:|
| EMA20/50, ATR 2x, **3R** | 91 | 34.1% | **+0.28R** |
| EMA20/50, ATR 2x, **2R** | 95 | **40.0%** | +0.12R |
| EMA20/50, ATR 1.5x, 3R | 96 | 32.3% | +0.18R |
| EMA20/50, ATR 1.5x, 2R | 97 | 39.2% | +0.07R |

**Default choice: ATR 2x / RR 2.0** — best balance of trade count, win rate, and still-positive expectancy.

### RR trade-off

| Target RR | Typical WR | Expectancy | Use when |
|----------:|------------|------------|----------|
| 1.0-1.5 | Higher | Lower | More wins; thinner edge after fees |
| **2.0** | ~40% | Modest + | **Default** |
| 2.5-3.0 | ~34% | Higher + | OK with more losses for bigger winners |

```bash
TARGET_RR=2.0
ATR_STOP_MULT=2.0
MIN_QUOTE_VOLUME_USDT=5000000
```

## Safety filters (mandatory after -$206 post-mortem)

- MIN_QUOTE_VOLUME_USDT = 5_000_000
- MIN_ENTRY_PRICE = 0.01
- MAX_RISK_PCT = 0.025
- Blacklist for leveraged/exotic basenames

**68.6% of prior gross losses were stop slippage on illiquid names.**

## Promotion gate

Do not claim live edge until:
1. >= 50 closed forward-test trades on the filtered universe
2. Net expectancy > 0 after fees
3. Observed stop slippage per trade <= 1.5x intended risk in >= 90% of trades


---

## Implementation status audit — 2026-11-05

**Important:** this document does not describe the executable strategy currently present in `main`. It is a historical V5 design/forward-test note.

The current `bot.py` implementation emits `REFERENCE_RSI_STOCH_ATR_RR15` signals with:
- 4H closed-candle RSI(14) recovery through 30 / 70;
- Stochastic(14,3) cross, with K below 38 for longs or above 62 for shorts;
- volume above 1.12 × the 20-bar average and candle direction confirmation;
- entry at the next 4H candle open;
- five-bar swing versus 1.32 × ATR(14) stop;
- one full-position target at 1.5R.

The EMA20/50 + EMA100 / 2R V5 configuration above is not the active entry path in the current `main` source. Do not treat its reported family-lab results as evidence for the current RSI/Stochastic implementation.

The safety-gate audit and proposed corrections are tracked separately in PR #28. They do not change the entry strategy or establish profitability.
