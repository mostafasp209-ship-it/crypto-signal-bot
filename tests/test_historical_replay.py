import unittest

from historical_replay import simulate_signal


def signal():
    return {
        "side": "BUY", "entry": 100, "initial_sl": 95, "sl": 95,
        "tp1": 105, "tp2": 110, "tp3": 115, "position_quantity": 10,
    }


class HistoricalReplayTests(unittest.TestCase):
    def test_stop_first_when_same_candle_touches_stop_and_target(self):
        result = simulate_signal(signal(), [[0, 100, 106, 94, 100, 1]])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["simulated_exit"], "SL")
        self.assertEqual(result["tp_hits"], [])
        self.assertEqual(result["ambiguous_stop_target_bars"], 1)

    def test_staged_targets_and_break_even_stop(self):
        candles = [
            [0, 100, 106, 99, 105, 1],
            [60000, 105, 111, 104, 110, 1],
            [120000, 110, 116, 109, 115, 1],
        ]
        result = simulate_signal(signal(), candles)
        self.assertEqual(result["simulated_exit"], "TP3")
        self.assertEqual(result["tp_hits"], ["tp1", "tp2", "tp3"])
        self.assertEqual(result["simulated_pnl_usd"], 82.915)

    def test_marks_open_remainder_at_last_close(self):
        result = simulate_signal(signal(), [[0, 100, 103, 99, 102, 1]])
        self.assertEqual(result["simulated_exit"], "MARKED_AT_LAST_CLOSE")
        self.assertIsNotNone(result["simulated_pnl_usd"])


if __name__ == "__main__":
    unittest.main()
