import unittest
from unittest.mock import patch

import pandas as pd

import bot
import exchange
from utils import safe_float
from analyze_signals import audit_signals


class NumericSafetyTests(unittest.TestCase):
    def test_safe_float_rejects_non_finite_values(self):
        self.assertEqual(safe_float("12.5"), 12.5)
        self.assertIsNone(safe_float("nan"))
        self.assertIsNone(safe_float("inf"))
        self.assertEqual(safe_float("invalid", default=7.0), 7.0)


class ExchangeInitializationTests(unittest.TestCase):
    def test_management_only_does_not_require_scanner_exchanges(self):
        manager = bot.ExchangeManager()
        with patch.object(exchange, "SCAN_EXCHANGE_NAMES", []):
            self.assertTrue(manager.build(require_scanners=False))

    def test_normal_scan_still_requires_at_least_one_scanner(self):
        manager = bot.ExchangeManager()
        with patch.object(exchange, "SCAN_EXCHANGE_NAMES", []):
            with self.assertRaises(RuntimeError):
                manager.build(require_scanners=True)


class CandleSafetyTests(unittest.TestCase):
    def test_closed_candles_excludes_forming_candle(self):
        raw = []
        for i in range(40):
            raw.append([i * 14400000, 100 + i, 102 + i, 99 + i, 101 + i, 1000])
        df = bot.closed_candles(raw)
        self.assertEqual(len(df), 39)
        self.assertEqual(int(df.iloc[-1]["timestamp"]), 38 * 14400000)

    def test_closed_candles_rejects_short_input(self):
        self.assertTrue(bot.closed_candles([]).empty)
        self.assertTrue(bot.closed_candles([[0, 1, 1, 1, 1, 1]] * 29).empty)

    def test_indicators_produce_expected_columns(self):
        raw = []
        for i in range(240):
            close = 100 + i * 0.2
            raw.append([i, close, close + 1, close - 1, close, 1000])
        df = bot.add_indicators(pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        ))
        for col in ("ema20", "ema50", "ema200", "rsi", "atr", "bb_upper", "bb_lower"):
            self.assertIn(col, df.columns)
        self.assertTrue(pd.notna(df.iloc[-1]["ema200"]))


class StrategyGateTests(unittest.TestCase):
    def test_v3_strategy_calculates_indicators_after_warmup(self):
        rows = []
        for i in range(260):
            close = 100 + i * 0.1
            if i == 259:
                close += 4.0  # Confirm a breakout above the prior 30-bar high.
            rows.append({
                "timestamp": 1735718400000 + i * 14400000,
                "open": close - 0.15, "high": close + 0.5,
                "low": close - 0.5, "close": close,
                "volume": 2000 if i == 259 else 1000,
            })
        result = bot.calculate_crypto_strategy_gates(pd.DataFrame(rows))
        for key in ("atr", "volume_avg", "ema200", "adx", "breakout_level", "volume_ratio"):
            self.assertIsNotNone(result[key], key)
        self.assertGreater(result["adx"], 32.0)
        self.assertGreater(result["volume_ratio"], 1.5)
        self.assertTrue(result["signal_long"])

    def test_v3_gate_requires_sufficient_closed_candle_history(self):
        rows = []
        base = pd.Timestamp("2026-10-24T08:00:00Z").value // 10**6
        for i in range(100):
            close = 100 + i * 0.1
            rows.append({
                "timestamp": base + i * 14400000,
                "open": close - 0.15, "high": close + 0.5,
                "low": close - 0.5, "close": close, "volume": 1000,
            })
        result = bot.calculate_crypto_strategy_gates(pd.DataFrame(rows))
        self.assertFalse(result["signal_long"])
        self.assertIsNone(result["atr"])
        self.assertIsNone(result["ema200"])
        self.assertIsNone(result["adx"])

    def test_v3_strategy_rejects_insufficient_history(self):
        df = pd.DataFrame([{
            "timestamp": 1735718400000, "open": 100, "high": 101,
            "low": 99, "close": 100, "volume": 1
        }] * 30)
        result = bot.calculate_crypto_strategy_gates(df)
        self.assertFalse(result["signal_long"])
        self.assertIsNone(result["atr"])
        self.assertIsNone(result["ema200"])
        self.assertIsNone(result["adx"])


