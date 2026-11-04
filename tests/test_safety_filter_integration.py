import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import bot


class FakeFuturesExchange:
    def __init__(self):
        self.markets = {}
        for symbol in ("BTC/USDT:USDT", "THIN/USDT:USDT", "BULL/USDT:USDT", "MISSING/USDT:USDT"):
            base = symbol.split("/")[0]
            self.markets[symbol] = {
                "active": True, "swap": True, "linear": True,
                "quote": "USDT", "settle": "USDT", "base": base,
            }

    def fetch_tickers(self, symbols):
        return {
            "BTC/USDT:USDT": {"quoteVolume": 8_000_000},
            "THIN/USDT:USDT": {"quoteVolume": 1_000_000},
            "BULL/USDT:USDT": {"quoteVolume": 12_000_000},
            "MISSING/USDT:USDT": {},
        }


def signal_frame():
    n = 150
    close = np.full(n, 100.0)
    opn = np.full(n, 99.8)
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)
    volume = np.full(n, 100.0)
    opn[-1], close[-1], low[-1], volume[-1] = 99.8, 100.4, 98.8, 200.0
    return pd.DataFrame({
        "timestamp": np.arange(n, dtype=np.int64) * 14_400_000,
        "open": opn, "high": high, "low": low, "close": close, "volume": volume,
    })


def bullish_indicator_patches(n, atr_value=1.0):
    rsi_values = np.full(n, 50.0)
    k_values = np.full(n, 50.0)
    d_values = np.full(n, 50.0)
    rsi_values[-2:] = [29.0, 31.0]
    k_values[-2:] = [20.0, 25.0]
    d_values[-2:] = [22.0, 24.0]
    return (
        patch("bot.rsi", return_value=pd.Series(rsi_values)),
        patch("bot.atr", return_value=pd.Series(np.full(n, atr_value))),
        patch("bot.stochastic", return_value=(pd.Series(k_values), pd.Series(d_values))),
    )


class SafetyGateIntegrationTests(unittest.TestCase):
    def test_scan_rejects_thin_blacklisted_and_unknown_liquidity_symbols(self):
        manager = bot.ExchangeManager()
        manager.exchanges["test"] = FakeFuturesExchange()
        with patch.object(bot, "SCAN_EXCHANGE_NAMES", ["test"]):
            symbols = manager.scan_symbols()
        self.assertEqual(symbols, [("test", "BTC/USDT:USDT")])

    def test_build_signal_rejects_micropriced_contract(self):
        frame = signal_frame()
        patches = bullish_indicator_patches(len(frame))
        with patches[0], patches[1], patches[2]:
            signal = bot.build_signal("BTC/USDT:USDT", "test", frame, next_open=0.005)
        self.assertIsNone(signal)

    def test_build_signal_enforces_two_point_five_percent_risk_cap(self):
        frame = signal_frame()
        patches = bullish_indicator_patches(len(frame), atr_value=2.0)
        with patches[0], patches[1], patches[2]:
            signal = bot.build_signal("BTC/USDT:USDT", "test", frame, next_open=100.0)
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
