#!/usr/bin/env python3
"""One-year no-lookahead backtest of the current 5m spot strategy."""
import json, math, time
from datetime import datetime, timezone
from pathlib import Path
import ccxt, numpy as np, pandas as pd

SYMBOLS=["BTC/USDT","ETH/USDT","SOL/USDT"]
BAR=300000; FEE=.001; RISK=10.; MAX_NOTIONAL=200.
OUT=Path("backtest-results"); OUT.mkdir(exist_ok=True)

def iso(ms): return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def fetch(ex,symbol,start,end):
    rows=[]; since=start; last=None
    while since<end:
        for attempt in range(6):
            try:
                batch=ex.fetch_ohlcv(symbol,timeframe="5m",since=since,limit=1000); break
            except Exception:
                if attempt==5: raise
                time.sleep(min(2**attempt,20))
        if not batch: break
        fresh=[x for x in batch if start<=int(x[0])<end and (last is None or int(x[0])>last)]
        if fresh:
            rows.extend(fresh); last=int(fresh[-1][0]); since=last+BAR
        else: since=max(since+BAR,int(batch[-1][0])+BAR)
        if len(batch)<1000 and int(batch[-1][0])>=end-BAR: break
    if not rows: raise RuntimeError("No OHLCV history for "+symbol)
    d=pd.DataFrame(rows,columns=["timestamp","open","high","low","close","volume"])
    d=d.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
    print(f"[data] {symbol}: {len(d):,} bars from {iso(int(d.timestamp.iloc[0]))} to {iso(int(d.timestamp.iloc[-1]))}",flush=True)
    return d

def signals(d):
    d=d.copy(); c,h,l=d.close,d.high,d.low
    e20=c.ewm(span=20,adjust=False).mean(); e50=c.ewm(span=50,adjust=False).mean()
    delta=c.diff(); gain=delta.clip(lower=0).rolling(7).mean(); loss=-delta.clip(upper=0).rolling(7).mean()
    rsi=100-100/(1+gain/loss.replace(0,np.nan))
    k=100*(c-l.rolling(5).min())/(h.rolling(5).max()-l.rolling(5).min()+1e-10); kd=k.rolling(3).mean()
    pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean(); baseline=atr.rolling(50).mean()*.8
    dt=pd.to_datetime(d.timestamp,unit="ms",utc=True); minute=dt.dt.hour*60+dt.dt.minute
    session=((minute>=420)&(minute<=660))|((minute>=780)&(minute<=1020))
    vol=atr.notna()&baseline.notna()&(atr>baseline)
    d["atr_bt"]=atr
    d["buy_signal"]=(e20>e50)&(rsi<30)&(k<20)&(k>kd)&vol&session
    d["sell_signal"]=(e20<e50)&(rsi>70)&(k>80)&(k<kd)&vol&session
    return d

def simulate(symbol,raw,start):
    d=signals(raw); trades=[]; counts={"buy_signals":0,"sell_signals_ignored_spot":int(d.loc[d.timestamp>=start,"sell_signal"].sum())}
    i=max(1,int(np.searchsorted(d.timestamp.to_numpy(),start))); expiry=48*12
    while i<len(d)-1:
        row=d.iloc[i]
        if not bool(row.buy_signal) or not math.isfinite(float(row.atr_bt)) or float(row.atr_bt)<=0:
            i+=1; continue
        counts["buy_signals"]+=1; ent_i=i+1
        entry=float(d.iloc[ent_i].open); atr=float(row.atr_bt); sl=entry-atr; tp=entry+1.5*atr
        qty=min(RISK/atr,MAX_NOTIONAL/entry); end_i=min(len(d)-1,ent_i+expiry)
        exit_price=None; reason="EXPIRY"; exit_i=end_i
        for j in range(ent_i,end_i+1):
            candle=d.iloc[j]
            if float(candle.low)<=sl: exit_price,reason,exit_i=sl,"SL",j; break
            if float(candle.high)>=tp: exit_price,reason,exit_i=tp,"TP",j; break
        if exit_price is None: exit_price=float(d.iloc[end_i].close)
        gross=(exit_price-entry)*qty; fees=(entry+exit_price)*qty*FEE; net=gross-fees; risk=qty*atr
        trades.append({"symbol":symbol,"signal_time_utc":iso(int(row.timestamp)),"entry_time_utc":iso(int(d.iloc[ent_i].timestamp)),
          "exit_time_utc":iso(int(d.iloc[exit_i].timestamp)),"entry":entry,"sl":sl,"tp":tp,"exit":exit_price,"qty":qty,
          "position_usd":qty*entry,"gross_pnl_usd":gross,"fees_usd":fees,"net_pnl_usd":net,"initial_risk_usd":risk,
          "net_r":net/risk,"exit_reason":reason,"bars_held":exit_i-ent_i})
        i=exit_i+1
    meta={"bars":len(raw),"first":iso(int(raw.timestamp.iloc[0])),"last":iso(int(raw.timestamp.iloc[-1]))}
    return trades,counts,meta