class SignalExecutionTests(unittest.TestCase):
    def test_stop_gap_is_recorded_separately_from_realized_pnl(self):
        signal = {
            "symbol": "TEST/USDT", "side": "BUY", "entry": 100.0, "sl": 95.0,
            "initial_sl": 95.0, "initial_risk_per_unit": 5.0,
            "position_quantity": 2.0, "remaining_fraction": 1.0,
            "realized_pnl": 0.0, "fees_paid": 0.0,
        }
        self.assertTrue(bot.close_piece(signal, 92.0, 1.0, "sl"))
        self.assertAlmostEqual(signal["stop_slippage_usd"], 6.0)
        self.assertEqual(signal["last_exit_price"], 92.0)
        self.assertEqual(signal["last_exit_label"], "sl")
        self.assertAlmostEqual(signal["realized_pnl"], (92.0 - 100.0) * 2.0 - (100.0 + 92.0) * 2.0 * bot.FEE_RATE)

    def test_stop_fill_at_or_better_than_stop_has_no_adverse_slippage(self):
        signal = {
            "symbol": "TEST/USDT", "side": "BUY", "entry": 100.0, "sl": 95.0,
            "initial_sl": 95.0, "initial_risk_per_unit": 5.0,
            "position_quantity": 1.0, "remaining_fraction": 1.0,
            "realized_pnl": 0.0, "fees_paid": 0.0,
        }
        self.assertTrue(bot.close_piece(signal, 95.0, 1.0, "sl"))
        self.assertEqual(signal["stop_slippage_usd"], 0.0)

    def test_reference_strategy_closes_full_position_at_single_target(self):
        signal = {
            "symbol": "TEST/USDT", "side": "BUY", "status": "ACTIVE",
            "entry": 100.0, "sl": 95.0, "tp1": 105.0, "tp2": 105.0, "tp3": 105.0,
            "components": {"setup": "REFERENCE_EMA_RSI_STOCH_ATR_SESSION"},
            "initial_sl": 95.0, "initial_risk_per_unit": 5.0,
            "position_quantity": 2.0, "remaining_fraction": 1.0,
            "realized_pnl": 0.0, "fees_paid": 0.0,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        }
        changed = bot.manage_signal(signal, 105.0)
        self.assertTrue(changed)
        self.assertEqual(signal["status"], "CLOSED")
        self.assertEqual(signal["closed_reason"], "TP1")
        self.assertEqual(signal["remaining_fraction"], 0.0)
        self.assertTrue(signal["tp1_hit"])
        self.assertFalse(signal["tp2_hit"])
        self.assertFalse(signal["tp3_hit"])
        self.assertAlmostEqual(signal["realized_pnl"], 9.59, places=2)

    def test_unbookable_tp_does_not_move_stop_or_mark_target_hit(self):
        signal = {
            "symbol": "TEST/USDT", "side": "BUY", "status": "ACTIVE",
            "entry": 100.0, "sl": 100.0, "tp1": 105.0, "tp2": 110.0, "tp3": 115.0,
            "remaining_fraction": 1.0, "realized_pnl": 0.0, "fees_paid": 0.0,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        }
        changed = bot.manage_signal(signal, 106.0)
        self.assertFalse(changed)
        self.assertEqual(signal["status"], "ACTIVE")
        self.assertEqual(signal["sl"], 100.0)
        self.assertFalse(signal["tp1_hit"])
        self.assertEqual(signal["remaining_fraction"], 1.0)

    def test_unbookable_stop_does_not_close_signal(self):
        signal = {
            "symbol": "TEST/USDT", "side": "BUY", "status": "ACTIVE",
            "entry": 100.0, "sl": 100.0, "tp1": 105.0, "tp2": 110.0, "tp3": 115.0,
            "remaining_fraction": 1.0, "realized_pnl": 0.0, "fees_paid": 0.0,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
        }
        changed = bot.manage_signal(signal, 99.0)
        self.assertFalse(changed)
        self.assertEqual(signal["status"], "ACTIVE")
        self.assertEqual(signal["remaining_fraction"], 1.0)
        self.assertEqual(signal["realized_pnl"], 0.0)


