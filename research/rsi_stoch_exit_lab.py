#!/usr/bin/env python3
"""Research-only audit of the bot's current 4H RSI/Stochastic USDT-M entry and exits.

Downloads public Binance USD-M futures OHLCV. No keys, private endpoints, or orders.
Entry edge is tested against side-matched random entries before any exit grid is run.
"""
from __future__ import annotations
import itertools, json, math, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]
START = "2021-01-01"
INTERVAL = "4h"
FEE = 0.0005
SLIPPAGE = 0.0002
SEED = 20261104
OUT = Path("research_results/rsi_stoch_exit_lab")
CACHE = Path("research_cache/usdm")
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def fetch(symbol: str) -> pd.DataFrame:
    path = CACHE / f"{symbol}_{INTERVAL}_{START}.csv"
    if path.exists():
        df = pd.read_csv(path, parse_dates=["timestamp"])
        df["timestamp"] = pd.to_datetime(df.timestamp, utc=True)
        return df
    start = int(pd.Timestamp(START, tz="UTC").timestamp() * 1000)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows, session = [], requests.Session()
    while start < now:
        response = session.get("https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": INTERVAL, "startTime": start, "endTime": now, "limit": 1500},
            timeout=30)
        response.raise_for_status()
        batch = response.json()
        if isinstance(batch, dict):
            raise RuntimeError(f"{symbol}: {batch}")
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 1
        if nxt <= start:
            raise RuntimeError(f"{symbol}: pagination did not advance")
        start = nxt
        if len(batch) < 1500:
            break
        time.sleep(0.12)
    if not rows:
        raise RuntimeError(f"{symbol}: no candles returned")
    cols = ["ts","open","high","low","close","volume","close_ts","quote_volume","trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(rows, columns=cols)
    df["timestamp"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["timestamp","open","high","low","close","volume"]].dropna().drop_duplicates("timestamp")
    # Exclude the still-forming candle.
    df = df[df.timestamp + pd.Timedelta(hours=4) <= pd.Timestamp.now(tz="UTC")]
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


def indicators(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame.copy()
    c, h, l, v = x.close, x.high, x.low, x.volume
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    prev = c.shift(1)
    tr = pd.concat([(h-l), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    lowest, highest = l.rolling(14, min_periods=14).min(), h.rolling(14, min_periods=14).max()
    x["stoch_k"] = 100 * (c-lowest) / (highest-lowest).replace(0, np.nan)
    x["stoch_d"] = x.stoch_k.rolling(3, min_periods=3).mean()
    x["vol_ma"] = v.rolling(20, min_periods=20).mean()
    x["signal"] = 0
    long = ((x.rsi.shift(1) < 30) & (x.rsi >= 30) &
            (x.stoch_k.shift(1) <= x.stoch_d.shift(1)) & (x.stoch_k > x.stoch_d) &
            (x.stoch_k < 38) & (v > 1.12*x.vol_ma) & (c > x.open))
    short = ((x.rsi.shift(1) > 70) & (x.rsi <= 70) &
             (x.stoch_k.shift(1) >= x.stoch_d.shift(1)) & (x.stoch_k < x.stoch_d) &
             (x.stoch_k > 62) & (v > 1.12*x.vol_ma) & (c < x.open))
    x.loc[long, "signal"] = 1
    x.loc[short, "signal"] = -1
    # Exact live stop construction: five-bar swing versus 1.32 ATR, wider stop wins.
    x["swing_low"] = l.rolling(5, min_periods=5).min()
    x["swing_high"] = h.rolling(5, min_periods=5).max()
    return x


def net_return(side: int, entry: float, exit_px: float) -> float:
    entry_fill = entry * (1 + SLIPPAGE if side == 1 else 1 - SLIPPAGE)
    exit_fill = exit_px * (1 - SLIPPAGE if side == 1 else 1 + SLIPPAGE)
    gross = side * (exit_fill-entry_fill)
    costs = FEE * (entry_fill+exit_fill)
    return gross-costs


def forward_samples(data: dict[str, pd.DataFrame], horizon: int, rng: np.random.Generator):
    actual, random_rows = [], []
    for symbol, x in data.items():
        n = len(x)
        valid = np.arange(220, n-horizon-1)
        sig_idx = np.flatnonzero(x.signal.to_numpy()) 
        sig_idx = sig_idx[(sig_idx >= 220) & (sig_idx < n-horizon-1)]
        # Avoid signals whose next-open entry would be outside the available data.
        for i in sig_idx:
            side = int(x.signal.iloc[i])
            entry = float(x.open.iloc[i+1])
            exit_px = float(x.close.iloc[i+horizon+1])
            risk = max(entry-float(x.swing_low.iloc[i]), 1.32*float(x.atr.iloc[i])) if side == 1 else max(float(x.swing_high.iloc[i])-entry, 1.32*float(x.atr.iloc[i]))
            if not np.isfinite(risk) or risk <= 0: continue
            actual.append({"symbol":symbol,"time":str(x.timestamp.iloc[i]),"side":side,"net_r":net_return(side,entry,exit_px)/risk})
        # Side-matched random timestamps from the same instrument, excluding actual signal bars.
        if len(sig_idx) and len(valid):
            for side in (-1, 1):
                count = int(np.sum(x.signal.iloc[sig_idx].to_numpy() == side))
                if count == 0: continue
                pool = np.setdiff1d(valid, sig_idx, assume_unique=False)
                if not len(pool): continue
                picks = rng.choice(pool, size=min(count,len(pool)), replace=False)
                for i in picks:
                    entry = float(x.open.iloc[i+1]); exit_px = float(x.close.iloc[i+horizon+1])
                    risk = max(1.32*float(x.atr.iloc[i]), entry-float(x.swing_low.iloc[i]) if side==1 else float(x.swing_high.iloc[i])-entry)
                    if not np.isfinite(risk) or risk <= 0: continue
                    random_rows.append({"symbol":symbol,"time":str(x.timestamp.iloc[i]),"side":side,"net_r":net_return(side,entry,exit_px)/risk})
    return actual, random_rows


def metrics(rows):
    if not rows: return {"trades":0,"expectancy_r":None,"profit_factor":None,"win_rate_pct":None,"avg_win_r":None,"avg_loss_r":None,"max_dd_r":None}
    rows = sorted(rows, key=lambda z: z.get("time", ""))
    r = np.asarray([z["net_r"] for z in rows], dtype=float)
    wins, losses = r[r>0], r[r<0]
    eq = np.cumsum(r); peak = np.maximum.accumulate(np.r_[0.0,eq])[1:]
    return {"trades":int(len(r)),"expectancy_r":float(r.mean()),
        "profit_factor":float(wins.sum()/abs(losses.sum())) if len(losses) and losses.sum()!=0 else None,
        "win_rate_pct":float((r>0).mean()*100),
        "avg_win_r":float(wins.mean()) if len(wins) else None,
        "avg_loss_r":float(losses.mean()) if len(losses) else None,
        "max_dd_r":float(np.max(peak-eq))}


def simulate(x, start, end, stop_atr, target_r, time_stop, partial, trail_atr):
    rows=[]; i=max(start,220); end=min(end,len(x))
    while i < end-1:
        side=int(x.signal.iloc[i])
        if side == 0 or i+1 >= end:
            i+=1; continue
        entry=float(x.open.iloc[i+1])
        atr=float(x.atr.iloc[i])
        if not np.isfinite(atr) or atr<=0: i+=1; continue
        swing=float(x.swing_low.iloc[i] if side==1 else x.swing_high.iloc[i])
        stop = min(swing,entry-stop_atr*atr) if side==1 else max(swing,entry+stop_atr*atr)
        risk=abs(entry-stop)
        if risk<=0 or risk/entry<0.0035 or risk/entry>0.038:
            i+=1; continue
        tp1=entry+side*1.5*risk
        final=entry+side*target_r*risk
        remaining=1.0; pnl=0.0; activated=False; stop_px=stop; reason="TIME"
        exit_i=min(end-1,i+time_stop)
        for j in range(i+1, min(end,i+time_stop+1)):
            hi,lo,cl=float(x.high.iloc[j]),float(x.low.iloc[j]),float(x.close.iloc[j])
            # Stop-first convention for any bar that can touch both stop and target.
            stop_hit = lo<=stop_px if side==1 else hi>=stop_px
            if stop_hit:
                pnl += remaining*net_return(side,entry,stop_px); remaining=0.0; reason="TRAIL" if activated else "SL"; exit_i=j; break
            if partial and remaining==1.0 and (hi>=tp1 if side==1 else lo<=tp1):
                pnl += 0.5*net_return(side,entry,tp1); remaining=0.5
            target_hit = hi>=final if side==1 else lo<=final
            if target_hit:
                pnl += remaining*net_return(side,entry,final); remaining=0.0; reason="TP"; exit_i=j; break
            # Trailing stop is calculated from the just-closed bar and is active next bar.
            favorable = (hi-entry)/risk if side==1 else (entry-lo)/risk
            if trail_atr is not None and favorable>=1.0:
                activated=True
                prev_atr=float(x.atr.iloc[j])
                candidate=cl-side*trail_atr*prev_atr
                stop_px=max(stop_px,candidate) if side==1 else min(stop_px,candidate)
        if remaining>0:
            px=float(x.close.iloc[exit_i])
            pnl += remaining*net_return(side,entry,px)
        rows.append({"symbol":"","time":str(x.timestamp.iloc[i]),"net_r":pnl/risk,"reason":reason})
        i=exit_i+1
    return rows


def monte_carlo(rows, seed=SEED, paths=5000):
    if not rows: return {}
    rng=np.random.default_rng(seed); r=np.asarray([z["net_r"] for z in rows],float); n=len(r)
    maxdds=[]
    for _ in range(paths):
        s=rng.permutation(r); eq=np.cumsum(s); peak=np.maximum.accumulate(np.r_[0.0,eq])[1:]
        maxdds.append(float(np.max(peak-eq)))
    return {"paths":paths,"max_dd_r_p50":float(np.percentile(maxdds,50)),
            "max_dd_r_p95":float(np.percentile(maxdds,95)),
            "max_dd_r_p99":float(np.percentile(maxdds,99)),
            "note":"Trade-order shuffle only; does not model parameter uncertainty or market regime changes."}


def main():
    rng=np.random.default_rng(SEED); data={}; failures={}
    for symbol in SYMBOLS:
        try:
            raw=fetch(symbol)
            if len(raw)<1000: raise RuntimeError(f"only {len(raw)} closed candles")
            data[symbol]=indicators(raw)
            print(f"[data] {symbol}: {len(raw)} bars {raw.timestamp.iloc[0]} .. {raw.timestamp.iloc[-1]}",flush=True)
        except Exception as exc:
            failures[symbol]=f"{type(exc).__name__}: {exc}"
            print(f"[data-error] {symbol}: {exc}",flush=True)
    if len(data)<3: raise RuntimeError(f"Need at least 3 instruments; got {list(data)}; errors={failures}")
    edge={}
    for horizon in (1,3,6,12):
        a,b=forward_samples(data,horizon,rng)
        ma,mb=metrics(a),metrics(b)
        edge[str(horizon)]={"strategy":ma,"side_matched_random":mb,
            "expectancy_lift_r":(ma["expectancy_r"]-mb["expectancy_r"]) if ma["expectancy_r"] is not None and mb["expectancy_r"] is not None else None}
    counts=[v["strategy"]["trades"] for v in edge.values()]
    primary=edge["6"]
    # Gate: >=100 six-bar signals, positive expectancy, and > random-entry expectancy.
    edge_pass=bool(primary["strategy"]["trades"]>=100 and primary["strategy"]["expectancy_r"] is not None
        and primary["strategy"]["expectancy_r"]>0 and primary["expectancy_lift_r"] is not None
        and primary["expectancy_lift_r"]>0)
    result={"generated_utc":datetime.now(timezone.utc).isoformat(),"market":"Binance USD-M perpetual public klines",
        "symbols_requested":SYMBOLS,"symbols_downloaded":list(data),"download_failures":failures,
        "timeframe":INTERVAL,"start":START,"fees_per_side":FEE,"slippage_per_side":SLIPPAGE,
        "entry_definition":"Exact current bot RSI14 recovery + Stoch14/3 cross + 1.12x volume + candle direction; next 4H open; long and short.",
        "entry_edge_gate":{"passed":edge_pass,"rule":">=100 six-bar observations, positive net expectancy, and positive expectancy lift vs side-matched random entries."},
        "forward_returns_vs_random":edge,"exit_grid":None,"walk_forward":None,"monte_carlo":None}
    if not edge_pass:
        result["decision"]="ENTRY_EDGE_NOT_ESTABLISHED: do not optimize exits; fix/test entry first."
        save_json(OUT/"research_report.json",result)
        print(json.dumps(result,indent=2,default=str)); return
    configs=list(itertools.product([1.0,1.5,2.0,2.5,3.0],[1.5,2.0,3.0,4.0,5.0],[6,9,12],[False,True],[None,2.0]))
    results=[]
    for ci,(sm,rr,ts,partial,trail) in enumerate(configs,1):
        trades=[]
        for sym,x in data.items():
            rows=simulate(x,220,len(x),sm,rr,ts,partial,trail)
            for row in rows: row["symbol"]=sym
            trades.extend(rows)
        results.append({"stop_atr":sm,"target_r":rr,"time_stop_bars":ts,"partial_50pct_at_1_5r":partial,
            "trail_atr_after_1r":trail,**metrics(trades),"_trades":trades})
        if ci%50==0: print(f"[grid] {ci}/{len(configs)}",flush=True)
    eligible=[r for r in results if r["trades"]>=100]
    eligible.sort(key=lambda r:(r["expectancy_r"],r["profit_factor"] or 0),reverse=True)
    # Four chronological test windows; choose a configuration on the preceding history only.
    wf=[]
    for fold, test_start in enumerate((0.40, 0.55, 0.70, 0.85), 1):
        test_end=min(1.0,test_start+0.15)
        fold_rows=[]
        for sym,x in data.items():
            a=int(len(x)*test_start); b=int(len(x)*test_end)
            # Selection uses only candles strictly before the test window.
            train_end=max(221,a)
            candidates=[]
            for sm,rr,ts,partial,trail in configs:
                tr=[]
                for ss,xx in data.items():
                    tr.extend(simulate(xx,220,min(train_end,len(xx)),sm,rr,ts,partial,trail))
                candidates.append((metrics(tr)["expectancy_r"] if metrics(tr)["expectancy_r"] is not None else -999,
                    sm,rr,ts,partial,trail))
            _,sm,rr,ts,partial,trail=max(candidates,key=lambda z:z[0])
            fold_rows.extend(simulate(x,a,b,sm,rr,ts,partial,trail))
        wf.append({"fold":fold,"test_fraction":[test_start,test_end],**metrics(fold_rows)})
    best=eligible[0] if eligible else None
    result["exit_grid"]={"combinations_tested":len(configs),"eligible_100plus_trades":len(eligible),
        "top_20":[{k:v for k,v in r.items() if k!="_trades"} for r in eligible[:20]],
        "multiple_testing_note":"Grid is exploratory; report all tested configurations. Do not promote the top rank without independent validation."}
    result["walk_forward"]={"windows":wf,"positive_windows":sum(1 for w in wf if (w["expectancy_r"] or 0)>0)}
    if best:
        result["best_exit_config"]={k:v for k,v in best.items() if k!="_trades"}
        result["monte_carlo"]=monte_carlo(best["_trades"])
        global FEE, SLIPPAGE
        old_fee, old_slippage = FEE, SLIPPAGE
        FEE, SLIPPAGE = 2*old_fee, 2*old_slippage
        doubled=[]
        for sym,x in data.items():
            doubled.extend(simulate(x,220,len(x),best["stop_atr"],best["target_r"],best["time_stop_bars"],best["partial_50pct_at_1_5r"],best["trail_atr_after_1r"]))
        FEE, SLIPPAGE = old_fee, old_slippage
        result["best_config_at_2x_costs"]=metrics(doubled)
        vals=np.asarray([t["net_r"] for t in best["_trades"]],dtype=float)
        if len(vals)>1 and vals.std(ddof=1)>0:
            z=float(vals.mean()/(vals.std(ddof=1)/math.sqrt(len(vals))))
            raw_p=math.erfc(abs(z)/math.sqrt(2.0))
            result["best_config_multiple_testing"]={"raw_normal_approx_p":raw_p,
                "bonferroni_adjusted_p":min(1.0,raw_p*len(configs)),
                "tests_count":len(configs),
                "note":"Normal approximation is diagnostic only; serial dependence and strategy selection bias remain. Walk-forward and untouched forward data are still required."}
    result["decision"]="RESEARCH_ONLY; promote nothing automatically. Require positive OOS expectancy, PF>1.2, >=3/4 positive WF windows, and stability under 2x costs."
    save_json(OUT/"research_report.json",result)
    pd.DataFrame([{k:v for k,v in r.items() if k!="_trades"} for r in results]).to_csv(OUT/"exit_grid.csv",index=False)
    print(json.dumps(result,indent=2,default=str))


if __name__=="__main__":
    main()
