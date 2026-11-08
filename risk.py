"""Pure position-risk and execution-cost calculations."""
from __future__ import annotations

import math


def _finite(value: float, *, allow_zero: bool = False) -> bool:
    try:
        number = float(value)
        return math.isfinite(number) and (number >= 0 if allow_zero else number > 0)
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
) -> dict[str, float | bool] | None:
    """Return fee/slippage-aware sizing; None means the plan is invalid.

    Risk is the entry-to-stop price distance plus estimated round-trip fees and
    adverse stop slippage. Target RR is net of entry/target fees and adverse
    target slippage, divided by the planned price-distance risk.
    """
    side = str(side).upper()
    if side not in {"BUY", "SELL"}:
        return None
    if not all(_finite(value) for value in (entry, stop, target, risk_budget_usd)):
        return None
    if not _finite(fee_rate, allow_zero=True) or not _finite(slippage_rate, allow_zero=True):
        return None
    if not _finite(max_position_usd, allow_zero=True):
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
    directional_target_reward = (target_fill - entry) if side == "BUY" else (entry - target_fill)
    net_target_reward = directional_target_reward - (entry + target) * fee_rate
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
        "position_capped": quantity + 1e-12 < raw_quantity,
    }
