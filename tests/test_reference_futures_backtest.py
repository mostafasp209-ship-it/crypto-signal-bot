import unittest
import pandas as pd
from research.reference_futures_backtest import funding_cashflow, metrics


class ReferenceFuturesBacktestTests(unittest.TestCase):
    def test_positive_funding_debits_long_and_credits_short(self):
        funding = pd.DataFrame({
            "funding_time": pd.to_datetime(["2024-01-01T08:00:00Z", "2024-01-01T16:00:00Z"], utc=True),
            "funding_rate": [0.001, 0.0005],
            "mark_price": [1000.0, 1000.0],
        })
        start = pd.Timestamp("2024-01-01T00:00:00Z")
        end = pd.Timestamp("2024-01-01T20:00:00Z")
        long_cash, n = funding_cashflow(funding, start, end, "LONG", 1.0)
        short_cash, _ = funding_cashflow(funding, start, end, "SHORT", 1.0)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(long_cash, -1.5)
        self.assertAlmostEqual(short_cash, 1.5)

    def test_funding_excludes_events_before_entry_and_after_exit(self):
        funding = pd.DataFrame({
            "funding_time": pd.to_datetime([
                "2024-01-01T00:00:00Z", "2024-01-01T08:00:00Z", "2024-01-01T16:00:00Z"
            ], utc=True),
            "funding_rate": [0.01, 0.001, 0.01],
            "mark_price": [100.0, 100.0, 100.0],
        })
        cash, n = funding_cashflow(
            funding, pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-01-01T12:00:00Z"), "LONG", 1.0)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(cash, -0.1)


    def test_funding_ignores_rows_without_valid_event_mark(self):
        funding = pd.DataFrame({
            "funding_time": pd.to_datetime(["2024-01-01T08:00:00Z", "2024-01-01T16:00:00Z"], utc=True),
            "funding_rate": [0.001, 0.001],
            "mark_price": [None, 100.0],
        })
        cash, n = funding_cashflow(
            funding, pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-01-01T20:00:00Z"), "LONG", 1.0)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(cash, -0.1)

    def test_metrics_reports_net_expectancy_and_drawdown(self):
        result = metrics([
            {"exit_time":"2024-01-01T04:00:00Z","net_r":1.0,"funding_quote_per_1_quote":0.001,"fees_quote_per_1_quote":0.002},
            {"exit_time":"2024-01-01T08:00:00Z","net_r":-0.5,"funding_quote_per_1_quote":-0.001,"fees_quote_per_1_quote":0.002},
        ])
        self.assertEqual(result["trades"], 2)
        self.assertAlmostEqual(result["expectancy_R"], 0.25)
        self.assertAlmostEqual(result["net_R"], 0.5)
        self.assertAlmostEqual(result["max_drawdown_R"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
