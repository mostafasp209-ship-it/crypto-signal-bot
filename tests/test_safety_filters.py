import unittest

from safety_filters import (
    BLACKLIST_SUBSTRINGS,
    MAX_RISK_PCT,
    MIN_ENTRY_PRICE,
    MIN_QUOTE_VOLUME_USDT,
    is_tradable_symbol,
)


class SafetyFilterTests(unittest.TestCase):
    def test_liquid_symbol_is_accepted(self):
        self.assertTrue(is_tradable_symbol("BTC/USDT:USDT", quote_volume=MIN_QUOTE_VOLUME_USDT))

    def test_low_volume_symbol_is_rejected(self):
        self.assertFalse(is_tradable_symbol("THIN/USDT:USDT", quote_volume=MIN_QUOTE_VOLUME_USDT - 1))

    def test_missing_volume_is_rejected_closed(self):
        self.assertFalse(is_tradable_symbol("UNKNOWN/USDT:USDT", quote_volume=None))

    def test_price_only_check_does_not_require_volume(self):
        self.assertTrue(is_tradable_symbol("BTC/USDT:USDT", entry_price=100.0,
                                           require_quote_volume=False))

    def test_blacklisted_leveraged_token_is_rejected(self):
        token = next(item for item in BLACKLIST_SUBSTRINGS if item in "BTC3L")
        self.assertFalse(is_tradable_symbol(f"BTC{token}/USDT:USDT", quote_volume=MIN_QUOTE_VOLUME_USDT))

    def test_micro_price_is_rejected(self):
        self.assertFalse(is_tradable_symbol("BTC/USDT:USDT", quote_volume=MIN_QUOTE_VOLUME_USDT,
                                            entry_price=MIN_ENTRY_PRICE - 0.001))

    def test_risk_policy_is_at_most_two_point_five_percent(self):
        self.assertLessEqual(MAX_RISK_PCT, 0.025)


if __name__ == "__main__":
    unittest.main(verbosity=2)
