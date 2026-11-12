import unittest

from risk import calculate_risk_plan


class RiskPlanTests(unittest.TestCase):
    def test_buy_plan_accounts_for_fees_slippage_and_position_cap(self):
        plan = calculate_risk_plan(
            side="BUY", entry=100, stop=98, target=103,
            risk_budget_usd=20, max_position_usd=500,
            fee_rate=0.001, slippage_rate=0.0005,
        )
        self.assertIsNotNone(plan)
        expected_cost = (100 + 98) * 0.001 + 98 * 0.0005
        expected_reward = abs(103 * (1 - 0.0005) - 100) - (100 + 103) * 0.001
        self.assertAlmostEqual(plan["cost_per_unit"], expected_cost)
        self.assertAlmostEqual(plan["net_rr"], expected_reward / 2)
        self.assertLessEqual(plan["position_value_usd"], 500)

    def test_sell_plan_uses_adverse_short_target_slippage(self):
        plan = calculate_risk_plan(
            side="SELL", entry=100, stop=102, target=97,
            risk_budget_usd=20, max_position_usd=0,
            fee_rate=0, slippage_rate=0.001,
        )
        self.assertIsNotNone(plan)
        expected_reward = 100 - 97 * (1 + 0.001)
        self.assertAlmostEqual(plan["net_target_reward_per_unit"], expected_reward)
        self.assertFalse(bool(plan["position_capped"]))

    def test_invalid_geometry_and_non_finite_inputs_are_rejected(self):
        self.assertIsNone(calculate_risk_plan(
            side="BUY", entry=100, stop=101, target=105,
            risk_budget_usd=10, max_position_usd=100,
            fee_rate=0.001, slippage_rate=0.001,
        ))
        self.assertIsNone(calculate_risk_plan(
            side="SELL", entry=100, stop=105, target=101,
            risk_budget_usd=10, max_position_usd=100,
            fee_rate=0.001, slippage_rate=0.001,
        ))
        self.assertIsNone(calculate_risk_plan(
            side="BUY", entry=float("nan"), stop=95, target=110,
            risk_budget_usd=10, max_position_usd=100,
            fee_rate=0, slippage_rate=0,
        ))

    def test_adverse_slippage_cannot_be_misread_as_positive_reward(self):
        plan = calculate_risk_plan(
            side="SELL", entry=100, stop=102, target=97,
            risk_budget_usd=10, max_position_usd=0,
            fee_rate=0, slippage_rate=0.05,
        )
        self.assertIsNone(plan)

    def test_zero_fee_and_slippage_are_valid_research_assumptions(self):
        plan = calculate_risk_plan(
            side="BUY", entry=100, stop=95, target=110,
            risk_budget_usd=10, max_position_usd=0,
            fee_rate=0, slippage_rate=0,
        )
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["net_rr"], 2.0)


if __name__ == "__main__":
    unittest.main()
