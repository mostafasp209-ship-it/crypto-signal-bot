#!/usr/bin/env python3
"""Research-only raw-pattern discovery and walk-forward test. Never places orders."""
from __future__ import annotations
import json, math, os, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,LINKUSDT").split(",")
START = int(pd.Timestamp(os.getenv("START_DATE", "2021-01-01"), tz="UTC").timestamp() * 1000)
INTERVAL = "4h"
INITIAL_EQUITY = 100.0
RISK_FRACTION = 0.02
LEVERAGE_CAP = 10.0
FEE_SIDE = 0.0004
SLIP_SIDE = 0.0002
HORIZON = 30
RR = 3.0
CACHE = Path("research_cache/raw_patterns")
OUT = Path("research_results/raw_patterns")
CACHE.mkdir(parents=True, exist_ok=True); OUT.mkdir(parents=True, exist_ok=True)

def fetch_klines(symbol):
    path = CACHE / f"{symbol}_{INTERVAL}.csv"
    if path.exists():
        df = pd.read_csv(path)
        if len(df) > 1000: return df
    rows, start = [], START
    while True:
        params = urlencode({"symbol": symbol, "interval": INTERVAL, "startTime": start, "limit": 1500})
        req = Request("https://fapi.binance.com/fapi/v1/klines?" + params, headers={"User-Agent":"research-bot/1.0"})
        with urlopen(req, timeout=30) as resp: batch = json.loads(resp.read())
        if not batch: break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 1
        if nxt <= start or len(batch) < 1500: break
        start = nxt
        time.sleep(0.08)
    cols = ["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty: raise RuntimeError(f"No data for {symbol}")
    for c in ["open","high","low","close","volume"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    # Exclude the still-forming final candle.
    if len(df) and df.iloc[-1]["open_time"] + pd.Timedelta(hours=4) > pd.Timestamp.now(tz="UTC"):
        df = df.iloc[:-1]
    df.to_csv(path, index=False)
    return df

def make_features(d):
    x = d.copy()
    o,h,l,c,v = (x[k] for k in ["open","high","low","close","volume"])
    prev = c.shift(1)
    tr = pd.concat([(h-l), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    rng = (h-l).replace(0, np.nan)
    x["ret1"] = c.pct_change()
    x["body"] = (c-o)/o
    x["range_pct"] = (h-l)/c
    x["body_to_range"] = (c-o)/rng
    x["upper_wick"] = (h-np.maximum(o,c))/rng
    x["lower_wick"] = (np.minimum(o,c)-l)/rng
    x["close_location"] = (c-l)/rng
    x["atr_pct"] = atr/c
    x["range_atr"] = rng/atr
    x["vol_ratio"] = v/(v.rolling(20).mean())
    x["quote_vol_proxy"] = (v*c)/(v*c).rolling(20).mean()
    for n in [3,6,12,24,48,96]:
        x[f"mom_{n}"] = c.pct_change(n)
        x[f"range_{n}"] = (h.rolling(n).max()-l.rolling(n).min())/c
        x[f"volatility_{n}"] = x["ret1"].rolling(n).std()
    for n in [10,20,50,100,200]:
        ema = c.ewm(span=n, adjust=False).mean()
        x[f"ema_dist_{n}"] = c/ema-1
    # Structure: distance to prior rolling extrema; shifted to avoid current-bar leakage.
    for n in [5,10,20,40,80]:
        hi,lo = h.rolling(n).max().shift(1), l.rolling(n).min().shift(1)
        x[f"prior_high_dist_{n}"] = c/hi-1
        x[f"prior_low_dist_{n}"] = c/lo-1
    # RSI is a feature only, never a preselected entry rule.
    delta = c.diff(); gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    x["rsi14"] = 100 - 100/(1 + gain/loss.replace(0,np.nan))
    # Future labels: entry next open; risk distance uses only known ATR at signal close.
    entry = o.shift(-1)
    stop_dist = (1.5*atr/c).clip(lower=0.002, upper=0.05)
    x["entry"] = entry
    x["stop_pct"] = stop_dist
    y = np.full(len(x), np.nan)
    for i in range(len(x)-HORIZON-1):
        if not np.isfinite(entry.iloc[i]) or not np.isfinite(stop_dist.iloc[i]): continue
        e, s = entry.iloc[i], stop_dist.iloc[i]
        if e <= 0 or s <= 0: continue
        # Evaluate both directions. Same-candle stop/target ambiguity resolves against the trade.
        long_stop, long_target = e*(1-s), e*(1+RR*s)
        short_stop, short_target = e*(1+s), e*(1-RR*s)
        outcome = 0
        for j in range(i+1, min(i+HORIZON+1,len(x))):
            if l.iloc[j] <= long_stop: long_done = -1; break
            if h.iloc[j] >= long_target: long_done = 1; break
        else: long_done = 0
        for j in range(i+1, min(i+HORIZON+1,len(x))):
            if h.iloc[j] >= short_stop: short_done = -1; break
            if l.iloc[j] <= short_target: short_done = 1; break
        else: short_done = 0
        # Separate directional labels are stored as two rows below.
        y[i] = 1 if long_done == 1 else 0
    x["label_long"] = y
    ys = np.full(len(x), np.nan)
    for i in range(len(x)-HORIZON-1):
        if not np.isfinite(entry.iloc[i]) or not np.isfinite(stop_dist.iloc[i]): continue
        e,s=entry.iloc[i],stop_dist.iloc[i]
        ss,tt=e*(1+s),e*(1-RR*s)
        for j in range(i+1,min(i+HORIZON+1,len(x))):
            if h.iloc[j] >= ss: ys[i]=0; break
            if l.iloc[j] <= tt: ys[i]=1; break
        else: ys[i]=0
    x["label_short"] = ys
    return x

FEATURES = [c for c in ["ret1","body","range_pct","body_to_range","upper_wick","lower_wick","close_location","atr_pct","range_atr","vol_ratio","quote_vol_proxy","rsi14"] +
 [f"{p}_{n}" for n in [3,6,12,24,48,96] for p in ["mom","range","volatility"]] +
 [f"ema_dist_{n}" for n in [10,20,50,100,200]] +
 [f"{p}_{n}" for n in [5,10,20,40,80] for p in ["prior_high_dist","prior_low_dist"]]]

def trade_sim(df, probs, side, threshold, start_equity=INITIAL_EQUITY):
    equity, peak, max_dd, trades = start_equity, start_equity, 0.0, []
    i=0
    while i < len(df)-HORIZON-2:
        if not np.isfinite(probs[i]) or probs[i] < threshold or not np.isfinite(df["stop_pct"].iloc[i]):
            i+=1; continue
        e=float(df["open"].iloc[i+1]); s=float(df["stop_pct"].iloc[i])
        if e<=0 or s<=0: i+=1; continue
        risk_cash=equity*RISK_FRACTION
        notional=min(risk_cash/s, equity*LEVERAGE_CAP)
        if notional<=0: i+=1; continue
        stop=e*(1-s) if side=="long" else e*(1+s)
        target=e*(1+RR*s) if side=="long" else e*(1-RR*s)
        result=None; exit_price=None; exit_i=i+1
        for j in range(i+1,min(i+HORIZON+1,len(df))):
            hi,lo=float(df["high"].iloc[j]),float(df["low"].iloc[j])
            if side=="long":
                if lo<=stop: result=-1; exit_price=stop*(1-SLIP_SIDE); exit_i=j; break
                if hi>=target: result=RR; exit_price=target*(1-SLIP_SIDE); exit_i=j; break
            else:
                if hi>=stop: result=-1; exit_price=stop*(1+SLIP_SIDE); exit_i=j; break
                if lo<=target: result=RR; exit_price=target*(1+SLIP_SIDE); exit_i=j; break
        if result is None:
            j=min(i+HORIZON,len(df)-1); exit_price=float(df["close"].iloc[j]); exit_i=j
            gross=((exit_price/e)-1)*(1 if side=="long" else -1)
            result=gross/s
        entry_fill=e*(1+SLIP_SIDE if side=="long" else 1-SLIP_SIDE)
        pnl=notional*result*s - notional*(2*FEE_SIDE) - notional*(2*SLIP_SIDE)
        equity += pnl
        peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak if peak>0 else 1)
        trades.append({"side":side,"entry_time":str(df["open_time"].iloc[i+1]),"exit_time":str(df["open_time"].iloc[exit_i]),"R_net":pnl/risk_cash if risk_cash else 0,"pnl":pnl,"equity":equity})
        i=exit_i+1
        if equity<=0: break
    if not trades: return {"trades":0,"win_rate":None,"profit_factor":None,"expectancy_R":None,"max_drawdown":0,"return_pct":0,"final_equity":equity}, trades
    t=pd.DataFrame(trades); wins=t[t.pnl>0].pnl; losses=t[t.pnl<0].pnl
    pf=float(wins.sum()/abs(losses.sum())) if len(losses) and losses.sum()!=0 else None
    return {"trades":len(t),"win_rate":float((t.pnl>0).mean()),"profit_factor":pf,"expectancy_R":float(t.R_net.mean()),"max_drawdown":float(max_dd),"return_pct":float(equity/start_equity-1),"final_equity":float(equity)}, trades

def main():
    all_reports=[]
    for symbol in SYMBOLS:
        try:
            raw=fetch_klines(symbol); d=make_features(raw)
            d=d.replace([np.inf,-np.inf],np.nan).reset_index(drop=True)
            n=len(d); a=int(n*.60); b=int(n*.80)
            for side,label in [("long","label_long"),("short","label_short")]:
                valid=d[label].notna()
                train=d.iloc[:a][valid.iloc[:a]]
                val=d.iloc[a:b][valid.iloc[a:b]]
                test=d.iloc[b:][valid.iloc[b:]]
                if len(train)<500 or len(val)<100 or len(test)<100:
                    raise RuntimeError(f"Insufficient labeled samples {symbol}/{side}")
                model=Pipeline([("imputer",SimpleImputer(strategy="median")),("rf",RandomForestClassifier(n_estimators=350,min_samples_leaf=20,max_features="sqrt",class_weight="balanced_subsample",n_jobs=-1,random_state=42))])
                model.fit(train[FEATURES],train[label].astype(int))
                val_probs=model.predict_proba(val[FEATURES])[:,1]
                # Threshold chosen on validation only; minimum trade count avoids tiny-sample winners.
                candidates=[]
                for th in np.arange(.50,.901,.025):
                    # Simulate only inside validation; never let a validation trade inspect test candles.
                    met,_=trade_sim(d.iloc[a:b].reset_index(drop=True),val_probs,side,float(th),INITIAL_EQUITY)
                    if met["trades"]>=20:
                        score=(met["profit_factor"] or 0)*math.sqrt(met["trades"])
                        candidates.append((score,float(th)))
                threshold=max(candidates)[1] if candidates else .60
                probs_test=np.full(n,np.nan)
                probs_test[test.index]=model.predict_proba(test[FEATURES])[:,1]
                met,trades=trade_sim(d.iloc[b:].reset_index(drop=True),probs_test[b:],side,threshold,INITIAL_EQUITY)
                report={"symbol":symbol,"side":side,"candles":n,"train_rows":len(train),"validation_rows":len(val),"test_rows":len(test),"validation_threshold":threshold,"test":met}
                all_reports.append(report)
                pd.DataFrame(trades).to_csv(OUT/f"{symbol}_{side}_test_trades.csv",index=False)
                print(json.dumps(report,allow_nan=False),flush=True)
        except Exception as e:
            print(json.dumps({"symbol":symbol,"error":str(e)}),flush=True)
    (OUT/"summary.json").write_text(json.dumps({"method":"raw OHLCV feature discovery via supervised barrier labels; chronological 60/20/20 split; RF; validation threshold; test untouched","assumptions":{"interval":INTERVAL,"start":"2021-01-01","RR":RR,"stop":"1.5 ATR, clipped 0.2%-5%","risk":RISK_FRACTION,"leverage_cap":LEVERAGE_CAP,"fee_per_side":FEE_SIDE,"slippage_per_side":SLIP_SIDE,"initial_equity":INITIAL_EQUITY,"funding":"NOT INCLUDED","liquidation":"approximate notional cap only; no exchange maintenance-margin model"},"reports":all_reports},indent=2,allow_nan=False))
    print(f"FINISHED: {len(all_reports)} symbol-direction reports. Funding and exact liquidation model excluded.",flush=True)

if __name__=="__main__": main()
