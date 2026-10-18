import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import bot


def make_ohlcv(closes):
    rows = []
    for i, close in enumerate(closes):
        open_price = closes[i - 1] if i else close - 0.2
        high = max(open_price, close) + 0.35
        low = min(open_price, close) - 0.35
        rows.append({
            "timestamp": i, "open": float(open_price), "high": float(high),
            "low": float(low), "close": float(close), "volume": 100.0,
        })
    return pd.DataFrame(rows)


class PriceActionTests(unittest.TestCase):
    def test_market_regime_short_history_is_unclear(self):
        df = make_ohlcv([100, 101, 100, 102, 101, 103])
        result = bot._market_regime(df, 1.0)
        self.assertEqual(result["regime"], "UNCLEAR")

    def test_h2_requires_pullback_and_second_attempt(self):
        # Pullback, first bullish trigger, a renewed pullback, then second trigger.
        closes = [100, 101, 102, 103, 104, 105, 104, 103, 102, 103, 102, 103, 104]
        fixture = make_ohlcv(closes)
        # Ensure the second trigger truly breaks the preceding bar's high.
        fixture.loc[11, "high"] = float(fixture.loc[10, "high"]) + 0.20
        result = bot._count_h1_h2(fixture, lookback=18)
        self.assertTrue(result["h1"])
        self.assertTrue(result["h2"])
        self.assertEqual(result["attempts"], 2)

    def test_no_pullback_does_not_create_h2(self):
        closes = [100 + i * 0.8 for i in range(20)]
        result = bot._count_h1_h2(make_ohlcv(closes), lookback=18)
        self.assertFalse(result["h2"])
        self.assertLess(result["attempts"], 2)

    def test_support_resistance_returns_stable_tuple_interface(self):
        x = np.arange(160, dtype=float)
        closes = 100 + 0.04 * x + 2 * np.sin(x / 4.0)
        df = make_ohlcv(closes.tolist())
        supports, resistances = bot.find_support_resistance(df, float(closes[-1]), 1.0)
        self.assertIsInstance(supports, list)
        self.assertIsInstance(resistances, list)
        for zone in supports + resistances:
            self.assertEqual(len(zone), 3)
            self.assertIn("center", zone[2])
            self.assertIn("touches", zone[2])
            self.assertGreaterEqual(zone[2]["touches"], 0)

    def test_breakout_retest_requires_bullish_trend(self):
        df = make_ohlcv([100 + np.sin(i / 3.0) for i in range(50)])
        self.assertIsNone(bot._find_breakout_retest(df, 1.0, False))

    def test_sparse_resistance_uses_ordered_fallback_targets(self):
        df = make_ohlcv([100 + np.sin(i / 3.0) for i in range(50)])
        support_zone = {"center": 95.0, "touches": 0, "pivot_count": 1}
        with patch.object(bot, "find_support_resistance", return_value=([(95.0, 0, support_zone)], [])):
            levels = bot.calculate_levels("BUY", 100.0, 2.0, df)
        self.assertIsNotNone(levels)
        self.assertLess(levels["sl"], levels["entry"])
        self.assertLess(levels["entry"], levels["tp1"])
        self.assertLess(levels["tp1"], levels["tp2"])
        self.assertLess(levels["tp2"], levels["tp3"])
        self.assertIn("fallback", " ".join(levels["tp_methods"]))


if __name__ == "__main__":
    unittest.main()