def main():
    end=int(datetime.now(timezone.utc).timestamp()*1000); end-=end%BAR
    start=end-365*24*60*60*1000
    ex=ccxt.binanceus({"enableRateLimit":True,"timeout":30000}); ex.load_markets()
    trades=[]; counts={"buy_signals":0,"sell_signals_ignored_spot":0}; data={}
    for symbol in SYMBOLS:
        if symbol not in ex.markets: print("[skip] not listed",symbol); continue
        hist=fetch(ex,symbol,start-300*BAR,end); t,c,m=simulate(symbol,hist,start)
        trades.extend(t); data[symbol]=m
        for k in counts: counts[k]+=c[k]
        print(f"[result] {symbol}: {len(t)} trades, net PnL {sum(x['net_pnl_usd'] for x in t):.2f} USD",flush=True)
    trades.sort(key=lambda x:x["exit_time_utc"]); df=pd.DataFrame(trades)
    if len(df):
        pnl=df.net_pnl_usd.astype(float); win=pnl[pnl>0]; loss=pnl[pnl<0]; eq=pnl.cumsum()
        dd=eq-eq.cummax().clip(lower=0); pf=float(win.sum()/abs(loss.sum())) if len(loss) else None
        by={}
        for sym,p in df.groupby("symbol"):
            v=p.net_pnl_usd.astype(float)
            by[sym]={"trades":len(p),"wins":int((v>0).sum()),"win_rate_pct":round(float((v>0).mean()*100),2),
              "net_pnl_usd":round(float(v.sum()),2),"profit_factor":round(float(v[v>0].sum()/abs(v[v<0].sum())),3) if (v<0).any() else None}
        s={"trades":len(df),"wins":int((pnl>0).sum()),"losses":int((pnl<0).sum()),"win_rate_pct":round(float((pnl>0).mean()*100),2),
          "gross_pnl_usd":round(float(df.gross_pnl_usd.sum()),2),"fees_usd":round(float(df.fees_usd.sum()),2),"net_pnl_usd":round(float(pnl.sum()),2),
          "profit_factor":round(pf,3) if pf is not None else None,"expectancy_usd":round(float(pnl.mean()),3),"expectancy_r":round(float(df.net_r.mean()),3),
          "max_drawdown_usd":round(abs(float(dd.min())),2),"exit_reasons":{str(k):int(v) for k,v in df.exit_reason.value_counts().items()},"by_symbol":by}
    else:
        s={"trades":0,"wins":0,"losses":0,"win_rate_pct":None,"gross_pnl_usd":0,"fees_usd":0,"net_pnl_usd":0,"profit_factor":None,"expectancy_usd":None,"expectancy_r":None,"max_drawdown_usd":0,"exit_reasons":{},"by_symbol":{}}
    s.update({"period_start_utc":iso(start),"period_end_utc":iso(end),"timeframe":"5m","symbols":SYMBOLS,"fee_each_side":FEE,
      "entry":"next candle open after closed-candle signal","stop_target":"1 ATR stop / 1.5 ATR target / 48h expiry",
      "same_candle":"stop first","sizing":"min($10 / stop distance, $200 notional cap)","data_source":"Binance.US public spot OHLCV via CCXT",
      "signal_counts":counts,"candle_data":data,"limitations":["BUY only; SELL signals are not shorts in spot.","No slippage model; 0.1% fee per side.",
      "OHLC candles cannot model order-book fills; stop wins if both stop and target touch.","One open trade per symbol; trades on different symbols can overlap."]})
    (OUT/"summary.json").write_text(json.dumps(s,indent=2),encoding="utf-8"); df.to_csv(OUT/"trades.csv",index=False)
    lines=["# One-year spot backtest — current bot strategy","",f"- Period UTC: {s['period_start_utc']} to {s['period_end_utc']}",
      "- Symbols: BTC/USDT, ETH/USDT, SOL/USDT | timeframe: 5m",f"- Trades: {s['trades']} (wins {s['wins']}, losses {s['losses']})",
      f"- Win rate: {s['win_rate_pct']}%","- Gross PnL: %.2f USD"%s["gross_pnl_usd"],"- Fees: %.2f USD"%s["fees_usd"],
      "- Net PnL: %.2f USD"%s["net_pnl_usd"],f"- Profit factor: {s['profit_factor']}",f"- Expectancy: {s['expectancy_usd']} USD/trade; {s['expectancy_r']} R/trade",
      "- Max drawdown: %.2f USD"%s["max_drawdown_usd"],f"- Exit reasons: {s['exit_reasons']}",f"- Signal counts: {counts}","","## Per symbol","","| Symbol | Trades | Win rate | Net PnL USD | PF |","|---|---:|---:|---:|---:|"]
    for sym,v in s["by_symbol"].items(): lines.append(f"| {sym} | {v['trades']} | {v['win_rate_pct']}% | {v['net_pnl_usd']:.2f} | {v['profit_factor']} |")
    lines += ["","## Limitations",""]+["- "+x for x in s["limitations"]]
    (OUT/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print((OUT/"report.md").read_text(encoding="utf-8"),flush=True)

if __name__=="__main__": main()
