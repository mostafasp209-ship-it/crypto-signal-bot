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

def component_value(row: dict[str, Any], key: str) -> Any:
    components = row.get("components")
    if isinstance(components, dict) and components.get(key) is not None:
        return components.get(key)
    return row.get(key)


def bucket_resistance_room(value: Any) -> str:
    room = finite_number(value)
    if room is None:
        return "UNKNOWN"
    if room < 1:
        return "<1R"
    if room < 1.5:
        return "1-1.5R"
    return "1.5R+"


def bucket_support_touches(value: Any) -> str:
    touches = finite_number(value)
    if touches is None:
        return "UNKNOWN"
    if touches <= 2:
        return "1-2"
    if touches <= 4:
        return "3-4"
    if touches <= 6:
        return "5-6"
    return "7+"


def outcome_sensitivity_without_largest_winner(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"excluded_symbol": None, **summarize_group([])}
    largest = max(records, key=lambda row: finite_number(row.get("realized_pnl")) or 0)
    remaining = [row for row in records if row is not largest]
    return {
        "excluded_symbol": largest.get("symbol"),
        "excluded_pnl": finite_number(largest.get("realized_pnl")),
        **summarize_group(remaining),
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

    stop_exits = [
        row for row in closed
        if str(row.get("closed_reason", "")).upper() in {"SL", "STOP_LOSS"}
    ]
    stop_slippage_values = [
        finite_number(row.get("stop_slippage_usd")) for row in stop_exits
    ]
    stop_slippage_values = [value for value in stop_slippage_values if value is not None]

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
        "stop_execution_audit": {
            "closed_by_stop": len(stop_exits),
            "records_with_slippage_data": len(stop_slippage_values),
            "total_adverse_stop_slippage_usd": round(sum(stop_slippage_values), 8),
            "note": "Only newly recorded stop exits have slippage data; older records cannot be reconstructed without exit-price history.",
        },
        "by_setup": grouped(closed, lambda row: component(row, "setup")),
        "by_score_bucket": grouped(closed, lambda row: bucket_score(row.get("score"))),
        "by_daily_trend": grouped(closed, lambda row: component(row, "daily_trend", component(row, "trend"))),
        "by_close_reason": grouped(closed, lambda row: row.get("closed_reason") or "UNKNOWN"),
        "by_market_regime": grouped(closed, lambda row: component_value(row, "market_regime") or "UNKNOWN"),
        "by_news_context": grouped(closed, lambda row: component_value(row, "news_context") or (row.get("news") or {}).get("label", "UNKNOWN")),
        "by_resistance_room_bucket": grouped(closed, lambda row: bucket_resistance_room(component_value(row, "nearest_resistance_room_r"))),
        "by_support_touches_bucket": grouped(closed, lambda row: bucket_support_touches(component_value(row, "sr_zone_touches"))),
        "by_spike_detected": grouped(closed, lambda row: str(bool(component_value(row, "spike_detected"))).upper()),
        "by_trendline_up_near": grouped(closed, lambda row: str(bool(component_value(row, "trendline_up_near"))).upper()),
        "by_h2_detected": grouped(closed, lambda row: str(bool(component_value(row, "h2_detected"))).upper()),
        "concentration_sensitivity": {
            "all_closed": summarize_group(closed),
            "excluding_largest_winner": outcome_sensitivity_without_largest_winner(closed),
            "note": "Sensitivity check only; excluding a winner is not a performance estimate.",
        },
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
