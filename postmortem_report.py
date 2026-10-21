#!/usr/bin/env python3
"""Generate an automated, evidence-led report on closed signal outcomes.

This script is read-only: it does not edit signals or alter live trading rules.
It intentionally reports small samples and missing entry snapshots instead of
turning correlations into automatic strategy filters.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def value_for(row: dict[str, Any], key: str) -> Any:
    snap = row.get("entry_snapshot")
    components = row.get("components")
    snap = snap if isinstance(snap, dict) else {}
    components = components if isinstance(components, dict) else {}
    aliases = {
        "daily_trend": ("daily_trend", "daily_trend"),
        "market_regime": ("market_regime", "market_regime"),
        "setup": ("setup", "setup"),
        "spike_detected": ("spike_detected", "spike_detected"),
        "trendline_up_near": ("trendline_up_near", "trendline_up_near"),
        "sr_zone_touches": ("support_zone_touches", "sr_zone_touches"),
        "nearest_resistance_room_r": ("resistance_room_r", "nearest_resistance_room_r"),
    }
    snap_key, component_key = aliases.get(key, (key, key))
    for source, name in ((snap, snap_key), (components, component_key), (row, key)):
        if source.get(name) is not None:
            return source[name]
    if key == "score":
        return row.get("score")
    return None


def bucket(key: str, value: Any) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    if key in {"spike_detected", "trendline_up_near"}:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"false", "0", "no", "off", ""}:
                return "false"
            if normalized in {"true", "1", "yes", "on"}:
                return "true"
        return str(bool(value)).lower()
    if key == "nearest_resistance_room_r":
        n = number(value)
        if n is None:
            return "UNKNOWN"
        if n < 1:
            return "<1R"
        if n < 1.5:
            return "1-1.5R"
        if n < 2:
            return "1.5-2R"
        return "2R+"
    if key == "sr_zone_touches":
        n = number(value)
        if n is None:
            return "UNKNOWN"
        if n <= 2:
            return "1-2"
        if n <= 4:
            return "3-4"
        if n <= 6:
            return "5-6"
        return "7+"
    if key == "score":
        n = number(value)
        if n is None:
            return "UNKNOWN"
        if n < 12:
            return "<12"
        if n < 15:
            return "12-14"
        if n < 18:
            return "15-17"
        return "18+"
    return str(value).upper()


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [number(r.get("realized_pnl")) for r in rows]
    pnls = [x for x in pnls if x is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(pnls) - len(wins) - len(losses),
        "win_rate_pct": round(100 * len(wins) / len(pnls), 2) if pnls else None,
        "net_pnl_usd": round(sum(pnls), 6),
        "average_pnl_usd": round(sum(pnls) / len(pnls), 6) if pnls else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else ("infinite" if gross_win else None),
    }


def build_report(payload: Any) -> dict[str, Any]:
    signals = payload if isinstance(payload, list) else payload.get("signals", []) if isinstance(payload, dict) else None
    if not isinstance(signals, list) or any(not isinstance(s, dict) for s in signals):
        raise ValueError("signals.json must be a list or an object containing a signals list")

    closed = [
        s for s in signals
        if str(s.get("status", "")).upper() in {"CLOSED", "EXPIRED"}
        and number(s.get("realized_pnl")) is not None
    ]
    winners = [s for s in closed if number(s.get("realized_pnl")) > 0]
    losers = [s for s in closed if number(s.get("realized_pnl")) < 0]
    stopped = [s for s in closed if str(s.get("closed_reason", "")).upper() in {"SL", "STOP_LOSS"}]

    feature_keys = [
        "setup", "daily_trend", "market_regime", "spike_detected",
        "trendline_up_near", "nearest_resistance_room_r", "sr_zone_touches", "score",
    ]
    comparisons: dict[str, Any] = {}
    for key in feature_keys:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in closed:
            groups[bucket(key, value_for(row, key))].append(row)
        comparisons[key] = {}
        for group_name, rows in sorted(groups.items()):
            m = metrics(rows)
            m["loss_rate_pct"] = round(100 * m["losses"] / m["trades"], 2) if m["trades"] else None
            m["winner_count"] = sum(1 for r in rows if number(r.get("realized_pnl")) > 0)
            comparisons[key][group_name] = m

    # Compare how common each feature is among winners vs losers. This is
    # descriptive only; low counts and overlapping features prevent causal claims.
    winner_loser_profile: dict[str, Any] = {}
    for key in feature_keys:
        winner_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        loser_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in winners:
            winner_groups[bucket(key, value_for(row, key))].append(row)
        for row in losers:
            loser_groups[bucket(key, value_for(row, key))].append(row)
        buckets = set(winner_groups) | set(loser_groups)
        profile_rows = []
        for group_name in buckets:
            w_count = len(winner_groups.get(group_name, []))
            l_count = len(loser_groups.get(group_name, []))
            w_share = 100 * w_count / len(winners) if winners else None
            l_share = 100 * l_count / len(losers) if losers else None
            profile_rows.append({
                "bucket": group_name,
                "winner_count": w_count,
                "winner_share_pct": round(w_share, 2) if w_share is not None else None,
                "loser_count": l_count,
                "loser_share_pct": round(l_share, 2) if l_share is not None else None,
                "share_difference_pp_loser_minus_winner": (
                    round(l_share - w_share, 2)
                    if l_share is not None and w_share is not None else None
                ),
                "small_sample": (w_count + l_count) < 5,
            })
        profile_rows.sort(
            key=lambda item: abs(item["share_difference_pp_loser_minus_winner"] or 0),
            reverse=True,
        )
        winner_loser_profile[key] = profile_rows

    missing_snapshot = [s for s in closed if not isinstance(s.get("entry_snapshot"), dict)]
    slippage = [number(s.get("stop_slippage_usd")) or 0.0 for s in stopped]
    anomalies = []
    for row in closed:
        pnl = number(row.get("realized_pnl"))
        if not isinstance(row.get("entry_snapshot"), dict):
            anomalies.append({"symbol": row.get("symbol"), "type": "missing_entry_snapshot",
                              "note": "Legacy trade; feature analysis falls back to components where possible."})
        if str(row.get("closed_reason", "")).upper() in {"SL", "STOP_LOSS"}:
            slip = number(row.get("stop_slippage_usd")) or 0.0
            if slip > 0:
                anomalies.append({"symbol": row.get("symbol"), "type": "stop_slippage",
                                  "amount_usd": round(slip, 6)})
            if pnl is not None and pnl > 0 and not any(bool(row.get(f"tp{i}_hit")) for i in (1, 2, 3)):
                anomalies.append({"symbol": row.get("symbol"), "type": "positive_pnl_stop_without_tp_flag",
                                  "note": "Reconcile exit flags and realized PnL."})

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Read-only analysis of recorded closed outcomes; not a candle replay, causal proof, or a trading recommendation.",
        "counts": {
            "all_signals": len(signals),
            "active": sum(1 for s in signals if str(s.get("status", "")).upper() == "ACTIVE"),
            "closed_with_pnl": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "stop_loss_exits": len(stopped),
            "entry_snapshot_coverage_pct": round(100 * (len(closed) - len(missing_snapshot)) / len(closed), 2) if closed else None,
            "total_recorded_stop_slippage_usd": round(sum(slippage), 6),
        },
        "overall": metrics(closed),
        "by_feature": comparisons,
        "winner_loser_profile": winner_loser_profile,
        "largest_winner_sensitivity": {
            "largest_winner_symbol": max(winners, key=lambda s: number(s.get("realized_pnl"))).get("symbol") if winners else None,
            "overall_net_excluding_largest_winner_usd": round(
                sum(number(s.get("realized_pnl")) or 0 for s in closed)
                - (max((number(s.get("realized_pnl")) or 0 for s in winners), default=0)), 6
            ),
        },
        "anomalies": anomalies,
        "interpretation_rules": [
            "Treat groups with fewer than 5 trades as exploratory only.",
            "A feature shared by losers is not a causal explanation; compare the same feature among winners.",
            "Do not automatically change live strategy thresholds based on this report.",
            "Missing entry snapshots limit the reliability of historical feature comparisons.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="signals.json")
    parser.add_argument("--output", default="postmortem-report.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = build_report(payload)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": args.output, "counts": report["counts"], "overall": report["overall"],
                      "top_setup_groups": report["by_feature"]["setup"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
