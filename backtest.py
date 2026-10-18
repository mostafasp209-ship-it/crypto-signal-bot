"""Conservative historical backtest for the spot price-action strategy.

This is a research tool, not a profitability guarantee. It uses only candles
available at each decision point, enters at the next 4h candle open, charges
entry/exit fees, and resolves same-candle stop/target ambiguity pessimistically.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
from datetime import datetime, timezone

import ccxt
import pandas as pd

import bot


FOUR_HOURS_MS = 4 * 60 * 60 * 1000
ONE_DAY_MS = 24 * 60 * 60 * 1000


def closed_historical_frame(raw, duration_ms, now_ms=None):
    """Return only fully closed OHLCV candles; preserve historical rows."""
    now_ms = int(now_ms or datetime.now(timezone.utc).timestamp() * 1000)
    if not raw:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame[frame["timestamp"] + duration_ms <= now_ms].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame.dropna(subset=["open", "high", "low", "close"], inplace=True)
    frame.sort_values("timestamp", inplace=True)
    frame.drop_duplicates("timestamp", keep="last", inplace=True)
    return frame.reset_index(drop=True)


def daily_trend_asof(daily, close_time_ms):
    available = daily[daily["timestamp"] + ONE_DAY_MS <= close_time_ms]
    if len(available) < 60:
        return "UNKNOWN"
    closes = available["close"].astype(float)
    ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    last = float(closes.iloc[-1])
    if last > ema50:
        return "BULL"
    if last < ema50:
        return "BEAR"
    return "NEUTRAL"


def simulate_trade(signal, candles, start_index, max_bars=12):
    """Simulate TP 50/30/20 with breakeven stop after TP1; return net PnL."""
    entry = float(signal["entry"])
    sl = float(signal["sl"])
    tp1, tp2, tp3 = float(signal["tp1"]), float(signal["tp2"]), float(signal["tp3"])
    quantity = float(signal["position_quantity"])
    if quantity <= 0 or entry <= 0:
        return {"pnl": 0.0, "exit_index": start_index, "reason": "invalid_size"}

    remaining = quantity
    realized = -entry * quantity * bot.FEE_RATE
    tp1_hit = tp2_hit = False
    last_index = min(len(candles) - 1, start_index + max_bars - 1)

    for j in range(start_index, last_index + 1):
        row = candles.iloc[j]
        low, high = float(row["low"]), float(row["high"])
        active_stop = entry if tp1_hit else sl

        # Conservative OHLC assumption: if stop and target are both touched,
        # assume the stop happened first.
        if low <= active_stop:
            realized += (active_stop - entry) * remaining
            realized -= active_stop * remaining * bot.FEE_RATE
            return {"pnl": realized, "exit_index": j, "reason": "breakeven" if tp1_hit else "SL"}

        if not tp1_hit and high >= tp1:
            sold = min(quantity * 0.50, remaining)
            realized += (tp1 - entry) * sold - tp1 * sold * bot.FEE_RATE
            remaining -= sold
            tp1_hit = True
            # Do not assume TP2/TP3 happened later in the same candle.
            continue

        if tp1_hit and not tp2_hit and high >= tp2:
            sold = min(quantity * 0.30, remaining)
            realized += (tp2 - entry) * sold - tp2 * sold * bot.FEE_RATE
            remaining -= sold
            tp2_hit = True
            continue

        if tp2_hit and high >= tp3:
            sold = min(remaining, quantity * 0.20)
            realized += (tp3 - entry) * sold - tp3 * sold * bot.FEE_RATE
            remaining -= sold
            realized -= 0.0
            return {"pnl": realized, "exit_index": j, "reason": "TP3"}

    exit_price = float(candles.iloc[last_index]["close"])
    if remaining > 0:
        realized += (exit_price - entry) * remaining
        realized -= exit_price * remaining * bot.FEE_RATE
    return {"pnl": realized, "exit_index": last_index, "reason": "expiry"}


def backtest_symbol(exchange, symbol, limit=700):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    raw_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=limit)
    raw_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=min(500, max(120, limit)))
    candles = closed_historical_frame(raw_4h, FOUR_HOURS_MS, now_ms)
    daily = closed_historical_frame(raw_1d, ONE_DAY_MS, now_ms)
    if len(candles) < 230 or len(daily) < 60:
        return {"symbol": symbol, "bars": len(candles), "trades": [], "error": "insufficient_history"}

    trades = []
    i = 205
    while i < len(candles) - 1:
        history = candles.iloc[:i + 1].copy()
        signal_trend = daily_trend_asof(daily, int(history.iloc[-1]["timestamp"]) + FOUR_HOURS_MS)
        if signal_trend == "UNKNOWN":
            i += 1
            continue

        # Suppress strategy diagnostic logs during the historical loop.
        with contextlib.redirect_stdout(io.StringIO()):
            signal = bot.build_signal(
                symbol, exchange.id, history, signal_trend, {},
                {"score_adjustment": 0, "label": "backtest", "headlines": []},
            )
        if not signal:
            i += 1
            continue

        # Conservative execution: fill at the next candle's open, not the signal close.
        entry = float(candles.iloc[i + 1]["open"])
        enriched = bot.add_indicators(history)
        atr = float(enriched.iloc[-1]["atr"])
        with contextlib.redirect_stdout(io.StringIO()):
            levels = bot.calculate_levels("BUY", entry, atr, history)
        if not levels:
            i += 1
            continue

        net_risk = float(levels["net_risk_per_unit"])
        if net_risk <= 0:
            i += 1
            continue
        qty_by_risk = bot.RISK_USD / net_risk
        qty_by_cap = bot.MAX_POSITION_USD / entry if bot.MAX_POSITION_USD > 0 else qty_by_risk
        quantity = min(qty_by_risk, qty_by_cap)
        simulated_signal = {
            "entry": entry, "sl": levels["sl"], "tp1": levels["tp1"],
            "tp2": levels["tp2"], "tp3": levels["tp3"],
            "position_quantity": quantity,
        }
        outcome = simulate_trade(simulated_signal, candles, i + 1)
        trades.append({
            "symbol": symbol, "signal_time": datetime.fromtimestamp(
                int(history.iloc[-1]["timestamp"]) / 1000, timezone.utc
            ).isoformat(),
            "entry": entry, "sl": levels["sl"], "tp1": levels["tp1"],
            "tp2": levels["tp2"], "tp3": levels["tp3"],
            "score": int(signal["score"]), "setup": signal.get("components", {}).get("setup", "unknown"),
            "rr": float(levels["rr"]), "pnl": round(float(outcome["pnl"]), 6),
            "exit_reason": outcome["reason"],
            "exit_time": datetime.fromtimestamp(
                int(candles.iloc[outcome["exit_index"]]["timestamp"]) / 1000, timezone.utc
            ).isoformat(),
        })
        # Do not allow overlapping positions in the same symbol.
        i = max(i + 1, int(outcome["exit_index"]) + 1)

    return {"symbol": symbol, "bars": len(candles), "trades": trades, "error": None}


def summarize(results):
    trades = [trade for result in results for trade in result.get("trades", [])]
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "symbols": [r["symbol"] for r in results],
        "bars_by_symbol": {r["symbol"]: r["bars"] for r in results},
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trades), 2) if trades else 0.0,
        "net_pnl_usd": round(sum(pnls), 4),
        "expectancy_usd_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if not gross_profit else "infinite"),
        "max_drawdown_usd": round(max_drawdown, 4),
        "note": "Exploratory backtest only; not proof of future profitability. Use out-of-sample and multiple market regimes.",
        "trade_details": trades,
        "errors": [{"symbol": r["symbol"], "error": r["error"]} for r in results if r.get("error")],
    }


def main():
    parser = argparse.ArgumentParser(description="Historical backtest for the spot price-action strategy")
    parser.add_argument("--exchange", default="binanceus")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    parser.add_argument("--limit", type=int, default=700)
    parser.add_argument("--output", default="backtest-results.json")
    args = parser.parse_args()

    exchange_class = getattr(ccxt, args.exchange)
    exchange = exchange_class({"enableRateLimit": True, "timeout": bot.REQUEST_TIMEOUT_MS})
    exchange.load_markets()
    results = []
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        if symbol not in exchange.markets:
            results.append({"symbol": symbol, "bars": 0, "trades": [], "error": "market_unavailable"})
            continue
        try:
            result = backtest_symbol(exchange, symbol, limit=max(250, min(args.limit, 1000)))
            results.append(result)
            print(f"[BACKTEST] {symbol}: bars={result['bars']} trades={len(result['trades'])} error={result.get('error')}")
        except Exception as exc:
            results.append({"symbol": symbol, "bars": 0, "trades": [], "error": f"{type(exc).__name__}: {exc}"})
            print(f"[BACKTEST ERROR] {symbol}: {type(exc).__name__}: {exc}")

    report = summarize(results)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "trade_details"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
