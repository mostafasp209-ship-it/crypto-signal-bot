import math
import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import bot


SETUP = "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3"


def make_breakout_frame():
    n = 260
    i = np.arange(n, dtype=float)
    close = 100.0 + 0.08 * i + 0.15 * np.sin(i / 3.0)
    close[-1] += 5.0
    return pd.DataFrame({
        "timestamp": (i.astype(np.int64) * 14_400_000),
        "open": close - 0.05,
        "high": close + 0.25,
        "low": close - 0.25,
        "close": close,
        "volume": np.r_[np.full(n - 1, 100.0), 200.0],
    })


def make_v3_signal():
    return {
        "id": "unit-test",
        "symbol": "TEST/USDT",
        "exchange": "binanceus",
        "side": "BUY",
        "status": "ACTIVE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entry": 100.0,
        "sl": 96.0,
        "initial_sl": 96.0,
        "initial_risk_per_unit": 4.0,
        "position_quantity": 1.0,
        "remaining_fraction": 1.0,
        "tp1": 103.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "components": {"setup": SETUP, "partial_pct": 1.0 / 3.0},
        "signal_candle_timestamp": 210.0,
        "last_trailing_candle_ts": 210.0,
        "highest_close_since_entry": 100.0,
        "trailing_active": False,
    }


class V3StrategyTests(unittest.TestCase):
    def test_breakout_gate_has_indicators_and_detects_confirmed_setup(self):
        frame = make_breakout_frame()
        result = bot.calculate_crypto_strategy_gates(frame)
        self.assertIsNotNone(result["atr"])
        self.assertIsNotNone(result["ema200"])
        self.assertIsNotNone(result["adx"])
        self.assertGreater(result["adx"], 32.0)
        self.assertTrue(result["signal_long"])
        self.assertEqual(result["lookback"], 30)

    def test_volume_filter_rejects_breakout_without_expansion(self):
        frame = make_breakout_frame()
        frame.loc[frame.index[-1], "volume"] = 100.0
        result = bot.calculate_crypto_strategy_gates(frame)
        self.assertFalse(result["signal_long"])

    def test_partial_exit_closes_one_third_and_moves_stop_to_entry(self):
        signal = make_v3_signal()
        signal["initial_risk_per_unit"] = 4.0
        signal["tp1"] = 103.0  # +0.75R for a 4-unit initial risk
        changed = bot.manage_signal(signal, 104.0)
        self.assertTrue(changed)
        self.assertTrue(signal["tp1_hit"])
        self.assertTrue(signal["trailing_active"])
        self.assertAlmostEqual(signal["remaining_fraction"], 2.0 / 3.0, places=6)
        self.assertAlmostEqual(signal["sl"], signal["entry"], places=8)
        self.assertGreater(signal["realized_pnl"], 0.0)

    def test_v3_alert_formats_variable_rr_without_crashing(self):
        signal = make_v3_signal()
        signal.update({
            "strength": "V3",
            "trend": "BULL — EMA200 4h",
            "risk_pct": 4.0,
            "rr": None,
            "risk_usd": 10.0,
            "risk_target_usd": 10.0,
            "actual_risk_usd": 8.5,
            "support": None,
            "resistance": 100.0,
            "sl_method": "2x_ATR14",
            "fundamentals": {},
            "news": {},
        })
        signal["components"].update({"adx14": 36.0, "ema200": 95.0})
        message = bot.format_signal(signal)
        self.assertIn("RR نهایی: متغیر", message)
        self.assertIn("ADX(14)", message)
        self.assertIn("تریلینگ", message)

    def test_chandelier_stop_only_ratchets_up_after_partial(self):
        n = 220
        close = 100.0 + np.arange(n, dtype=float) * 0.05
        close[211:] = close[210] + np.arange(1, 10, dtype=float) * 2.0
        frame = pd.DataFrame({
            "timestamp": np.arange(n, dtype=float),
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 100.0),
        })
        signal = make_v3_signal()
        signal["entry"] = float(close[210])
        signal["sl"] = float(close[210])
        signal["initial_sl"] = float(close[210] - 4.0)
        signal["signal_candle_timestamp"] = 210.0
        signal["last_trailing_candle_ts"] = 210.0
        signal["highest_close_since_entry"] = float(close[210])
        signal["tp1_hit"] = True
        signal["trailing_active"] = True
        changed = bot.update_chandelier_stop(signal, frame)
        self.assertTrue(changed)
        self.assertGreater(signal["sl"], signal["entry"])
        raised_stop = signal["sl"]
        self.assertFalse(bot.update_chandelier_stop(signal, frame))
        self.assertGreaterEqual(signal["sl"], raised_stop)


if __name__ == "__main__":
    unittest.main(verbosity=2)
