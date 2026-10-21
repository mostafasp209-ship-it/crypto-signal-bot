"""Per-signal, evidence-led post-mortem diagnostics.

The rules below generate hypotheses from features recorded at entry. They never
assert causality from a single stopped trade; aggregate repeated patterns before
changing strategy rules.
"""
from __future__ import annotations

import math
from typing import Any


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def diagnose_signal(signal: dict[str, Any]) -> dict[str, Any]:
    snapshot = signal.get("entry_snapshot")
    components = signal.get("components")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    components = components if isinstance(components, dict) else {}

    reason = str(signal.get("closed_reason") or "").upper()
    pnl = _num(signal.get("realized_pnl"))
    setup = str(snapshot.get("setup") or components.get("setup") or "UNKNOWN")
    findings: list[dict[str, str]] = []
    missing: list[str] = []

    def flag(code: str, severity: str, evidence: str, hypothesis: str) -> None:
        findings.append({
            "code": code,
            "severity": severity,
            "evidence": evidence,
            "hypothesis_not_proven": hypothesis,
        })

    if not snapshot:
        missing.append("entry_snapshot")
    if reason not in {"SL", "STOP_LOSS"}:
        if reason in {"TP3", "TP2", "TP1"} or signal.get("tp3_hit"):
            outcome = "WIN_OR_TARGET_EXIT"
        elif pnl is not None and pnl < 0:
            outcome = "LOSS_OTHER_EXIT"
        else:
            outcome = "NON_STOP_OR_UNKNOWN"
    else:
        outcome = "STOP_LOSS"

    trend = str(snapshot.get("daily_trend", components.get("daily_trend", signal.get("trend", "UNKNOWN")))).upper()
    regime = str(snapshot.get("market_regime", components.get("market_regime", "UNKNOWN"))).upper()
    room_r = _num(snapshot.get("resistance_room_r", components.get("nearest_resistance_room_r")))
    ema20_dist = _num(snapshot.get("ema20_distance_atr"))
    atr_pct = _num(snapshot.get("atr_pct"))
    rsi = _num(snapshot.get("rsi"))
    volume_ratio = _num(snapshot.get("volume_vs_median"))
    touches = _num(snapshot.get("support_zone_touches", components.get("sr_zone_touches")))
    score = _num(snapshot.get("score", signal.get("score")))

    if reason in {"SL", "STOP_LOSS"}:
        if trend == "BEAR":
            flag("COUNTER_DAILY_TREND", "high", f"daily_trend={trend}",
                 "The long setup may have fought the daily trend; compare stop rate with BULL/NEUTRAL cases.")
        if regime == "DOWNTREND":
            flag("COUNTER_MARKET_REGIME", "high", f"market_regime={regime}",
                 "Long entries in a downtrend regime may be fragile; verify this across many signals.")
        if room_r is not None and room_r < 1.0:
            flag("LIMITED_RESISTANCE_ROOM", "high", f"nearest resistance room={room_r:.2f}R",
                 "Overhead resistance may have capped upside before the trade could reach its targets.")
        if ema20_dist is not None and ema20_dist > 1.5:
            flag("EXTENDED_FROM_EMA20", "medium", f"entry was {ema20_dist:.2f} ATR above EMA20",
                 "The entry may have chased an extended move that was vulnerable to mean reversion.")
        if rsi is not None and rsi > 68:
            flag("OVERBOUGHT_MOMENTUM", "medium", f"RSI={rsi:.1f}",
                 "Momentum may have been late/overextended; test whether this bucket has elevated stop rate.")
        if volume_ratio is not None and volume_ratio < 0.7:
            flag("WEAK_VOLUME_CONFIRMATION", "medium", f"volume/median={volume_ratio:.2f}",
                 "The signal may have lacked participation; validate against comparable winners.")
        if touches is not None and touches >= 3:
            flag("RETESTED_SUPPORT_ZONE", "medium", f"support-zone touches={touches:.0f}",
                 "Repeated tests may have weakened support; compare fresh/first-touch outcomes.")
        if score is not None and score >= 16:
            flag("HIGH_SCORE_DID_NOT_PREVENT_LOSS", "info", f"entry score={score:.0f}",
                 "Check whether scoring bonuses reward correlated confirmations without enough independent evidence.")
        if not findings:
            flag("NO_SINGLE_RULE_VIOLATION_IDENTIFIED", "info",
                 "No recorded entry feature crossed the current diagnostic thresholds.",
                 "This can be an ordinary losing trade or missing evidence; inspect candles and execution data.")

    return {
        "signal_id": signal.get("id"),
        "symbol": signal.get("symbol"),
        "setup": setup,
        "status": signal.get("status"),
        "closed_reason": signal.get("closed_reason"),
        "realized_pnl": pnl,
        "outcome_class": outcome,
        "findings": findings,
        "missing_evidence": missing,
        "interpretation": "Hypotheses only; a single signal cannot prove a rule caused the loss.",
    }


def diagnose_signals(signals: list[dict[str, Any]]) -> dict[str, Any]:
    per_signal = [diagnose_signal(row) for row in signals if isinstance(row, dict)]
    stopped = [row for row in per_signal if row["outcome_class"] == "STOP_LOSS"]
    counts: dict[str, int] = {}
    for report in stopped:
        for finding in report["findings"]:
            code = finding["code"]
            counts[code] = counts.get(code, 0) + 1
    return {
        "scope": "Per-signal rule hypotheses based on features captured at entry; not causal proof or a candle replay.",
        "signals_analyzed": len(per_signal),
        "stop_losses_analyzed": len(stopped),
        "stop_findings_frequency": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        "signals": per_signal,
        "next_step": "Compare finding frequency and stop rate against winners for each setup/feature bucket before changing strategy rules.",
    }
