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
        report = backtest.summarize([
            {"symbol": "TEST/USDT", "bars": 10,
             "trades": [{"pnl": 2.0}, {"pnl": -1.0}, {"pnl": 3.0}], "error": None}
        ])
        self.assertEqual(report["trades"], 3)
        self.assertAlmostEqual(report["net_pnl_usd"], 4.0)
        self.assertAlmostEqual(report["expectancy_usd_per_trade"], 4.0 / 3.0)
        self.assertAlmostEqual(report["max_drawdown_usd"], 1.0)


if __name__ == "__main__":
    unittest.main()
