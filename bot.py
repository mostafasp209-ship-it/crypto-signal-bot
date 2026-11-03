import os
import json
import time
import math
import traceback
import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

import ccxt
import numpy as np
import pandas as pd
import requests

from safety_filters import (
    MIN_QUOTE_VOLUME_USDT,
    MAX_RISK_PCT,
    MIN_ENTRY_PRICE,
    MAX_ABS_SLIPPAGE_USD,
    BLACKLIST_SUBSTRINGS,
    is_tradable_symbol,
)

# ============================================================
# CONFIG
# ============================================================

EXCHANGE_NAMES = [
    # Legacy venues may still be needed to manage already-open signals.
    "binanceus", "bitstamp", "bitfinex", "cryptocom", "bitget", "binanceusdm", "bybit",
]
# Binance USD-M returned HTTP 451 from GitHub Actions (restricted-location response).
# Use liquid USDT linear perpetual venues as scanner sources instead.
SCAN_EXCHANGE_NAMES = ["bybit", "bitget"]
FUNDING_SOURCES = ["bybit", "bitget"]

QUOTE = "USDT"
TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"

MAX_COINS = None  # Scan every eligible active USDT-M perpetual contract
MAX_SYMBOLS_PER_EXCHANGE = None  # No per-exchange symbol cap
MIN_SCORE = 12
# Reject BUY entries with too little upside room before the nearest resistance.
# Initial threshold; validate against out-of-sample backtests before tuning.
MIN_RESISTANCE_ROOM_R = 0.75
# RR is weighted across TP1/TP2/TP3 and includes estimated round-trip fees.
MIN_RR = 1.35

SIGNAL_EXPIRY_HOURS = 48
RISK_USD = 10.0
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

TP_WEIGHTS = {
    "tp1": 0.50,
    "tp2": 0.30,
    "tp3": 0.20,
}

DATA_FILE = "signals.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", TELEGRAM_CHAT_ID).strip()
USERS_FILE = os.getenv("USERS_FILE", "users.json")

REQUEST_TIMEOUT_MS = 15000
OHLCV_LIMIT = 260
DAILY_LIMIT = 120
SLEEP_BETWEEN_SYMBOLS = 0.05
BITFINEX_RATE_LIMIT_COOLDOWN_SECONDS = 0.0  # Skip Bitfinex for the remainder of a scan after a rate-limit response
NEWS_FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/", "https://cointelegraph.com/rss"]
NEWS_LOOKBACK_HOURS = 24
NEWS_TIMEOUT_SECONDS = 8


# ============================================================
# UTILITIES
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        x = float(value)
        if not math.isfinite(x):
            return default
        return x
    except Exception:
        return default


def fmt_price(value):
    x = safe_float(value)
    if x is None:
        return "N/A"
    if abs(x) >= 100:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}"
    if abs(x) >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}"


def pct(a, b):
    a = safe_float(a)
    b = safe_float(b)
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ============================================================
# PERSISTENCE
# ============================================================

def load_state():
    if not os.path.exists(DATA_FILE):
        return {"signals": [], "last_run": None}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {"signals": [], "last_run": None}

        data.setdefault("signals", [])
        data.setdefault("last_run", None)
        return data
    except Exception:
        print("WARNING: signals.json could not be read; starting clean.")
        return {"signals": [], "last_run": None}


def save_state(state):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_recipients():
    """Return admin plus opted-in users from users.json, without duplicate chat IDs.

    Supported users.json shapes:
      {"users": [{"chat_id": 123, "blocked": false, "notify": true}]}
      [{"chat_id": 123, "blocked": false, "notify": true}]
      {"123": {"blocked": false, "notify": true}, "456": true}
      {"users": [123, 456]}
    """
    recipients = []
    seen = set()

    def add(chat_id):
        if chat_id is None:
            return
        chat_id = str(chat_id).strip()
        if not chat_id or chat_id in seen:
            return
        seen.add(chat_id)
        recipients.append(chat_id)

    # Keep the configured administrator/main chat as a recipient.
    add(ADMIN_CHAT_ID or TELEGRAM_CHAT_ID)

    if not os.path.exists(USERS_FILE):
        print(f"[TELEGRAM] {USERS_FILE} not found; only the admin chat will receive messages.")
        return recipients

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[TELEGRAM] Could not read {USERS_FILE}: {exc}; using admin only.")
        return recipients

    users = data.get("users", data) if isinstance(data, dict) else data
    if isinstance(users, dict):
        entries = []
        for key, value in users.items():
            # A dict keyed by chat ID is supported as well as a wrapper object.
            if key == "users" and isinstance(value, list):
                entries.extend(value)
            else:
                entries.append((key, value))
    elif isinstance(users, list):
        entries = users
    else:
        entries = []

    for entry in entries:
        chat_id = None
        settings = {}
        if isinstance(entry, (str, int)):
            chat_id = entry
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            chat_id, settings = entry
            if not isinstance(settings, dict):
                settings = {"notify": bool(settings)}
        elif isinstance(entry, dict):
            settings = entry
            chat_id = entry.get("chat_id", entry.get("telegram_id", entry.get("user_id", entry.get("id"))))
        if settings.get("blocked") is True or settings.get("is_blocked") is True:
            continue
        if settings.get("notify") is False or settings.get("notifications") is False or settings.get("active") is False:
            continue
        add(chat_id)

    print(f"[TELEGRAM] recipients loaded: {len(recipients)}")
    return recipients


