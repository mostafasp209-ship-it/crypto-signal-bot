import unittest

import pandas as pd

import backtest


def bars(rows):
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    return pd.DataFrame([
        {
            "timestamp": start + pd.Timedelta(hours=4 * i),
            "open": row[0],
            "high": row[1],
            "low": row[2],
            "close": row[3],
            "volume": 1000.0,
        }
        for i, row in enumerate(rows)
    ])


def signal(**overrides):
    result = {
        "entry": 100.0,
        "sl": 95.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "resistance": None,
        "score": 16,
        "components": {"setup": "SR_FIRST_TOUCH", "market_regime": "RANGE"},
    }
    result.update(overrides)
    return result


class BacktestSimulationTests(unittest.TestCase):
    def test_stop_first_when_stop_and_target_hit_same_candle(self):
        data = bars([
            (100, 101, 99, 100),
            (100, 106, 94, 101),
            (101, 102, 100, 101),
        ])
        trade = backtest.simulate_trade(signal(), data, 0, "BTC/USDT")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["exit_reason"], "SL")
        self.assertFalse(trade["tp1_hit"])
        self.assertLess(trade["net_return_pct"], -5.0)

    def test_tp1_moves_stop_to_entry_for_remaining_position(self):
        data = bars([
            (100, 101, 99, 100),
            (100, 106, 99, 105),
            (101, 102, 101, 101),
            (101, 102, 100, 101),
        ])
        trade = backtest.simulate_trade(signal(), data, 0, "BTC/USDT")
        self.assertIsNotNone(trade)
        self.assertTrue(trade["tp1_hit"])
        self.assertEqual(trade["exit_reason"], "SL_AFTER_TP")
        self.assertGreater(trade["net_return_pct"], 0.0)

    def test_expiry_uses_twelve_four_hour_bars(self):
        data = bars([(100, 101, 99, 100) for _ in range(14)])
        trade = backtest.simulate_trade(signal(), data, 0, "BTC/USDT")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["exit_reason"], "EXPIRY")
        self.assertEqual(
            pd.Timestamp(trade["exit_time"]) - pd.Timestamp(trade["entry_time"]),
            pd.Timedelta(hours=48),
        )

    def test_summary_handles_empty_trade_list(self):
        self.assertEqual(backtest.summarize(trades := [])["trades"], len(trades))


if __name__ == "__main__":
    unittest.main()
