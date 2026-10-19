"""Walk-forward, OHLCV-only backtest for the spot BUY setup engine.

Input CSVs must contain timestamp, open, high, low, close, volume. Timestamps
may be ISO-8601 or Unix milliseconds and represent candle OPEN time. Daily
candles are only usable after their full 24-hour interval has closed.

This is a research harness, not a profitability guarantee. It models next-bar
open entries, 50/30/20 exits, stop-first ambiguity, 0.1% fees per side, and
48-hour expiry. It does not model spread, slippage, partial fills, or exchange
outages; treat results as optimistic unless you add those costs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import pandas as pd

import bot

FEE_RATE = 0.001
TP_FRACTIONS = ((1, 0.50), (2, 0.30), (3, 0.20))
MAX_HOLD_BARS = 12  # 48 hours on 4h candles


def load_ohlcv(path: str, timeframe_hours: int) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")

    raw_ts = df["timestamp"]
    if pd.api.types.is_numeric_dtype(raw_ts):
        ts = pd.to_datetime(raw_ts, unit="ms", utc=True, errors="coerce")
    else:
        ts = pd.to_datetime(raw_ts, utc=True, errors="coerce")
        # Numeric-looking timestamps sometimes arrive as strings.
        if ts.isna().all():
            ts = pd.to_datetime(pd.to_numeric(raw_ts, errors="coerce"), unit="ms", utc=True)

    df["timestamp"] = ts
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    if len(df) < 210:
        raise ValueError(f"{path}: need at least 210 valid candles; found {len(df)}")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"{path}: OHLC prices must be positive")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError(f"{path}: invalid candle where high is below another OHLC value")
    if (df["low"] > df[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{path}: invalid candle where low is above another OHLC value")
    df.attrs["timeframe_hours"] = timeframe_hours
    return df


def daily_trend_at(daily: pd.DataFrame, signal_close_time: pd.Timestamp) -> str:
    # A daily candle is known only after its open time + 24 hours.
    eligible = daily[daily["timestamp"] + pd.Timedelta(hours=24) <= signal_close_time]
    if len(eligible) < 51:
        return "UNKNOWN"
    close = eligible["close"]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    last_close = float(close.iloc[-1])
    if last_close > ema50:
        return "BULL"
    if last_close < ema50:
        return "BEAR"
    return "NEUTRAL"


def simulate_trade(signal: dict, bars: pd.DataFrame, signal_index: int, symbol: str) -> dict | None:
    """Enter at the next candle open and process subsequent candles conservatively."""
    if signal_index + 1 >= len(bars):
        return None
    entry_bar = bars.iloc[signal_index + 1]
    entry = float(entry_bar["open"])
    stop = float(signal["sl"])
    targets = [float(signal["tp1"]), float(signal["tp2"]), float(signal["tp3"])]
    resistance = bot.safe_float(signal.get("resistance"))
    if entry <= stop or any(t <= entry for t in targets):
        return None

    initial_risk = entry - stop
    if resistance and resistance > entry:
        if (resistance - entry) / initial_risk < bot.MIN_RESISTANCE_ROOM_R:
            return None

    remaining = 1.0
    net_pnl = -FEE_RATE  # Entry fee on the whole notional, expressed as return on initial notional.
    gross_pnl = 0.0
    fees = FEE_RATE
    hits = [False, False, False]
    exit_reason = "DATA_END"
    exit_time = bars.iloc[-1]["timestamp"]
    exit_price = float(bars.iloc[-1]["close"])
    last_index = min(len(bars) - 1, signal_index + MAX_HOLD_BARS + 1)

    for j in range(signal_index + 1, last_index + 1):
        bar = bars.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])

        # If stop and any still-relevant target are both touched, assume stop first.
        if low <= stop:
            exit_price = stop
            exit_time = bar["timestamp"]
            gross_pnl += remaining * (exit_price / entry - 1.0)
            exit_fee = remaining * FEE_RATE
            fees += exit_fee
            net_pnl += remaining * (exit_price / entry - 1.0) - exit_fee
            remaining = 0.0
            exit_reason = "SL_AFTER_TP" if any(hits) else "SL"
            break

        for target_index, (target_number, fraction) in enumerate(TP_FRACTIONS):
            if hits[target_index] or high < targets[target_index]:
                continue
            # Fixed 50/30/20 of the original position; the final target closes the remainder.
            close_fraction = min(fraction, remaining)
            target_price = targets[target_index]
            gross_pnl += close_fraction * (target_price / entry - 1.0)
            exit_fee = close_fraction * FEE_RATE
            fees += exit_fee
            net_pnl += close_fraction * (target_price / entry - 1.0) - exit_fee
            remaining -= close_fraction
            hits[target_index] = True
            if target_index == 0:
                stop = entry  # Move stop to breakeven after TP1.
            if remaining <= 1e-9:
                remaining = 0.0
                exit_price = target_price
                exit_time = bar["timestamp"]
                exit_reason = "TP3"
                break
        if remaining <= 1e-9:
            break
        if j == last_index:
            exit_price = float(bar["close"])
            exit_time = bar["timestamp"]
            gross_pnl += remaining * (exit_price / entry - 1.0)
            exit_fee = remaining * FEE_RATE
            fees += exit_fee
            net_pnl += remaining * (exit_price / entry - 1.0) - exit_fee
            remaining = 0.0
            exit_reason = "EXPIRY" if last_index == signal_index + MAX_HOLD_BARS + 1 else "DATA_END"

    risk_pct = initial_risk / entry
    return {
        "symbol": symbol,
        "setup": signal.get("components", {}).get("setup", "UNKNOWN"),
        "regime": signal.get("components", {}).get("market_regime", "UNKNOWN"),
        "score": int(signal.get("score", 0)),
        "signal_time": bars.iloc[signal_index]["timestamp"].isoformat(),
        "entry_time": entry_bar["timestamp"].isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry": entry,
        "initial_sl": float(signal["sl"]),
        "tp1": targets[0],
        "tp2": targets[1],
        "tp3": targets[2],
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "tp1_hit": hits[0],
        "tp2_hit": hits[1],
        "tp3_hit": hits[2],
        "gross_return_pct": gross_pnl * 100.0,
        "fees_pct": fees * 100.0,
        "net_return_pct": net_pnl * 100.0,
        "r_multiple_net": net_pnl / risk_pct if risk_pct > 0 else 0.0,
    }


def run_backtest(four_hour: pd.DataFrame, daily: pd.DataFrame, symbol: str) -> list[dict]:
    trades = []
    i = 205
    while i < len(four_hour) - 1:
        candle = four_hour.iloc[i]
        signal_close_time = candle["timestamp"] + pd.Timedelta(hours=4)
        trend = daily_trend_at(daily, signal_close_time)
        if trend == "UNKNOWN":
            i += 1
            continue

        history = four_hour.iloc[: i + 1].copy()
        # The current 4h candle is included only after its close; build_signal itself
        # uses no bars after this point.
        signal = bot.build_signal(
            symbol=symbol,
            exchange_name="backtest",
            df=history,
            trend=trend,
            fundamentals={},
            news_context={"score_adjustment": 0, "label": "backtest_no_news", "headlines": []},
        )
        if not signal:
            i += 1
            continue

        trade = simulate_trade(signal, four_hour, i, symbol)
        if trade:
            trades.append(trade)
            # No overlapping positions on the same symbol. Resume after the exit candle.
            exit_ts = pd.Timestamp(trade["exit_time"])
            matching = four_hour.index[four_hour["timestamp"] == exit_ts]
            exit_index = int(matching[-1]) if len(matching) else i + 1
            i = max(i + 1, exit_index + 1)
        else:
            i += 1
    return trades


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "message": "No trades generated; no profitability conclusion is possible."}
    pnls = [float(t["net_return_pct"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity *= max(0.000001, 1.0 + pnl / 100.0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2),
        "net_return_pct_compounded": round((equity - 1.0) * 100.0, 2),
        "average_trade_net_pct": round(sum(pnls) / len(pnls), 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "average_r_multiple_net": round(sum(float(t["r_multiple_net"]) for t in trades) / len(trades), 3),
        "fees_pct_of_initial_notional_sum": round(sum(float(t["fees_pct"]) for t in trades), 3),
        "tp1_rate_pct": round(sum(bool(t["tp1_hit"]) for t in trades) / len(trades) * 100.0, 2),
        "tp2_rate_pct": round(sum(bool(t["tp2_hit"]) for t in trades) / len(trades) * 100.0, 2),
        "tp3_rate_pct": round(sum(bool(t["tp3_hit"]) for t in trades) / len(trades) * 100.0, 2),
        "by_setup": _group_summary(trades, "setup"),
        "by_regime": _group_summary(trades, "regime"),
        "caveats": [
            "Uses next 4h candle open for entry to avoid same-candle look-ahead.",
            "If a candle touches stop and target, stop is assumed first.",
            "Includes 0.1% entry and exit fees, but no spread or slippage.",
            "No live news or fundamental confirmation is available in this CSV-only run.",
            "Use separate train/validation/test date ranges; do not tune on the final test period.",
        ],
    }


def _group_summary(trades: list[dict], key: str) -> dict:
    result = {}
    values = sorted({str(t.get(key, "UNKNOWN")) for t in trades})
    for value in values:
        group = [t for t in trades if str(t.get(key, "UNKNOWN")) == value]
        pnls = [float(t["net_return_pct"]) for t in group]
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        result[value] = {
            "trades": len(group),
            "win_rate_pct": round(sum(p > 0 for p in pnls) / len(group) * 100.0, 2),
            "net_return_pct_compounded": round((math.prod(max(0.000001, 1 + p / 100) for p in pnls) - 1) * 100, 2),
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward backtest for the spot price-action strategy")
    parser.add_argument("--4h-csv", required=True, help="CSV with 4h OHLCV candles")
    parser.add_argument("--1d-csv", required=True, help="CSV with daily OHLCV candles")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--out", default="backtest-results")
    args = parser.parse_args()

    four_hour = load_ohlcv(args.__dict__["4h_csv"], 4)
    daily = load_ohlcv(args.__dict__["1d_csv"], 24)
    trades = run_backtest(four_hour, daily, args.symbol)
    report = summarize(trades)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        if trades:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
        else:
            f.write("")
    (out / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {len(trades)} trades to {out.resolve()}")


if __name__ == "__main__":
    main()
