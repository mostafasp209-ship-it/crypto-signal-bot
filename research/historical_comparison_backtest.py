#!/usr/bin/env python3
"""Research-only, approximate historical comparison. Does not place orders."""
import json, time
from datetime import datetime, timezone
import ccxt
import numpy as np
import pandas as pd

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
DAYS = 30
FEE = 0.001
SLIPPAGE = 0.0002
START_USD = 1000.0

def fetch(ex, symbol, timeframe, since):
    rows = []
    cursor = since
    step = ex.parse_timeframe(timeframe) * 1000
    end = ex.milliseconds()
    while cursor < end:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + step
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 2:
            break
        time.sleep(ex.rateLimit / 1000)
    if not rows:
        raise RuntimeError(f"No candles returned for {symbol} {timeframe}")
    df = pd.DataFrame(rows, columns=["ms","open","high","low","close","volume"])
    df = df.drop_duplicates("ms").sort_values("ms")
    df = df[(df.ms >= since) & (df.ms < end)].copy()
    df["timestamp"] = pd.to_datetime(df.ms, unit="ms", utc=True)
    return df.reset_index(drop=True)

def rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = -d.clip(upper=0).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def current_signals(df):
    c,h,l = df.close,df.high,df.low
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["rsi7"] = rsi(c,7)
    lo,hi = l.rolling(5).min(),h.rolling(5).max()
    df["k"] = 100*(c-lo)/(hi-lo+1e-10)
    df["d"] = df.k.rolling(3).mean()
    pc = c.shift(1)
    tr = pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["atr_gate"] = df.atr.rolling(50).mean()*0.8
    hour = df.timestamp.dt.hour
    session = ((hour>=7)&(hour<=11))|((hour>=13)&(hour<=17))
    long = (df.ema20>df.ema50)&(df.rsi7<30)&(df.k<20)&(df.k>df.d)&(df.atr>df.atr_gate)&session
    short = (df.ema20<df.ema50)&(df.rsi7>70)&(df.k>80)&(df.k<df.d)&(df.atr>df.atr_gate)&session
    df["entry_side"] = np.select([long,short],[1,-1],default=0)
    return df

def rl_fallback_signals(df):
    # Faithful approximation of the public strategy's fallback path, NOT its RL agent.
    df["rsi14"] = rsi(df.close,14)
    df["sma20"] = df.close.rolling(20).mean()
    ema12 = df.close.ewm(span=12,adjust=False).mean()
    ema26 = df.close.ewm(span=26,adjust=False).mean()
    df["macd"] = ema12-ema26
    df["macdsig"] = df.macd.ewm(span=9,adjust=False).mean()
    long = (df.rsi14<40)&(df.volume>0)&(df.close>df.sma20)&(df.macd>df.macdsig)
    short = (df.rsi14>60)&(df.volume>0)&(df.close<df.sma20)&(df.macd<df.macdsig)
    df["entry_side"] = np.select([long,short],[1,-1],default=0)
    df["exit_long"] = (df.rsi14>70)|(df.macd<df.macdsig)|(df.close<df.sma20)
    df["exit_short"] = (df.rsi14<30)|(df.macd>df.macdsig)|(df.close>df.sma20)
    return df

