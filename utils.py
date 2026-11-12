"""Small, dependency-light helpers shared by bot modules."""
from __future__ import annotations

import math
from typing import Any


def safe_float(value: Any, default: float | None = None) -> float | None:
    """Convert finite numeric input to float; return default for invalid values."""
    try:
        if value is None:
            return default
        number = float(value)
        if not math.isfinite(number):
            return default
        return number
    except (TypeError, ValueError, OverflowError):
        return default
