"""Pure position-risk and execution-cost calculations."""
from __future__ import annotations

import math


def _finite_positive(value: float) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def calculate_risk_plan(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    risk_budget_usd: float,
    max_position_usd: float,
    fee_rate: float,
    slippage_rate: float,
) -> dict[str, float] | None:
    """Return fee/slippage-aware sizing; None means the plan is invalid.

    Risk is the entry-to-stop price distance plus estimated round-trip fees and
    adverse stop slippage. Target RR is net of entry/target fees and adverse
    target slippage, divided by the planned price-distance risk.
    """
    side = str(side).upper()
    if side not in {"BUY", "SELL"}:
        return None
    values = (entry, stop, target, risk_budget_usd, fee_rate, slippage_rate)
    if not all(_finite_positive(value) for value in values):
        return None
    if not math.isfinite(float(max_position_usd)) or float(max_position_usd) < 0:
        return None

    entry, stop, target = float(entry), float(stop), float(target)
    risk_budget_usd = float(risk_budget_usd)
    fee_rate, slippage_rate = float(fee_rate), float(slippage_rate)
    if side == "BUY":
        if not stop < entry < target:
            return None
        risk = entry - stop
        target_fill = target * (1.0 - slippage_rate)
    else:
        if not target < entry < stop:
            return None
        risk = stop - entry
        target_fill = target * (1.0 + slippage_rate)

    cost_per_unit = (entry + stop) * fee_rate + stop * slippage_rate
    net_target_reward = abs(target_fill - entry) - (entry + target) * fee_rate
    net_rr = net_target_reward / risk
    if not all(math.isfinite(x) for x in (risk, cost_per_unit, net_target_reward, net_rr)):
        return None
    if risk <= 0 or cost_per_unit < 0 or net_target_reward <= 0:
        return None

    raw_quantity = risk_budget_usd / (risk + cost_per_unit)
    cap_quantity = (
        float(max_position_usd) / entry
        if float(max_position_usd) > 0
        else raw_quantity
    )
    quantity = min(raw_quantity, cap_quantity)
    if not all(math.isfinite(x) and x > 0 for x in (raw_quantity, cap_quantity, quantity)):
        return None

    return {
        "risk_per_unit": risk,
        "cost_per_unit": cost_per_unit,
        "net_target_reward_per_unit": net_target_reward,
        "net_rr": net_rr,
        "raw_quantity": raw_quantity,
        "quantity": quantity,
        "actual_risk_usd": quantity * (risk + cost_per_unit),
        "position_value_usd": quantity * entry,
        "position_capped": float(quantity + 1e-12 < raw_quantity),
    }
