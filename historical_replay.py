#!/usr/bin/env python3
"""Fetch public 1-minute candles and replay CLOSED signals without changing live rules.

This is an approximate audit, not an exchange-grade fill simulation: OHLCV cannot
reveal the exact intraminute order of stop/target touches. Bars that touch a stop
and target together are handled stop-first. Public exchange history may be missing.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt

TIMEFRAME = "1m"
BAR_MS = 60_000
FEE_RATE = 0.001
TP_WEIGHTS = (("tp1", 0.50), ("tp2", 0.30), ("tp3", 0.20))


def as_float(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def timestamp_ms(value: Any) -> int:
    if not value:
        raise ValueError("missing timestamp")
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def simulate_signal(signal: dict[str, Any], candles: list[list[Any]]) -> dict[str, Any]:
    entry = as_float(signal.get("entry"))
    quantity = as_float(signal.get("position_quantity", signal.get("quantity")))
    side = str(signal.get("side", "BUY")).upper()
    stop = as_float(signal.get("initial_sl", signal.get("sl")))
    targets = {name: as_float(signal.get(name)) for name, _ in TP_WEIGHTS}
    if entry is None or entry <= 0 or quantity is None or quantity <= 0 or stop is None:
        return {"status": "INVALID_SIGNAL_FIELDS", "simulated_pnl_usd": None, "bars": len(candles)}
    if any(targets[name] is None for name, _ in TP_WEIGHTS):
        return {"status": "MISSING_TARGETS", "simulated_pnl_usd": None, "bars": len(candles)}
    if not candles:
        return {"status": "NO_CANDLES", "simulated_pnl_usd": None, "bars": 0}

    remaining = 1.0
    pnl = 0.0
    fees = 0.0
    tp_hits: list[str] = []
    exit_reason = None
    stop_first_ambiguous_bars = 0
    last_close = None

    def touched_stop(low: float, high: float, level: float) -> bool:
        return low <= level if side == "BUY" else high >= level

    def touched_target(low: float, high: float, level: float) -> bool:
        return high >= level if side == "BUY" else low <= level

    for candle in candles:
        if len(candle) < 6:
            continue
        _, op, high, low, close, volume = candle[:6]
        high, low, close = as_float(high), as_float(low), as_float(close)
        if high is None or low is None or close is None:
            continue
        last_close = close

        # OHLCV does not reveal whether target or stop happened first.
        next_targets = [targets[name] for name, weight in TP_WEIGHTS if name not in tp_hits and remaining > 0]
        if touched_stop(low, high, stop):
            if any(touched_target(low, high, target) for target in next_targets):
                stop_first_ambiguous_bars += 1
            fraction = remaining
            exit_price = stop
            gross = ((exit_price - entry) if side == "BUY" else (entry - exit_price)) * quantity * fraction
            piece_fees = (entry + exit_price) * quantity * fraction * FEE_RATE
            pnl += gross - piece_fees
            fees += piece_fees
            remaining = 0.0
            exit_reason = "SL"
            break

        for name, weight in TP_WEIGHTS:
            target = targets[name]
            if name in tp_hits or remaining <= 1e-9:
                continue
            if touched_target(low, high, target):
                fraction = min(weight, remaining)
                gross = ((target - entry) if side == "BUY" else (entry - target)) * quantity * fraction
                piece_fees = (entry + target) * quantity * fraction * FEE_RATE
                pnl += gross - piece_fees
                fees += piece_fees
                remaining -= fraction
                tp_hits.append(name)
                if name == "tp1":
                    stop = entry
                if remaining <= 1e-9:
                    remaining = 0.0
                    exit_reason = "TP3"
                    break
        if exit_reason == "TP3":
            break

    if remaining > 1e-9 and last_close is not None:
        # Remaining position at the recorded close/expiry horizon is marked to
        # the last available close. This is an estimate, not a confirmed fill.
        gross = ((last_close - entry) if side == "BUY" else (entry - last_close)) * quantity * remaining
        piece_fees = (entry + last_close) * quantity * remaining * FEE_RATE
        pnl += gross - piece_fees
        fees += piece_fees
        exit_reason = "MARKED_AT_LAST_CLOSE"

    return {
        "status": "OK",
        "simulated_exit": exit_reason,
        "simulated_pnl_usd": round(pnl, 6),
        "simulated_fees_usd": round(fees, 6),
        "tp_hits": tp_hits,
        "bars": len(candles),
        "ambiguous_stop_target_bars": stop_first_ambiguous_bars,
        "last_candle_close_marked": exit_reason == "MARKED_AT_LAST_CLOSE",
        "caveat": "1m OHLCV cannot prove intrabar ordering or actual fill/slippage.",
    }


def fetch_candles(exchange: Any, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    # Skip the partial entry minute to avoid using high/low that may predate entry.
    since = ((start_ms // BAR_MS) + 1) * BAR_MS
    candles: dict[int, list[Any]] = {}
    calls = 0
    while since <= end_ms and calls < 12:
        batch = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=since, limit=1000)
        calls += 1
        if not batch:
            break
        for row in batch:
            ts = int(row[0])
            if since <= ts <= end_ms:
                candles[ts] = row
        last_ts = int(batch[-1][0])
        if last_ts >= end_ms or len(batch) < 2:
            break
        next_since = last_ts + BAR_MS
        if next_since <= since:
            break
        since = next_since
        if calls < 12 and since <= end_ms:
            time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))
    return [candles[key] for key in sorted(candles)]


def replay_all(signals: list[dict[str, Any]]) -> dict[str, Any]:
    output = []
    cache: dict[tuple[str, str, int, int], list[list[Any]]] = {}
    exchange_cache: dict[str, Any] = {}
    for signal in signals:
        if str(signal.get("status", "")).upper() not in {"CLOSED", "EXPIRED"}:
            continue
        item = {
            "signal_id": signal.get("id"),
            "symbol": signal.get("symbol"),
            "exchange": signal.get("exchange"),
            "side": signal.get("side"),
            "recorded_status": signal.get("status"),
            "recorded_reason": signal.get("closed_reason"),
            "recorded_pnl_usd": as_float(signal.get("realized_pnl")),
            "created_at": signal.get("created_at"),
            "closed_at": signal.get("updated_at"),
        }
        try:
            start_ms = timestamp_ms(signal.get("created_at"))
            end_ms = timestamp_ms(signal.get("updated_at"))
            if end_ms <= start_ms:
                raise ValueError("close timestamp is not after entry timestamp")
            exchange_id = str(signal.get("exchange", "")).lower()
            if not exchange_id or not hasattr(ccxt, exchange_id):
                raise ValueError(f"unsupported CCXT exchange id: {exchange_id}")
            if exchange_id not in exchange_cache:
                exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 20000})
                exchange.load_markets()
                exchange_cache[exchange_id] = exchange
            exchange = exchange_cache[exchange_id]
            symbol = str(signal.get("symbol", ""))
            if symbol not in exchange.markets:
                raise ValueError(f"symbol not listed on {exchange_id}: {symbol}")
            key = (exchange_id, symbol, start_ms, end_ms)
            if key not in cache:
                cache[key] = fetch_candles(exchange, symbol, start_ms, end_ms)
            candles = cache[key]
            item["candle_count"] = len(candles)
            item["replay"] = simulate_signal(signal, candles)
            if not candles:
                item["data_status"] = "NO_HISTORICAL_CANDLES"
            else:
                item["data_status"] = "OK"
                replay_pnl = item["replay"].get("simulated_pnl_usd")
                recorded_pnl = item["recorded_pnl_usd"]
                item["pnl_difference_vs_recorded_usd"] = (
                    round(replay_pnl - recorded_pnl, 6)
                    if replay_pnl is not None and recorded_pnl is not None else None
                )
        except Exception as exc:
            item["data_status"] = "FETCH_OR_REPLAY_ERROR"
            item["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        output.append(item)

    usable = [x for x in output if x.get("data_status") == "OK" and x.get("replay", {}).get("simulated_pnl_usd") is not None]
    recorded_total = sum(x["recorded_pnl_usd"] or 0 for x in usable)
    replay_total = sum(x["replay"]["simulated_pnl_usd"] for x in usable)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Historical 1m OHLCV replay of closed signals. Approximation only; no exact fills, order-book, spread, or reliable intraminute path.",
        "timeframe": TIMEFRAME,
        "closed_signals_found": len(output),
        "signals_with_replay_data": len(usable),
        "signals_without_replay_data": len(output) - len(usable),
        "recorded_pnl_on_replayed_subset_usd": round(recorded_total, 6),
        "simulated_pnl_on_replayed_subset_usd": round(replay_total, 6),
        "simulated_minus_recorded_usd": round(replay_total - recorded_total, 6),
        "signals": output,
        "safety": "Read-only. Does not modify bot.py, signal thresholds, or live trading rules.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="signals.json")
    parser.add_argument("--output", default="historical-replay-report.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    signals = payload if isinstance(payload, list) else payload.get("signals", [])
    report = replay_all(signals)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "closed_signals_found", "signals_with_replay_data", "signals_without_replay_data",
        "recorded_pnl_on_replayed_subset_usd", "simulated_pnl_on_replayed_subset_usd",
        "simulated_minus_recorded_usd")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
