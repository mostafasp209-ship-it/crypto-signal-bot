# Post-Mortem & Data-Science Audit — Crypto Signal Bot
**Date:** 2026-11-01  
**Sample:** 22 closed trades from `signals.json`  
**Author:** automated deep audit

---

## 1. Executive Summary

| Metric | Value | Verdict |
|--------|------:|---------|
| Closed trades | 22 | Small sample — directional only |
| Win rate | **18.2%** | Catastrophic |
| Expectancy / trade | **−$9.40** | Strongly negative edge |
| Total realized PnL | **−$206.88** | — |
| Total stop slippage | **$142.92** | **68.6% of all gross losses** |
| Max single loss | −$52.66 | 5.3× intended $10 risk |
| Max single slippage | $48.14 | On a $10-risk trade |
| Profit factor | 0.007 | Effectively zero |

**Primary root cause:** the bot systematically entered illiquid, micro-priced, and leveraged/exotic symbols. When price gapped through the stop, realized loss became 3–5× the intended risk. Strategy edge was secondary; **execution and universe selection destroyed the account**.

---

## 2. Causal Decomposition of Losses

```
Gross losses          ≈ $208.3
  of which slippage   ≈ $142.9  (68.6%)
  of which fees       ≈ $8.4    (4.0%)
  of which clean SL   ≈ $57.0   (27.4%)
```

Interpretation:
- Even a perfect strategy would have been ruined by slippage on this universe.
- Improving entry logic without fixing the universe is wasted effort.

---

## 3. Failure Modes (Ranked by Impact)

### F1 — Toxic Symbol Universe (Critical)
Observed losing symbols included:
- Micro-priced alts: `IOST`, `XEC`, `RLS` (entry < $0.01 → quantity explosion)
- Leveraged / exotic stock-tokens: `RPANW`, `RMU`, `RAMD`, `RAVGO`, `RLITE`, `RMRVL`, `RCSCO`, `RMSI`

These names produce:
1. Wide ATR → high `risk_pct` (observed up to 6%)
2. Thin order books → stop orders fill far beyond theoretical SL
3. Overnight gaps that skip the stop entirely

### F2 — Risk-Pct Cap Too Loose
Config allowed `risk_pct` up to 3.8%. Ten of 22 trades exceeded 2% risk per unit. Combined with position capping at $200 notional, dollar risk still stayed near $10 *on paper*, but **gap risk scaled with volatility**, not with `RISK_USD`.

### F3 — Missing Hard Liquidity Floor
`scan_symbols` ranked by `quoteVolume` but never *rejected* low-volume names. Ranking alone is insufficient; a hard minimum is required.

### F4 — RR Filter Not Applied on All Paths
`MIN_RR = 1.35` exists, yet 9/11 REFERENCE trades had RR < 1.0 (4 of them negative). The structural RR check was not reached by the active REFERENCE builder path.

### F5 — Asymmetric Payoff
- Average win: **+$0.35**
- Average loss: **−$11.57**
- Ratio ≈ 1 : 33

Even at 50% win rate this would be unprofitable. At 18% it is catastrophic.

### F6 — Duplicate / Near-Duplicate Entries
`MNT/USDT SELL` appeared three times in the same hour window. Dedup logic exists but failed under concurrent or rapid re-scan conditions.

### F7 — Legacy Strategy Pollution
History contains both the old 5m `REFERENCE_EMA_RSI_STOCH_ATR_SESSION` and the newer 4h paths. Management code still branches on multiple setup names, increasing complexity and bug surface.

---

## 4. Statistical Notes (Caveats)

- n = 22 is far below the threshold for reliable inference. Treat numbers as **diagnostic**, not as a stable edge estimate.
- No formal walk-forward or bootstrap confidence intervals are claimed.
- Slippage is measured from ticker polls, not from actual exchange fill reports; real fills could be worse.

---

## 5. Fixes Applied (2026-11-01)

| Fix | Location | Effect |
|-----|----------|--------|
| `MIN_QUOTE_VOLUME_USDT = 5_000_000` | CONFIG + `scan_symbols` | Drops thin markets before strategy evaluation |
| `BLACKLIST_SUBSTRINGS` | CONFIG + `is_tradable_symbol` | Blocks leveraged / exotic basenames seen in the loss sample |
| `MIN_ENTRY_PRICE = 0.01` | `is_tradable_symbol` | Prevents micro-price quantity explosion |
| `MAX_RISK_PCT = 0.025` | `build_signal` | Hard stop on wide-ATR entries |
| `is_tradable_symbol()` | new helper | Single gate used by scanner and signal builder |

These changes are **defensive**. They do not invent edge; they stop the bot from trading names that historically produced unbounded slippage.

---

## 6. Remaining Work (Priority Order)

1. **Paper-trade the patched bot for ≥ 50 trades** on the filtered universe before any capital.
2. Re-run the research lab (`research/comprehensive_strategy_lab.py`) **only on symbols that pass the new filters**.
3. Enforce `MIN_RR` on every builder path; reject signals whose fee-adjusted RR < 1.35.
4. Add a runtime circuit-breaker: if observed `stop_slippage_usd > MAX_ABS_SLIPPAGE_USD`, auto-disable that symbol for 7 days.
5. Split `bot.py` into modules (`universe.py`, `strategy_reference.py`, `strategy_v3.py`, `risk.py`, `execution.py`).
6. Retire all 5m / legacy setup branches from management code after a versioned history reset.

---

## 7. Decision Rule Going Forward

> No strategy candidate is promoted to live until:
> - it is evaluated exclusively on the post-filter universe,
> - out-of-sample trade count ≥ 80,
> - net expectancy > 0 after fees + 2× historical slippage estimate,
> - max adverse excursion per trade ≤ 1.5 × intended risk in ≥ 95% of trades.

Until those gates pass, the bot should run in **scan + alert only** mode (no sizing that implies real capital).
