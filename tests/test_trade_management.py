import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import bot


def make_signal(**overrides):
    signal = {
        "symbol": "BTC/USDT",
        "exchange": "binanceus",
        "side": "BUY",
        "status": "ACTIVE",
        "entry": 100.0,
        "sl": 95.0,
        "initial_sl": 95.0,
        "initial_risk_per_unit": 5.0,
        "position_quantity": 2.0,
        "remaining_fraction": 1.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    signal.update(overrides)
    return signal


def candle(ts, o, h, low, close):
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": close}


class TradeManagementTests(unittest.TestCase):
    def test_tp1_books_half_and_moves_stop_to_entry(self):
        signal = make_signal()
        changed = bot.manage_signal_candle(signal, candle(1_800_000_000_000, 100, 106, 99, 105))
        self.assertTrue(changed)
        self.assertTrue(signal["tp1_hit"])
        self.assertEqual(signal["sl"], signal["entry"])
        self.assertAlmostEqual(signal["remaining_fraction"], 0.5)
        self.assertGreater(signal["fees_paid"], 0)

    def test_stop_wins_when_stop_and_target_touch_same_candle(self):
        signal = make_signal()
        changed = bot.manage_signal_candle(signal, candle(1_800_000_000_000, 100, 106, 94, 101))
        self.assertTrue(changed)
        self.assertEqual(signal["status"], "CLOSED")
        self.assertEqual(signal["closed_reason"], "SL")
        self.assertFalse(signal["tp1_hit"])
        self.assertAlmostEqual(signal["remaining_fraction"], 0.0)

    def test_failed_pnl_booking_does_not_advance_candle_watermark(self):
        signal = make_signal()
        with patch.object(bot, "close_piece", return_value=False):
            changed = bot.manage_signal_candle(signal, candle(1_800_000_000_000, 100, 106, 99, 105))
        self.assertIsNone(changed)
        self.assertNotIn("last_processed_candle_ts", signal)
        self.assertFalse(signal["tp1_hit"])

    def test_expired_signal_books_remaining_position_before_closing(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        signal = make_signal(created_at=old)
        changed = bot.manage_signal(signal, 101.0)
        self.assertTrue(changed)
        self.assertEqual(signal["status"], "EXPIRED")
        self.assertEqual(signal["closed_reason"], "48h expiry")
        self.assertAlmostEqual(signal["remaining_fraction"], 0.0)
        self.assertTrue(signal["expiry_hit"])

    def test_failed_expiry_booking_keeps_signal_active(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        signal = make_signal(created_at=old)
        with patch.object(bot, "close_piece", return_value=False):
            changed = bot.manage_signal(signal, 101.0)
        self.assertFalse(changed)
        self.assertEqual(signal["status"], "ACTIVE")
        self.assertNotIn("closed_reason", signal)


if __name__ == "__main__":
    unittest.main()
