"""Audit historical signal outcomes without changing the live trading bot.

Usage:
    python analyze_signals.py --input signals.json --out outcome-report.json

The report describes the recorded log; it is not a backtest and does not prove
that any feature causes better results.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


CLOSED_STATUSES = {"CLOSED", "EXPIRED"}


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def bucket_score(value: Any) -> str:
    score = finite_number(value)
    if score is None:
        return "UNKNOWN"
    if score < 12:
        return "<12"
    if score < 15:
        return "12-14"
    if score < 18:
        return "15-17"
    return "18+"


def summarize_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [finite_number(row.get("realized_pnl")) for row in records]
    pnls = [value for value in pnls if value is not None]
    winners = [value for value in pnls if value > 0]
    losers = [value for value in pnls if value < 0]
    return {
        "trades": len(pnls),
        "wins": len(winners),
        "losses": len(losers),
        "flat": len(pnls) - len(winners) - len(losers),
        "win_rate_pct": round(100 * len(winners) / len(pnls), 2) if pnls else None,
        "net_realized_pnl": round(sum(pnls), 8),
        "average_pnl": round(sum(pnls) / len(pnls), 8) if pnls else None,
        "profit_factor": round(sum(winners) / abs(sum(losers)), 4) if losers else (
            None if not winners else "infinite"
        ),
    }


def grouped(records: list[dict[str, Any]], key_fn) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        groups[str(key_fn(row))].append(row)
    return {
        key: summarize_group(group)
        for key, group in sorted(groups.items())
    }


def audit_signals(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("signals"), list):
        records = payload["signals"]
    else:
        raise ValueError("Input JSON must be a list or an object with a 'signals' list")

    if any(not isinstance(row, dict) for row in records):
        raise ValueError("Every signal record must be a JSON object")

    closed = [
        row for row in records
        if str(row.get("status", "")).upper() in CLOSED_STATUSES
        and finite_number(row.get("realized_pnl")) is not None
    ]
    winners = [row for row in closed if finite_number(row.get("realized_pnl")) > 0]
    losers = [row for row in closed if finite_number(row.get("realized_pnl")) < 0]

    def component(row: dict[str, Any], key: str, fallback: str = "UNKNOWN") -> str:
        components = row.get("components")
        value = components.get(key) if isinstance(components, dict) else None
        value = value if value is not None else row.get(key)
        return str(value) if value not in (None, "") else fallback

    anomalies = []
    for row in closed:
        pnl = finite_number(row.get("realized_pnl"))
        reason = str(row.get("closed_reason", "UNKNOWN"))
        status = str(row.get("status", "UNKNOWN")).upper()
        remaining = finite_number(row.get("remaining_fraction"))
        hits = [bool(row.get(f"tp{i}_hit", False)) for i in (1, 2, 3)]
        if reason.upper() in {"SL", "STOP_LOSS"} and pnl is not None and pnl > 0:
            # This can be legitimate after partial TP exits; flag for reconciliation, not as a proven bug.
            anomalies.append({
                "id": row.get("id"), "symbol": row.get("symbol"),
                "type": "positive_pnl_with_stop_reason",
                "pnl": pnl, "tp_hits": hits,
                "note": "May be valid after partial take-profits; reconcile close logic and realized PnL.",
            })
        if status == "EXPIRED" and remaining is not None and remaining > 1e-9:
            anomalies.append({
                "id": row.get("id"), "symbol": row.get("symbol"),
                "type": "expired_with_open_fraction",
                "remaining_fraction": remaining,
            })
        if hits[2] and not hits[1] or hits[1] and not hits[0]:
            anomalies.append({
                "id": row.get("id"), "symbol": row.get("symbol"),
                "type": "take_profit_flags_out_of_order", "tp_hits": hits,
            })
        if remaining is not None and not (0 <= remaining <= 1):
            anomalies.append({
                "id": row.get("id"), "symbol": row.get("symbol"),
                "type": "invalid_remaining_fraction", "remaining_fraction": remaining,
            })

    return {
        "scope": "Recorded signal log only; not an independent historical-candle backtest.",
        "counts": {
            "all_records": len(records),
            "closed_with_pnl": len(closed),
            "active": sum(str(row.get("status", "")).upper() == "ACTIVE" for row in records),
            "wins": len(winners),
            "losses": len(losers),
            "win_rate_pct": round(100 * len(winners) / len(closed), 2) if closed else None,
            "net_realized_pnl": round(sum(finite_number(row.get("realized_pnl")) or 0 for row in closed), 8),
        },
        "by_setup": grouped(closed, lambda row: component(row, "setup")),
        "by_score_bucket": grouped(closed, lambda row: bucket_score(row.get("score"))),
        "by_daily_trend": grouped(closed, lambda row: component(row, "daily_trend", component(row, "trend"))),
        "by_close_reason": grouped(closed, lambda row: row.get("closed_reason") or "UNKNOWN"),
        "by_tp1_hit": grouped(closed, lambda row: "YES" if row.get("tp1_hit") else "NO"),
        "top_winners": sorted(
            [{
                "symbol": row.get("symbol"),
                "setup": component(row, "setup"),
                "score": row.get("score"),
                "daily_trend": component(row, "daily_trend", component(row, "trend")),
                "pnl": finite_number(row.get("realized_pnl")),
                "closed_reason": row.get("closed_reason"),
                "tp1_hit": bool(row.get("tp1_hit")),
                "tp2_hit": bool(row.get("tp2_hit")),
                "tp3_hit": bool(row.get("tp3_hit")),
            } for row in winners],
            key=lambda item: item["pnl"] or 0, reverse=True
        )[:20],
        "top_losers": sorted(
            [{
                "symbol": row.get("symbol"),
                "setup": component(row, "setup"),
                "score": row.get("score"),
                "daily_trend": component(row, "daily_trend", component(row, "trend")),
                "pnl": finite_number(row.get("realized_pnl")),
                "closed_reason": row.get("closed_reason"),
                "tp1_hit": bool(row.get("tp1_hit")),
                "tp2_hit": bool(row.get("tp2_hit")),
                "tp3_hit": bool(row.get("tp3_hit")),
            } for row in losers],
            key=lambda item: item["pnl"] or 0
        )[:20],
        "anomalies": anomalies,
        "interpretation_warning": (
            "Small groups are noisy. Group differences are descriptive, not causal. "
            "Do not tune strategy thresholds on this live log alone."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize historical signal outcomes")
    parser.add_argument("--input", default="signals.json", help="Path to signals.json")
    parser.add_argument("--out", default="outcome-report.json", help="Output JSON path")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = audit_signals(payload)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved report to {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
