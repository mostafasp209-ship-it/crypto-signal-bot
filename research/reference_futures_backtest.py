#!/usr/bin/env python3
"""Historical Binance USD-M 4h backtest for the live RSI/Stochastic RR1.5 setup.
Research only. Uses closed candles, next-open entries, adverse fills, funding cashflows,
stop-first intrabar collision handling, chronological train/validation/holdout splits.
"""
from __future__ import annotations
import itertools, json, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
           "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT"]
START = "2021-01-01"
INTERVAL = "4h"
FEE = 0.0005
SLIPPAGE = 0.0002
OUT = Path("research_results/reference_futures")
CACHE = Path("research_cache/usdm")
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)
BASE = "https://fapi.binance.com"


def _get(session, path, params):
    last = None
    for attempt in range(5):
        try:
            r = session.get(BASE + path, params=params, timeout=30)
            if r.status_code in (418, 429):
                time.sleep(min(2 ** attempt, 20))
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "code" in data:
                raise RuntimeError(str(data))
            return data
        except Exception as exc:
            last = exc
            time.sleep(min(1.5 ** attempt, 10))
    raise RuntimeError(f"{path} request failed: {last}")


def fetch_paginated(symbol, path, start_ms, end_ms, limit=1000):
    session = requests.Session()
    rows, cursor = [], start_ms
    while cursor < end_ms:
        data = _get(session, path, {"symbol": symbol, "startTime": cursor,
                                    "endTime": end_ms, "limit": limit})
        if not data:
            break
        rows.extend(data)
        last_time = data[-1]["fundingTime"] if path.endswith("/fundingRate") else data[-1][0]
        nxt = int(last_time) + 1
        if nxt <= cursor:
            raise RuntimeError(f"{symbol}: pagination stalled for {path}")
        cursor = nxt
        if len(data) < limit:
            break
        time.sleep(0.08)
    return rows


