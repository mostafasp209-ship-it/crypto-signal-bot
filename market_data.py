"""OHLCV normalization and closed-candle validation."""
from __future__ import annotations

import pandas as pd


def closed_candles(raw):
    if not raw or len(raw) < 30:
        return pd.DataFrame()

    df = pd.DataFrame(
        raw,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    # Always ignore the currently forming candle.
    df = df.iloc[:-1].copy()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    return df
