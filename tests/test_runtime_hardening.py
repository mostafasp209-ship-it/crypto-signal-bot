import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from safety_filters import is_tradable_symbol


class RuntimeHardeningTests(unittest.TestCase):
    def test_liquidity_and_symbol_safety_filters(self):
        self.assertTrue(is_tradable_symbol("BTC/USDT:USDT", quote_volume=6_000_000, entry_price=100))
        self.assertFalse(is_tradable_symbol("BTC/USDT:USDT", quote_volume=4_999_999, entry_price=100))
        self.assertFalse(is_tradable_symbol("BTC3L/USDT:USDT", quote_volume=10_000_000, entry_price=100))
        self.assertFalse(is_tradable_symbol("LOW/USDT:USDT", quote_volume=10_000_000, entry_price=0.001))

    def test_rtm_target_closes_the_entire_position_once(self):
        signal = {
            "status": "ACTIVE",
            "side": "BUY",
            "entry": 100.0,
            "sl": 90.0,
            "tp1": 130.0,
            "tp2": 130.0,
            "tp3": 130.0,
            "initial_risk_per_unit": 10.0,
            "initial_sl": 90.0,
            "position_quantity": 1.0,
            "remaining_fraction": 1.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
            "components": {
                "setup": "RTM_ICHIMOKU_15M_1H_4H_3R",
                "fee_rate": 0.0,
                "slippage_rate": 0.0,
            },
        }
        self.assertTrue(bot.manage_signal(signal, 130.0))
        self.assertEqual(signal["status"], "CLOSED")
        self.assertEqual(signal["closed_reason"], "TP1")
        self.assertTrue(signal["tp1_hit"])
        self.assertFalse(signal["tp2_hit"])
        self.assertFalse(signal["tp3_hit"])
        self.assertEqual(signal["remaining_fraction"], 0.0)
        self.assertAlmostEqual(signal["realized_pnl"], 30.0)
        self.assertFalse(bot.manage_signal(signal, 131.0))

    def test_performance_report_counts_only_current_rtm_setup(self):
        state = {
            "signals": [
                {"status": "CLOSED", "created_at": "2026-11-02T00:00:00+00:00",
                 "realized_pnl": 12.5, "fees_paid": 0.5,
                 "components": {"setup": "RTM_ICHIMOKU_15M_1H_4H_3R"}},
                {"status": "CLOSED", "created_at": "2026-11-02T00:00:00+00:00",
                 "realized_pnl": 99.0, "fees_paid": 0.0,
                 "components": {"setup": "REFERENCE_RSI_STOCH_ATR_RR15"}},
            ]
        }
        report = bot.performance_report(state)
        self.assertIn("معاملات بسته: 1", report)
        self.assertIn("Net PnL: $12.50", report)
        self.assertNotIn("$99.00", report)

    def test_corrupt_signal_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals.json"
            path.write_text("{broken", encoding="utf-8")
            with patch.object(bot, "DATA_FILE", str(path)):
                with self.assertRaises(RuntimeError):
                    bot.load_state()

    def test_invalid_signal_state_shape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals.json"
            path.write_text(json.dumps({"signals": {}}), encoding="utf-8")
            with patch.object(bot, "DATA_FILE", str(path)):
                with self.assertRaises(RuntimeError):
                    bot.load_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
