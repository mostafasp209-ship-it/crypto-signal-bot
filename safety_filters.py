"""Safety filters from 2026-11-01 post-mortem. Import in bot.py."""
import os

MIN_QUOTE_VOLUME_USDT = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "5_000_000"))
MAX_RISK_PCT = 0.025
MIN_ENTRY_PRICE = 0.01
MAX_ABS_SLIPPAGE_USD = 3.0
BLACKLIST_SUBSTRINGS = (
    "3L", "3S", "5L", "5S", "BULL", "BEAR",
    "RMSI", "RCSCO", "RPANW", "RMU", "RAMD", "RAVGO", "RLITE", "RMRVL", "RLS",
)


def is_tradable_symbol(symbol, quote_volume=None, entry_price=None):
    """Hard quality gates from the 2026-11-01 post-mortem."""
    base = (symbol or "").split("/")[0].upper()
    for token in BLACKLIST_SUBSTRINGS:
        if token in base:
            print(f"[FILTER] {symbol}: blacklisted substring '{token}'")
            return False
    if entry_price is not None and entry_price < MIN_ENTRY_PRICE:
        print(f"[FILTER] {symbol}: entry {entry_price} < MIN_ENTRY_PRICE={MIN_ENTRY_PRICE}")
        return False
    if quote_volume is None:
        print(f"[FILTER] {symbol}: quoteVolume unavailable; rejecting without liquidity evidence")
        return False
    if quote_volume < MIN_QUOTE_VOLUME_USDT:
        print(f"[FILTER] {symbol}: quoteVolume {quote_volume:.0f} < MIN_QUOTE_VOLUME_USDT={MIN_QUOTE_VOLUME_USDT:.0f}")
        return False
    return True
