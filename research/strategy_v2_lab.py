"""Research-only Binance USD-M perpetual strategy-family lab."""
from __future__ import annotations
import json, os, time
from datetime import datetime, timezone
from pathlib import Path
import ccxt
import numpy as np
import pandas as pd

SYMBOLS=["BTC/USDT:USDT","ETH/USDT:USDT","BNB/USDT:USDT","SOL/USDT:USDT","XRP/USDT:USDT","ADA/USDT:USDT","DOGE/USDT:USDT","LINK/USDT:USDT"]
START=os.getenv("V2_RESEARCH_START","2021-01-01")
FEE=float(os.getenv("V2_FEE_PER_SIDE","0.0005"))
SLIP=float(os.getenv("V2_SLIPPAGE_PER_SIDE","0.0002"))
ATR_MULT=1.5
TARGET_R=2.0
MAX_HOLD=48
OUT=Path("research_results/v2_futures"); CACHE=Path("research_cache/v2_futures")
OUT.mkdir(parents=True,exist_ok=True); CACHE.mkdir(parents=True,exist_ok=True)
FAMILIES=("current_rsi_stoch_baseline","trend_breakout_20","trend_pullback_ema20","trend_macd_cross","range_mean_reversion")

def validate_ohlcv(frame, tf_ms):
    cols=["timestamp","open","high","low","close","volume"]
    if frame.empty or not set(cols).issubset(frame.columns): raise ValueError("Missing/empty OHLCV")
    x=frame[cols].copy()
    for c in cols: x[c]=pd.to_numeric(x[c],errors="coerce")
    x.replace([np.inf,-np.inf],np.nan,inplace=True); x.dropna(subset=cols,inplace=True)
    x.drop_duplicates("timestamp",keep="last",inplace=True); x.sort_values("timestamp",kind="stable",inplace=True)
    ok=(x.timestamp>=0)&(x[["open","high","low","close"]]>0).all(axis=1)&(x.volume>=0)
    ok &= x.high>=x[["open","close","low"]].max(axis=1)
    ok &= x.low<=x[["open","close","high"]].min(axis=1)
    x=x.loc[ok]
    if len(x)<250: raise ValueError(f"Only {len(x)} valid bars")
    return x.reset_index(drop=True)

def fetch_history(exchange,symbol,timeframe,start_ms):
    tf_ms=exchange.parse_timeframe(timeframe)*1000
    safe=symbol.replace("/","_").replace(":","_")
    cache=CACHE/f"{safe}_{timeframe}_{START}.csv"
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    if cache.exists():
        old=pd.read_csv(cache)
        if len(old) and int(old.timestamp.max())+tf_ms>=now-2*tf_ms:
            return validate_ohlcv(old[old.timestamp+tf_ms<=now],tf_ms)
    rows={}; since=start_ms
    for _ in range(80):
        batch=exchange.fetch_ohlcv(symbol,timeframe=timeframe,since=since,limit=1500)
        if not batch: break
        for row in batch:
            ts=int(row[0])
            if ts>=start_ms and ts+tf_ms<=now: rows[ts]=row[:6]
        nxt=int(batch[-1][0])+tf_ms
        if nxt<=since or len(batch)<2: break
        since=nxt; time.sleep(max(0,float(getattr(exchange,"rateLimit",0) or 0)/1000))
    if not rows: raise RuntimeError(f"No closed {timeframe} candles: {symbol}")
    frame=pd.DataFrame([[t,*rows[t][1:6]] for t in sorted(rows)],columns=["timestamp","open","high","low","close","volume"])
    frame=validate_ohlcv(frame,tf_ms); frame.to_csv(cache,index=False)
    return frame

