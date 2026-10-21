"""Compare original and mirrored signal outcomes using supplied OHLCV CSVs.

Run:
  python backtest_inverse.py --signals signals.json --candles-dir candles

Put one CSV per symbol in candles/, e.g. BTC_USDT.csv, with timestamp,open,high,low,close columns. Timestamp can be ISO-8601 or Unix seconds/milliseconds.

Both sides use the logged entry and mirror the entry-to-SL/TP distances for the inverse trade. Each uses the logged position_quantity when available, 50/30/20% TP weights, fee per side, and conservative stop-first handling when a candle touches both stop and target. Results are estimates: OHLC bars cannot reveal intrabar ordering or realistic fills/slippage.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

TP_WEIGHTS = ((1, 0.50), (2, 0.30), (3, 0.20))


def to_epoch(value):
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


def read_candles(path):
    result = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            row = {str(k).strip().lower(): v for k, v in row.items() if k}
            raw_ts = row.get("timestamp") or row.get("time") or row.get("datetime") or row.get("date")
            if raw_ts is None:
                raise ValueError(f"{path}: missing timestamp column")
            result.append({"ts": to_epoch(raw_ts), **{k: float(row[k]) for k in ("open", "high", "low", "close")}})
    return sorted(result, key=lambda item: item["ts"])


def candle_file(directory, symbol):
    variants = (symbol.replace("/", "_") + ".csv", symbol.replace("/", "-") + ".csv", symbol.replace("/", "") + ".csv")
    return next((directory / name for name in variants if (directory / name).exists()), None)


def simulate(signal, candles, fee_rate, inverse):
    original_side = str(signal.get("side", "BUY")).upper()
    if original_side not in {"BUY", "SELL"}:
        return {"status": "SKIP", "reason": "unsupported_side"}
    side = ({"BUY": "SELL", "SELL": "BUY"}[original_side] if inverse else original_side)
    entry = float(signal["entry"])
    old_sl = float(signal["sl"])
    if inverse:
        stop = entry + (entry - old_sl) if original_side == "BUY" else entry - (old_sl - entry)
        targets = {
            n: (entry - (float(signal[f"tp{n}"]) - entry) if original_side == "BUY"
                else entry + (entry - float(signal[f"tp{n}"])))
            for n, _ in TP_WEIGHTS
        }
    else:
        stop = old_sl
        targets = {n: float(signal[f"tp{n}"]) for n, _ in TP_WEIGHTS}

    start = to_epoch(signal["created_at"])
    end_raw = signal.get("closed_at") or signal.get("updated_at") or signal.get("expiry_at")
    end = to_epoch(end_raw) if end_raw else None
    bars = [bar for bar in candles if bar["ts"] >= start and (end is None or bar["ts"] <= end)]
    if not bars:
        return {"status": "NO_DATA", "reason": "no_candles_in_trade_window"}

    qty = float(signal.get("position_quantity") or signal.get("quantity") or 1.0)
    remaining = 1.0
    gross = 0.0
    fees = entry * qty * fee_rate
    hits = []
    exit_reason = "TIME_EXIT"
    last = bars[-1]["close"]
    for bar in bars:
        last = bar["close"]
        stop_hit = bar["high"] >= stop if side == "SELL" else bar["low"] <= stop
        target_hits = [
            n for n, _ in TP_WEIGHTS if n not in hits and remaining > 1e-12
            and ((bar["low"] <= targets[n]) if side == "SELL" else (bar["high"] >= targets[n]))
        ]
        # If OHLC says stop and target were both touched, assume the worse stop-first path.
        if stop_hit:
            gross += remaining * qty * ((entry - stop) if side == "SELL" else (stop - entry))
            fees += remaining * qty * stop * fee_rate
            remaining = 0.0
            exit_reason = "SL"
            break
        for n in target_hits:
            fraction = min(dict(TP_WEIGHTS)[n], remaining)
            price = targets[n]
            gross += fraction * qty * ((entry - price) if side == "SELL" else (price - entry))
            fees += fraction * qty * price * fee_rate
            remaining -= fraction
            hits.append(n)
            if remaining <= 1e-12:
                exit_reason = f"TP{n}"
    if remaining > 1e-12:
        gross += remaining * qty * ((entry - last) if side == "SELL" else (last - entry))
        fees += remaining * qty * last * fee_rate
    return {
        "status": "SIMULATED", "symbol": signal.get("symbol"),
        "strategy": "inverse" if inverse else "original", "side": side,
        "position_quantity": qty, "entry": entry, "stop": stop,
        "targets": {f"tp{n}": p for n, p in targets.items()},
        "tp_hits": hits, "exit_reason": exit_reason, "bars": len(bars),
        "gross_pnl_usd": round(gross, 8), "fees_usd": round(fees, 8),
        "net_pnl_usd": round(gross - fees, 8),
    }


def main():
    parser = argparse.ArgumentParser(description="Compare logged signals with opposite-side replay")
    parser.add_argument("--signals", default="signals.json")
    parser.add_argument("--candles-dir", default="candles")
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--out", default="direction-comparison.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    signals = payload if isinstance(payload, list) else payload.get("signals", [])
    directory = Path(args.candles_dir)
    cache, results = {}, []
    eligible = [s for s in signals if isinstance(s, dict) and s.get("status") in {"CLOSED", "EXPIRED"}]
    for signal in eligible:
        path = candle_file(directory, str(signal.get("symbol", "")))
        if path is None:
            results.append({"symbol": signal.get("symbol"), "status": "NO_DATA", "reason": "missing_candle_file"})
            continue
        try:
            if path not in cache:
                cache[path] = read_candles(path)
            for inverse in (False, True):
                results.append({"symbol": signal.get("symbol"), **simulate(signal, cache[path], args.fee_rate, inverse)})
        except (KeyError, TypeError, ValueError, OSError) as exc:
            results.append({"symbol": signal.get("symbol"), "status": "SKIP", "reason": str(exc)})

    summary = {}
    for strategy in ("original", "inverse"):
        rows = [r for r in results if r.get("status") == "SIMULATED" and r.get("strategy") == strategy]
        pnls = [r["net_pnl_usd"] for r in rows]
        summary[strategy] = {
            "trades": len(rows), "wins": sum(p > 0 for p in pnls), "losses": sum(p < 0 for p in pnls),
            "win_rate_pct": round(100 * sum(p > 0 for p in pnls) / len(pnls), 2) if pnls else None,
            "net_pnl_usd": round(sum(pnls), 8),
            "profit_factor": round(sum(p for p in pnls if p > 0) / abs(sum(p for p in pnls if p < 0)), 4)
                if any(p < 0 for p in pnls) else None,
        }
    report = {
        "scope": "Historical OHLCV replay, only for signals with supplied candles.",
        "assumptions": {"fee_rate_per_side": args.fee_rate, "tp_weights": {"tp1": .5, "tp2": .3, "tp3": .2},
                        "same_bar_stop_and_target": "stop-first", "inverse_levels": "mirror distances around entry",
                        "position_sizing": "uses logged position_quantity; does not model slippage or funding"},
        "eligible_logged_signals": len(eligible),
        "signals_missing_candles": sum(r.get("status") == "NO_DATA" for r in results),
        "summary": summary, "results": results,
        "warning": "Do not enable inverse trading from this report alone; inspect data coverage, fills, and out-of-sample performance.",
    }
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"eligible": len(eligible), "missing_candles": report["signals_missing_candles"], "summary": summary}, indent=2))
    print(f"Saved report to {args.out}")


if __name__ == "__main__":
    main()
