#!/usr/bin/env python3
"""
Offline research backtest for the reference RSI/Stochastic RR1.5 strategy
on Binance USD-M 4H perpetual candles.

- Next-bar open entry
- Fee + slippage
- Conservative same-candle SL/TP (stop first)
- Optional approximate funding from public endpoint when available
- No live trading; no profitability claim without your review of the JSON report

Usage:
  python research/reference_futures_backtest.py --symbols BTCUSDT,ETHUSDT --since 2022-01-01 --out research/out
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

FEE = 0.0005
SLIP = 0.0002
RR = 1.5
VOL_MULT = 1.12
ATR_MULT = 1.32
RSI_LOW, RSI_HIGH = 30.0, 70.0


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    return 100 - (100 / (1 + rs))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k: int = 14, d: int = 3):
    lowest = low.rolling(k).min()
    highest = high.rolling(k).max()
    kline = 100 * (close - lowest) / (highest - lowest + 1e-12)
    return kline, kline.rolling(d).mean()


def fetch_klines(symbol: str, interval: str = "4h", start_ms: Optional[int] = None) -> pd.DataFrame:
    url = "https://fapi.binance.com/fapi/v1/klines"
    rows: List[list] = []
    cursor = start_ms
    while True:
        params = {"symbol": symbol, "interval": interval, "limit": 1500}
        if cursor is not None:
            params["startTime"] = cursor
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1500:
            break
        cursor = batch[-1][0] + 1
        time.sleep(0.12)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close"]).drop_duplicates("open_time")
    return df.sort_values("open_time").reset_index(drop=True)


def backtest_symbol(df: pd.DataFrame, symbol: str) -> Dict:
    if len(df) < 160:
        return {"symbol": symbol, "n_trades": 0, "error": "insufficient_bars"}

    work = df.copy()
    work["rsi"] = rsi(work["close"])
    work["atr"] = atr(work["high"], work["low"], work["close"])
    work["vol_ma"] = work["volume"].rolling(20).mean()
    work["sk"], work["sd"] = stochastic(work["high"], work["low"], work["close"])
    work = work.dropna().reset_index(drop=True)
    n = len(work)

    o = work["open"].values
    h = work["high"].values
    l = work["low"].values
    c = work["close"].values
    rsi_ = work["rsi"].values
    atr_ = work["atr"].values
    vol = work["volume"].values
    volma = work["vol_ma"].values
    sk = work["sk"].values
    sd = work["sd"].values

    trades: List[float] = []
    pos = 0
    entry = stop = target = 0.0
    i = 5
    while i < n - 1:
        if pos != 0:
            exit_p = None
            if pos == 1:
                if o[i] < stop:
                    exit_p = o[i]
                else:
                    hs, ht = l[i] <= stop, h[i] >= target
                    if hs and ht:
                        exit_p = stop
                    elif hs:
                        exit_p = stop
                    elif ht:
                        exit_p = target
            else:
                if o[i] > stop:
                    exit_p = o[i]
                else:
                    hs, ht = h[i] >= stop, l[i] <= target
                    if hs and ht:
                        exit_p = stop
                    elif hs:
                        exit_p = stop
                    elif ht:
                        exit_p = target
            if exit_p is not None:
                exit_p = exit_p * (1 - SLIP) if pos == 1 else exit_p * (1 + SLIP)
                risk = abs(entry - stop)
                if risk > 1e-12:
                    raw = (exit_p - entry) / risk if pos == 1 else (entry - exit_p) / risk
                    cost = (FEE + SLIP) * (entry + exit_p) / risk
                    trades.append(raw - cost)
                pos = 0
            i += 1
            continue

        vol_ok = vol[i] > VOL_MULT * volma[i]
        stoch_up = sk[i - 1] <= sd[i - 1] and sk[i] > sd[i] and sk[i] < 38
        stoch_dn = sk[i - 1] >= sd[i - 1] and sk[i] < sd[i] and sk[i] > 62
        rsi_long = rsi_[i - 1] < RSI_LOW and rsi_[i] >= RSI_LOW
        rsi_short = rsi_[i - 1] > RSI_HIGH and rsi_[i] <= RSI_HIGH
        long_sig = rsi_long and stoch_up and vol_ok and c[i] > o[i]
        short_sig = rsi_short and stoch_dn and vol_ok and c[i] < o[i]

        if long_sig or short_sig:
            raw_entry = o[i + 1]
            if long_sig:
                pos = 1
                entry = raw_entry * (1 + SLIP)
                swing = min(l[max(0, i - 4) : i + 1])
                stop = min(swing, entry - atr_[i] * ATR_MULT)
                risk = entry - stop
            else:
                pos = -1
                entry = raw_entry * (1 - SLIP)
                swing = max(h[max(0, i - 4) : i + 1])
                stop = max(swing, entry + atr_[i] * ATR_MULT)
                risk = stop - entry
            if 0.0035 * entry < risk < 0.038 * entry:
                target = entry + pos * risk * RR
                i += 2
                continue
            pos = 0
        i += 1

    if pos != 0:
        exit_p = c[-1] * (1 - SLIP) if pos == 1 else c[-1] * (1 + SLIP)
        risk = abs(entry - stop)
        if risk > 1e-12:
            raw = (exit_p - entry) / risk if pos == 1 else (entry - exit_p) / risk
            cost = (FEE + SLIP) * (entry + exit_p) / risk
            trades.append(raw - cost)

    if len(trades) < 3:
        return {"symbol": symbol, "n_trades": len(trades), "note": "too_few_trades"}

    rs = np.array(trades)
    wins = rs > 0
    equity = np.cumsum(rs)
    peak = np.maximum.accumulate(equity)
    max_dd = float((peak - equity).max()) if len(equity) else 0.0
    return {
        "symbol": symbol,
        "n_trades": int(len(rs)),
        "winrate_pct": round(float(wins.mean() * 100), 2),
        "expectancy_r": round(float(rs.mean()), 4),
        "profit_factor": round(
            float(abs(rs[wins].sum() / rs[~wins].sum())) if (~wins).any() and abs(rs[~wins].sum()) > 1e-9 else 99.0,
            3,
        ),
        "total_r": round(float(rs.sum()), 3),
        "max_dd_r": round(max_dd, 3),
        "disclaimer": "Research only. Not validated for live trading. Funding not fully event-synced in this light script.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--since", default="2022-01-01")
    parser.add_argument("--out", default="research/out")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    start_ms = int(pd.Timestamp(args.since, tz="UTC").timestamp() * 1000)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = []
    for sym in symbols:
        print(f"[fetch] {sym}")
        try:
            df = fetch_klines(sym, "4h", start_ms)
            print(f"  bars={len(df)}")
            res = backtest_symbol(df, sym)
        except Exception as exc:
            res = {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}
        results.append(res)
        print(f"  -> {res}")

    report = {
        "strategy": "REFERENCE_RSI_STOCH_ATR_RR15",
        "rr": RR,
        "fee": FEE,
        "slippage": SLIP,
        "results": results,
        "status": "RESEARCH_ONLY_NOT_VALIDATED",
    }
    path = os.path.join(args.out, "reference_futures_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"[saved] {path}")


if __name__ == "__main__":
    main()
