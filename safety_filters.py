"""Safety filters from 2026-11-01 post-mortem. Import in bot.py."""
import os

MIN_QUOTE_VOLUME_USDT = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "5000000"))
MAX_RISK_PCT = 0.025
MIN_ENTRY_PRICE = 0.01
MAX_ABS_SLIPPAGE_USD = 3.0
# Suffixes only. Short tokens such as "RLS" used to match inside normal tickers and
# silently delete most of the universe.
LEVERAGED_SUFFIXES = ("3L", "3S", "5L", "5S", "BULL", "BEAR")
EXACT_BLACKLIST = {
    "RMSI", "RCSCO", "RPANW", "RMU", "RAMD", "RAVGO", "RLITE", "RMRVL", "RLS",
}


def is_tradable_symbol(symbol, quote_volume=None, entry_price=None, verbose=True):
    """Hard quality gates from the 2026-11-01 post-mortem."""
    base = (symbol or "").split("/")[0].split(":")[0].upper()
    if base in EXACT_BLACKLIST or base.endswith(LEVERAGED_SUFFIXES):
        if verbose:
            print(f"[FILTER] {symbol}: blacklisted base '{base}'")
        return False
    if entry_price is not None and entry_price < MIN_ENTRY_PRICE:
        if verbose:
            print(f"[FILTER] {symbol}: entry {entry_price} < MIN_ENTRY_PRICE={MIN_ENTRY_PRICE}")
        return False
    if quote_volume is not None and quote_volume < MIN_QUOTE_VOLUME_USDT:
        if verbose:
            print(f"[FILTER] {symbol}: quoteVolume {quote_volume:.0f} < MIN_QUOTE_VOLUME_USDT={MIN_QUOTE_VOLUME_USDT:.0f}")
        return False
    return True