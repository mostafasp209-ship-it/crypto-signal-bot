import unittest

import numpy as np
import pandas as pd

import bot


def make_trend_frame(direction, n=260):
    step = 0.08 if direction == "bull" else -0.08
    close = 80.0 + np.arange(n) * step
    return pd.DataFrame({
        "timestamp": np.arange(n, dtype=np.int64) * 3_600_000,
        "open": close - step * 0.2,
        "high": close + 0.25,
        "low": close - 0.25,
        "close": close,
        "volume": np.full(n, 1000.0),
    })


def make_entry_frame(side):
    n = 100
    opn = np.full(n, 100.0)
    high = np.full(n, 100.2)
    low = np.full(n, 99.8)
    close = np.full(n, 100.0)
    volume = np.full(n, 100.0)

    if side == "BUY":
        # A completed sell-side liquidity sweep and reclaim.
        opn[-2], high[-2], low[-2], close[-2] = 99.9, 100.2, 99.5, 100.1
        # Fresh bullish BOS with displacement and above-average volume.
        opn[-1], high[-1], low[-1], close[-1] = 100.2, 101.1, 100.1, 101.0
    else:
        # A completed buy-side liquidity sweep and reclaim.
        opn[-2], high[-2], low[-2], close[-2] = 100.1, 100.5, 99.8, 99.9
        # Fresh bearish BOS with displacement and above-average volume.
        opn[-1], high[-1], low[-1], close[-1] = 99.8, 99.9, 98.9, 99.0

    volume[-1] = 250.0
    return pd.DataFrame({
        "timestamp": np.arange(n, dtype=np.int64) * 900_000,
        "open": opn, "high": high, "low": low,
        "close": close, "volume": volume,
    })


class RTMIchimokuStrategyTests(unittest.TestCase):
    def test_long_setup_requires_sweep_bos_displacement_and_aligned_htf(self):
        result = bot.calculate_rtm_ichimoku_gates(
            make_entry_frame("BUY"),
            make_trend_frame("bull"),
            make_trend_frame("bull"),
        )
        self.assertTrue(result["signal_long"])
        self.assertFalse(result["signal_short"])
        self.assertEqual(result["setup"], "RTM_ICHIMOKU_15M_1H_4H_3R")
        self.assertGreater(result["atr"], 0)
        self.assertGreater(result["volume_ratio"], 1.0)

    def test_short_setup_requires_sweep_bos_displacement_and_aligned_htf(self):
        result = bot.calculate_rtm_ichimoku_gates(
            make_entry_frame("SELL"),
            make_trend_frame("bear"),
            make_trend_frame("bear"),
        )
        self.assertTrue(result["signal_short"])
        self.assertFalse(result["signal_long"])
        self.assertEqual(result["setup"], "RTM_ICHIMOKU_15M_1H_4H_3R")

    def test_opposite_higher_timeframe_trend_blocks_entry(self):
        result = bot.calculate_rtm_ichimoku_gates(
            make_entry_frame("BUY"),
            make_trend_frame("bear"),
            make_trend_frame("bull"),
        )
        self.assertFalse(result["signal_long"])
        self.assertFalse(result["signal_short"])

    def test_signal_requires_next_candle_open(self):
        signal = bot.build_signal(
            "BTC/USDT:USDT", "okx", make_entry_frame("BUY"),
            next_open=None,
            h1_df=make_trend_frame("bull"),
            h4_df=make_trend_frame("bull"),
        )
        self.assertIsNone(signal)

    def test_long_and_short_signal_risk_and_target_are_valid(self):
        for side, trend in (("BUY", "bull"), ("SELL", "bear")):
            with self.subTest(side=side):
                signal = bot.build_signal(
                    "BTC/USDT:USDT", "okx", make_entry_frame(side),
                    next_open=101.0 if side == "BUY" else 99.0,
                    h1_df=make_trend_frame(trend),
                    h4_df=make_trend_frame(trend),
                )
                self.assertIsNotNone(signal)
                self.assertEqual(signal["side"], side)
                self.assertEqual(signal["components"]["setup"], "RTM_ICHIMOKU_15M_1H_4H_3R")
                self.assertAlmostEqual(signal["rr"], 3.0)
                self.assertGreater(signal["actual_risk_usd"], 0)
                self.assertLessEqual(signal["actual_risk_usd"], bot.RISK_USD + 1e-8)
                self.assertLessEqual(signal["position_value"], bot.MAX_POSITION_USD + 1e-8)
                if side == "BUY":
                    self.assertLess(signal["sl"], signal["entry"])
                    self.assertGreater(signal["tp1"], signal["entry"])
                else:
                    self.assertGreater(signal["sl"], signal["entry"])
                    self.assertLess(signal["tp1"], signal["entry"])

    def test_insufficient_higher_timeframe_history_blocks_signal(self):
        short_history = make_trend_frame("bull").tail(100).reset_index(drop=True)
        result = bot.calculate_rtm_ichimoku_gates(
            make_entry_frame("BUY"), short_history, make_trend_frame("bull")
        )
        self.assertFalse(result["signal_long"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