class SignalAuditTests(unittest.TestCase):
    def test_audit_reports_closed_pnl_and_anomalies(self):
        report = audit_signals({"signals": [
            {"id": "a", "status": "CLOSED", "realized_pnl": 3.0, "components": {"setup": "pullback"}, "score": 15},
            {"id": "b", "status": "CLOSED", "realized_pnl": -1.0, "closed_reason": "SL", "tp1_hit": False},
            {"id": "c", "status": "ACTIVE"},
        ]})
        self.assertEqual(report["counts"]["closed_with_pnl"], 2)
        self.assertEqual(report["counts"]["active"], 1)
        self.assertEqual(report["counts"]["wins"], 1)
        self.assertEqual(report["counts"]["losses"], 1)
        self.assertIn("pullback", report["by_setup"])

    def test_audit_groups_legacy_features_and_checks_winner_concentration(self):
        report = audit_signals({"signals": [
            {"id": "big-win", "symbol": "AAA/USDT", "status": "CLOSED", "realized_pnl": 10,
             "components": {"setup": "X", "market_regime": "RANGE", "nearest_resistance_room_r": 2,
                            "sr_zone_touches": 3, "spike_detected": False, "trendline_up_near": False,
                            "h2_detected": False}},
            {"id": "loss", "symbol": "BBB/USDT", "status": "CLOSED", "realized_pnl": -4,
             "components": {"setup": "X", "market_regime": "DOWNTREND", "nearest_resistance_room_r": 0.5,
                            "sr_zone_touches": 8, "spike_detected": True, "trendline_up_near": True,
                            "h2_detected": True}},
        ]})
        self.assertIn("DOWNTREND", report["by_market_regime"])
        self.assertIn("<1R", report["by_resistance_room_bucket"])
        self.assertIn("7+", report["by_support_touches_bucket"])
        self.assertEqual(report["concentration_sensitivity"]["excluding_largest_winner"]["net_realized_pnl"], -4.0)

    def test_audit_rejects_invalid_shape(self):
        with self.assertRaises(ValueError):
            audit_signals({"unexpected": []})

    def test_audit_ignores_non_finite_pnl_and_flags_inconsistent_tp_order(self):
        report = audit_signals({"signals": [
            {"id": "nan", "status": "CLOSED", "realized_pnl": "nan"},
            {"id": "tp-order", "status": "CLOSED", "realized_pnl": -2,
             "tp1_hit": False, "tp2_hit": True, "tp3_hit": True},
        ]})
        self.assertEqual(report["counts"]["closed_with_pnl"], 1)
        self.assertEqual(report["counts"]["net_realized_pnl"], -2)
        self.assertIn(
            "take_profit_flags_out_of_order",
            [item["type"] for item in report["anomalies"]],
        )

    def test_audit_handles_empty_log_without_division_errors(self):
        report = audit_signals([])
        self.assertEqual(report["counts"]["all_records"], 0)
        self.assertEqual(report["counts"]["closed_with_pnl"], 0)
        self.assertIsNone(report["counts"]["win_rate_pct"])

    def test_stop_diagnostic_flags_counter_trend_and_tight_resistance(self):
        from strategy_diagnostics import diagnose_signal
        report = diagnose_signal({
            "id": "loss-1", "symbol": "ABC/USDT", "status": "CLOSED",
            "closed_reason": "SL", "realized_pnl": -4.0, "score": 16,
            "entry_snapshot": {
                "setup": "SR_BOUNCE", "daily_trend": "BEAR",
                "market_regime": "DOWNTREND", "resistance_room_r": 0.4,
                "ema20_distance_atr": 0.2, "rsi": 55,
                "volume_vs_median": 1.0, "support_zone_touches": 1,
                "score": 16,
            },
        })
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("COUNTER_DAILY_TREND", codes)
        self.assertIn("COUNTER_MARKET_REGIME", codes)
        self.assertIn("LIMITED_RESISTANCE_ROOM", codes)
        self.assertIn("HIGH_SCORE_DID_NOT_PREVENT_LOSS", codes)

    def test_diagnostic_marks_missing_snapshot_instead_of_inventing_cause(self):
        from strategy_diagnostics import diagnose_signal
        report = diagnose_signal({
            "id": "old", "status": "CLOSED", "closed_reason": "SL",
            "realized_pnl": -1.0,
        })
        self.assertEqual(report["missing_evidence"], ["entry_snapshot"])
        self.assertTrue(report["findings"])
        self.assertIn("Hypotheses only", report["interpretation"])


if __name__ == "__main__":
    unittest.main()
