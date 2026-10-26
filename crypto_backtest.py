# -*- coding: utf-8 -*-
"""Research-only BTCUSDT 4h backtest: breakout+volume and resistance/RTM short."""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

URLS = ["https://api.binance.us/api/v3/klines", "https://data-api.binance.vision/api/v3/klines", "https://api.binance.com/api/v3/klines"]
COLS = ["open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
FEE = 0.0004  # approximate fee per side; slippage is not included


def fetch_klines(symbol="BTCUSDT", interval="4h", limit=5000):
    pages = {}
    end_time = None
    with requests.Session() as session:
        while len(pages) < limit:
            count_before = len(pages)
            n = min(1000, limit - len(pages))
            params = {"symbol": symbol, "interval": interval, "limit": n}
            if end_time is not None:
                params["endTime"] = end_time
            response = None
            errors = []
            for endpoint in URLS:
                try:
                    candidate = session.get(endpoint, params=params, timeout=30)
                    if candidate.status_code in (403, 451):
                        errors.append(f"{endpoint}: HTTP {candidate.status_code}")
                        continue
                    candidate.raise_for_status()
                    response = candidate
                    break
                except requests.RequestException as exc:
                    errors.append(f"{endpoint}: {exc}")
            if response is None:
                raise RuntimeError("All Binance market-data endpoints failed: " + " | ".join(errors))
            batch = response.json()
            if not batch:
                break
            for candle in batch:
                pages[int(candle[0])] = candle
            if len(pages) == count_before:
                raise RuntimeError("Pagination made no progress; endpoint may be ignoring endTime.")
            oldest = min(int(x[0]) for x in batch)
            end_time = oldest - 1
            if len(batch) < n:
                break
            time.sleep(0.05)
    if not pages:
        raise RuntimeError("Binance returned no candles")
    rows = [pages[k] for k in sorted(pages)][-limit:]
    df = pd.DataFrame(rows, columns=COLS)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df[df["close_time"] < pd.Timestamp.now(tz="UTC")]
    df = df.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    if len(df) < 250:
        raise RuntimeError(f"Not enough closed candles: {len(df)}")
    return df


def indicators(df):
    df = df.copy()
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"]-df["low"], (df["high"]-pc).abs(),
                    (df["low"]-pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    df["vol_avg"] = df["volume"].rolling(20, min_periods=20).mean().shift(1)
    df["ma200"] = df["close"].rolling(200, min_periods=200).mean()
    return df


def simulate(df, start, side, entry, sl, tp, hold=30):
    risk = entry-sl if side == "long" else sl-entry
    if risk <= 0 or entry <= 0 or sl <= 0 or tp <= 0:
        return None
    end = min(start + hold, len(df))
    exit_price = None
    for j in range(start, end):
        r = df.iloc[j]
        o, h, l = float(r.open), float(r.high), float(r.low)
        if side == "long":
            if l <= sl:  # conservative if both SL and TP are touched
                exit_price = min(o, sl); break
            if h >= tp:
                exit_price = tp; break
        else:
            if h >= sl:
                exit_price = max(o, sl); break
            if l <= tp:
                exit_price = tp; break
    if exit_price is None:
        if end <= start:
            return None
        exit_price = float(df.iloc[end-1].close)
    gross = (exit_price-entry)/risk if side == "long" else (entry-exit_price)/risk
    return float(gross - FEE*(entry+exit_price)/risk)


def breakout(df, rr, vm, am, lb, hold=30):
    trades = []
    for i in range(max(lb, 200), len(df)-1):
        r = df.iloc[i]
        if pd.isna(r.atr) or pd.isna(r.vol_avg) or pd.isna(r.ma200) or r.atr <= 0:
            continue
        prior = df.iloc[i-lb:i]
        volume_ok = r.volume > vm*r.vol_avg
        entry = float(df.iloc[i+1].open)
        if not volume_ok:
            continue
        if r.close > prior.high.max() and r.close >= r.ma200:
            sl = entry-am*r.atr; tp = entry+rr*(entry-sl)
            x = simulate(df, i+1, "long", entry, sl, tp, hold)
            if x is not None: trades.append(x)
        elif r.close < prior.low.min() and r.close <= r.ma200:
            sl = entry+am*r.atr; tp = entry-rr*(sl-entry)
            x = simulate(df, i+1, "short", entry, sl, tp, hold)
            if x is not None: trades.append(x)
    return trades


def multiconfluence(df, rr, vm, fib_tol, am=1.0, lb=100, hold=30):
    trades = []
    for i in range(max(200, lb), len(df)-1):
        r = df.iloc[i]
        if pd.isna(r.atr) or pd.isna(r.vol_avg) or r.atr <= 0 or r.vol_avg <= 0:
            continue
        swing = df.iloc[i-lb:i]  # prior candles only
        hi, lo = float(swing.high.max()), float(swing.low.min())
        span = hi-lo
        if span <= 0:
            continue
        fibs = [hi-0.382*span, hi-0.5*span, hi-0.618*span]
        candle_hi = float(r.high)
        near_fib = any(abs(candle_hi-f) <= fib_tol*r.atr for f in fibs)
        body = abs(float(r.close)-float(r.open))
        upper_wick = candle_hi-max(float(r.open), float(r.close))
        rejection = r.close < r.open and upper_wick >= max(1.5*body, 0.1*r.atr)
        vol_ok = r.volume > vm*r.vol_avg
        near_resistance = candle_hi >= hi-0.5*r.atr
        if not (near_fib and rejection and vol_ok and near_resistance):
            continue
        entry = float(df.iloc[i+1].open)
        sl = max(candle_hi, entry)+am*r.atr
        tp = entry-rr*(sl-entry)
        x = simulate(df, i+1, "short", entry, sl, tp, hold)
        if x is not None: trades.append(x)
    return trades


def metrics(trades):
    if not trades:
        return {"n": 0, "winrate_pct": 0.0, "avg_r": None, "sum_r": 0.0,
                "profit_factor": None, "max_drawdown_r": 0.0}
    a = np.asarray(trades, dtype=float)
    gp, gl = float(a[a > 0].sum()), float(-a[a < 0].sum())
    eq = np.r_[0.0, np.cumsum(a)]
    dd = eq-np.maximum.accumulate(eq)
    pf = gp/gl if gl > 0 else (None if gp == 0 else "inf")
    return {"n": int(len(a)), "winrate_pct": round(float((a > 0).mean()*100), 2),
            "avg_r": round(float(a.mean()), 4), "sum_r": round(float(a.sum()), 3),
            "profit_factor": round(pf, 3) if isinstance(pf, float) else pf,
            "max_drawdown_r": round(float(dd.min()), 3)}


def main():
    df = indicators(fetch_klines())
    print(f"DATA: BTCUSDT 4h | closed candles={len(df)} | {df.open_time.iloc[0]} to {df.close_time.iloc[-1]}")
    results = {}
    for rr in [1.5, 2.0]:
        for vm in [1.0, 1.5, 2.0]:
            for am in [1.0, 1.2]:
                for lb in [10, 20]:
                    key = f"BO rr={rr} vol={vm} atr={am} lb={lb}"
                    results[key] = metrics(breakout(df, rr, vm, am, lb))
    for rr in [1.5, 2.0]:
        for vm in [1.5, 2.0, 2.5]:
            for ft in [0.3, 0.5, 0.8]:
                key = f"MC rr={rr} vol={vm} fibtol={ft}"
                results[key] = metrics(multiconfluence(df, rr, vm, ft))
    rows = [{"strategy": k, **v} for k, v in results.items()]
    out = Path("backtest-results")
    out.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out/"results.csv", index=False)
    (out/"results.json").write_text(json.dumps({
        "symbol": "BTCUSDT", "interval": "4h", "candles": len(df),
        "start": str(df.open_time.iloc[0]), "end": str(df.close_time.iloc[-1]),
        "fee_per_side": FEE, "slippage_included": False, "results": results
    }, indent=2), encoding="utf-8")
    print("\nTOP 10 BY NET SUM R (gross trading R minus approximate fees):")
    ranked = sorted(results.items(), key=lambda kv: kv[1]["sum_r"], reverse=True)
    for key, m in ranked[:10]:
        print(f'{key} | n={m["n"]} WR={m["winrate_pct"]:.1f}% avgR={m["avg_r"]} sumR={m["sum_r"]:.2f} PF={m["profit_factor"]} MaxDD={m["max_drawdown_r"]}R')
    print("\nTOP WITH >=30 TRADES:")
    eligible = [(k,v) for k,v in ranked if v["n"] >= 30]
    for key, m in eligible[:10]:
        print(f'{key} | n={m["n"]} WR={m["winrate_pct"]:.1f}% avgR={m["avg_r"]} sumR={m["sum_r"]:.2f} PF={m["profit_factor"]} MaxDD={m["max_drawdown_r"]}R')
    print("\nWARNING: same-period parameter search is exploratory; no slippage or out-of-sample validation.")
if __name__ == "__main__":
    main()
