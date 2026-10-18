import unittest
from unittest.mock import patch

import pandas as pd

import backtest


def bars(values):
    return pd.DataFrame(values, columns=["timestamp", "open", "high", "low", "close", "volume"])


class BacktestSimulationTests(unittest.TestCase):
    def test_staged_tp_50_30_20_closes_at_tp3(self):
        candles = bars([
            [0, 100, 106, 101, 104, 1],
            [1, 104, 111, 101, 109, 1],
            [2, 109, 116, 101, 114, 1],
        ])
        signal = {"entry": 100, "sl": 95, "tp1": 105, "tp2": 110,
                  "tp3": 115, "position_quantity": 1}
        with patch.object(backtest.bot, "FEE_RATE", 0.0):
            result = backtest.simulate_trade(signal, candles, 0)
        self.assertEqual(result["reason"], "TP3")
        self.assertEqual(result["exit_index"], 2)
        self.assertAlmostEqual(result["pnl"], 8.5)

    def test_stop_wins_if_stop_and_tp1_touch_same_candle(self):
        candles = bars([[0, 100, 106, 94, 100, 1]])
        signal = {"entry": 100, "sl": 95, "tp1": 105, "tp2": 110,
                  "tp3": 115, "position_quantity": 1}
        with patch.object(backtest.bot, "FEE_RATE", 0.0):
            result = backtest.simulate_trade(signal, candles, 0)
        self.assertEqual(result["reason"], "SL")
        self.assertAlmostEqual(result["pnl"], -5.0)

    def test_summary_reports_expectancy_and_drawdown(self):
        trades = [
            {"symbol": "TEST/USDT", "signal_time": "2026-01-01T00:00:00+00:00", "pnl": 2.0},
            {"symbol": "TEST/USDT", "signal_time": "2026-01-02T00:00:00+00:00", "pnl": -1.0},
            {"symbol": "TEST/USDT", "signal_time": "2026-01-04T00:00:00+00:00", "pnl": 3.0},
        ]
        report = backtest.summarize([
            {"symbol": "TEST/USDT", "bars": 10, "trades": trades, "error": None,
             "start_time": "2025-12-01T00:00:00+00:00",
             "end_time": "2026-01-10T00:00:00+00:00",
             "oos_cutoff": "2026-01-03T00:00:00+00:00"}
        ])
        overall = report["overall"]
        self.assertEqual(overall["trades"], 3)
        self.assertAlmostEqual(overall["net_pnl_usd"], 4.0)
        self.assertAlmostEqual(overall["expectancy_usd_per_trade"], 4.0 / 3.0, places=3)
        self.assertAlmostEqual(overall["max_drawdown_usd"], 1.0)
        self.assertEqual(report["out_of_sample_last_30_percent"]["trades"], 1)
        self.assertAlmostEqual(report["out_of_sample_last_30_percent"]["net_pnl_usd"], 3.0)


if __name__ == "__main__":
    unittest.main()
