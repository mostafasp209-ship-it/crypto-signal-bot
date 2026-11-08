"""Central configuration for the crypto signal bot.

Environment variables are read once at process start. Keep strategy defaults
here so runtime behavior is discoverable and tests can override module values.
"""
from __future__ import annotations

import os

# Exchanges used to manage legacy signals plus current scanner venues.
EXCHANGE_NAMES = [
    "binanceus", "bitstamp", "bitfinex", "cryptocom",
    "bitget", "binanceusdm", "bybit",
]
SCAN_EXCHANGE_NAMES = ["bitget", "okx", "gateio"]
FUNDING_SOURCES = ["bitget", "okx", "gateio"]

QUOTE = "USDT"
TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"

MAX_COINS = None
MAX_SYMBOLS_PER_EXCHANGE = None
MIN_SCORE = 12
MIN_RESISTANCE_ROOM_R = 0.75
MIN_RR = 1.35

SIGNAL_EXPIRY_HOURS = 48
RISK_USD = 10.0
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

TP_WEIGHTS = {"tp1": 0.50, "tp2": 0.30, "tp3": 0.20}

DATA_FILE = os.getenv("SIGNALS_FILE", "signals.json")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", TELEGRAM_CHAT_ID).strip()
USERS_FILE = os.getenv("USERS_FILE", "users.json")

REQUEST_TIMEOUT_MS = 15_000
OHLCV_LIMIT = 260
DAILY_LIMIT = 120
SLEEP_BETWEEN_SYMBOLS = 0.05
BITFINEX_RATE_LIMIT_COOLDOWN_SECONDS = 0.0
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
NEWS_LOOKBACK_HOURS = 24
NEWS_TIMEOUT_SECONDS = 8