def fetch_market(symbol):
    cache = CACHE / f"{symbol}_{INTERVAL}_{START}.csv"
    fcache = CACHE / f"{symbol}_funding_{START}.csv"
    if cache.exists() and fcache.exists():
        candles = pd.read_csv(cache, parse_dates=["timestamp"])
        funding = pd.read_csv(fcache, parse_dates=["funding_time"])
        return candles, funding
    start_ms = int(pd.Timestamp(START, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    raw = fetch_paginated(symbol, "/fapi/v1/klines", start_ms, end_ms)
    if not raw:
        raise RuntimeError(f"{symbol}: no futures candles returned")
    cols = ["open_time","open","high","low","close","volume","close_time",
            "quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"]
    df = pd.DataFrame(raw, columns=cols)
    df["timestamp"] = pd.to_datetime(df.open_time, unit="ms", utc=True)
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["timestamp","open","high","low","close","volume"]].drop_duplicates("timestamp")
    now = pd.Timestamp.now(tz="UTC")
    df = df[df.timestamp + pd.Timedelta(hours=4) <= now].sort_values("timestamp").reset_index(drop=True)
    funding_raw = fetch_paginated(symbol, "/fapi/v1/fundingRate", start_ms, end_ms, 1000)
    if funding_raw:
        fr = pd.DataFrame(funding_raw)
        fr["funding_time"] = pd.to_datetime(pd.to_numeric(fr.fundingTime), unit="ms", utc=True)
        fr["funding_rate"] = pd.to_numeric(fr.fundingRate, errors="coerce")
        fr = fr[["funding_time","funding_rate"]].drop_duplicates("funding_time").sort_values("funding_time")
    else:
        fr = pd.DataFrame(columns=["funding_time","funding_rate"])
    df.to_csv(cache, index=False)
    fr.to_csv(fcache, index=False)
    return df, fr


def indicators(df):
    x = df.copy()
    c, h, l, v = x.close, x.high, x.low, x.volume
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x["rsi"] = 100 - 100/(1 + gain/loss.replace(0, np.nan))
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    x["k"] = 100*(c-lo14)/(hi14-lo14+1e-12)
    x["d"] = x.k.rolling(3).mean()
    x["vol_ma"] = v.rolling(20, min_periods=20).mean()
    x["ema200"] = c.ewm(span=200, adjust=False, min_periods=200).mean()
    return x


def make_signals(x, cfg):
    rp, rn = x.rsi.shift(1), x.rsi
    kp, dp, kn, dn = x.k.shift(1), x.d.shift(1), x.k, x.d
    volume_ok = x.volume > cfg["vol_mult"] * x.vol_ma
    long = ((rp < cfg["rsi_long"]) & (rn >= cfg["rsi_long"]) &
            (kp <= dp) & (kn > dn) & (kn < cfg["stoch_long"]) &
            volume_ok & (x.close > x.open))
    short = ((rp > cfg["rsi_short"]) & (rn <= cfg["rsi_short"]) &
             (kp >= dp) & (kn < dn) & (kn > cfg["stoch_short"]) &
             volume_ok & (x.close < x.open))
    if cfg["trend_filter"]:
        long &= x.close > x.ema200
        short &= x.close < x.ema200
    return long.fillna(False).to_numpy(), short.fillna(False).to_numpy()


def funding_cashflow(funding, entry_time, exit_time, side, quantity):
    if funding.empty:
        return 0.0, 0
    rows = funding[(funding.funding_time > entry_time) & (funding.funding_time <= exit_time)]
    if rows.empty:
        return 0.0, 0
    # Binance funding is charged on position notional at the funding timestamp.
    mark = rows["mark_price"].to_numpy(float) if "mark_price" in rows else np.ones(len(rows))
    sign = 1.0 if side == "LONG" else -1.0
    cash = -sign * quantity * mark * rows.funding_rate.to_numpy(float)
    return float(cash.sum()), int(len(rows))


def simulate(x, funding, cfg, start_i, end_i, symbol):
    op, hi, lo, cl, atr = (x[k].to_numpy(float) for k in ("open","high","low","close","atr"))
    ts = pd.to_datetime(x.timestamp, utc=True).reset_index(drop=True)
    long_sig, short_sig = make_signals(x, cfg)
    trades, i = [], max(start_i, 220)
    # Funding notional is marked using the candle open at the funding timestamp (or
    # the most recent prior open if an exchange timestamp is unexpectedly missing).
    if not funding.empty:
        marks = pd.merge_asof(
            funding.sort_values("funding_time"),
            x[["timestamp", "open"]].sort_values("timestamp"),
            left_on="funding_time", right_on="timestamp", direction="backward")
        funding = funding.copy()
        funding["mark_price"] = marks["open"].to_numpy(float)
    while i < end_i - 1:
        side = "LONG" if long_sig[i] else ("SHORT" if short_sig[i] else None)
        if side is None or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        ei = i + 1
        raw_entry = op[ei]
        entry = raw_entry*(1+SLIPPAGE) if side == "LONG" else raw_entry*(1-SLIPPAGE)
        swing = np.min(lo[max(0,i-4):i+1]) if side == "LONG" else np.max(hi[max(0,i-4):i+1])
        atr_stop = entry - cfg["atr_mult"]*atr[i] if side == "LONG" else entry + cfg["atr_mult"]*atr[i]
        stop = min(swing, atr_stop) if side == "LONG" else max(swing, atr_stop)
        risk = abs(entry-stop)
        risk_pct = risk/entry
        if not (0.0035 < risk_pct < 0.038):
            i += 1
            continue
        target = entry + 1.5*risk if side == "LONG" else entry - 1.5*risk
        # Live signals expire 48h after creation. Approximate the expiry-time
        # ticker with the next 4h candle open; do not inspect that candle's later high/low.
        expiry_i = ei + 12
        if expiry_i < end_i:
            exit_i, raw_exit, reason = expiry_i, op[expiry_i], "EXPIRY_48H"
            scan_end = expiry_i
        else:
            exit_i, raw_exit, reason = end_i-1, cl[end_i-1], "TIME_END"
            scan_end = end_i
        for j in range(ei, scan_end):
            # Conservative gap handling: stop gaps fill at open, not the stale stop.
            if side == "LONG":
                if op[j] <= stop:
                    exit_i, raw_exit, reason = j, op[j], "SL_GAP"; break
                if op[j] >= target:
                    exit_i, raw_exit, reason = j, target, "TP"; break
                if lo[j] <= stop:
                    exit_i, raw_exit, reason = j, stop, "SL"; break
                if hi[j] >= target:
                    exit_i, raw_exit, reason = j, target, "TP"; break
            else:
                if op[j] >= stop:
                    exit_i, raw_exit, reason = j, op[j], "SL_GAP"; break
                if op[j] <= target:
                    exit_i, raw_exit, reason = j, target, "TP"; break
                if hi[j] >= stop:
                    exit_i, raw_exit, reason = j, stop, "SL"; break
                if lo[j] <= target:
                    exit_i, raw_exit, reason = j, target, "TP"; break
        exit_fill = raw_exit*(1-SLIPPAGE) if side == "LONG" else raw_exit*(1+SLIPPAGE)
        qty = 1.0/entry  # Normalize initial notional to one unit of quote currency.
        direction = 1.0 if side == "LONG" else -1.0
        gross = direction*(exit_fill-entry)*qty
        fees = FEE*(entry+exit_fill)*qty
        funding_pnl, funding_events = funding_cashflow(funding, ts.iloc[ei], ts.iloc[exit_i], side, qty)
        net = gross-fees+funding_pnl
        # R denominator includes price risk plus estimated round-trip fees; funding is realized cashflow.
        initial_risk_quote = risk*qty + FEE*(entry+stop)*qty + SLIPPAGE*stop*qty
        net_r = net/initial_risk_quote
        trades.append({"symbol":symbol,"side":side,"signal_time":str(ts.iloc[i]),
            "entry_time":str(ts.iloc[ei]),"exit_time":str(ts.iloc[exit_i]),
            "entry":entry,"stop":stop,"target":target,"exit":exit_fill,"reason":reason,
            "risk_pct":risk_pct*100,"gross_quote_per_1_quote":gross,
            "fees_quote_per_1_quote":fees,"funding_quote_per_1_quote":funding_pnl,
            "funding_events":funding_events,"net_quote_per_1_quote":net,"net_r":net_r,
            "net_return_pct_on_entry":net*100})
        i = exit_i + 1
    return trades


def metrics(trades):
    if not trades:
        return {"trades":0,"win_rate_pct":None,"expectancy_R":None,"profit_factor":None,
                "net_R":0.0,"max_drawdown_R":None,"funding_quote":0.0}
    t = sorted(trades, key=lambda z:z["exit_time"])
    # Aggregate simultaneous exits before drawdown calculation to avoid arbitrary
    # ordering of symbols that close on the same 4h timestamp.
    grouped = {}
    for trade in t:
        grouped[trade["exit_time"]] = grouped.get(trade["exit_time"], 0.0) + float(trade["net_r"])
    r = np.array([grouped[k] for k in sorted(grouped)], float)
    trade_r = np.array([z["net_r"] for z in t], float)
    wins, losses = trade_r[trade_r>0], trade_r[trade_r<0]
    eq = np.cumsum(r)
    dd = np.maximum.accumulate(np.r_[0.0,eq]) - eq
    return {"trades":len(t),"win_rate_pct":round(float((trade_r>0).mean()*100),2),
        "expectancy_R":round(float(trade_r.mean()),4),
        "profit_factor":round(float(wins.sum()/abs(losses.sum())),4) if len(losses) and losses.sum()!=0 else None,
        "net_R":round(float(r.sum()),3),"max_drawdown_R":round(float(dd.max()),3),
        "funding_quote":round(float(sum(z["funding_quote_per_1_quote"] for z in t)),6),
        "fees_quote":round(float(sum(z["fees_quote_per_1_quote"] for z in t)),6)}


def configs():
    # Baseline plus controlled one-factor/two-factor variations; RR fixed at 1.5.
    base = {"rsi_long":30,"rsi_short":70,"stoch_long":38,"stoch_short":62,
            "vol_mult":1.12,"atr_mult":1.32,"trend_filter":False}
    out = [("baseline",base)]
    axes = [
        ("rsi_long",[25,35]), ("stoch_long",[30,45]), ("vol_mult",[1.0,1.25,1.5]),
        ("atr_mult",[1.1,1.5]), ("trend_filter",[True])
    ]
    for key, vals in axes:
        for val in vals:
            c = dict(base); c[key]=val
            if key=="rsi_long": c["rsi_short"]=100-val
            if key=="stoch_long": c["stoch_short"]=100-val
            out.append((f"{key}={val}",c))
    # A small interaction screen for volume + trend regime.
    for vol, trend in itertools.product([1.0,1.25,1.5],[True]):
        c=dict(base,vol_mult=vol,trend_filter=trend)
        out.append((f"volume={vol}+ema200_filter",c))
    return out


def main():
    datasets, errors = {}, {}
    for symbol in SYMBOLS:
        try:
            candles, funding = fetch_market(symbol)
            if len(candles) < 800:
                raise RuntimeError(f"Only {len(candles)} closed 4h candles")
            datasets[symbol]=(indicators(candles),funding)
            print(f"DATA {symbol}: {len(candles)} candles; funding events={len(funding)}; "
                  f"{candles.timestamp.iloc[0]} .. {candles.timestamp.iloc[-1]}",flush=True)
        except Exception as exc:
            errors[symbol]=str(exc); print(f"DATA_ERROR {symbol}: {exc}",flush=True)
    if len(datasets)<3:
        raise RuntimeError(f"Need at least 3 markets for research; downloaded {len(datasets)}: {errors}")
    all_rows=[]
    for name,cfg in configs():
        pooled={"train":[],"validation":[],"holdout":[]}
        by_symbol={}
        for symbol,(x,fr) in datasets.items():
            n=len(x); a,b=int(n*.6),int(n*.8)
            local={}
            for split,(s,e) in {"train":(220,a),"validation":(a,b),"holdout":(b,n)}.items():
                local[split]=simulate(x,fr,cfg,s,e,symbol)
                pooled[split].extend(local[split])
            by_symbol[symbol]={k:metrics(v) for k,v in local.items()}
        all_rows.append({"name":name,"config":cfg,"pooled":{k:metrics(v) for k,v in pooled.items()},
                         "by_symbol":by_symbol})
        print(f"CONFIG {name}: validation={pooled and metrics(pooled['validation'])}",flush=True)
    # Rank on validation only; untouched holdout is reported but never used to select.
    ranked=sorted(all_rows,key=lambda z:(z["pooled"]["validation"]["expectancy_R"] if z["pooled"]["validation"]["expectancy_R"] is not None else -999),reverse=True)
    trades=[]
    best_name=ranked[0]["name"]
    best_cfg=ranked[0]["config"]
    for symbol,(x,fr) in datasets.items():
        n=len(x); trades.extend(simulate(x,fr,best_cfg,int(n*.8),n,symbol))
    pd.DataFrame(trades).to_csv(OUT/"best_candidate_holdout_trades.csv",index=False)
    report={"generated_utc":datetime.now(timezone.utc).isoformat(),"market":"Binance USDⓈ-M perpetual futures",
        "symbols_requested":SYMBOLS,"symbols_downloaded":list(datasets),"download_errors":errors,
        "interval":INTERVAL,"start":START,"fees_per_side":FEE,"slippage_per_side":SLIPPAGE,
        "funding":"Historical Binance fundingRate events accrued at each timestamp during open trades; positive rate means longs pay shorts.",
        "entry":"Signal on closed 4h candle; entry next candle open with adverse slippage.",
        "stops":"5-bar swing vs 1.32*ATR baseline; stop gaps fill at next open; if SL and TP both touch in one candle, SL first.",
        "splits":"Chronological 60% train / 20% validation / 20% untouched holdout, per symbol.",
        "selection":"Configs ranked only by pooled validation expectancy. Holdout must not be used for tuning.",
        "survivorship_bias":"Universe is a fixed list of currently liquid symbols, not a delisted-contract universe; results are subject to survivorship and listing-history bias.",
        "position_model":"One open trade per symbol; equal notional normalization; no portfolio leverage or cross-symbol capital constraint.",
        "ranked_results":ranked,"best_validation_config":best_name,
        "best_validation_holdout_trades":metrics(trades),
        "warning":"This is a research backtest, not a fill-perfect exchange simulation. OHLC cannot reveal intrabar path; stop-first is conservative. Do not deploy from one holdout result."}
    (OUT/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False))
    pd.DataFrame([{"name":r["name"],**r["config"],
        **{f"{s}_{k}":v for s in ("train","validation","holdout") for k,v in r["pooled"][s].items()}}
        for r in ranked]).to_csv(OUT/"summary.csv",index=False)
    print(json.dumps({"symbols":list(datasets),"errors":errors,"best_validation_config":best_name,
        "best_validation":ranked[0]["pooled"]["validation"],
        "best_holdout":ranked[0]["pooled"]["holdout"]},indent=2),flush=True)


if __name__=="__main__":
    main()
