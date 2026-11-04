import unittest
import pandas as pd

from research.rsi_stoch_exit_lab import metrics, net_return, simulate


class RsiStochExitLabTests(unittest.TestCase):
    def test_costs_reduce_gross_profit(self):
        gross = net_return(1, 100.0, 102.0, fee_rate=0.0, slippage=0.0)
        net = net_return(1, 100.0, 102.0, fee_rate=0.0005, slippage=0.0002)
        self.assertGreater(gross, net)
        self.assertGreater(net, 0)

    def test_metrics_are_r_multiple_based(self):
        result = metrics([{"net_r": 2.0, "time": "2026-01-01"},
                          {"net_r": -1.0, "time": "2026-01-02"}])
        self.assertEqual(result["trades"], 2)
        self.assertAlmostEqual(result["expectancy_r"], 0.5)
        self.assertAlmostEqual(result["profit_factor"], 2.0)

    def test_same_bar_stop_and_target_is_stop_first(self):
        n = 230
        frame = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
            "signal": [0] * n,
            "open": [100.0] * n,
            "high": [100.5] * n,
            "low": [99.5] * n,
            "close": [100.0] * n,
            "atr": [1.0] * n,
            "swing_low": [99.0] * n,
            "swing_high": [101.0] * n,
        })
        frame.loc[220, "signal"] = 1
        frame.loc[221, "high"] = 103.0
        frame.loc[221, "low"] = 98.0
        trades = simulate(frame, 220, n, 1.0, 1.5, 6, False, None)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "SL")
        self.assertLess(trades[0]["net_r"], 0)


if __name__ == "__main__":
    unittest.main()