def rsi(close,n=14):
    delta=close.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    loss=(-delta.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs=gain/(loss+1e-12)
    return 100-100/(1+rs)

def add_features(four_hour,daily):
    x=validate_ohlcv(four_hour,14_400_000); d=validate_ohlcv(daily,86_400_000)
    c,h,l,v=x.close,x.high,x.low,x.volume; prev=c.shift(1)
    tr=pd.concat([h-l,(h-prev).abs(),(l-prev).abs()],axis=1).max(axis=1)
    x["atr"]=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    for n in (20,50,200): x[f"ema{n}"]=c.ewm(span=n,adjust=False,min_periods=n).mean()
    x["vol_avg"]=v.rolling(20,min_periods=20).mean().shift(1); x["vol_ratio"]=v/x.vol_avg.replace(0,np.nan)
    x["rsi"]=rsi(c)
    x["volume_ma20"]=v.rolling(20,min_periods=20).mean()
    lowest=l.rolling(14,min_periods=14).min(); highest=h.rolling(14,min_periods=14).max()
    x["stoch_k"]=100*(c-lowest)/(highest-lowest+1e-12)
    x["stoch_d"]=x.stoch_k.rolling(3,min_periods=3).mean()
    x["macd"]=c.ewm(span=12,adjust=False,min_periods=12).mean()-c.ewm(span=26,adjust=False,min_periods=26).mean()
    x["macd_signal"]=x.macd.ewm(span=9,adjust=False,min_periods=9).mean()
    x["bb_mid"]=c.rolling(20,min_periods=20).mean(); sd=c.rolling(20,min_periods=20).std()
    x["bb_upper"]=x.bb_mid+2*sd; x["bb_lower"]=x.bb_mid-2*sd
    up,down=h.diff(),-l.diff()
    plus=pd.Series(np.where((up>down)&(up>0),up,0.0),index=x.index)
    minus=pd.Series(np.where((down>up)&(down>0),down,0.0),index=x.index)
    pdi=100*plus.ewm(alpha=1/14,adjust=False,min_periods=14).mean()/x.atr.replace(0,np.nan)
    mdi=100*minus.ewm(alpha=1/14,adjust=False,min_periods=14).mean()/x.atr.replace(0,np.nan)
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    x["adx"]=dx.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    d=d.copy(); d["daily_ema50"]=d.close.ewm(span=50,adjust=False,min_periods=50).mean()
    d["available_at"]=d.timestamp+86_400_000; x["available_at"]=x.timestamp+14_400_000
    aligned=pd.merge_asof(x[["available_at"]].sort_values("available_at"),
        d[["available_at","close","daily_ema50"]].sort_values("available_at"),
        on="available_at",direction="backward",allow_exact_matches=True)
    x["daily_close"]=aligned.close.to_numpy(); x["daily_ema50"]=aligned.daily_ema50.to_numpy()
    x.drop(columns="available_at",inplace=True)
    x["donchian_high20"]=h.shift(1).rolling(20,min_periods=20).max()
    x["donchian_low20"]=l.shift(1).rolling(20,min_periods=20).min()
    return x

def generate_candidates(x):
    c,o,h,l=x.close,x.open,x.high,x.low
    up=(c>x.ema200)&(x.ema50>x.ema200)&(x.daily_close>x.daily_ema50)
    down=(c<x.ema200)&(x.ema50<x.ema200)&(x.daily_close<x.daily_ema50)
    valid=x[["atr","adx","rsi","vol_ratio"]].notna().all(axis=1)
    baseline_valid=x[["atr","rsi","stoch_k","stoch_d","volume_ma20"]].notna().all(axis=1)
    raw={
      "current_rsi_stoch_baseline":(
        (x.rsi.shift(1)<30)&(x.rsi>=30)&
        (x.stoch_k.shift(1)<=x.stoch_d.shift(1))&(x.stoch_k>x.stoch_d)&(x.stoch_k<38)&
        (x.volume>1.12*x.volume_ma20)&(c>o)&baseline_valid,
        (x.rsi.shift(1)>70)&(x.rsi<=70)&
        (x.stoch_k.shift(1)>=x.stoch_d.shift(1))&(x.stoch_k<x.stoch_d)&(x.stoch_k>62)&
        (x.volume>1.12*x.volume_ma20)&(c<o)&baseline_valid
      ),
      "trend_breakout_20":(up&(c>x.donchian_high20)&(x.vol_ratio>=1.2)&(x.adx>=20),
                           down&(c<x.donchian_low20)&(x.vol_ratio>=1.2)&(x.adx>=20)),
      "trend_pullback_ema20":(up&(l<=x.ema20)&(c>x.ema20)&(c>o)&x.rsi.between(45,68),
                              down&(h>=x.ema20)&(c<x.ema20)&(c<o)&x.rsi.between(32,55)),
      "trend_macd_cross":(up&(x.macd>x.macd_signal)&(x.macd.shift(1)<=x.macd_signal.shift(1))&(x.adx>=18),
                         down&(x.macd<x.macd_signal)&(x.macd.shift(1)>=x.macd_signal.shift(1))&(x.adx>=18)),
      "range_mean_reversion":((x.adx<18)&(c.shift(1)<x.bb_lower.shift(1))&(c>x.bb_lower)&(x.rsi.shift(1)<30)&(x.rsi>=30),
                              (x.adx<18)&(c.shift(1)>x.bb_upper.shift(1))&(c<x.bb_upper)&(x.rsi.shift(1)>70)&(x.rsi<=70))
    }
    return {k:{"BUY":(a&valid).fillna(False).to_numpy(bool),"SELL":(b&valid).fillna(False).to_numpy(bool)} for k,(a,b) in raw.items()}

def simulate_one(frame,i,side,end,family=None):
    ei=i+1
    if ei>=end: return None
    row,nxt=frame.iloc[i],frame.iloc[ei]; atr=float(row.atr); direction=1 if side=="BUY" else -1
    entry=float(nxt.open)*(1+direction*SLIP)
    target_r=1.5 if family=="current_rsi_stoch_baseline" else TARGET_R
    if family=="current_rsi_stoch_baseline":
        recent=frame.iloc[max(0,i-4):i+1]
        swing_low=float(recent.low.min()); swing_high=float(recent.high.max())
        stop=min(swing_low,entry-1.32*atr) if side=="BUY" else max(swing_high,entry+1.32*atr)
        distance=abs(entry-stop)
        risk_pct=distance/entry if entry>0 else float("inf")
        if not (0.0035<risk_pct<=0.025): return None
    else:
        distance=atr*ATR_MULT
        stop=entry-direction*distance
    if not np.isfinite(entry) or not np.isfinite(distance) or entry<=0 or distance<=0: return None
    target=entry+direction*distance*target_r
    if stop<=0 or target<=0: return None
    last=min(end-1,ei+MAX_HOLD-1); exit_raw=float(frame.iloc[last].close); reason="TIME"
    for j in range(ei,last+1):
        bar=frame.iloc[j]; opn,hi,lo=float(bar.open),float(bar.high),float(bar.low)
        stop_hit=lo<=stop if side=="BUY" else hi>=stop
        target_hit=hi>=target if side=="BUY" else lo<=target
        if stop_hit:
            last=j; exit_raw=min(stop,opn) if side=="BUY" else max(stop,opn); reason="STOP"; break
        if target_hit: last=j; exit_raw=target; reason="TARGET"; break
    exit_fill=exit_raw*(1-SLIP) if side=="BUY" else exit_raw*(1+SLIP)
    gross=direction*(exit_fill-entry); fees=FEE*(entry+exit_fill); net=gross-fees
    denom=distance+FEE*(entry+stop)+SLIP*(entry+stop)
    if not np.isfinite(net) or denom<=0: return None
    return {"signal_index":i,"entry_index":ei,"exit_index":last,"signal_time":int(row.timestamp),
      "entry_time":int(nxt.timestamp),"exit_time":int(frame.iloc[last].timestamp),"side":side,
      "entry":entry,"stop":stop,"target":target,"exit":exit_fill,"reason":reason,
      "net_r":net/denom,"net_return_pct":net/entry*100}

def simulate_candidate(frame,signals,start,end,family=None):
    trades=[]; i=max(start,220); end=min(end,len(frame))
    while i<end-1:
        long_sig,short_sig=bool(signals["BUY"][i]),bool(signals["SELL"][i])
        if long_sig==short_sig: i+=1; continue
        trade=simulate_one(frame,i,"BUY" if long_sig else "SELL",end,family=family)
        if trade is None: i+=1; continue
        trades.append(trade); i=trade["exit_index"]+1
    return trades

def metrics(trades):
    if not trades: return {"trades":0,"win_rate_pct":None,"expectancy_r":None,"profit_factor":None,"net_total_r":0.0,"max_drawdown_r":None,"long_trades":0,"short_trades":0}
    r=np.asarray([t["net_r"] for t in trades],float); wins,losses=r[r>0],r[r<0]
    eq=np.cumsum(r); peak=np.maximum.accumulate(np.r_[0.0,eq])[1:]
    pf=float(wins.sum()/abs(losses.sum())) if len(losses) and abs(losses.sum()) else None
    return {"trades":len(r),"win_rate_pct":round(float((r>0).mean()*100),2),
      "expectancy_r":round(float(r.mean()),4),"profit_factor":round(pf,4) if pf is not None else None,
      "net_total_r":round(float(r.sum()),3),"max_drawdown_r":round(float((peak-eq).max()),3),
      "long_trades":sum(t["side"]=="BUY" for t in trades),"short_trades":sum(t["side"]=="SELL" for t in trades)}

def splits(n):
    a,b=int(n*.6),int(n*.8)
    return {"train":(220,a),"validation":(a,b),"holdout":(b,n)}

def run_lab():
    ex=ccxt.binanceusdm({"enableRateLimit":True,"timeout":30000}); ex.load_markets()
    start=int(pd.Timestamp(START,tz="UTC").timestamp()*1000); data={}; failures=[]
    for symbol in SYMBOLS:
        try:
            if symbol not in ex.markets: raise ValueError("Not a listed USD-M perpetual")
            f=fetch_history(ex,symbol,"4h",start); d=fetch_history(ex,symbol,"1d",start)
            x=add_features(f,d)
            if len(x)<1200: raise ValueError(f"Only {len(x)} valid 4h bars")
            data[symbol]=x; print(f"[data] {symbol}: {len(x)} 4h bars",flush=True)
        except Exception as e: failures.append({"symbol":symbol,"error":f"{type(e).__name__}: {e}"})
    if len(data)<4: raise RuntimeError(f"Need at least 4 symbols; got {len(data)}")
    results=[]
    for family in FAMILIES:
        by_symbol={}
        for symbol,x in data.items():
            sig=generate_candidates(x)[family]
            by_symbol[symbol]={name:metrics(simulate_candidate(x,sig,a,b,family=family)) for name,(a,b) in splits(len(x)).items()}
        eligible=[v["validation"] for v in by_symbol.values() if v["validation"]["trades"]>=8]
        score=float(np.median([v["expectancy_r"] for v in eligible])) if len(eligible)>=3 else -999.0
        results.append({"family":family,"validation_median_symbol_expectancy_r":round(score,4),
          "validation_symbols_with_8plus_trades":len(eligible),"by_symbol":by_symbol})
        checkpoint={"completed":len(results),"total":len(FAMILIES),"failures":failures,"results":results}
        tmp=OUT/"checkpoint.json.tmp"; tmp.write_text(json.dumps(checkpoint,indent=2,allow_nan=False)); tmp.replace(OUT/"checkpoint.json")
        print(f"[checkpoint] {len(results)}/{len(FAMILIES)}",flush=True)
    results.sort(key=lambda x:x["validation_median_symbol_expectancy_r"],reverse=True)
    report={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"market":"Binance USD-M linear perpetual OHLCV",
      "timeframes":{"signal":"4h","regime":"last fully closed 1d candle"},"symbols":list(data),"start":START,
      "fee_per_side":FEE,"adverse_slippage_per_side":SLIP,"stop_atr_multiple":ATR_MULT,"target_r":TARGET_R,
      "max_hold_4h_bars":MAX_HOLD,"split":"60/20/20 chronological; validation ranks candidates",
      "fill_model":"next 4h open; adverse slippage both sides; stop-first same-bar ambiguity",
      "funding":"not included; required before any live promotion",
      "caveats":["Research simulation, not exchange execution.","Funding, liquidation, maintenance margin, partial fills, spread and market impact omitted.",
      "OHLC stop-first is conservative but cannot reveal intrabar path.","Holdout is reported, never used for selection."],
      "failures":failures,"ranked_families":results}
    tmp=OUT/"strategy_v2_report.json.tmp"; tmp.write_text(json.dumps(report,indent=2,allow_nan=False)); tmp.replace(OUT/"strategy_v2_report.json")
    flat=[{"rank":rank,"family":r["family"],"symbol":s,"split":sp,**m}
      for rank,r in enumerate(results,1) for s,parts in r["by_symbol"].items() for sp,m in parts.items()]
    pd.DataFrame(flat).to_csv(OUT/"strategy_v2_report.csv",index=False)
    return report

if __name__=="__main__":
    result=run_lab()
    print(json.dumps([{"family":x["family"],"validation_median_expectancy_r":x["validation_median_symbol_expectancy_r"],
      "symbols":x["validation_symbols_with_8plus_trades"]} for x in result["ranked_families"]],indent=2))
