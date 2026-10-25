#!/usr/bin/env python3
"""Chronological train/test grid for long-only spot entries with fixed 3R target."""
import json, time
from datetime import datetime, timezone
from pathlib import Path
import ccxt, numpy as np, pandas as pd
SYMBOLS=["BTC/USDT","ETH/USDT","SOL/USDT"]; BAR=300000; FEE=.001; SLIP=.0002
OUT=Path("optimization-results"); OUT.mkdir(exist_ok=True)
def iso(ms): return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
def fetch(ex,symbol,start,end):
 rows=[]; since=start; last=None
 while since<end:
  for attempt in range(6):
   try: batch=ex.fetch_ohlcv(symbol,timeframe="5m",since=since,limit=1000); break
   except Exception:
    if attempt==5: raise
    time.sleep(min(2**attempt,20))
  if not batch: break
  fresh=[x for x in batch if start<=int(x[0])<end and (last is None or int(x[0])>last)]
  if fresh: rows.extend(fresh); last=int(fresh[-1][0]); since=last+BAR
  else: since=max(since+BAR,int(batch[-1][0])+BAR)
  if len(batch)<1000 and int(batch[-1][0])>=end-BAR: break
 if not rows: raise RuntimeError("No OHLCV history for "+symbol)
 d=pd.DataFrame(rows,columns=["timestamp","open","high","low","close","volume"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
 for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
 d=d.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
 print("[data]",symbol,len(d),flush=True); return d
def features(raw):
 d=raw.copy(); c,h,l,o,v=d.close,d.high,d.low,d.open,d.volume
 for n in [20,50,200]: d["ema"+str(n)]=c.ewm(span=n,adjust=False).mean()
 delta=c.diff(); gain=delta.clip(lower=0).rolling(7).mean(); loss=-delta.clip(upper=0).rolling(7).mean()
 d["rsi"]=100-100/(1+gain/loss.replace(0,np.nan))
 pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
 d["atr"]=tr.rolling(14).mean(); d["atr_pct"]=d.atr/c
 d["body_pct"]=(c-o).abs()/(h-l+1e-12); d["close_loc"]=(c-l)/(h-l+1e-12)
 d["prior_high"]=h.shift(1).rolling(20).max(); d["prior_low"]=l.shift(1).rolling(20).min()
 d["vol_ratio"]=v/(v.rolling(20).mean()+1e-12); d["slope"]=d.ema50.pct_change(6)
 d["pull20"]=(c>d.ema200)&(d.ema50>d.ema200)&(d.slope>0)&(l<=d.ema20)&(c>o)&(d.close_loc>=.65)
 d["pull50"]=(c>d.ema200)&(d.ema50>d.ema200)&(d.slope>0)&(l<=d.ema50)&(c>o)&(d.close_loc>=.65)
 d["breakout"]=(c>d.prior_high)&(c>o)&(d.body_pct>=.55)&(d.vol_ratio>=1.1)&(c>d.ema200)
 d["support"]=(l<=d.prior_low*1.003)&(c>d.prior_low)&(c>o)&(d.close_loc>=.7)&(d.rsi<55)&(c>d.ema50)
 k=100*(c-l.rolling(5).min())/(h.rolling(5).max()-l.rolling(5).min()+1e-12); kd=k.rolling(3).mean()
 d["baseline"]=(d.ema20>d.ema50)&(d.rsi<30)&(k<20)&(k>kd)&(d.atr>d.atr.rolling(50).mean()*.8)
 return d
CANDIDATES=[{"setup":s,"rsi_min":r0,"rsi_max":r1,"atr_min":a,"sl_mult":m,"rr":3}
 for s in ["pull20","pull50","breakout","support","baseline"]
 for r0,r1 in [(30,70),(35,60),(40,60),(45,65)]
 for a in [.0005,.001,.0015] for m in [1.,1.25,1.5]]
def run_one(d,p,start_i,end_i):
 sig=(d[p["setup"]].fillna(False)&d.rsi.between(p["rsi_min"],p["rsi_max"])&(d.atr_pct>=p["atr_min"])).fillna(False).to_numpy()
 trades=[]; i=max(1,start_i)
 while i<min(end_i,len(d)-2):
  if not sig[i] or not np.isfinite(d.atr.iloc[i]) or d.atr.iloc[i]<=0: i+=1; continue
  ent=i+1; entry=float(d.open.iloc[ent]); dist=float(d.atr.iloc[i])*p["sl_mult"]; sl=entry-dist; tp=entry+3*dist
  qty=min(10/dist,200/entry); last=min(end_i-1,ent+576); ex=None; reason="EXPIRY"
  for j in range(ent,last+1):
   if float(d.low.iloc[j])<=sl: ex=sl; reason="SL"; last=j; break
   if float(d.high.iloc[j])>=tp: ex=tp; reason="TP"; last=j; break
  if ex is None: ex=float(d.close.iloc[last])
  gross=(ex-entry)*qty; costs=(entry+ex)*qty*(FEE+SLIP); trades.append({"net":gross-costs,"r":(gross-costs)/(qty*dist),"reason":reason})
  i=last+1
 if not trades: return {"trades":0,"win_rate_pct":None,"net_pnl_usd":0,"profit_factor":None,"expectancy_r":None,"max_drawdown_usd":0,"tp_pct":0}
 v=np.array([x["net"] for x in trades]); rs=np.array([x["r"] for x in trades]); w=v[v>0]; z=v[v<0]; eq=v.cumsum(); dd=eq-np.maximum.accumulate(np.maximum(eq,0))
 return {"trades":len(v),"win_rate_pct":round((v>0).mean()*100,2),"net_pnl_usd":round(v.sum(),2),
 "profit_factor":round(w.sum()/abs(z.sum()),3) if len(z) else None,"expectancy_r":round(rs.mean(),3),
 "max_drawdown_usd":round(abs(dd.min()),2),"tp_pct":round(sum(x["reason"]=="TP" for x in trades)/len(trades)*100,2)}
def main():
 end=int(datetime.now(timezone.utc).timestamp()*1000); end-=end%BAR; start=end-365*86400000; split=start+int((end-start)*.7)
 ex=ccxt.binanceus({"enableRateLimit":True,"timeout":30000}); ex.load_markets(); data={}
 for sym in SYMBOLS:
  if sym in ex.markets: data[sym]=features(fetch(ex,sym,start-250*BAR,end))
 rows=[]
 for n,p in enumerate(CANDIDATES,1):
  pieces={"train":[],"test":[]}
  for sym,d in data.items():
   ts=d.timestamp.to_numpy(); a=max(1,int(np.searchsorted(ts,start))); b=int(np.searchsorted(ts,split)); z=len(d)-1
   for part,lo,hi in [("train",a,b),("test",b,z)]:
    # preserve chronological trade order per symbol; combine summaries below
    pieces[part].append(run_one(d,p,lo,hi))
  row={**p}
  for part in ["train","test"]:
   vals=[x for x in pieces[part] if x["trades"]]
   # approximate aggregate using summed PnL and trade-count-weighted win rate/expectancy
   nt=sum(x["trades"] for x in vals)
   row[part]={"trades":nt,"net_pnl_usd":round(sum(x["net_pnl_usd"] for x in vals),2),
    "win_rate_pct":round(sum(x["win_rate_pct"]*x["trades"] for x in vals)/nt,2) if nt else None,
    "profit_factor":None,"expectancy_r":round(sum(x["expectancy_r"]*x["trades"] for x in vals)/nt,3) if nt else None,
    "max_drawdown_usd":round(sum(x["max_drawdown_usd"] for x in vals),2),
    "tp_pct":round(sum(x["tp_pct"]*x["trades"] for x in vals)/nt,2) if nt else 0}
  # PF from total net / approximate downside cannot be reliably aggregated from summary, compute from candidates separately below
  rows.append(row)
  if n%50==0: print("[grid]",n,"/",len(CANDIDATES),flush=True)
 # Re-run candidate trades with compact per-trade data to calculate valid pooled PF and pooled drawdown.
 for row in rows:
  p={k:row[k] for k in ["setup","rsi_min","rsi_max","atr_min","sl_mult","rr"]}
  for part in ["train","test"]:
   allv=[]
   for sym,d in data.items():
    ts=d.timestamp.to_numpy(); lo=max(1,int(np.searchsorted(ts,start))) if part=="train" else int(np.searchsorted(ts,split))
    hi=int(np.searchsorted(ts,split)) if part=="train" else len(d)-1
    sig=(d[p["setup"]].fillna(False)&d.rsi.between(p["rsi_min"],p["rsi_max"])&(d.atr_pct>=p["atr_min"])).fillna(False).to_numpy(); i=lo
    while i<min(hi,len(d)-2):
     if not sig[i] or not np.isfinite(d.atr.iloc[i]) or d.atr.iloc[i]<=0: i+=1; continue
     ent=i+1; entry=float(d.open.iloc[ent]); dist=float(d.atr.iloc[i])*p["sl_mult"]; sl=entry-dist; tp=entry+3*dist; qty=min(10/dist,200/entry); last=min(hi-1,ent+576); ex=None
     for j in range(ent,last+1):
      if float(d.low.iloc[j])<=sl: ex=sl; last=j; break
      if float(d.high.iloc[j])>=tp: ex=tp; last=j; break
     if ex is None: ex=float(d.close.iloc[last])
     allv.append((ex-entry)*qty-(entry+ex)*qty*(FEE+SLIP)); i=last+1
   if allv:
    v=np.array(allv); w=v[v>0]; z=v[v<0]; row[part]["profit_factor"]=round(float(w.sum()/abs(z.sum())),3) if len(z) else None
 # Choose only using training set, not test.
 eligible=[x for x in rows if x["train"]["trades"]>=30 and x["train"]["profit_factor"] and x["train"]["profit_factor"]>1 and x["train"]["expectancy_r"]>0]
 eligible.sort(key=lambda x:(x["train"]["expectancy_r"],x["train"]["profit_factor"],x["train"]["trades"]),reverse=True)
 testrank=sorted(rows,key=lambda x:((x["test"]["expectancy_r"] if x["test"]["expectancy_r"] is not None else -999),(x["test"]["profit_factor"] or -1)),reverse=True)
 result={"period_start_utc":iso(start),"period_end_utc":iso(end),"train_until_utc":iso(split),"train_fraction":.7,"symbols":SYMBOLS,"timeframe":"5m",
 "candidate_count":len(rows),"fixed_rr":3,"fee_each_side":FEE,"slippage_each_side":SLIP,
 "selection_rule":"training only: at least 30 trades, PF > 1 and positive expectancy; test held out",
 "best_train":eligible[:20],"best_test_diagnostic":testrank[:20],"all_candidates":rows,
 "caveats":["Research grid can overfit even with chronological split.","Held-out results are not a guarantee of future performance.",
 "Stop is assumed first if stop and target are both touched within one candle.","Only long spot trades; symbols can have concurrent positions."]}
 (OUT/"optimization.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
 flat=[]
 for x in rows:
  flat.append({**{k:x[k] for k in ["setup","rsi_min","rsi_max","atr_min","sl_mult","rr"]},
   **{"train_"+k:v for k,v in x["train"].items()},**{"test_"+k:v for k,v in x["test"].items()}})
 pd.DataFrame(flat).to_csv(OUT/"candidates.csv",index=False)
 def table(items,key):
  a=["| Setup | RSI | ATR min | SL ATR | Trades | Win rate | PF | Expectancy R | Net PnL USD |","|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
  for x in items:
   s=x[key]; a.append("| %s | %s–%s | %.4f | %.2f | %s | %s%% | %s | %s | %.2f |"%(x["setup"],x["rsi_min"],x["rsi_max"],x["atr_min"],x["sl_mult"],s["trades"],s["win_rate_pct"],s["profit_factor"],s["expectancy_r"],s["net_pnl_usd"]))
  return a
 lines=["# 3R strategy optimization","", "Period: "+iso(start)+" to "+iso(end),"Train/test boundary: "+iso(split)+" (70/30 chronological)",
 "Candidates: "+str(len(rows))+"; target fixed at 3R","Fees: 0.1% each side; adverse slippage: 0.02% each side",
 "Entry at next candle open; stop-first when SL and TP touch in same candle.","",
 "## Candidates selected on training only",""]+table(eligible[:20],"train") if eligible else ["# 3R strategy optimization","","No candidate passed the minimum training criteria."]
 lines += ["","## Held-out test leaders (diagnostic only; not used to select parameters)",""]+table(testrank[:20],"test")
 lines += ["","## Caveats",""]+["- "+x for x in result["caveats"]]
 (OUT/"report.md").write_text("\\n".join(lines)+"\\n",encoding="utf-8")
 print((OUT/"report.md").read_text(encoding="utf-8"),flush=True)
if __name__=="__main__": main()
