# -*- coding: utf-8 -*-
"""Sequential BTCUSDT 4h backtest with chronological train/test split."""
import itertools, json, time
from pathlib import Path
import numpy as np
import pandas as pd
import requests

URLS = [
    "https://api.binance.us/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
]
COLS = ["open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore"]
FEE = 0.0004
HOLD = 30


def fetch_klines(symbol="BTCUSDT", interval="4h", limit=5000):
    candles = {}
    end_time = None
    with requests.Session() as session:
        while len(candles) < limit:
            before = len(candles)
            n = min(1000, limit-len(candles))
            params = {"symbol":symbol,"interval":interval,"limit":n}
            if end_time is not None: params["endTime"] = end_time
            response, errors = None, []
            for url in URLS:
                try:
                    candidate = session.get(url,params=params,timeout=30)
                    if candidate.status_code in (403,451):
                        errors.append(f"{url}: HTTP {candidate.status_code}"); continue
                    candidate.raise_for_status()
                    response = candidate; break
                except requests.RequestException as exc:
                    errors.append(f"{url}: {exc}")
            if response is None:
                raise RuntimeError("All market-data endpoints failed: "+" | ".join(errors))
            batch = response.json()
            if not batch: break
            for c in batch: candles[int(c[0])] = c
            if len(candles) == before:
                raise RuntimeError("Pagination made no progress")
            end_time = min(int(c[0]) for c in batch)-1
            if len(batch)<n: break
            time.sleep(.03)
    if not candles: raise RuntimeError("No candles received")
    df = pd.DataFrame([candles[k] for k in sorted(candles)][-limit:],columns=COLS)
    for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    df["open_time"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
    df["close_time"]=pd.to_datetime(df["close_time"],unit="ms",utc=True)
    df=df[df.close_time<pd.Timestamp.now(tz="UTC")].dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    if len(df)<500: raise RuntimeError(f"Too few closed candles: {len(df)}")
    return df


def indicators(df):
    d=df.copy(); pc=d.close.shift(1)
    tr=pd.concat([d.high-d.low,(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    d["atr"]=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    d["vol_avg"]=d.volume.rolling(20,min_periods=20).mean().shift(1)
    d["ma200"]=d.close.rolling(200,min_periods=200).mean()
    return d


def metrics(trades):
    if not trades:
        return {"n":0,"wr":0.0,"avg_r":None,"sum_r":0.0,"pf":None,"maxdd":0.0}
    a=np.asarray(trades,float); gp=float(a[a>0].sum()); gl=float(-a[a<0].sum())
    eq=np.r_[0.0,np.cumsum(a)]; dd=float((eq-np.maximum.accumulate(eq)).min())
    pf=gp/gl if gl>0 else ("inf" if gp>0 else 0.0)
    return {"n":len(a),"wr":round(float((a>0).mean()*100),2),
            "avg_r":round(float(a.mean()),4),"sum_r":round(float(a.sum()),3),
            "pf":round(pf,3) if isinstance(pf,float) else pf,"maxdd":round(dd,3)}


def simulate_one(df, entry_i, end_i, side, entry, sl, tp, hold):
    risk=entry-sl if side=="L" else sl-entry
    if risk<=0 or min(entry,sl,tp)<=0: return None, entry_i
    last_i=min(end_i,entry_i+hold)
    exitp=None; exit_i=entry_i
    for j in range(entry_i,last_i):
        r=df.iloc[j]; o,h,l=float(r.open),float(r.high),float(r.low)
        if side=="L":
            if l<=sl: exitp=min(o,sl); exit_i=j; break
            if h>=tp: exitp=tp; exit_i=j; break
        else:
            if h>=sl: exitp=max(o,sl); exit_i=j; break
            if l<=tp: exitp=tp; exit_i=j; break
    if exitp is None:
        if last_i<=entry_i: return None,entry_i
        exit_i=last_i-1; exitp=float(df.iloc[exit_i].close)
    gross=(exitp-entry)/risk if side=="L" else (entry-exitp)/risk
    net=gross-FEE*(entry+exitp)/risk
    return float(net),exit_i


def backtest(df, start, end, strategy, p):
    trades=[]; i=max(start,201); end=min(end,len(df)-1)
    while i<end:
        r=df.iloc[i]
        if pd.isna(r.atr) or pd.isna(r.vol_avg) or pd.isna(r.ma200) or r.atr<=0 or r.vol_avg<=0:
            i+=1; continue
        side=None; atr=float(r.atr); entry=float(df.iloc[i+1].open)
        if strategy=="BO":
            lb,vm,am,rr,trend=p["lb"],p["vm"],p["am"],p["rr"],p["trend"]
            if i<lb: i+=1; continue
            prior=df.iloc[i-lb:i]
            vok=float(r.volume)>vm*float(r.vol_avg)
            if vok and float(r.close)>float(prior.high.max()) and (not trend or r.close>=r.ma200):
                side="L"; sl=entry-am*atr; tp=entry+rr*(entry-sl)
            elif vok and float(r.close)<float(prior.low.min()) and (not trend or r.close<=r.ma200):
                side="S"; sl=entry+am*atr; tp=entry-rr*(sl-entry)
        else:
            lb,vm,am,rr,tol,mode=p["lb"],p["vm"],p["am"],p["rr"],p["tol"],p["mode"]
            if i<lb: i+=1; continue
            sw=df.iloc[i-lb:i]; hi=float(sw.high.max()); lo=float(sw.low.min()); span=hi-lo
            if span>0:
                fibs=[hi-.382*span,hi-.5*span,hi-.618*span]
                ch=float(r.high); body=abs(float(r.close)-float(r.open))
                wick=ch-max(float(r.open),float(r.close))
                rejection=float(r.close)<float(r.open) and wick>=max(1.2*body,.1*atr)
                nearfib=any(abs(ch-f)<=tol*atr for f in fibs)
                nearhigh=ch>=hi-tol*atr
                vok=float(r.volume)>vm*float(r.vol_avg)
                # mode controls whether location is near swing high, fib zone, or either.
                location=(nearhigh if mode=="resistance" else nearfib if mode=="fib" else (nearhigh or nearfib))
                if rejection and vok and location:
                    side="S"; sl=max(ch,entry)+am*atr; tp=entry-rr*(sl-entry)
        if side is None:
            i+=1; continue
        result, exit_i=simulate_one(df,i+1,end,side,entry,float(sl),float(tp),HOLD)
        if result is not None: trades.append(result)
        # one position at a time; no overlapping trades
        i=max(i+1,exit_i+1)
    return trades


def main():
    df=indicators(fetch_klines())
    split=int(len(df)*.70)
    print(f"DATA: BTCUSDT 4h | closed={len(df)} | {df.open_time.iloc[0]} -> {df.close_time.iloc[-1]}")
    print(f"CHRONOLOGICAL SPLIT: train={split} candles; test={len(df)-split} candles")
    rows=[]
    bo_grid=[]
    for rr,vm,am,lb,trend in itertools.product([1.5,2.0,2.5,3.0],[1.0,1.5,2.0],[1.0,1.5],[10,20,30],[True,False]):
        bo_grid.append({"rr":rr,"vm":vm,"am":am,"lb":lb,"trend":trend})
    mc_grid=[]
    for rr,vm,am,tol,lb,mode in itertools.product([1.5,2.0,2.5],[1.5,2.0],[1.0,1.5],[0.5,1.0,1.5],[50,100],["resistance","fib","either"]):
        mc_grid.append({"rr":rr,"vm":vm,"am":am,"tol":tol,"lb":lb,"mode":mode})
    for name,grid in [("BO",bo_grid),("MC",mc_grid)]:
        print(f"GRID {name}: {len(grid)} parameter sets",flush=True)
        for n,p in enumerate(grid,1):
            tr=metrics(backtest(df,201,split,name,p))
            te=metrics(backtest(df,split,len(df)-1,name,p))
            rows.append({"strategy":name,"params":json.dumps(p,sort_keys=True),**{"train_"+k:v for k,v in tr.items()},**{"test_"+k:v for k,v in te.items()}})
            if n%100==0: print(f"  {name}: {n}/{len(grid)}",flush=True)
    out=Path("backtest-results"); out.mkdir(exist_ok=True)
    result=pd.DataFrame(rows)
    result.to_csv(out/"optimization.csv",index=False)
    # Select only on training set, then report those exact params on untouched test set.
    train_candidates=result[(result.train_n>=30)&(result.train_pf.apply(lambda x: isinstance(x,(float,int)) and x>1.0))].copy()
    train_candidates=train_candidates.sort_values(["train_sum_r","train_pf"],ascending=False)
    print("\nBEST BY TRAIN SUM R (>=30 train trades, PF>1):")
    print(train_candidates.head(10).to_string(index=False) if len(train_candidates) else "No eligible candidates")
    print("\nTEST RESULTS FOR TOP TRAIN-SELECTED CANDIDATES:")
    cols=["strategy","params","train_n","train_wr","train_sum_r","train_pf","test_n","test_wr","test_sum_r","test_pf","test_maxdd"]
    print(train_candidates.head(15)[cols].to_string(index=False) if len(train_candidates) else "No eligible candidates")
    # Also provide exploratory test leaderboard, clearly marked as selection-biased.
    test_candidates=result[(result.test_n>=20)&result.test_pf.apply(lambda x:isinstance(x,(float,int)) and x>0)].sort_values(["test_sum_r","test_pf"],ascending=False)
    print("\nEXPLORATORY TEST LEADERBOARD (selection-biased; do NOT treat as validation):")
    print(test_candidates.head(10)[cols].to_string(index=False) if len(test_candidates) else "No test candidates with >=20 trades")
    summary={"candles":len(df),"start":str(df.open_time.iloc[0]),"end":str(df.close_time.iloc[-1]),"split_index":split,
             "train_end":str(df.close_time.iloc[split-1]),"test_start":str(df.open_time.iloc[split]),
             "fee_per_side":FEE,"slippage_included":False,"overlapping_trades":False,
             "strategy_sets":{"BO":len(bo_grid),"MC":len(mc_grid)}}
    (out/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print("\nSaved backtest-results/optimization.csv and summary.json")
    print("CAUTION: parameter search is large; test leaderboard is selection-biased. Prefer train-selected candidates and run a second untouched period.")
if __name__=="__main__": main()
