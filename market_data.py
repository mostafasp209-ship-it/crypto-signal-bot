"""OHLCV normalization and closed-candle validation."""
from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
PRICE_COLUMNS = ["open", "high", "low", "close"]


def closed_candles(raw):
    """Return validated, chronologically ordered closed candles.

    The final exchange row is excluded because CCXT commonly includes the
    still-forming candle. Invalid, duplicate, non-finite and impossible OHLCV
    rows are rejected rather than passed into indicators.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 30:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    try:
        frame = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
    except (TypeError, ValueError):
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    for column in OHLCV_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame.replace([np.inf, -np.inf], np.nan, inplace=True)
    frame.dropna(subset=OHLCV_COLUMNS, inplace=True)
    if frame.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    frame = frame.sort_values("timestamp", kind="stable")
    frame.drop_duplicates(subset="timestamp", keep="last", inplace=True)

    # Never let the latest potentially-forming exchange candle enter a signal.
    if len(frame) > 0:
        frame = frame.iloc[:-1].copy()

    if frame.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    prices = frame[PRICE_COLUMNS]
    valid = (
        (frame["timestamp"] >= 0)
        & (prices > 0).all(axis=1)
        & (frame["volume"] >= 0)
        & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
    )
    frame = frame.loc[valid].copy()
    if len(frame) < 29:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
