# SAFETY PATCHES — apply to bot.py (2026-11-01 post-mortem)

Full analysis: [POSTMORTEM_2026-11-01.md](POSTMORTEM_2026-11-01.md)

## HOW TO APPLY
1. Add the CONFIG block below to the CONFIG section of `bot.py`
2. Add the `is_tradable_symbol()` function to the UTILITIES section
3. Replace the `risk_pct` check inside `build_signal()`
4. Replace the symbols ranking block inside `ExchangeManager.scan_symbols()`

---

## 1) CONFIG (add after SLIPPAGE_RATE)

```python
MIN_QUOTE_VOLUME_USDT = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "5_000_000"))
MAX_RISK_PCT = 0.025
MIN_ENTRY_PRICE = 0.01
MAX_ABS_SLIPPAGE_USD = 3.0
BLACKLIST_SUBSTRINGS = (
    "3L", "3S", "5L", "5S", "BULL", "BEAR",
    "RMSI", "RCSCO", "RPANW", "RMU", "RAMD", "RAVGO", "RLITE", "RMRVL", "RLS",
)
```

## 2) UTILITIES

```python
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
    if quote_volume is not None and quote_volume < MIN_QUOTE_VOLUME_USDT:
        print(f"[FILTER] {symbol}: quoteVolume {quote_volume:.0f} < MIN_QUOTE_VOLUME_USDT={MIN_QUOTE_VOLUME_USDT:.0f}")
        return False
    return True
```

## 3) inside build_signal() — replace risk_pct block

```python
risk_pct = risk / entry
if not (0.0035 < risk_pct <= MAX_RISK_PCT):
    print(f"[FILTER] {symbol}: risk_pct={risk_pct*100:.2f}% outside bounds")
    return None
if not is_tradable_symbol(symbol, entry_price=entry):
    return None
```

## 4) inside ExchangeManager.scan_symbols ranking

```python
symbols = [
    symbol for vol, symbol in sorted(ranked, reverse=True)
    if vol >= MIN_QUOTE_VOLUME_USDT and is_tradable_symbol(symbol, quote_volume=vol)
]
dropped = len(ranked) - len(symbols)
if dropped:
    print(f"[LIQUIDITY] {name}: dropped {dropped} symbols (volume/blacklist)")
```