def simulate(df, kind):
    # One position per pair; signal is known at bar close, entry at next bar open.
    cash = START_USD
    equity_curve = []
    trades = []
    pos = None
    for i in range(1,len(df)):
        row, prev = df.iloc[i], df.iloc[i-1]
        if pos:
            side=pos["side"]; entry=pos["entry"]; qty=pos["qty"]
            stop_hit = row.low <= pos["stop"] if side==1 else row.high >= pos["stop"]
            target_hit = row.high >= pos["target"] if side==1 else row.low <= pos["target"]
            exit_price=None; reason=None
            if stop_hit:
                exit_price=pos["stop"]; reason="SL"
            elif kind=="current" and target_hit:
                exit_price=pos["target"]; reason="TP"
            elif kind=="rl_fallback" and ((side==1 and bool(prev.exit_long)) or (side==-1 and bool(prev.exit_short))):
                exit_price=row.open; reason="signal_exit"
            elif kind=="rl_fallback" and ((side==1 and row.low<=entry*(1-0.051)) or (side==-1 and row.high>=entry*(1+0.051))):
                exit_price=entry*(1-0.051*side); reason="SL"
            elif (row.timestamp-pos["time"]).total_seconds() > (48*3600 if kind=="current" else 840*3600):
                exit_price=row.open; reason="time_exit"
            if exit_price is not None:
                exit_price *= (1-SLIPPAGE*side)
                gross=(exit_price-entry)*qty*side
                fees=(entry+exit_price)*qty*FEE
                pnl=gross-fees
                cash += pnl
                trades.append({"pnl":pnl,"side":side,"reason":reason,"entry":entry,"exit":exit_price})
                pos=None
        if pos is None and int(prev.entry_side)!=0 and i>1:
            side=int(prev.entry_side)
            entry=float(row.open)*(1+SLIPPAGE*side)
            if kind=="current":
                atr=float(prev.atr) if pd.notna(prev.atr) else np.nan
                if not np.isfinite(atr) or atr<=0:
                    equity_curve.append(cash); continue
                stop=entry-side*atr
                target=entry+side*1.5*atr
            else:
                stop=entry*(1-0.051*side)
                target=entry*(1+0.228*side)
            qty= (START_USD*0.01 / max(abs(entry-stop),1e-12))
            qty=min(qty, START_USD/entry)
            pos={"side":side,"entry":entry,"stop":stop,"target":target,"qty":qty,"time":row.timestamp}
        equity_curve.append(cash)
    if pos:
        last=df.iloc[-1]; exit_price=float(last.close)*(1-SLIPPAGE*pos["side"])
        pnl=(exit_price-pos["entry"])*pos["qty"]*pos["side"]-(pos["entry"]+exit_price)*pos["qty"]*FEE
        cash+=pnl
        trades.append({"pnl":pnl,"side":pos["side"],"reason":"end_mark","entry":pos["entry"],"exit":exit_price})
    pnls=[t["pnl"] for t in trades]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    curve=np.array([START_USD]+[START_USD+sum(pnls[:i+1]) for i in range(len(pnls))],dtype=float)
    peaks=np.maximum.accumulate(curve)
    dd=((peaks-curve)/np.maximum(peaks,1e-12)).max()*100 if len(curve) else 0
    return {"trades":len(pnls),"wins":len(wins),"losses":len(losses),
            "win_rate_pct":round(100*len(wins)/len(pnls),2) if pnls else None,
            "net_pnl_usd":round(sum(pnls),4),"return_pct":round(100*sum(pnls)/START_USD,3),
            "profit_factor":round(sum(wins)/abs(sum(losses)),3) if losses else ("inf" if wins else None),
            "max_drawdown_trade_sequence_pct":round(float(dd),3),
            "fees_rate_per_side":FEE,"slippage_rate_per_side":SLIPPAGE,
            "note":"Approximate OHLCV simulation; stop checked before target when both touched."}

def main():
    ex=ccxt.kraken({"enableRateLimit":True,"timeout":20000})
    since=ex.milliseconds()-DAYS*24*60*60*1000
    report={"generated_at":datetime.now(timezone.utc).isoformat(),"days":DAYS,
            "symbols":SYMBOLS,"fee_per_side":FEE,"slippage_per_side":SLIPPAGE,
            "starting_capital_per_pair_usd":START_USD,
            "warning":"RL result below is the strategy's public fallback rules, NOT a trained RL agent; no reproducible model artifact was available.",
            "results":{}}
    for symbol in SYMBOLS:
        for kind,tf,fn in [("current","5m",current_signals),("rl_fallback","1h",rl_fallback_signals)]:
            df=fetch(ex,symbol,tf,since)
            df=fn(df)
            key=f"{symbol}:{kind}:{tf}"
            report["results"][key]=simulate(df,kind)
            print(key,report["results"][key],flush=True)
    with open("comparison-backtest-report.json","w") as f: json.dump(report,f,indent=2)
    print("WROTE comparison-backtest-report.json",flush=True)

if __name__=="__main__":
    main()