def telegram_send(text):
    """Send a message to the administrator and all subscribed users."""
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram bot token is missing; message skipped.")
        return False

    recipients = telegram_recipients()
    if not recipients:
        print("No Telegram recipients configured; message skipped.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    delivered = 0
    failed = 0

    for chat_id in recipients:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if r.ok:
                delivered += 1
            else:
                failed += 1
                # Telegram often returns 403 when a user has not started the bot or blocked it.
                print(f"[TELEGRAM FAIL] chat_id={chat_id}: HTTP {r.status_code} {r.text[:250]}")
        except Exception as exc:
            failed += 1
            print(f"[TELEGRAM FAIL] chat_id={chat_id}: {type(exc).__name__}: {exc}")
        time.sleep(0.05)

    print(f"[TELEGRAM] delivered={delivered} failed={failed} total={len(recipients)}")
    return delivered > 0


# ============================================================
# EXCHANGE MANAGER
# ============================================================

class ExchangeManager:
    def __init__(self):
        self.exchanges = {}
        self.market_counts = {}

    def build(self, required_exchanges=None):
        # Load only Binance for scanning plus venues that actually hold active legacy signals.
        # Loading every configured exchange on every run adds several avoidable network round-trips.
        names = list(dict.fromkeys(
            SCAN_EXCHANGE_NAMES + list(required_exchanges or [])
        ))
        for name in names:
            try:
                cls = getattr(ccxt, name)
                ex = cls({
                    "enableRateLimit": True,
                    "timeout": REQUEST_TIMEOUT_MS,
                })
                ex.load_markets()
                self.exchanges[name] = ex
                self.market_counts[name] = len(ex.markets)
                print(f"[OK] {name}: {len(ex.markets)} markets")
            except Exception as exc:
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")

        available_scanners = [name for name in SCAN_EXCHANGE_NAMES if name in self.exchanges]
        if not available_scanners:
            raise RuntimeError(
                "No configured futures data source initialized successfully: "
                + ", ".join(SCAN_EXCHANGE_NAMES)
            )
        print("[DATA SOURCES] active futures scanners: " + ", ".join(available_scanners))
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
            # Prefer liquid markets instead of alphabetically scanning obscure tickers first.
            try:
                tickers = ex.fetch_tickers(symbols)
                ranked = []
                for symbol in symbols:
                    ticker = tickers.get(symbol, {})
                    quote_volume = safe_float(ticker.get("quoteVolume"), 0.0) or 0.0
                    ranked.append((quote_volume, symbol))
                symbols = [
                    symbol for vol, symbol in sorted(ranked, reverse=True)
                    if vol >= MIN_QUOTE_VOLUME_USDT and is_tradable_symbol(symbol, quote_volume=vol)
                ]
                dropped = len(ranked) - len(symbols)
                if dropped:
                    print(f"[LIQUIDITY] {name}: dropped {dropped} symbols (volume/blacklist)")
            except Exception as exc:
                print(f"[LIQUIDITY] {name}: ticker ranking unavailable ({type(exc).__name__}); using market list")
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


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):
    df = df.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    df["bb_mid"] = mid
    df["bb_upper"] = mid + 2 * std
    df["bb_lower"] = mid - 2 * std

    # Ichimoku
    tenkan = (
        high.rolling(9).max() +
        low.rolling(9).min()
    ) / 2

    kijun = (
        high.rolling(26).max() +
        low.rolling(26).min()
    ) / 2

    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = (
        (
            high.rolling(52).max() +
            low.rolling(52).min()
        ) / 2
    ).shift(26)

    df["tenkan"] = tenkan
    df["kijun"] = kijun
    df["span_a"] = span_a
    df["span_b"] = span_b

    return df
