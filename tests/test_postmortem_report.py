import unittest

from postmortem_report import build_report


class PostmortemReportTests(unittest.TestCase):
    def test_groups_outcomes_and_marks_small_samples(self):
        report = build_report({"signals": [
            {"symbol": "A/USDT", "status": "CLOSED", "realized_pnl": 4,
             "closed_reason": "TP3", "components": {"setup": "FAILED_BREAKDOWN", "spike_detected": False}},
            {"symbol": "B/USDT", "status": "CLOSED", "realized_pnl": -2,
             "closed_reason": "SL", "components": {"setup": "FAILED_BREAKDOWN", "spike_detected": True}},
            {"symbol": "C/USDT", "status": "ACTIVE", "realized_pnl": 0},
        ]})
        self.assertEqual(report["counts"]["closed_with_pnl"], 2)
        self.assertEqual(report["overall"]["net_pnl_usd"], 2)
        self.assertEqual(report["by_feature"]["setup"]["FAILED_BREAKDOWN"]["trades"], 2)
        self.assertEqual(report["by_feature"]["spike_detected"]["true"]["losses"], 1)
        self.assertEqual(report["counts"]["entry_snapshot_coverage_pct"], 0)
        self.assertEqual(report["largest_winner_sensitivity"]["overall_net_excluding_largest_winner_usd"], -2)

    def test_winner_loser_profile_compares_prevalence_and_parses_string_false(self):
        report = build_report({"signals": [
            {"symbol": "A/USDT", "status": "CLOSED", "realized_pnl": 4,
             "closed_reason": "TP3", "components": {"spike_detected": False}},
            {"symbol": "B/USDT", "status": "CLOSED", "realized_pnl": -2,
             "closed_reason": "SL", "components": {"spike_detected": "false"}},
            {"symbol": "C/USDT", "status": "CLOSED", "realized_pnl": -3,
             "closed_reason": "SL", "components": {"spike_detected": "true"}},
        ]})
        profile = {row["bucket"]: row for row in report["winner_loser_profile"]["spike_detected"]}
        self.assertEqual(profile["false"]["winner_count"], 1)
        self.assertEqual(profile["false"]["loser_count"], 1)
        self.assertEqual(profile["true"]["loser_count"], 1)
        self.assertTrue(profile["true"]["small_sample"])

    def test_candidate_filters_are_labeled_diagnostic_and_do_not_mutate_data(self):
        signals = [
            {"symbol": "A/USDT", "status": "CLOSED", "realized_pnl": 5,
             "components": {"spike_detected": False, "trendline_up_near": False, "setup": "FAILED_BREAKDOWN"}},
            {"symbol": "B/USDT", "status": "CLOSED", "realized_pnl": -3,
             "components": {"spike_detected": "true", "trendline_up_near": False, "setup": "FAILED_BREAKDOWN"}},
            {"symbol": "C/USDT", "status": "CLOSED", "realized_pnl": -2,
             "components": {"spike_detected": False, "trendline_up_near": True, "setup": "SR_BOUNCE"}},
        ]
        report = build_report({"signals": signals})
        candidate = report["candidate_filter_diagnostics"]["exclude_spike_or_trendline_near"]
        self.assertEqual(candidate["trades"], 1)
        self.assertEqual(candidate["net_pnl_usd"], 5)
        self.assertEqual(candidate["excluded_trades"], 2)
        self.assertEqual(candidate["excluded_net_pnl_usd"], -5)
        self.assertEqual(candidate["interpretation"], "IN_SAMPLE_DIAGNOSTIC_NOT_VALIDATION")
        self.assertEqual(len(signals), 3)

    def test_empty_input_is_safe(self):
        report = build_report({"signals": []})
        self.assertEqual(report["counts"]["closed_with_pnl"], 0)
        self.assertIsNone(report["overall"]["win_rate_pct"])
        self.assertEqual(report["anomalies"], [])

    def test_missing_signal_list_is_rejected(self):
        with self.assertRaises(ValueError):
            build_report({"unexpected": []})


if __name__ == "__main__":
    unittest.main()
