#!/usr/bin/env python3
"""Research-only RTM/liquidity-sweep/Ichimoku USD-M futures experiment.

15m entries, completed 1h location and 4h regime features. Public candles only.
No keys, private endpoints, order execution, or live bot imports.
"""
from __future__ import annotations
import json, time
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT"]
START = "2022-01-01"
FEE = 0.0005
SLIPPAGE = 0.0002
OUT = Path("research_results/rtm_ichimoku_v1")
CACHE = Path("research_cache/rtm_ichimoku_v1")
OUT.mkdir(parents=True, exist_ok=True); CACHE.mkdir(parents=True, exist_ok=True)


def fetch_15m(symbol: str) -> pd.DataFrame:
    path=CACHE/f"{symbol}_15m.csv"
    if path.exists():
        d=pd.read_csv(path,parse_dates=["timestamp"]); d.timestamp=pd.to_datetime(d.timestamp,utc=True); return d
    start=int(pd.Timestamp(START,tz="UTC").timestamp()*1000); now=int(datetime.now(timezone.utc).timestamp()*1000)
    rows=[]; cursor=start; session=requests.Session()
    while cursor<now:
        r=session.get("https://fapi.binance.com/fapi/v1/klines",params={"symbol":symbol,"interval":"15m","startTime":cursor,"endTime":now,"limit":1500},timeout=30)
        r.raise_for_status(); batch=r.json()
        if isinstance(batch,dict) or not batch: break
        rows.extend(batch); nxt=int(batch[-1][0])+1
        if nxt<=cursor: raise RuntimeError("Binance pagination stalled")
        cursor=nxt
        if len(batch)<1500: break
        time.sleep(.12)
    if len(rows)<1000: raise RuntimeError(f"{symbol}: insufficient Binance USD-M history ({len(rows)} bars)")
    d=pd.DataFrame(rows,columns=["ts","open","high","low","close","volume","close_ts","qv","n","tb","tq","ignore"])
    d["timestamp"]=pd.to_datetime(d.ts,unit="ms",utc=True)
    for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d[["timestamp","open","high","low","close","volume"]].dropna()
    d=d[d.timestamp+pd.Timedelta(minutes=15)<=pd.Timestamp.now(tz="UTC")].drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    d.to_csv(path,index=False); return d


def resample_ohlcv(d: pd.DataFrame, rule: str) -> pd.DataFrame:
    x=d.set_index("timestamp")
    y=x.resample(rule,closed="right",label="right").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    return y.reset_index()


