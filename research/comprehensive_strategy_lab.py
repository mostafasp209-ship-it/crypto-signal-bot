#!/usr/bin/env python3
"""Checkpointed second-stage strategy-family comparison. Research only; never places orders."""
from __future__ import annotations
import itertools, json, os, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT"]
START = "2021-01-01"
INTERVAL = "4h"
FEE = 0.001
SLIPPAGE = 0.0002
OUT = Path("research_results/comprehensive")
CACHE = Path("research_cache")
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)


def atomic_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str, allow_nan=False))
    tmp.replace(path)


def fetch(symbol: str) -> pd.DataFrame:
    cache = CACHE / f"{symbol}_{INTERVAL}_{START}.csv"
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["timestamp"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    start_ms = int(pd.Timestamp(START, tz="UTC").timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows, session = [], requests.Session()
    while start_ms < end_ms:
        payload = None
        last_error = None
        for base in ("https://data-api.binance.vision", "https://api.binance.com"):
            try:
                res = session.get(base + "/api/v3/klines", params={
                    "symbol": symbol, "interval": INTERVAL, "startTime": start_ms,
                    "endTime": end_ms, "limit": 1000}, timeout=25)
                res.raise_for_status()
                payload = res.json()
                if isinstance(payload, dict):
                    raise RuntimeError(str(payload))
                break
            except Exception as exc:
                last_error = exc
        if payload is None:
            raise RuntimeError(f"{symbol}: {last_error}")
        if not payload:
            break
        rows.extend(payload)
        nxt = int(payload[-1][0]) + 1
        if nxt <= start_ms:
            break
        start_ms = nxt
        if len(payload) < 1000:
            break
        time.sleep(0.08)
    if not rows:
        raise RuntimeError(f"No candles for {symbol}")
    cols = ["ts","open","high","low","close","volume","ct","qv","trades","tb","tq","ignore"]
    df = pd.DataFrame(rows, columns=cols)
    df["timestamp"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    for c in ("open","high","low","close","volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["timestamp","open","high","low","close","volume"]].drop_duplicates("timestamp")
    df = df[df.timestamp + pd.Timedelta(hours=4) <= pd.Timestamp.now(tz="UTC")]
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


def add_indicators(df):
    x = df.copy()
    c, h, l, v = x.close, x.high, x.low, x.volume
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    for n in (10,20,50,100,150,200):
        x[f"ema{n}"] = c.ewm(span=n, adjust=False, min_periods=n).mean()
    x["vol_ratio"] = v / v.rolling(20, min_periods=20).mean().shift(1)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    e12 = c.ewm(span=12, adjust=False, min_periods=12).mean()
    e26 = c.ewm(span=26, adjust=False, min_periods=26).mean()
    x["macd"] = e12 - e26
    x["macd_sig"] = x.macd.ewm(span=9, adjust=False, min_periods=9).mean()
    x["bb_mid"] = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std()
    x["bb_up"], x["bb_low"] = x.bb_mid + 2*sd, x.bb_mid - 2*sd
    x["donchian20"] = h.shift(1).rolling(20, min_periods=20).max()
    x["donchian55"] = h.shift(1).rolling(55, min_periods=55).max()
    x["prior_low20"] = l.shift(1).rolling(20, min_periods=20).min()
    x["cloud_a"] = (x.ema50 + x.ema100) / 2
    return x


def candidates(x):
    # All signals use information available at the signal candle close.
    c, o, h, l = x.close, x.open, x.high, x.low
    uptrend = (c > x.ema200) & (x.ema50 > x.ema100)
    out = {}
    for n in (20, 55):
        level = x[f"donchian{n}"] if f"donchian{n}" in x else h.shift(1).rolling(n, min_periods=n).max()
        out[f"breakout_{n}"] = (c > level) & (x.vol_ratio > 1.2) & (c > x.ema200)
    out["ema20_pullback"] = uptrend & (l <= x.ema20) & (c > x.ema20) & (c > o)
    out["ema_cross_20_50"] = (x.ema20 > x.ema50) & (x.ema20.shift(1) <= x.ema50.shift(1)) & (c > x.ema200)
    out["macd_cross_trend"] = (x.macd > x.macd_sig) & (x.macd.shift(1) <= x.macd_sig.shift(1)) & (c > x.ema200)
    out["bb_breakout"] = (c > x.bb_up) & (c > x.ema200) & (x.vol_ratio > 1.0)
    out["rsi_reversion_trend"] = (x.rsi.shift(1) < 30) & (x.rsi >= 30) & (c > x.ema200)
    out["bb_reversion_trend"] = (c.shift(1) < x.bb_low.shift(1)) & (c > x.bb_low) & (x.rsi < 50) & (c > x.ema200)
    out["failed_breakdown_reclaim"] = (l < x.prior_low20) & (c > x.prior_low20) & (c > o) & (c > x.ema100)
    out["ichimoku_proxy_trend_break"] = (c > x.cloud_a) & (x.ema50 > x.ema100) & (c.shift(1) <= x.cloud_a.shift(1))
    return {k: v.fillna(False).to_numpy(dtype=bool) for k,v in out.items()}


def backtest(x, sig, start, end, atr_mult, rr):
    # One position at a time; next-open entry; conservative stop-first OHLC fills.
    rows, i, n = [], max(start, 220), min(end, len(x))
    op, hi, lo, cl, atr = (x[k].to_numpy(float) for k in ("open","high","low","close","atr"))
    while i < n-1:
        if not sig[i] or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1; continue
        ei = i+1
        entry = op[ei] * (1+SLIPPAGE)
        risk = atr[i]*atr_mult
        if risk <= 0 or entry <= risk:
            i += 1; continue
        stop, target = entry-risk, entry+risk*rr
        exit_i, px, reason = n-1, cl[n-1], "end"
        j = ei
        while j < n:
            if lo[j] <= stop:
                exit_i, px, reason = j, stop, "sl"; break
            if hi[j] >= target:
                exit_i, px, reason = j, target, "tp"; break
            j += 1
        fill = px*(1-SLIPPAGE)
        net = (fill-entry) - FEE*(entry+fill)
        rows.append({"signal_i":int(i),"entry_i":int(ei),"exit_i":int(exit_i),
                     "net_r":float(net/risk),"net_pct":float(net/entry*100),"reason":reason})
        i = exit_i+1
    return rows


def metrics(trades):
    if not trades:
        return {"trades":0,"win_rate":0.0,"expectancy_r":0.0,"profit_factor":0.0,"net_return_pct":0.0,"max_drawdown_pct":0.0}
    r = np.array([t["net_r"] for t in trades], float)
    pct = np.array([t["net_pct"] for t in trades], float)
    wins, losses = r[r>0], r[r<0]
    eq = np.cumprod(1 + pct/100)
    peak = np.maximum.accumulate(np.r_[1.0,eq])[1:]
    dd = (eq/peak-1)*100
    return {"trades":int(len(r)),"win_rate":float((r>0).mean()),
            "expectancy_r":float(r.mean()),
            "profit_factor":float(wins.sum()/abs(losses.sum())) if len(losses) and abs(losses.sum()) else 0.0,
            "net_return_pct":float((eq[-1]-1)*100),
            "max_drawdown_pct":float(dd.min())}


def main():
    all_results, failures = [], []
    symbols_data = {}
    for sym in SYMBOLS:
        try:
            frame = add_indicators(fetch(sym))
            if len(frame) < 1000:
                raise RuntimeError(f"Only {len(frame)} bars")
            symbols_data[sym] = frame
            print(f"[data] {sym}: {len(frame)} closed candles", flush=True)
        except Exception as e:
            failures.append({"symbol":sym,"error":str(e)})
            print(f"[data-error] {sym}: {e}", flush=True)
    if not symbols_data:
        raise RuntimeError("No symbol data downloaded")
    total = sum(len(x) for x in symbols_data.values())
    # Chronological 60/20/20 train/validation/test; the test is reported, not used to tune.
    configs = list(itertools.product(
        ["breakout_20","breakout_55","ema20_pullback","ema_cross_20_50","macd_cross_trend",
         "bb_breakout","rsi_reversion_trend","bb_reversion_trend","failed_breakdown_reclaim",
         "ichimoku_proxy_trend_break"],
        [1.5,2.0], [1.5,2.0,3.0]))
    done = 0
    for ci,(family,atr_mult,rr) in enumerate(configs):
        by_symbol = {}
        for sym, frame in symbols_data.items():
            sigs = candidates(frame)[family]
            n = len(frame); a, b = int(n*.6), int(n*.8)
            train = backtest(frame,sigs,220,a,atr_mult,rr)
            valid = backtest(frame,sigs,a,b,atr_mult,rr)
            test = backtest(frame,sigs,b,n,atr_mult,rr)
            by_symbol[sym] = {"train":metrics(train),"validation":metrics(valid),"test":metrics(test)}
        # Ranking score uses validation only; test metrics are kept untouched for reporting.
        val = [v["validation"] for v in by_symbol.values() if v["validation"]["trades"] >= 5]
        score = float(np.mean([m["expectancy_r"] for m in val])) if val else -999.0
        all_results.append({"family":family,"atr_mult":atr_mult,"rr":rr,"validation_score":score,"by_symbol":by_symbol})
        done += 1
        if done % 10 == 0 or done == len(configs):
            atomic_json(OUT/"checkpoint.json",{"completed_configs":done,"total_configs":len(configs),
                "updated_utc":datetime.now(timezone.utc).isoformat(),"failures":failures,"results":all_results})
            print(f"[checkpoint] {done}/{len(configs)} configs; symbols={len(symbols_data)}",flush=True)
    all_results.sort(key=lambda r:r["validation_score"],reverse=True)
    atomic_json(OUT/"comprehensive_results.json",{"generated_utc":datetime.now(timezone.utc).isoformat(),
        "interval":INTERVAL,"start":START,"symbols":list(symbols_data),"fees_per_side":FEE,
        "slippage_per_side":SLIPPAGE,"split":"60/20/20 chronological","ranking_uses":"validation only",
        "test_warning":"Test results are out-of-sample estimates; do not tune against them.",
        "failures":failures,"ranked_results":all_results})
    flat=[]
    for rank,item in enumerate(all_results,1):
        for sym, parts in item["by_symbol"].items():
            flat.append({"rank_validation":rank,"family":item["family"],"atr_mult":item["atr_mult"],"rr":item["rr"],
                "validation_score":item["validation_score"],"symbol":sym,
                **{f"{split}_{k}":v for split in ("train","validation","test") for k,v in parts[split].items()}})
    pd.DataFrame(flat).to_csv(OUT/"comprehensive_results.csv",index=False)
    print(f"[done] {len(all_results)} configurations; report in {OUT}",flush=True)


if __name__ == "__main__":
    main()
