#!/usr/bin/env python3
"""Targeted second-pass search: increase trade count without sacrificing out-of-sample win rate.
Research only; never places orders or changes the live bot.
"""
import itertools, json, time
from pathlib import Path
import pandas as pd
import numpy as np
from binance_v3_parameter_search import BASES, fetch_klines, indicators, simulate_segment, stats

OUT = Path("research_results")
OUT.mkdir(exist_ok=True)
MIN_TRAIN, MIN_TEST = 35, 20
STYLES = ["ema_cross", "pullback", "macd_cross", "breakout", "bollinger_breakout"]
EMA = ["ema50", "ema100", "ema200"]
ATR = [0.75, 1.0, 1.5]
RR = [1.5, 2.0, 3.0]
VOL = [None, 1.2]
ADX = [None, 25]
RSI = [None, 45, 55]
FILTERS = [(False, False), (True, True)]
LOOKBACKS = [10, 20, 30, 40, 55]

def main():
    datasets = {}
    for symbol in BASES:
        try:
            raw = fetch_klines(symbol)
            if len(raw) >= 500:
                datasets[symbol] = indicators(raw)
                print(f"DATA {symbol}: {len(raw)} candles", flush=True)
        except Exception as e:
            print(f"DATA_ERROR {symbol}: {e}", flush=True)
    if len(datasets) < 3:
        raise RuntimeError("Not enough symbols for a reliable comparison")
    started = time.time()
    # Deliberately coarse, focused grid; lookback only applies to breakout.
    configs = []
    for style, ema, atr, rr, vol, adx, rsi, (macd, stoch) in itertools.product(STYLES, EMA, ATR, RR, VOL, ADX, RSI, FILTERS):
        lbs = LOOKBACKS if style == "breakout" else [None]
        for lb in lbs:
            # Avoid excessive redundant filters; focus on configurations with plausible signal frequency.
            if style == "breakout" and vol is None and not macd and not stoch:
                continue
            configs.append(dict(style=style, lookback=lb, vol_mult=vol, adx_min=adx,
                                rsi_min=rsi, macd_filter=macd, stoch_filter=stoch,
                                atr_mult=atr, rr=rr, ema_col=ema))
    rows = []
    for ci, cfg in enumerate(configs, 1):
        tr_all, te_all = [], []
        for symbol, df in datasets.items():
            split = int(len(df)*0.70)
            local = dict(cfg, symbol=symbol)
            tr_all += simulate_segment(df, local, 220, split)
            te_all += simulate_segment(df, local, split, len(df))
        tr, te = stats(tr_all), stats(te_all)
        # Selection focuses on a meaningful increase in trade count, with win rate and positive expectancy.
        eligible = (tr["trades"] >= MIN_TRAIN and te["trades"] >= MIN_TEST and
                    te["win_rate_pct"] is not None and te["net_expectancy_R"] is not None and
                    te["net_expectancy_R"] > 0)
        rows.append({"config": cfg, "train": tr, "test": te, "eligible": eligible})
        if ci % 100 == 0:
            print(f"PROGRESS {ci}/{len(configs)} | elapsed {time.time()-started:.0f}s", flush=True)
        if ci % 100 == 0:
            tmp = OUT / "frequency_optimizer_checkpoint.json.tmp"
            tmp.write_text(json.dumps(rows, indent=2))
            tmp.replace(OUT / "frequency_optimizer_checkpoint.json")
    eligible = [r for r in rows if r["eligible"]]
    # Highest priority: test win rate, then trade count, expectancy and PF. This is exploratory;
    # the holdout is not used for any future tuning after this pass.
    eligible.sort(key=lambda r: (r["test"]["win_rate_pct"], r["test"]["trades"],
                                 r["test"]["net_expectancy_R"], r["test"]["profit_factor"] or 0), reverse=True)
    best = eligible[:30]
    result = {
        "method": "coarse targeted second-pass; same symbols/cost model as first pass",
        "symbols": list(datasets), "interval": "4h", "start": "2021-01-01",
        "fees_per_side": 0.001, "adverse_slippage_per_side": 0.0002,
        "train_test_split": "chronological 70/30",
        "configs_tested": len(rows), "eligible_count": len(eligible),
        "best_by_winrate_then_trade_count": best,
        "warning": "Small holdout and grid-selection bias are possible. Confirm with walk-forward before any deployment."
    }
    (OUT / "frequency_optimizer_results.json").write_text(json.dumps(result, indent=2))
    lines = ["# Targeted trade-frequency optimization", "",
             f"- Configurations tested: {len(rows)}", f"- Eligible candidates: {len(eligible)}",
             "- Data: Binance Spot 4h, 8 large-cap pairs when available, since 2021-01-01",
             "- Costs: 0.10% fee per side + 0.02% adverse slippage per side",
             "- Split: chronological 70% train / 30% holdout; next-candle-open entries; stop-first same-candle handling", "",
             "## Best eligible candidates (holdout ranking)", "",
             "| # | Style | Filters | Stop ATR | TP R | Test trades | Win rate | Expectancy R | PF | Max DD R |",
             "|--:|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    for n, r in enumerate(best, 1):
        c, t = r["config"], r["test"]
        filt = f"EMA {c['ema_col']}; vol={c['vol_mult']}; ADX={c['adx_min']}; RSI≥{c['rsi_min']}; MACD={c['macd_filter']}; Stoch={c['stoch_filter']}"
        lines.append(f"| {n} | {c['style']} (LB {c['lookback']}) | {filt} | {c['atr_mult']} | {c['rr']} | {t['trades']} | {t['win_rate_pct']}% | {t['net_expectancy_R']} | {t['profit_factor']} | {t['max_drawdown_R']} |")
    lines += ["", "## Important", "", "These are research candidates, not a live-bot change. The original live V3 partial-exit/Chandelier management is not reproduced by this fixed-target optimizer. Promote only after walk-forward and paper trading."]
    (OUT / "frequency_optimizer_report.md").write_text("\n".join(lines) + "\n")
    print(f"COMPLETE tested={len(rows)} eligible={len(eligible)}", flush=True)
    print("\n".join(lines[:min(len(lines), 20)]), flush=True)

if __name__ == "__main__":
    main()