def atr(d, n=14):
    pc=d.close.shift(1)
    tr=pd.concat([d.high-d.low,(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()


def ichimoku(d):
    h,l=d.high,d.low
    ten=(h.rolling(9).max()+l.rolling(9).min())/2
    kij=(h.rolling(26).max()+l.rolling(26).min())/2
    span_a=(ten+kij)/2
    span_b=(h.rolling(52).max()+l.rolling(52).min())/2
    return ten,kij,span_a,span_b


def htf_features(d, rule):
    x=resample_ohlcv(d,rule)
    ten,kij,a,b=ichimoku(x)
    x["atr"]=atr(x)
    x["ema200"]=x.close.ewm(span=200,adjust=False,min_periods=200).mean()
    x["ema_slope"]=x.ema200.pct_change(5)
    x["cloud_top"]=pd.concat([a,b],axis=1).max(axis=1)
    x["cloud_bottom"]=pd.concat([a,b],axis=1).min(axis=1)
    x["bull"]= (x.close>x.cloud_top)&(ten>kij)&(x.ema_slope>0)
    x["bear"]= (x.close<x.cloud_bottom)&(ten<kij)&(x.ema_slope<0)
    x=x.reset_index()
    # Values become available only at the HTF candle close; backward as-of join.
    return x[["timestamp","bull","bear","atr","cloud_top","cloud_bottom"]].rename(columns={"timestamp":f"{rule}_time"})


def build_features(raw):
    x=raw.copy().sort_values("timestamp").reset_index(drop=True)
    x["atr"]=atr(x); x["atr_pct"]=x.atr/x.close
    x["vol_rel"]=x.volume/x.volume.rolling(20).mean()
    # 15m liquidity sweep: prior 20-bar extreme, reclaim by close; current bar excluded.
    prior_hi=x.high.shift(1).rolling(20).max(); prior_lo=x.low.shift(1).rolling(20).min()
    x["sweep_long"]=(x.low<prior_lo)&(x.close>prior_lo)
    x["sweep_short"]=(x.high>prior_hi)&(x.close<prior_hi)
    # Displacement and close-confirmed local structure break.
    x["bos_long"]=x.close>x.high.shift(1).rolling(5).max()
    x["bos_short"]=x.close<x.low.shift(1).rolling(5).min()
    body=(x.close-x.open).abs()
    x["displacement"]=(body>x.atr*.8)&(x.vol_rel>1.0)
    # RTM proxy: last opposing candle's body zone before an impulse; all shifted.
    x["demand_low"]=np.where(x.close.shift(1)>x.open.shift(1),x.low.shift(1),np.nan)
    x["supply_high"]=np.where(x.close.shift(1)<x.open.shift(1),x.high.shift(1),np.nan)
    # HTF bars are joined only after their close timestamp is <= entry candle time.
    for rule in ["1h","4h"]:
        h=htf_features(raw,rule)
        x=pd.merge_asof(x.sort_values("timestamp"),h.sort_values(f"{rule}_time"),left_on="timestamp",right_on=f"{rule}_time",direction="backward")
    x["long_setup"]=x["4h_bull"].fillna(False)&x["1h_bull"].fillna(False)&x.sweep_long&x.bos_long&x.displacement
    x["short_setup"]=x["4h_bear"].fillna(False)&x["1h_bear"].fillna(False)&x.sweep_short&x.bos_short&x.displacement
    x["side"]=np.select([x.long_setup,x.short_setup],[1,-1],default=0)
    return x


def trade_sim(x, start, end, fee=FEE, slip=SLIPPAGE):
    trades=[]; i=max(start,400); end=min(end,len(x))
    while i<end-2:
        side=int(x.side.iloc[i])
        if not side: i+=1; continue
        entry=float(x.open.iloc[i+1]); av=float(x.atr.iloc[i])
        if not np.isfinite(av) or av<=0: i+=1; continue
        # Structural stop beyond sweep bar, with ATR floor.
        stop=float(x.low.iloc[i]-0.15*av if side==1 else x.high.iloc[i]+0.15*av)
        risk=abs(entry-stop)
        if risk/entry<0.002 or risk/entry>0.025: i+=1; continue
        tp=entry+side*3*risk; exit_reason="TIME"; exit_i=min(i+1+96,end-1); exit_px=float(x.close.iloc[exit_i])
        for j in range(i+1,min(i+1+97,end)):
            hi,lo=float(x.high.iloc[j]),float(x.low.iloc[j])
            stop_hit=lo<=stop if side==1 else hi>=stop
            tp_hit=hi>=tp if side==1 else lo<=tp
            # Conservative: if both touched in one candle, stop is assumed first.
            if stop_hit:
                exit_px=stop; exit_reason="SL"; exit_i=j; break
            if tp_hit:
                exit_px=tp; exit_reason="TP3R"; exit_i=j; break
        ef=entry*(1+slip if side==1 else 1-slip)
        xf=exit_px*(1-slip if side==1 else 1+slip)
        gross=side*(xf-ef)/risk
        cost=fee*(ef+xf)/risk
        nr=gross-cost
        trades.append({"symbol":str(x.symbol.iloc[0]) if "symbol" in x else "","timestamp":str(x.timestamp.iloc[i]),"side":"LONG" if side==1 else "SHORT","entry":entry,"stop":stop,"target":tp,"exit":exit_px,"exit_reason":exit_reason,"net_r":nr,"bars_held":exit_i-i})
        i=exit_i+1
    return trades


def summarize(trades):
    if not trades:return {"trades":0,"win_rate_pct":None,"expectancy_r":None,"profit_factor":None,"max_drawdown_r":None}
    r=np.array([t["net_r"] for t in trades],float); wins=r[r>0]; losses=r[r<0]; eq=np.cumsum(r); peak=np.maximum.accumulate(np.r_[0,eq])[1:]
    return {"trades":len(r),"win_rate_pct":round(float((r>0).mean()*100),2),"expectancy_r":round(float(r.mean()),4),"profit_factor":round(float(wins.sum()/abs(losses.sum())),4) if len(losses) else None,"max_drawdown_r":round(float(np.max(peak-eq)),3)}


def main():
    all_trades=[]; failures={}; counts={}
    for s in SYMBOLS:
        try:
            raw=fetch_15m(s); f=build_features(raw); f["symbol"]=s
            t=trade_sim(f,400,len(f)); all_trades.extend(t); counts[s]={"bars":len(f),"signals":int((f.side!=0).sum()),"trades":len(t)}
            print(f"[{s}] bars={len(f)} signals={counts[s]['signals']} trades={len(t)}",flush=True)
        except Exception as e: failures[s]=f"{type(e).__name__}: {e}"; print(f"[FAIL] {s}: {e}",flush=True)
    all_trades.sort(key=lambda z:z["timestamp"])
    # Chronological folds: first 60% warm-up/train; report later 20% windows separately.
    folds=[]
    for a,b in [(0.60,0.73),(0.73,0.86),(0.86,1.0)]:
        part=[]
        for s in SYMBOLS:
            try:
                raw=fetch_15m(s); f=build_features(raw); f["symbol"]=s
                part.extend(trade_sim(f,int(len(f)*a),int(len(f)*b)))
            except Exception: pass
        part.sort(key=lambda z:z["timestamp"]); folds.append({"window":[a,b],**summarize(part)})
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"market":"Binance USD-M perpetual futures","timeframe_entry":"15m","higher_timeframes":["1h","4h"],"symbols":counts,"failures":failures,"costs":{"fee_per_side":FEE,"slippage_per_side":SLIPPAGE},"rules":{"regime":"4h Ichimoku cloud + EMA200 slope","location":"1h Ichimoku trend alignment; RTM base/impulse proxy to be ablated","trigger":"15m prior-20-bar liquidity sweep/reclaim + 5-bar close BOS + displacement/relative volume","entry":"next 15m open","stop":"sweep-bar extreme +/- 0.15 ATR; reject <0.2% or >2.5%","target":"fixed 3R","same_bar":"stop first","funding":"not included; timestamped historical funding unavailable in this initial run"},"overall":summarize(all_trades),"walk_forward_folds":folds,"trade_ledger":"trade_ledger.csv","decision":"RESEARCH_ONLY; no promotion unless >=200 resolved OOS trades, positive net expectancy, stable walk-forward and stressed-cost performance."}
    (OUT/"report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    pd.DataFrame(all_trades).to_csv(OUT/"trade_ledger.csv",index=False)
    print(json.dumps(report,indent=2),flush=True)


if __name__=="__main__": main()
