"""CCXT adapters and market-universe discovery."""
from __future__ import annotations

import traceback

import ccxt

from config import (
    MAX_COINS,
    MAX_SYMBOLS_PER_EXCHANGE,
    OHLCV_LIMIT,
    QUOTE,
    REQUEST_TIMEOUT_MS,
    SCAN_EXCHANGE_NAMES,
    TIMEFRAME,
)
from safety_filters import is_tradable_symbol
from utils import safe_float


class ExchangeManager:
    def __init__(self):
        self.exchanges = {}
        self.market_counts = {}

    def build(self, required_exchanges=None, require_scanners=True):
        # Load only Binance for scanning plus venues that actually hold active legacy signals.
        # Loading every configured exchange on every run adds several avoidable network round-trips.
        scanner_names = SCAN_EXCHANGE_NAMES if require_scanners else []
        names = list(dict.fromkeys(scanner_names + list(required_exchanges or [])))
        for name in names:
            try:
                cls = getattr(ccxt, name)
                exchange_config = {
                    "enableRateLimit": True,
                    "timeout": REQUEST_TIMEOUT_MS,
                }
                if name == "bitget":
                    # Only load linear perpetual markets; unrelated spot/options
                    # metadata endpoints are not required by this scanner.
                    exchange_config["options"] = {
                        "defaultType": "swap",
                        "defaultSubType": "linear",
                        "fetchMarkets": ["swap"],
                    }
                ex = cls(exchange_config)
                ex.load_markets()
                self.exchanges[name] = ex
                self.market_counts[name] = len(ex.markets)
                print(f"[OK] {name}: {len(ex.markets)} markets")
            except Exception as exc:
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
                # Keep the root cause visible in GitHub Actions and continue to other venues.
                traceback.print_exc(limit=5)

        available_scanners = [name for name in scanner_names if name in self.exchanges]
        if require_scanners and not available_scanners:
            raise RuntimeError(
                "No configured futures data source initialized successfully: "
                + ", ".join(scanner_names)
            )
        if available_scanners:
            print("[DATA SOURCES] active futures scanners: " + ", ".join(available_scanners))
        elif not require_scanners:
            print("[DATA SOURCES] scanner initialization skipped; managing existing signals only")
        return True

    def get_exchange(self, name):
        return self.exchanges.get(name)

    def market_symbols(self, exchange):
        result = []
        for symbol, market in exchange.markets.items():
            if not market.get("active", True):
                continue
            if not market.get("spot", False):
                continue
            if market.get("quote") != QUOTE:
                continue
            if market.get("base") in {"USDT", "USDC", "USD", "BUSD"}:
                continue
            result.append(symbol)
        return sorted(set(result))

    def futures_symbols(self, exchange):
        """Active linear USDT-margined perpetual contracts."""
        result = []
        for symbol, market in exchange.markets.items():
            if not market.get("active", True) or not market.get("swap", False):
                continue
            if not market.get("linear", False):
                continue
            if market.get("quote") != QUOTE or market.get("settle") != QUOTE:
                continue
            if market.get("base") in {"USDT", "USDC", "USD", "BUSD"}:
                continue
            result.append(symbol)
        return sorted(set(result))

    def scan_symbols(self):
        # New entries are exclusively Binance USDⓈ-M perpetual futures.
        ordered = []
        seen = set()

        for name in SCAN_EXCHANGE_NAMES:
            ex = self.exchanges.get(name)
            if not ex:
                continue

            symbols = self.futures_symbols(ex)
            # Fail closed: do not scan symbols when quote-volume evidence is unavailable.
            try:
                tickers = ex.fetch_tickers(symbols)
                ranked = []
                rejected = 0
                for symbol in symbols:
                    ticker = tickers.get(symbol, {})
                    quote_volume = safe_float(ticker.get("quoteVolume"))
                    if not is_tradable_symbol(symbol, quote_volume=quote_volume):
                        rejected += 1
                        continue
                    ranked.append((quote_volume, symbol))
                symbols = [symbol for _, symbol in sorted(ranked, reverse=True)]
                print(f"[LIQUIDITY] {name}: accepted={len(symbols)} rejected={rejected}")
            except Exception as exc:
                print(f"[LIQUIDITY] {name}: ticker data unavailable; skipping venue for safety ({type(exc).__name__})")
                continue
            print(f"{name}: {len(symbols)} {QUOTE} USDT-M perpetual symbols")

            added_from_exchange = 0
            for symbol in symbols:
                if symbol in seen:
                    continue
                ordered.append((name, symbol))
                seen.add(symbol)
                added_from_exchange += 1

                if MAX_COINS is not None and len(ordered) >= MAX_COINS:
                    return ordered
                if MAX_SYMBOLS_PER_EXCHANGE is not None and added_from_exchange >= MAX_SYMBOLS_PER_EXCHANGE:
                    break

        if not ordered:
            raise RuntimeError(
                "No active linear USDT-margined perpetual markets found on: "
                + ", ".join(SCAN_EXCHANGE_NAMES)
            )
        print(f"[SCAN UNIVERSE] {len(ordered)} active USDT perpetual markets")
        return ordered

    def fetch_ohlcv(self, exchange, symbol, timeframe=TIMEFRAME, limit=OHLCV_LIMIT):
        return exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
        )

    def ticker_price(self, exchange, symbol):
        try:
            ticker = exchange.fetch_ticker(symbol)
            return safe_float(ticker.get("last"))
        except Exception:
            return None
