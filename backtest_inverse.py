"""Replay mirrored (opposite-side) trades against historical OHLCV candles.

Input candles: one CSV per symbol, named like BTC_USDT.csv (also accepts
BTC-USDT.csv), with timestamp,open,high,low,close columns. Timestamp may be
ISO-8601 or Unix milliseconds. Example:
  python backtest_inverse.py --signals signals.json --candles-dir candles

This is a research tool, not a live-trading feature. For each recorded signal it
mirrors the entry-relative SL/TP distances, uses 50/30/20% TP weights, charges
the configured fee on entry and exit notionals, and conservatively treats a bar
touching both stop and target as stop-first. Missing candles are reported, not
silently treated as losses or wins.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path


TP_WEIGHTS = ((1, 0.50), (2, 0.30), (3, 0.20))


def timestamp(value):
    value = str(value).strip()
    try:
        number = float(value)
        if number > 10_000_000_000:
            return number / 1000.0
        if number > 1_000_000_000:
            return number
    except ValueError:
        pass
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def parse_time(value):
    if not value:
        return None
    return timestamp(value)


def read_candles(path):
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            normalized = {str(k).strip().lower(): v for k, v in row.items() if k}
            ts = normalized.get("timestamp") or normalized.get("time") or normalized.get("datetime") or normalized.get("date")
            if ts is None:
                raise ValueError(f"{path}: expected timestamp/time/datetime/date column")
            rows.append({
                "ts": timestamp(ts),
                "open": float(normalized["open"]),
                "high": float(normalized["high"]),
                "low": float(normalized["low"]),
                "close": float(normalized["close"]),
            })
    return sorted(rows, key=lambda row: row["ts"])


def find_candles(directory, symbol):
    stem = symbol.replace("/", "_")
    candidates = (f"{stem}.csv", f"{symbol.replace('/', '-')}.csv", f"{symbol.replace('/', '')}.csv")
    for name in candidates:
        path = directory / name
        if path.exists():
            return path
    return None


def simulate(signal, candles, fee_rate):
    entry = float(signal["entry"])
    side = str(signal.get("side", "BUY")).upper()
    if side not in {"BUY", "SELL"}:
        return {"status": "SKIP", "reason": "unsupported_side"}

    # Reverse direction while preserving the original entry-to-level distances.
    inverse_side = "SELL" if side == "BUY" else "BUY"
    original_sl = float(signal["sl"])
    inverse_sl = entry + (entry - original_sl) if side == "BUY" else entry - (original_sl - entry)
    targets = {}
    for number, _ in TP_WEIGHTS:
        original_tp = float(signal[f"tp{number}"])
        targets[number] = entry - (original_tp - entry) if side == "BUY" else entry + (entry - original_tp)

    start = parse_time(signal.get("created_at"))
    end = parse_time(signal.get("closed_at") or signal.get("updated_at") or signal.get("expiry_at"))
    if start is None:
        return {"status": "SKIP", "reason": "missing_created_at"}
    relevant = [bar for bar in candles if bar["ts"] >= start and (end is None or bar["ts"] <= end)]
    if not relevant:
        return {"status": "NO_DATA", "reason": "no_candles_in_trade_window"}

    remaining = 1.0
    gross_pnl = 0.0
    fees = 0.0
    hits = []
    exit_reason = "TIME_EXIT"
    last_price = relevant[-1]["close"]

    # Charge entry fee once on the full position. Exit fees are charged per closed slice.
    fees += entry * fee_rate
    for bar in relevant:
        last_price = bar["close"]
        if inverse_side == "SELL":
            stop_hit = bar["high"] >= inverse_sl
            hit_targets = [n for n, _ in TP_WEIGHTS if remaining > 1e-12 and bar["low"] <= targets[n] and n not in hits]
        else:
            stop_hit = bar["low"] <= inverse_sl
            hit_targets = [n for n, _ in TP_WEIGHTS if remaining > 1e-12 and bar["high"] >= targets[n] and n not in hits]

        # OHLC bars cannot establish intrabar ordering; choose the conservative stop-first path.
        if stop_hit:
            exit_price = inverse_sl
            gross_pnl += remaining * ((entry - exit_price) if inverse_side == "SELL" else (exit_price - entry))
            fees += remaining * exit_price * fee_rate
            remaining = 0.0
            exit_reason = "SL"
            break

        for number in hit_targets:
            weight = dict(TP_WEIGHTS)[number]
            fraction = min(weight, remaining)
            exit_price = targets[number]
            gross_pnl += fraction * ((entry - exit_price) if inverse_side == "SELL" else (exit_price - entry))
            fees += fraction * exit_price * fee_rate
            remaining -= fraction
            hits.append(number)
            exit_reason = f"TP{number}" if remaining <= 1e-12 else exit_reason

    if remaining > 1e-12:
        gross_pnl += remaining * ((entry - last_price) if inverse_side == "SELL" else (last_price - entry))
        fees += remaining * last_price * fee_rate
    net = gross_pnl - fees
    return {
        "status": "SIMULATED",
        "symbol": signal.get("symbol"),
        "original_side": side,
        "inverse_side": inverse_side,
        "entry": entry,
        "inverse_sl": inverse_sl,
        "inverse_targets": {f"tp{n}": value for n, value in targets.items()},
        "tp_hits": hits,
        "exit_reason": exit_reason,
        "bars": len(relevant),
        "gross_pnl_per_unit": round(gross_pnl, 10),
        "fees_per_unit": round(fees, 10),
        "net_pnl_per_unit": round(net, 10),
        "assumed_position_value": float(signal.get("position_value") or 1.0),
        "note": "PnL is per unit unless scaled; original position sizing may not suit the mirrored stop.",
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest opposite-side versions of logged signals")
    parser.add_argument("--signals", default="signals.json")
    parser.add_argument("--candles-dir", default="candles")
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--out", default="inverse-backtest.json")
    args = parser.parse_args()

    payload = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    signals = payload if isinstance(payload, list) else payload.get("signals", [])
    candle_dir = Path(args.candles_dir)
    cache = {}
    results = []
    for signal in signals:
        if not isinstance(signal, dict) or signal.get("status") not in {"CLOSED", "EXPIRED"}:
            continue
        symbol = signal.get("symbol", "")
        path = find_candles(candle_dir, symbol)
        if path is None:
            results.append({"status": "NO_DATA", "symbol": symbol, "reason": "missing_candle_file"})
            continue
        try:
            if path not in cache:
                cache[path] = read_candles(path)
            results.append(simulate(signal, cache[path], args.fee_rate))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            results.append({"status": "SKIP", "symbol": symbol, "reason": str(exc)})

    simulated = [row for row in results if row.get("status") == "SIMULATED"]
    pnl = [row["net_pnl_per_unit"] for row in simulated]
    report = {
        "scope": "Mirrored trade replay; only as reliable as supplied OHLCV data and assumptions.",
        "assumptions": {
            "fee_rate_per_side": args.fee_rate,
            "tp_weights": {"tp1": 0.5, "tp2": 0.3, "tp3": 0.2},
            "same_bar_stop_and_target": "stop-first conservative",
            "inverse_levels": "mirror each original entry-to-SL/TP distance around entry",
            "position_sizing": "net PnL is per unit; position sizing is not normalized to equal risk",
        },
        "counts": {
            "closed_or_expired_signals": len(results),
            "simulated": len(simulated),
            "missing_data": sum(row.get("status") == "NO_DATA" for row in results),
            "skipped": sum(row.get("status") == "SKIP" for row in results),
            "positive_per_unit": sum(value > 0 for value in pnl),
            "negative_per_unit": sum(value < 0 for value in pnl),
            "win_rate_pct": round(100 * sum(value > 0 for value in pnl) / len(pnl), 2) if pnl else None,
            "net_pnl_per_unit_sum": round(sum(pnl), 10),
        },
        "results": results,
        "warning": "Do not deploy a reverse strategy based on win rate alone. Normalize risk, verify candle coverage, and test out of sample.",
    }
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["counts"], indent=2))
    print(f"Saved report to {args.out}")


if __name__ == "__main__":
    main()
