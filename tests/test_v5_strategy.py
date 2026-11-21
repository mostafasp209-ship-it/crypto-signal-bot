import unittest

import numpy as np
import pandas as pd

import bot
from safety_filters import is_tradable_symbol


def flat_then_cross(direction="up"):
    n = 130
    close = np.full(n, 100.0)
    high = np.full(n, 100.4)
    low = np.full(n, 99.6)
    opn = np.full(n, 100.0)
    volume = np.full(n, 1000.0)
    if direction == "up":
        close[-1] = 108.0
        high[-1] = 108.2
        low[-1] = 100.0
        opn[-1] = 100.2
    else:
        close[-1] = 96.0
        high[-1] = 100.0
        low[-1] = 95.8
        opn[-1] = 99.8
    volume[-1] = 2500.0
    return pd.DataFrame({
        "timestamp": np.arange(n, dtype=np.int64) * 14_400_000,
        "open": opn, "high": high, "low": low, "close": close, "volume": volume,
    })


class V5StrategyTests(unittest.TestCase):
    def test_ema_cross_up_emits_long(self):
        df = flat_then_cross("up")
        gates = bot.calculate_v5_gates(df)
        self.assertTrue(gates["signal_long"])
        self.assertFalse(gates["signal_short"])
        signal = bot.build_v5_signal("BTC/USDT:USDT", "okx", df, next_open=108.1, quote_volume=20_000_000)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["side"], "BUY")
        self.assertEqual(signal["components"]["setup"], bot.V5_SETUP)
        self.assertAlmostEqual(signal["rr"], 2.0)
        self.assertLess(signal["sl"], signal["entry"])
        self.assertGreater(signal["tp1"], signal["entry"])
        self.assertLessEqual(signal["position_value"], bot.MAX_POSITION_USD + 1e-6)
        self.assertLessEqual(signal["risk_pct"], 2.5)

    def test_ema_cross_down_emits_short(self):
        df = flat_then_cross("down")
        signal = bot.build_v5_signal("ETH/USDT:USDT", "bitget", df, next_open=96.0, quote_volume=20_000_000)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["side"], "SELL")
        self.assertGreater(signal["sl"], signal["entry"])
        self.assertLess(signal["tp1"], signal["entry"])

    def test_flat_market_has_no_signal(self):
        n = 130
        close = np.full(n, 100.0)
        df = pd.DataFrame({
            "timestamp": np.arange(n) * 14_400_000,
            "open": close, "high": close + 0.2, "low": close - 0.2,
            "close": close, "volume": np.full(n, 1000.0),
        })
        self.assertFalse(bot.calculate_v5_gates(df)["signal_long"])
        self.assertIsNone(bot.build_v5_signal("BTC/USDT:USDT", "okx", df, next_open=100.0, quote_volume=20_000_000))

    def test_illiquid_and_leveraged_names_are_rejected(self):
        df = flat_then_cross("up")
        self.assertIsNone(bot.build_v5_signal("BTC/USDT:USDT", "okx", df, next_open=108.0, quote_volume=1000))
        self.assertIsNone(bot.build_v5_signal("BTC3L/USDT:USDT", "okx", df, next_open=108.0, quote_volume=20_000_000))
        self.assertFalse(is_tradable_symbol("RLS/USDT:USDT", quote_volume=20_000_000, verbose=False))
        self.assertTrue(is_tradable_symbol("SOL/USDT:USDT", quote_volume=20_000_000, verbose=False))

    def test_single_target_closes_full_v5_position(self):
        signal = {
            "symbol": "BTC/USDT:USDT", "side": "BUY", "status": "ACTIVE",
            "entry": 100.0, "sl": 98.0, "tp1": 104.0, "tp2": 104.0, "tp3": 104.0,
            "components": {"setup": bot.V5_SETUP},
            "initial_sl": 98.0, "initial_risk_per_unit": 2.0,
            "position_quantity": 1.0, "remaining_fraction": 1.0,
            "realized_pnl": 0.0, "fees_paid": 0.0,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        }
        self.assertTrue(bot.manage_signal(signal, 104.0))
        self.assertEqual(signal["status"], "CLOSED")
        self.assertEqual(signal["closed_reason"], "TP1")
        self.assertEqual(signal["remaining_fraction"], 0)


if __name__ == "__main__":
    unittest.main()
