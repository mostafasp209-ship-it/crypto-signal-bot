#!/usr/bin/env python3
"""Walk-forward parameter research for the V3 4h spot-long breakout.
Downloads public Binance Spot klines; never places orders. Conservative OHLC fills:
if SL and TP are touched in the same candle, SL is assumed first.
"""
from __future__ import annotations
import itertools
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASES = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]
INTERVAL = "4h"
START = "2021-01-01"
FEE_PER_SIDE = 0.001       # conservative 0.10% spot fee per fill
SLIPPAGE_PER_SIDE = 0.0002 # 0.02% adverse slippage per fill
MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 12
OUT = Path("research_results")
OUT.mkdir(exist_ok=True)


def fetch_klines(symbol: str) -> pd.DataFrame:
    start_ms = int(pd.Timestamp(START, tz="UTC").timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = []
    session = requests.Session()
    # Public market-data endpoint; fallback to the standard Binance endpoint.
    bases = ["https://data-api.binance.vision", "https://api.binance.com"]
    while start_ms < end_ms:
        last_error = None
        batch = None
        for base in bases:
            try:
                r = session.get(
                    base + "/api/v3/klines",
                    params={"symbol": symbol, "interval": INTERVAL, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
                    timeout=20,
                )
                r.raise_for_status()
                payload = r.json()
                if isinstance(payload, dict):
                    raise RuntimeError(f"Binance API error: {payload}")
                batch = payload
                break
            except Exception as exc:
                last_error = exc
        if batch is None:
            raise RuntimeError(f"Failed downloading {symbol}: {last_error}")
        if not batch:
            break
        rows.extend(batch)
        next_start = int(batch[-1][0]) + 1
        if next_start <= start_ms:
            break
        start_ms = next_start
        if len(batch) < 1000:
            break
        time.sleep(0.08)
    if not rows:
        raise RuntimeError(f"No candles returned for {symbol}")
    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    # Do not use the currently-forming candle.
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["timestamp"] + pd.Timedelta(hours=4) <= now].reset_index(drop=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    prev_close = x.close.shift(1)
    tr = pd.concat([x.high-x.low, (x.high-prev_close).abs(), (x.low-prev_close).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    up, down = x.high.diff(), -x.low.diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=x.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=x.index)
    pdi = 100 * plus.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / x.atr.replace(0, np.nan)
    mdi = 100 * minus.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / x.atr.replace(0, np.nan)
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    x["adx"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x["ema200"] = x.close.ewm(span=200, adjust=False, min_periods=200).mean()
    x["ema100"] = x.close.ewm(span=100, adjust=False, min_periods=100).mean()
    x["vol_avg"] = x.volume.rolling(20, min_periods=20).mean().shift(1)
    for lb in [20, 30, 40, 55]:
        x[f"break_{lb}"] = x.high.shift(1).rolling(lb, min_periods=lb).max()
    x["vol_ratio"] = x.volume / x.vol_avg.replace(0, np.nan)
    return x


def simulate_segment(df: pd.DataFrame, cfg: dict, start_i: int, end_i: int) -> list:
    """Simulate one strictly bounded time segment; positions cannot leak across split."""
    x = df.reset_index(drop=True)
    signals = (
        (x.close > x[f"break_{cfg['lookback']}"]) &
        (x.vol_ratio >= cfg["vol_mult"]) &
        (x.adx >= cfg["adx_min"]) &
        (x.close > x[cfg["ema_col"]]) &
        x.atr.notna()
    ).fillna(False).to_numpy()
    trades = []
    i = max(start_i, 220)
    while i < end_i - 1:
        if not signals[i]:
            i += 1
            continue
        entry_i = i + 1
        entry = float(x.open.iloc[entry_i]) * (1 + SLIPPAGE_PER_SIDE)
        atr = float(x.atr.iloc[i])
        risk = atr * cfg["atr_mult"]
        if not np.isfinite(risk) or risk <= 0 or entry <= risk:
            i += 1
            continue
        stop = entry - risk
        target = entry + risk * cfg["rr"]
        exit_i, exit_price, reason = end_i - 1, float(x.close.iloc[end_i - 1]), "TIME_END"
        for j in range(entry_i, end_i):
            lo, hi = float(x.low.iloc[j]), float(x.high.iloc[j])
            hit_sl, hit_tp = lo <= stop, hi >= target
            if hit_sl:  # conservative when both levels touched in one OHLC candle
                exit_i, exit_price, reason = j, stop, "SL"
                break
            if hit_tp:
                exit_i, exit_price, reason = j, target, "TP"
                break
        exit_fill = exit_price * (1 - SLIPPAGE_PER_SIDE)
        gross_pnl = exit_fill - entry
        costs = entry * FEE_PER_SIDE + exit_fill * FEE_PER_SIDE
        net_pnl = gross_pnl - costs
        trade = {
            "symbol": cfg.get("symbol", ""),
            "signal_time": str(x.timestamp.iloc[i]),
            "entry_time": str(x.timestamp.iloc[entry_i]),
            "exit_time": str(x.timestamp.iloc[exit_i]),
            "entry": round(entry, 8), "stop": round(stop, 8), "target": round(target, 8),
            "reason": reason, "net_r": float(net_pnl / risk),
            "net_return_pct": float(net_pnl / entry * 100),
            "config": json.dumps({k:v for k,v in cfg.items() if k not in ("symbol","ema_col")}, sort_keys=True),
        }
        trades.append(trade)
        i = exit_i + 1
    return trades

def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate_pct": None, "net_expectancy_R": None, "profit_factor": None, "net_total_R": 0.0, "max_drawdown_R": None}
    rs = np.array([t["net_r"] for t in trades], dtype=float)
    wins, losses = rs[rs > 0], rs[rs < 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) else None
    equity = np.cumsum(rs)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    dd = peak - equity
    return {
        "trades": len(trades),
        "win_rate_pct": round(float((rs > 0).mean()*100), 2),
        "net_expectancy_R": round(float(rs.mean()), 4),
        "profit_factor": round(pf, 4) if pf is not None else None,
        "net_total_R": round(float(rs.sum()), 3),
        "max_drawdown_R": round(float(dd.max()), 3),
        "tp_rate_pct": round(100*sum(t["reason"]=="TP" for t in trades)/len(trades), 2),
        "sl_rate_pct": round(100*sum(t["reason"]=="SL" for t in trades)/len(trades), 2),
    }


def main():
    datasets = {}
    errors = {}
    for symbol in BASES:
        try:
            raw = fetch_klines(symbol)
            if len(raw) < 500:
                raise RuntimeError(f"Only {len(raw)} candles")
            datasets[symbol] = indicators(raw)
            print(f"DATA {symbol}: {len(raw)} closed 4h candles, {raw.timestamp.iloc[0]} .. {raw.timestamp.iloc[-1]}")
        except Exception as exc:
            errors[symbol] = str(exc)
            print(f"DATA_ERROR {symbol}: {exc}")
    if len(datasets) < 3:
        raise RuntimeError(f"Not enough Binance symbols downloaded: {errors}")
    # Chronological 70/30 split per symbol. Grid is selected on train only.
    grids = itertools.product(
        [20, 30, 40, 55],       # breakout lookback
        [1.2, 1.5, 1.8],        # volume multiple
        [20, 25, 32],           # ADX minimum
        [1.5, 2.0, 2.5],        # ATR stop
        [2.0, 3.0],             # target R multiple
        ["ema100", "ema200"],   # trend filter
    )
    results = []
    for lookback, vol_mult, adx_min, atr_mult, rr, ema_col in grids:
        cfg = {"lookback":lookback,"vol_mult":vol_mult,"adx_min":adx_min,"atr_mult":atr_mult,"rr":rr,"ema_col":ema_col}
        tr_all, te_all = [], []
        for symbol, df in datasets.items():
            split_at = int(len(df)*0.70)
            local = dict(cfg, symbol=symbol)
            tr = simulate_segment(df, local, 220, split_at)
            te = simulate_segment(df, local, split_at, len(df))
            tr_all.extend(tr); te_all.extend(te)
        train_stats, test_stats = stats(tr_all), stats(te_all)
        results.append({**cfg,"train":train_stats,"test":test_stats,
                        "eligible":train_stats["trades"]>=MIN_TRAIN_TRADES and test_stats["trades"]>=MIN_TEST_TRADES})
    # Score is net expectancy and PF; do not optimize for win rate alone.
    eligible = [r for r in results if r["eligible"] and r["test"]["profit_factor"] is not None]
    eligible.sort(key=lambda r: (r["test"]["net_expectancy_R"], r["test"]["profit_factor"], r["test"]["win_rate_pct"]), reverse=True)
    results.sort(key=lambda r: (r["train"]["net_expectancy_R"], r["train"]["profit_factor"] or 0), reverse=True)
    (OUT/"all_parameter_results.json").write_text(json.dumps(results, indent=2))
    summary = {
        "market": "Binance Spot",
        "symbols_requested": BASES,
        "symbols_downloaded": list(datasets),
        "download_errors": errors,
        "interval": INTERVAL,
        "start": START,
        "fees_per_side": FEE_PER_SIDE,
        "slippage_per_side": SLIPPAGE_PER_SIDE,
        "split": "chronological 70% train / 30% untouched holdout",
        "candles": {s:len(d) for s,d in datasets.items()},
        "parameter_combinations": len(results),
        "eligible_holdout_configs": len(eligible),
        "best_holdout_configs": eligible[:10],
        "best_train_configs": results[:5],
        "warning": "Grid search is exploratory. 70% win rate and 3R are targets, not promises; inspect holdout trades and regime stability before deployment."
    }
    (OUT/"summary.json").write_text(json.dumps(summary, indent=2))
    with (OUT/"summary.md").open("w") as f:
        f.write("# Binance V3 parameter research\n\n")
        f.write(f"- Data: Binance Spot {INTERVAL}, from {START}\n- Symbols: {', '.join(datasets)}\n- Combinations: {len(results)}\n- Fees: {FEE_PER_SIDE*100:.2f}% each side; slippage: {SLIPPAGE_PER_SIDE*100:.02f}% each side\n- Split: chronological 70% train / 30% holdout\n\n")
        f.write("## Best eligible configs by holdout expectancy\n\n")
        if not eligible:
            f.write("No configuration met minimum trade counts on train and holdout. Do not claim a validated edge.\n")
        else:
            f.write("| Lookback | Vol x | ADX | SL ATR x | Target R | EMA | Test trades | Win rate | Expectancy (R) | PF | Max DD (R) |\n|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|\n")
            for r in eligible[:10]:
                s=r["test"]
                f.write(f"| {r['lookback']} | {r['vol_mult']} | {r['adx_min']} | {r['atr_mult']} | {r['rr']} | {r['ema_col']} | {s['trades']} | {s['win_rate_pct']}% | {s['net_expectancy_R']} | {s['profit_factor']} | {s['max_drawdown_R']} |\n")
        f.write("\n## Notes\n\n")
        f.write("- Entry occurs at the next candle open, not at the signal close.\n- When stop and target are both touched in one candle, stop is counted first.\n- Fixed target backtest is a research variant and is not the current Chandelier runner implementation.\n- This is spot-long only; no shorts, funding, or leverage.\n- 3R target with 70% win rate is an unusually ambitious combination and is not assumed achievable.\n")
    print("SUMMARY_JSON")
    print(json.dumps(summary, indent=2))
    print("SUMMARY_MARKDOWN")
    print((OUT/"summary.md").read_text())


if __name__ == "__main__":
    main()
