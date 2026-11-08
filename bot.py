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

from safety_filters import MAX_RISK_PCT, is_tradable_symbol


# ============================================================
# CONFIG
# ============================================================

EXCHANGE_NAMES = [
    # Legacy venues may still be needed to manage already-open signals.
    "binanceus", "bitstamp", "bitfinex", "cryptocom", "bitget", "binanceusdm", "bybit",
]
# Binance USD-M returned HTTP 451 from GitHub Actions (restricted-location response).
# Use liquid USDT linear perpetual venues as scanner sources instead.
# Bybit is geo-blocked on GitHub-hosted runners (HTTP 403). Keep multiple independent USDT-margined venues so one outage cannot stop scanning.
SCAN_EXCHANGE_NAMES = ["bitget", "okx", "gateio"]
FUNDING_SOURCES = ["bitget", "okx", "gateio"]

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


def closed_candles(raw):
    if not raw or len(raw) < 30:
        return pd.DataFrame()

    df = pd.DataFrame(
        raw,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    # Always ignore the currently forming candle.
    df = df.iloc[:-1].copy()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    return df


# ============================================================
# TECHNICAL SCORE COMPONENTS
# ============================================================

def ema_score(last):
    score = 0
    if last["close"] > last["ema20"]:
        score += 1
    if last["ema20"] > last["ema50"]:
        score += 1
    if last["ema50"] > last["ema200"]:
        score += 1

    bull = score >= 2
    bear = score <= 1
    return bull, bear, score


def rsi_score(last):
    rsi = safe_float(last["rsi"])
    if rsi is None:
        return False, False, 0

    if 52 <= rsi <= 68:
        return True, False, 2
    if 32 <= rsi <= 48:
        return False, True, 2
    if rsi > 70:
        return False, True, 1
    if rsi < 30:
        return True, False, 1

    return False, False, 0


def bb_score(last):
    close = last["close"]
    upper = last["bb_upper"]
    lower = last["bb_lower"]

    if pd.isna(upper) or pd.isna(lower):
        return False, False, 0

    width = upper - lower
    if width <= 0:
        return False, False, 0

    pos = (close - lower) / width

    if pos < 0.25:
        return True, False, 1
    if pos > 0.75:
        return False, True, 1

    return False, False, 0


def ichimoku_score(last):
    close = last["close"]
    span_a = last["span_a"]
    span_b = last["span_b"]
    tenkan = last["tenkan"]
    kijun = last["kijun"]

    if any(pd.isna(x) for x in [span_a, span_b, tenkan, kijun]):
        return False, False, 0

    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)

    if close > cloud_top and tenkan > kijun:
        return True, False, 2

    if close < cloud_bottom and tenkan < kijun:
        return False, True, 2

    return False, False, 0


def fibonacci_score(df):
    recent = df.tail(80)
    hi = recent["high"].max()
    lo = recent["low"].min()

    if hi <= lo:
        return False, False, 0

    close = df.iloc[-1]["close"]
    span = hi - lo

    levels = [
        lo + span * 0.382,
        lo + span * 0.500,
        lo + span * 0.618,
    ]

    distance = min(abs(close - x) for x in levels) / span

    if distance < 0.04 and close > lo:
        return True, False, 1

    if distance < 0.04 and close < hi:
        return False, True, 1

    return False, False, 0


def sr_score(df):
    recent = df.tail(60)
    close = df.iloc[-1]["close"]

    resistance = recent["high"].quantile(0.90)
    support = recent["low"].quantile(0.10)

    bull = close <= support * 1.03
    bear = close >= resistance * 0.97

    if bull:
        return True, False, 1
    if bear:
        return False, True, 1

    return False, False, 0


def brooks_score(df):
    # Price-action proxy inspired by trend/bar pressure.
    recent = df.tail(8)
    body = recent["close"] - recent["open"]
    bullish = int((body > 0).sum())
    bearish = int((body < 0).sum())

    if bullish >= 6:
        return True, False, 1
    if bearish >= 6:
        return False, True, 1

    return False, False, 0


def rtm_score(df):
    # Supply/demand proxy: strong displacement after a short base.
    if len(df) < 12:
        return False, False, 0

    recent = df.tail(12)
    returns = recent["close"].pct_change().dropna()

    recent_returns = returns.tail(4)
    positive = recent_returns.clip(lower=0).sum()
    negative = recent_returns.clip(upper=0).sum()

    if positive > 0.025 and positive > abs(negative):
        return True, False, 1
    if negative < -0.025 and abs(negative) > positive:
        return False, True, 1

    return False, False, 0


# ============================================================
# TREND FILTER
# ============================================================

def daily_trend(exchange, symbol):
    try:
        raw = exchange.fetch_ohlcv(
            symbol,
            timeframe=TREND_TIMEFRAME,
            limit=DAILY_LIMIT,
        )
        df = closed_candles(raw)
        if len(df) < 60:
            return "UNKNOWN"

        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        last = df.iloc[-1]

        if last["close"] > last["ema50"]:
            return "BULL"
        if last["close"] < last["ema50"]:
            return "BEAR"
        return "NEUTRAL"

    except Exception as exc:
        message = str(exc).lower()
        is_rate_limit = (
            isinstance(exc, getattr(ccxt, "RateLimitExceeded", ()))
            or "ratelimit" in message
            or "rate limit" in message
            or "11010" in message
        )
        if is_rate_limit:
            print(f"[RATE LIMIT] {exchange.id} daily trend {symbol}: {exc}")
            return "RATE_LIMIT"
        print(f"daily trend failed {symbol}: {type(exc).__name__}: {exc}")
        return "UNKNOWN"


# ============================================================
# FUNDAMENTALS
# ============================================================

def fundamental_data(manager, symbol):
    result = {
        "funding": None,
        "open_interest": None,
        "long_short": None,
        "source": None,
    }

    for name in FUNDING_SOURCES:
        exchange = manager.get_exchange(name)
        if not exchange:
            continue

        if symbol not in exchange.markets:
            continue

        # These are optional. Spot-only exchanges normally don't expose
        # funding/OI, so N/A is expected rather than an error.
        try:
            if exchange.has.get("fetchFundingRate"):
                result["funding"] = safe_float(
                    exchange.fetch_funding_rate(symbol).get("fundingRate")
                )
                result["source"] = name
        except Exception:
            pass

        try:
            if exchange.has.get("fetchOpenInterest"):
                oi = exchange.fetch_open_interest(symbol)
                result["open_interest"] = safe_float(
                    oi.get("openInterestValue") or oi.get("openInterestAmount")
                )
        except Exception:
            pass

        if result["funding"] is not None or result["open_interest"] is not None:
            break

    return result


def fundamental_score(data, side):
    funding = data.get("funding")

    if funding is None:
        return 0

    # Funding is a secondary confirmation only.
    # Positive funding favors shorts; negative funding favors longs.
    if side == "BUY" and funding < 0:
        return 1
    if side == "SELL" and funding > 0:
        return 1

    return 0


# ============================================================
# NEWS CONTEXT (lightweight fundamental risk filter)
# ============================================================

_NEGATIVE_NEWS_TERMS = (
    "hack", "hacked", "exploit", "breach", "lawsuit", "charged",
    "banned", "probe", "investigation", "fraud", "insolvency", "bankrupt",
    "outage", "halt", "suspend", "delist", "liquidation", "attack",
    "sec sues", "withdrawals paused", "security flaw",
)
_POSITIVE_NEWS_TERMS = (
    "approved", "approval", "partnership", "integrates", "integration",
    "launches", "launch", "upgrade", "adoption", "inflows", "etf inflows",
    "mainnet", "funding round",
)

def fetch_news_context():
    """Fetch recent headlines once per scan; unavailable feeds never stop scanning."""
    now = utc_now()
    items = []
    seen = set()
    for url in NEWS_FEEDS:
        try:
            response = requests.get(url, timeout=NEWS_TIMEOUT_SECONDS,
                                    headers={"User-Agent": "crypto-signal-bot/1.0"})
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for node in root.findall(".//item"):
                title = " ".join((node.findtext("title") or "").split())
                description = " ".join((node.findtext("description") or "").split())
                link = (node.findtext("link") or "").strip()
                pub_date = node.findtext("pubDate")
                if not title:
                    continue
                key = title.casefold()
                if key in seen:
                    continue
                published = None
                if pub_date:
                    try:
                        published = parsedate_to_datetime(pub_date)
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=timezone.utc)
                        published = published.astimezone(timezone.utc)
                    except Exception:
                        published = None
                age_hours = (now - published).total_seconds() / 3600 if published else None
                if age_hours is not None and (age_hours < -1 or age_hours > NEWS_LOOKBACK_HOURS):
                    continue
                seen.add(key)
                items.append({"title": title[:300], "description": description[:500],
                              "link": link, "published_at": published.isoformat() if published else None,
                              "age_hours": round(age_hours, 1) if age_hours is not None else None})
        except Exception as exc:
            print(f"[NEWS] feed unavailable {url}: {type(exc).__name__}: {exc}")
    print(f"[NEWS] recent headlines loaded: {len(items)}")
    return items

def news_context_for_symbol(symbol, headlines):
    """Conservative keyword-based context; not an AI sentiment model."""
    base = symbol.split("/")[0].upper()
    aliases = {
        "BTC": ("BITCOIN", "BTC"), "ETH": ("ETHEREUM", "ETH"),
        "SOL": ("SOLANA", "SOL"), "XRP": ("RIPPLE", "XRP"),
        "DOGE": ("DOGECOIN", "DOGE"), "ADA": ("CARDANO", "ADA"),
        "AVAX": ("AVALANCHE", "AVAX"), "LINK": ("CHAINLINK", "LINK"),
        "DOT": ("POLKADOT", "DOT"), "INJ": ("INJECTIVE", "INJ"),
        "BNB": ("BINANCE COIN", "BNB"),
    }
    long_names = {"BITCOIN", "ETHEREUM", "SOLANA", "RIPPLE", "DOGECOIN",
                  "CARDANO", "AVALANCHE", "CHAINLINK", "POLKADOT",
                  "INJECTIVE", "BINANCE COIN"}
    terms = aliases.get(base, (base,))
    relevant = []
    for item in headlines:
        text = (item["title"] + " " + item.get("description", "")).upper()
        matched = any(
            (term in text if term in long_names else
             bool(re.search(r"(?<![A-Z0-9])" + re.escape(term) + r"(?![A-Z0-9])", text)))
            for term in terms
        )
        macro_terms = ("FED", "FEDERAL RESERVE", "INFLATION", "CPI", "INTEREST RATE",
                       "US ECONOMY", "SEC", "CRYPTO MARKET", "BITCOIN ETF")
        # Macro/regulatory headlines may affect the broader crypto market,
        # not only the largest tickers.
        if not matched:
            matched = any(term in text for term in macro_terms)
        if not matched:
            continue
        lower = text.lower()
        negative = any(term in lower for term in _NEGATIVE_NEWS_TERMS)
        positive = any(term in lower for term in _POSITIVE_NEWS_TERMS)
        sentiment = "negative" if negative and not positive else "positive" if positive and not negative else "mixed"
        relevant.append({"title": item["title"], "link": item.get("link"),
                         "published_at": item.get("published_at"),
                         "sentiment": sentiment, "age_hours": item.get("age_hours")})
    relevant.sort(key=lambda x: (x["age_hours"] is None, x["age_hours"] or 999))
    recent_negative = [x for x in relevant if x["sentiment"] == "negative"
                       and x.get("age_hours") is not None and x["age_hours"] <= 12]
    penalty = min(2, len(recent_negative))
    return {"score_adjustment": -penalty,
            "label": "negative" if penalty else ("mixed/positive" if relevant else "no_relevant_news"),
            "headlines": relevant[:3]}


# ============================================================
# SIGNAL ENGINE
# ============================================================

def find_support_resistance(df, entry, atr, lookback=120):
    """Build ATR-sized support/resistance zones from confirmed 2-left/2-right pivots."""
    if df is None or len(df) < 30 or not entry or not atr or atr <= 0:
        return [], []
    recent = df.tail(lookback).reset_index(drop=True)
    highs = recent["high"].astype(float).to_numpy()
    lows = recent["low"].astype(float).to_numpy()
    closes = recent["close"].astype(float).to_numpy()
    tolerance = max(atr * 0.45, entry * 0.002)
    pivots = []

    # A pivot is only usable after two candles have closed to its right.
    for i in range(2, len(recent) - 2):
        if lows[i] <= min(lows[i-2:i]) and lows[i] <= min(lows[i+1:i+3]):
            pivots.append({"price": float(lows[i]), "idx": i, "kind": "support"})
        if highs[i] >= max(highs[i-2:i]) and highs[i] >= max(highs[i+1:i+3]):
            pivots.append({"price": float(highs[i]), "idx": i, "kind": "resistance"})

    def make_zones(kind):
        points = sorted((p for p in pivots if p["kind"] == kind), key=lambda p: p["price"])
        groups = []
        for point in points:
            if not groups or point["price"] - sum(p["price"] for p in groups[-1]) / len(groups[-1]) > tolerance:
                groups.append([point])
            else:
                groups[-1].append(point)

        zones = []
        for group in groups:
            center = sum(p["price"] for p in group) / len(group)
            lo_zone, hi_zone = center - tolerance * 0.35, center + tolerance * 0.35
            # Count distinct revisits after the first pivot; don't count the
            # pivot bars that define this zone as retests.
            touches = 0
            armed = True
            last_touch = None
            pivot_indices = {p["idx"] for p in group}
            for j in range(min(p["idx"] for p in group) + 3, len(recent)):
                if j in pivot_indices:
                    continue
                intersects = lows[j] <= hi_zone and highs[j] >= lo_zone
                if armed and intersects:
                    touches += 1
                    last_touch = j
                    armed = False
                if not intersects and abs(closes[j] - center) >= max(atr * 0.65, tolerance):
                    armed = True
            last_pivot = max(p["idx"] for p in group)
            zones.append({
                "center": center, "low": lo_zone, "high": hi_zone,
                "pivot_count": len(group), "touches": touches,
                "last_pivot": last_pivot, "last_touch": last_touch,
                "age": len(recent) - 1 - last_pivot,
                "fresh": touches == 0,
            })
        return zones

    support_zones = [z for z in make_zones("support") if z["center"] < entry]
    resistance_zones = [z for z in make_zones("resistance") if z["center"] > entry]
    support_zones.sort(key=lambda z: entry - z["center"])
    resistance_zones.sort(key=lambda z: z["center"] - entry)
    # Preserve the tuple interface used by level/notification code.
    supports = [(z["center"], z["touches"], z) for z in support_zones]
    resistances = [(z["center"], z["touches"], z) for z in resistance_zones]
    return supports, resistances


def _candle_rejection(row, side, atr):
    opn, close = float(row["open"]), float(row["close"])
    high, low = float(row["high"]), float(row["low"])
    span = max(high - low, 1e-12)
    body = abs(close - opn)
    close_loc = (close - low) / span
    lower_wick = min(opn, close) - low
    upper_wick = high - max(opn, close)
    if side == "BUY":
        return close > opn and close_loc >= 0.62 and (lower_wick >= body * 0.35 or body >= 0.35 * atr)
    return close < opn and close_loc <= 0.38 and (upper_wick >= body * 0.35 or body >= 0.35 * atr)


def _trendline_context(df, atr):
    """Return a conservative trendline read from the last confirmed swing pivots."""
    if df is None or len(df) < 20:
        return {"up_near": False, "down_near": False, "up_break": False, "down_break": False}
    hi = df["high"].astype(float).to_numpy()
    lo = df["low"].astype(float).to_numpy()
    piv_hi, piv_lo = [], []
    for i in range(2, len(df) - 2):
        if hi[i] >= max(hi[i-2:i]) and hi[i] >= max(hi[i+1:i+3]):
            piv_hi.append((i, hi[i]))
        if lo[i] <= min(lo[i-2:i]) and lo[i] <= min(lo[i+1:i+3]):
            piv_lo.append((i, lo[i]))
    last_close = float(df.iloc[-1]["close"])
    out = {"up_near": False, "down_near": False, "up_break": False, "down_break": False}
    if len(piv_lo) >= 2:
        (x1, y1), (x2, y2) = piv_lo[-2:]
        if x2 > x1 and y2 > y1:
            projected = y2 + (y2-y1) / (x2-x1) * (len(df)-1-x2)
            out["up_near"] = abs(last_close-projected) <= 0.45*atr
            out["up_break"] = last_close < projected - 0.20*atr
    if len(piv_hi) >= 2:
        (x1, y1), (x2, y2) = piv_hi[-2:]
        if x2 > x1 and y2 < y1:
            projected = y2 + (y2-y1) / (x2-x1) * (len(df)-1-x2)
            out["down_near"] = abs(last_close-projected) <= 0.45*atr
            out["down_break"] = last_close > projected + 0.20*atr
    return out


def _market_regime(df, atr):
    """Classify the latest closed-bar context without using future candles."""
    if df is None or len(df) < 60 or not atr or atr <= 0:
        return {"regime": "UNCLEAR", "higher_highs": False, "higher_lows": False}
    work = df.tail(100).reset_index(drop=True)
    hi = work["high"].astype(float).to_numpy()
    lo = work["low"].astype(float).to_numpy()
    piv_hi, piv_lo = [], []
    for j in range(2, len(work) - 2):
        if hi[j] >= max(hi[j-2:j]) and hi[j] >= max(hi[j+1:j+3]):
            piv_hi.append((j, hi[j]))
        if lo[j] <= min(lo[j-2:j]) and lo[j] <= min(lo[j+1:j+3]):
            piv_lo.append((j, lo[j]))
    higher_highs = len(piv_hi) >= 3 and piv_hi[-1][1] > piv_hi[-2][1] > piv_hi[-3][1]
    higher_lows = len(piv_lo) >= 3 and piv_lo[-1][1] > piv_lo[-2][1] > piv_lo[-3][1]
    lower_highs = len(piv_hi) >= 3 and piv_hi[-1][1] < piv_hi[-2][1] < piv_hi[-3][1]
    lower_lows = len(piv_lo) >= 3 and piv_lo[-1][1] < piv_lo[-2][1] < piv_lo[-3][1]
    close = float(work.iloc[-1]["close"])
    ema = work["close"].ewm(span=50, adjust=False).mean()
    slope = float(ema.iloc[-1] - ema.iloc[-6])
    if higher_highs and higher_lows and close > float(ema.iloc[-1]) and slope > 0:
        regime = "UPTREND"
    elif lower_highs and lower_lows and close < float(ema.iloc[-1]) and slope < 0:
        regime = "DOWNTREND"
    else:
        recent_range = float(work["high"].tail(20).max() - work["low"].tail(20).min())
        net_move = abs(float(work["close"].iloc[-1]) - float(work["close"].iloc[-21]))
        efficiency = net_move / max(recent_range, 1e-12)
        regime = "RANGE" if efficiency < 0.30 else "TRANSITION"
    return {"regime": regime, "higher_highs": bool(higher_highs),
            "higher_lows": bool(higher_lows), "lower_highs": bool(lower_highs),
            "lower_lows": bool(lower_lows), "ema50_slope": slope}


def _count_h1_h2(df, lookback=18):
    """Approximate Brooks-style H1/H2 attempts after a pullback in a bull trend.

    A trigger is a bullish bar that trades above the previous bar's high.
    H2 requires a distinct second trigger attempt after an intervening pullback;
    two bullish candles by themselves do not qualify.
    """
    result = {"h1": False, "h2": False, "attempts": 0, "pullback_bars": 0}
    if df is None or len(df) < 8:
        return result
    w = df.tail(lookback).reset_index(drop=True)
    pullback_start = None
    for start in range(max(1, len(w) - 10), len(w) - 3):
        segment = w.iloc[start:min(start + 4, len(w))]
        values = segment["close"].to_numpy()
        if int((values[1:] < values[:-1]).sum()) >= 2 and pullback_start is None:
            # Keep the start of the pullback sequence so the first trigger,
            # intervening retracement, and second trigger can all be counted.
            pullback_start = start
    if pullback_start is None:
        return result
    post = w.iloc[pullback_start + 1:].reset_index(drop=True)
    result["pullback_bars"] = int(len(post))
    attempts = 0
    previous_trigger = None
    retraced_after_trigger = False
    for j in range(1, len(post)):
        row, prev = post.iloc[j], post.iloc[j-1]
        trigger = float(row["close"]) > float(row["open"]) and float(row["high"]) > float(prev["high"])
        if trigger:
            if attempts == 0:
                attempts = 1
                previous_trigger = j
            elif retraced_after_trigger and j > (previous_trigger or 0):
                attempts = 2
                break
        if attempts and previous_trigger is not None and j > previous_trigger:
            if float(row["low"]) < float(post.iloc[previous_trigger]["low"]) or float(row["close"]) < float(row["open"]):
                retraced_after_trigger = True
    result["attempts"] = attempts
    result["h1"] = attempts >= 1
    result["h2"] = attempts >= 2
    return result


def _find_breakout_retest(df, atr, bullish_trend):
    """Find a prior confirmed close above a resistance pivot and a later retest."""
    if df is None or len(df) < 30 or atr <= 0 or not bullish_trend:
        return None
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    i = len(df) - 1
    for k in range(i - 2, max(4, i - 7), -1):
        for p in range(max(2, k - 35), k - 2):
            if highs[p] >= max(highs[p-2:p]) and highs[p] >= max(highs[p+1:p+3]):
                level = highs[p]
                if closes[k] <= level + 0.12 * atr or closes[k-1] > level:
                    continue
                touched = any(lows[j] <= level + 0.20 * atr and highs[j] >= level - 0.20 * atr
                              for j in range(k + 1, i + 1))
                last = df.iloc[-1]
                if touched and lows[i] <= level + 0.20 * atr and closes[i] > level and _candle_rejection(last, "BUY", atr):
                    return {"center": float(level), "low": float(level - 0.20*atr),
                            "high": float(level + 0.20*atr), "touches": 1,
                            "pivot_count": 1, "fresh": False, "last_pivot": p}
    return None


def calculate_levels(side, entry, atr, df):
    """Set structural invalidation and staged targets; use R-multiples if SR is sparse."""
    entry, atr = safe_float(entry), safe_float(atr)
    if not entry or not atr or atr <= 0:
        return None
    supports, resistances = find_support_resistance(df, entry, atr)
    buffer = max(atr * 0.22, entry * 0.0015)

    if side == "BUY":
        if not supports:
            print("[FILTER] no structural support below entry")
            return None
        support = supports[0][0]
        sl = support - buffer
        risk = entry - sl
        structural = [float(item[0]) for item in resistances
                      if float(item[0]) > entry + max(atr * 0.25, entry * 0.001)]
        structural = sorted(set(structural))
        tp1 = structural[0] if structural else entry + 1.25 * risk
        tp2 = structural[1] if len(structural) > 1 and structural[1] > tp1 + 0.25*risk else max(entry + 1.8*risk, tp1 + 0.5*risk)
        tp3 = structural[2] if len(structural) > 2 and structural[2] > tp2 + 0.25*risk else max(entry + 2.6*risk, tp2 + 0.5*risk)
        tp_methods = [
            "resistance_zone" if structural else "fallback_1.25R",
            "resistance_zone" if len(structural) > 1 and structural[1] > tp1 + 0.25*risk else "fallback_R_multiple",
            "resistance_zone" if len(structural) > 2 and structural[2] > tp2 + 0.25*risk else "fallback_R_multiple",
        ]
    elif side == "SELL":
        if not resistances:
            return None
        resistance = resistances[0][0]
        sl = resistance + buffer
        risk = sl - entry
        structural = sorted(set(float(item[0]) for item in supports
                                if float(item[0]) < entry - max(atr * 0.25, entry * 0.001)), reverse=True)
        tp1 = structural[0] if structural else entry - 1.25 * risk
        tp2 = structural[1] if len(structural) > 1 and structural[1] < tp1 - 0.25*risk else min(entry - 1.8*risk, tp1 - 0.5*risk)
        tp3 = structural[2] if len(structural) > 2 and structural[2] < tp2 - 0.25*risk else min(entry - 2.6*risk, tp2 - 0.5*risk)
        tp_methods = [
            "support_zone" if structural else "fallback_1.25R",
            "support_zone" if len(structural) > 1 and structural[1] < tp1 - 0.25*risk else "fallback_R_multiple",
            "support_zone" if len(structural) > 2 and structural[2] < tp2 - 0.25*risk else "fallback_R_multiple",
        ]
    else:
        return None

    if risk <= 0 or (side == "BUY" and sl >= entry) or (side == "SELL" and sl <= entry):
        return None
    if side == "BUY" and not (entry < tp1 < tp2 < tp3):
        return None
    if side == "SELL" and not (entry > tp1 > tp2 > tp3):
        return None

    weighted_reward = sum(TP_WEIGHTS[k] * abs(target-entry) for k, target in
                          (("tp1", tp1), ("tp2", tp2), ("tp3", tp3)))
    entry_fee = entry * FEE_RATE
    stop_fee = abs(sl) * FEE_RATE
    target_fee = sum(TP_WEIGHTS[k] * abs(target) * FEE_RATE for k, target in
                     (("tp1", tp1), ("tp2", tp2), ("tp3", tp3)))
    net_risk = risk + entry_fee + stop_fee
    net_reward = weighted_reward - entry_fee - target_fee
    rr = net_reward / net_risk if net_risk > 0 else 0.0
    if rr < MIN_RR:
        print(f"[FILTER] structural/fallback RR {rr:.2f} < MIN_RR={MIN_RR}")
        return None
    return {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "risk_per_unit": risk, "net_risk_per_unit": net_risk, "rr": rr,
        "support": supports[0][0] if supports else None,
        "resistance": resistances[0][0] if resistances else None,
        "tp_methods": tp_methods,
        "sl_method": "below_support_zone" if side == "BUY" else "above_resistance_zone",
    }


def calculate_crypto_strategy_gates(df):
    """
    V3 spot-long breakout strategy using only fully closed 4h candles.
    The strictest of the 10/20/30-bar levels is used (equivalent to the 30-bar high).
    Entry gates: volume >= 1.5x prior 20-bar average, close > EMA200, ADX(14) > 32.
    """
    result = {
        "atr": None, "volume_avg": None, "volume_ratio": None,
        "breakout_level": None, "lookback": 30,
        "lookbacks": [10, 20, 30], "volume_multiplier": 1.5,
        "atr_multiplier": 2.0, "partial_r": 0.75, "partial_pct": 1.0 / 3.0,
        "chandelier_atr_multiplier": 3.0, "ema200": None, "adx": None,
        "signal_long": False,
    }
    required = {"open", "high", "low", "close", "volume"}
    if df is None or len(df) < 220 or not required.issubset(df.columns):
        return result

    work = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    if len(work) < 220:
        return result

    close, high, low, volume = work["close"], work["high"], work["low"], work["volume"]
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    up_move, down_move = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=work.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=work.index)
    smoothed_plus = plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    smoothed_minus = minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    plus_di = 100.0 * smoothed_plus / atr.replace(0, np.nan)
    minus_di = 100.0 * smoothed_minus / atr.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum).replace([np.inf, -np.inf], np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    levels = {
        lookback: high.shift(1).rolling(lookback, min_periods=lookback).max()
        for lookback in (10, 20, 30)
    }
    # Levels are shifted: the breakout candle never contributes to its own level.
    breakout = pd.concat(list(levels.values()), axis=1).max(axis=1)
    volume_avg = volume.rolling(20, min_periods=20).mean().shift(1)
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()

    values = {
        "atr": safe_float(atr.iloc[-1]),
        "volume_avg": safe_float(volume_avg.iloc[-1]),
        "close": safe_float(close.iloc[-1]),
        "volume": safe_float(volume.iloc[-1]),
        "breakout": safe_float(breakout.iloc[-1]),
        "ema200": safe_float(ema200.iloc[-1]),
        "adx": safe_float(adx.iloc[-1]),
    }
    result.update({
        "atr": values["atr"],
        "volume_avg": values["volume_avg"],
        "volume_ratio": (
            values["volume"] / values["volume_avg"]
            if values["volume"] is not None and values["volume_avg"] is not None
            and values["volume_avg"] > 0 else None
        ),
        "breakout_level": values["breakout"],
        "ema200": values["ema200"],
        "adx": values["adx"],
    })
    if any(value is None for value in values.values()) or values["atr"] <= 0 or values["volume_avg"] <= 0:
        return result

    result["signal_long"] = bool(
        values["close"] > values["breakout"]
        and values["volume"] >= 1.5 * values["volume_avg"]
        and values["close"] > values["ema200"]
        and values["adx"] > 32.0
    )
    return result

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    return 100 - (100 / (1 + rs))


def atr(high, low, close, period=14):
    previous_close = close.shift(1)
    tr = pd.concat([high-low, (high-previous_close).abs(), (low-previous_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def stochastic(high, low, close, k_period=14, d_period=3):
    lowest, highest = low.rolling(k_period).min(), high.rolling(k_period).max()
    k = 100 * (close-lowest) / (highest-lowest+1e-12)
    return k, k.rolling(d_period).mean()


def calculate_reference_strategy_gates(df):
    """Evaluate the confirmed 4H RSI/Stochastic reversal on the latest closed bar."""
    result = {"signal_long": False, "signal_short": False, "atr": None, "rsi": None,
              "stoch_k": None, "stoch_d": None, "volume_ratio": None, "signal_timestamp": None}
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if df is None or len(df) < 140 or not required.issubset(df.columns):
        return result
    work = df.copy()
    for col in required:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=list(required)).reset_index(drop=True)
    if len(work) < 140:
        return result

    close, high, low = work["close"], work["high"], work["low"]
    rv, av = rsi(close, 14), atr(high, low, close, 14)
    sk, sd = stochastic(high, low, close, 14, 3)
    volma = work["volume"].rolling(20, min_periods=20).mean()
    i = len(work)-1
    vals = [safe_float(rv.iloc[i-1]), safe_float(rv.iloc[i]), safe_float(av.iloc[i]),
            safe_float(sk.iloc[i-1]), safe_float(sd.iloc[i-1]),
            safe_float(sk.iloc[i]), safe_float(sd.iloc[i]), safe_float(volma.iloc[i])]
    if any(v is None for v in vals):
        return result
    rp, rn, atr_now, kp, dp, kn, dn, vm = vals
    volume = safe_float(work.iloc[i]["volume"])
    if volume is None or vm <= 0 or atr_now <= 0:
        return result
    vr = volume / vm
    long_cross = kp <= dp and kn > dn and kn < 38
    short_cross = kp >= dp and kn < dn and kn > 62
    long_rsi = rp < 30 and rn >= 30
    short_rsi = rp > 70 and rn <= 70
    candle = work.iloc[i]
    result.update({
        "atr": atr_now, "rsi": rn, "stoch_k": kn, "stoch_d": dn,
        "volume_ratio": vr, "signal_timestamp": safe_float(candle["timestamp"]),
        "signal_long": bool(long_rsi and long_cross and volume > 1.12*vm and candle["close"] > candle["open"]),
        "signal_short": bool(short_rsi and short_cross and volume > 1.12*vm and candle["close"] < candle["open"]),
    })
    return result


def build_signal(symbol, exchange_name, df, next_open=None, trend=None, fundamentals=None, news_context=None):
    """Create a Binance USDⓈ-M 4H long/short signal using next candle open."""
    st = calculate_reference_strategy_gates(df)
    if not st["signal_long"] and not st["signal_short"]:
        return None
    raw_entry, atr_now = safe_float(next_open), safe_float(st["atr"])
    if raw_entry is None or raw_entry <= 0 or atr_now is None or atr_now <= 0:
        print(f"[SIGNAL SKIP] {symbol}: next 4H open unavailable; no close-price fallback")
        return None
    side = "BUY" if st["signal_long"] else "SELL"
    entry = raw_entry * (1+SLIPPAGE_RATE) if side == "BUY" else raw_entry * (1-SLIPPAGE_RATE)
    recent = df.tail(5)
    if side == "BUY":
        swing = safe_float(pd.to_numeric(recent["low"], errors="coerce").min())
        if swing is None: return None
        stop = min(swing, entry-1.32*atr_now)
        risk = entry-stop
    else:
        swing = safe_float(pd.to_numeric(recent["high"], errors="coerce").max())
        if swing is None: return None
        stop = max(swing, entry+1.32*atr_now)
        risk = stop-entry
    risk_pct = risk/entry
    # Hard cap from the post-mortem safety policy; never silently use the older 3.8% limit.
    if not (0.0035 < risk_pct <= MAX_RISK_PCT):
        return None
    target = entry + (1.5*risk if side=="BUY" else -1.5*risk)
    # Entry slippage is already embedded in entry. Estimate adverse target-exit
    # slippage and round-trip fees before admitting the signal.
    cost_unit = (entry+stop)*FEE_RATE + stop*SLIPPAGE_RATE
    target_fill = target * (1-SLIPPAGE_RATE) if side == "BUY" else target * (1+SLIPPAGE_RATE)
    target_net_reward = abs(target_fill-entry) - (entry+target)*FEE_RATE
    net_rr = target_net_reward / (risk + cost_unit)
    if not math.isfinite(net_rr) or net_rr < MIN_RR:
        print(f"[SIGNAL SKIP] {symbol}: net RR {net_rr:.3f} below MIN_RR={MIN_RR:.2f}")
        return None
    raw_qty = RISK_USD/(risk+cost_unit)
    cap_qty = MAX_POSITION_USD/entry if MAX_POSITION_USD>0 else raw_qty
    qty = min(raw_qty, cap_qty)
    now = iso_now()
    setup = "REFERENCE_RSI_STOCH_ATR_RR15"
    components = {
        "setup": setup, "strategy_mode": "rsi_recovery_stochastic_cross", "timeframe": "4h",
        "rr": 1.5, "net_rr": net_rr, "max_risk_pct": MAX_RISK_PCT,
        "rsi_period": 14, "rsi_long_recovery": 30, "rsi_short_recovery": 70,
        "stoch_k_period": 14, "stoch_d_period": 3, "stoch_long_zone_max": 38,
        "stoch_short_zone_min": 62, "volume_multiplier": 1.12, "volume_ma_period": 20,
        "atr_period": 14, "atr_multiplier": 1.32, "swing_lookback": 5,
        "atr14": atr_now, "rsi": st["rsi"], "stoch_k": st["stoch_k"],
        "stoch_d": st["stoch_d"], "volume_ratio": st["volume_ratio"],
        "fee_rate": FEE_RATE, "slippage_rate": SLIPPAGE_RATE,
        "entry_model": "next_4h_open", "out_of_sample_status": "NOT_VALIDATED_ON_REAL_MARKET_DATA",
    }
    return {
        "id": f"{symbol.replace('/', '_').replace(':', '_')}_{side}_{int(time.time())}",
        "symbol": symbol, "exchange": exchange_name, "side": side, "status": "ACTIVE",
        "strength": "Research candidate RR1.5", "score": 0, "created_at": now, "updated_at": now,
        "expiry_at": (utc_now()+timedelta(hours=SIGNAL_EXPIRY_HOURS)).isoformat(),
        "entry": entry, "sl": stop, "tp1": target, "tp2": target, "tp3": target,
        "support": None, "resistance": None, "sl_method": "5-bar_swing_vs_1.32_ATR14",
        "tp_methods": ["single_target_1.5R"], "rr": 1.5, "risk_pct": risk_pct*100,
        "initial_risk_per_unit": risk, "initial_sl": stop, "risk_usd": RISK_USD,
        "net_rr": net_rr,
        "risk_target_usd": RISK_USD, "actual_risk_usd": qty*(risk+cost_unit),
        "position_quantity": qty, "quantity": qty, "position_value": qty*entry,
        "position_capped": qty+1e-12 < raw_qty, "signal_candle_timestamp": st["signal_timestamp"],
        "components": components,
        "entry_snapshot": {"timeframe": "4h", "setup": setup, "rr": 1.5, "rsi": st["rsi"],
            "stoch_k": st["stoch_k"], "stoch_d": st["stoch_d"], "volume_ratio": st["volume_ratio"],
            "atr": atr_now, "swing_level": swing, "candle_timestamp": st["signal_timestamp"],
            "next_open": raw_entry, "captured_at": now},
        "trend": "4H RSI/Stochastic reversal", "fundamentals": fundamentals or {},
        "news": news_context or {"score_adjustment": 0, "label": "not_used_for_entry", "headlines": []},
        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "remaining_fraction": 1.0,
        "realized_pnl": 0.0, "fees_paid": 0.0, "closed_reason": None,
    }

# ============================================================
# TRADE MANAGEMENT
# ============================================================

def duplicate_exists(state, symbol, side):
    for signal in state.get("signals", []):
        if signal.get("symbol") == symbol and signal.get("side") == side:
            if signal.get("status") == "ACTIVE":
                return True
    return False

def duplicate_on_candle_exists(state, candidate):
    """Prevent repeated entries from the same closed 4h signal candle."""
    candle_ts = candidate.get("signal_candle_timestamp")
    if candle_ts is None:
        return False
    for signal in state.get("signals", []):
        if (
            signal.get("symbol") == candidate.get("symbol")
            and signal.get("side") == candidate.get("side")
            and signal.get("signal_candle_timestamp") == candle_ts
        ):
            return True
    return False


def close_piece(signal, exit_price, fraction, label):
    """Book a partial exit only when original risk can be determined reliably."""
    entry = safe_float(signal.get("entry"))
    exit_price = safe_float(exit_price)
    fraction = safe_float(fraction)
    if entry is None or entry <= 0 or exit_price is None or exit_price <= 0 or fraction is None or fraction <= 0:
        print(f"[PNL ERROR] Invalid close values for {signal.get('symbol')}")
        return False

    risk_per_unit = safe_float(signal.get("initial_risk_per_unit"))
    original_sl = safe_float(signal.get("initial_sl"))
    if (risk_per_unit is None or risk_per_unit <= 0) and original_sl is None:
        current_sl = safe_float(signal.get("sl"))
        # Never infer risk from TP1; it may not equal the initial stop distance.
        if current_sl is not None and not math.isclose(current_sl, entry, rel_tol=1e-9, abs_tol=1e-12):
            original_sl = current_sl
    if risk_per_unit is None or risk_per_unit <= 0:
        risk_per_unit = abs(entry - original_sl) if original_sl is not None else 0.0
    if risk_per_unit <= 0:
        print(f"[PNL ERROR] Cannot determine original risk for {signal.get('symbol')}; PnL not booked")
        return False

    quantity = safe_float(signal.get("position_quantity"))
    if quantity is None or quantity <= 0:
        quantity = RISK_USD / risk_per_unit
        signal["position_quantity"] = quantity
    remaining = safe_float(signal.get("remaining_fraction"), 1.0)
    fraction = min(fraction, max(0.0, remaining))
    if fraction <= 0:
        return False

    piece_qty = quantity * fraction
    is_reference_futures = signal.get("components", {}).get("setup") == "REFERENCE_RSI_STOCH_ATR_RR15"
    if is_reference_futures:
        slip = safe_float(signal.get("components", {}).get("slippage_rate"), SLIPPAGE_RATE) or 0.0
        exit_price = exit_price * (1-slip) if signal.get("side") == "BUY" else exit_price * (1+slip)
    gross_piece = ((exit_price - entry) if signal.get("side") == "BUY" else (entry - exit_price)) * piece_qty
    # Preserve the 0.10% fee model for known legacy setups; generic/new records
    # continue to use the configured current fee unless they carry their own rate.
    setup_name = signal.get("components", {}).get("setup")
    legacy_setup = setup_name in {
        "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3",
        "REFERENCE_EMA_RSI_STOCH_ATR_SESSION",
    }
    fee_default = 0.001 if legacy_setup else FEE_RATE
    fee_rate = safe_float(signal.get("components", {}).get("fee_rate"), fee_default)
    fees = (entry + exit_price) * piece_qty * fee_rate
    signal["realized_pnl"] = safe_float(signal.get("realized_pnl"), 0.0) + gross_piece - fees
    signal["fees_paid"] = safe_float(signal.get("fees_paid"), 0.0) + fees
    signal["remaining_fraction"] = max(0.0, remaining - fraction)
    # Preserve execution evidence so the audit can distinguish strategy loss from
    # stop execution slippage. Ticker polling can observe a price beyond the stop.
    signal["last_exit_price"] = exit_price
    signal["last_exit_label"] = label
    if label == "sl":
        stop_price = safe_float(signal.get("sl"))
        if stop_price is not None:
            if signal.get("side") == "BUY":
                slippage = max(0.0, stop_price - exit_price) * piece_qty
            else:
                slippage = max(0.0, exit_price - stop_price) * piece_qty
            signal["stop_slippage_usd"] = safe_float(signal.get("stop_slippage_usd"), 0.0) + slippage
    signal[f"{label}_hit"] = True
    signal["updated_at"] = iso_now()
    return True


def price_hit(signal, price, level_name):
    price = safe_float(price)
    level = safe_float(signal.get(level_name))

    if price is None or level is None:
        return False

    if signal["side"] == "BUY":
        return price >= level
    return price <= level


def stop_hit(signal, price):
    price = safe_float(price)
    sl = safe_float(signal.get("sl"))

    if price is None or sl is None:
        return False

    if signal["side"] == "BUY":
        return price <= sl
    return price >= sl


def update_chandelier_stop(signal, df):
    """Ratchet the V3 stop using closed-candle highest close minus 3*ATR(14)."""
    if signal.get("components", {}).get("setup") != "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3":
        return False
    if df is None or len(df) < 20 or not {"high", "low", "close", "timestamp"}.issubset(df.columns):
        return False

    work = df.copy()
    for col in ("high", "low", "close", "timestamp"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["high", "low", "close", "timestamp"]).reset_index(drop=True)
    if len(work) < 20:
        return False

    latest_ts = safe_float(work.iloc[-1]["timestamp"])
    previous_ts = safe_float(signal.get("last_trailing_candle_ts"))
    if latest_ts is None or (previous_ts is not None and latest_ts <= previous_ts):
        return False

    entry_ts = safe_float(signal.get("signal_candle_timestamp"))
    if entry_ts is not None:
        post_entry = work[work["timestamp"] >= entry_ts]
    else:
        post_entry = work
    if post_entry.empty:
        post_entry = work.tail(1)

    highest_close = safe_float(post_entry["close"].max())
    signal["last_trailing_candle_ts"] = latest_ts
    if highest_close is not None:
        signal["highest_close_since_entry"] = max(
            safe_float(signal.get("highest_close_since_entry"), safe_float(signal.get("entry"), 0.0)) or 0.0,
            highest_close,
        )

    # Before the first partial, the original 2*ATR stop remains in force.
    if not signal.get("tp1_hit") or not signal.get("trailing_active"):
        return False

    close = work["close"]
    previous_close = close.shift(1)
    true_range = pd.concat([
        work["high"] - work["low"],
        (work["high"] - previous_close).abs(),
        (work["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr = safe_float(true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1])
    peak = safe_float(signal.get("highest_close_since_entry"))
    entry = safe_float(signal.get("entry"))
    current_sl = safe_float(signal.get("sl"))
    if any(value is None for value in (atr, peak, entry, current_sl)) or atr <= 0:
        return False

    candidate = max(entry, peak - 3.0 * atr)
    if candidate > current_sl:
        signal["sl"] = candidate
        signal["updated_at"] = iso_now()
        return True
    return False


def manage_signal(signal, current_price):
    if signal.get("status") != "ACTIVE":
        return False

    changed = False

    created = signal.get("created_at")
    if created:
        try:
            created_dt = datetime.fromisoformat(created)
            if utc_now() - created_dt >= timedelta(hours=SIGNAL_EXPIRY_HOURS):
                # Close remaining position at current price.
                remaining = safe_float(signal.get("remaining_fraction"), 0)
                if remaining > 0 and not close_piece(signal, current_price, remaining, "expiry"):
                    # Do not finalize a trade when its exit could not be booked.
                    # Keep it active so the accounting problem remains visible/retryable.
                    print(f"[MANAGE ERROR] {signal.get('symbol')}: expiry exit not booked; keeping signal active")
                    return False
                signal["status"] = "EXPIRED"
                signal["closed_reason"] = "48h expiry"
                signal["updated_at"] = iso_now()
                return True
        except Exception:
            pass

    is_v3 = (
        signal.get("components", {}).get("setup")
        == "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3"
    )
    if is_v3:
        # Stop takes priority over an unbooked partial target.
        if stop_hit(signal, current_price):
            remaining = safe_float(signal.get("remaining_fraction"), 0.0) or 0.0
            if remaining > 0 and not close_piece(signal, current_price, remaining, "sl"):
                print(f"[MANAGE ERROR] {signal.get('symbol')}: V3 stop exit not booked; keeping signal active")
                return False
            signal["status"] = "CLOSED"
            entry = safe_float(signal.get("entry"), 0.0) or 0.0
            stop = safe_float(signal.get("sl"), entry) or entry
            if signal.get("trailing_active") and stop > entry + 1e-12:
                signal["closed_reason"] = "TRAILING_SL"
            elif signal.get("trailing_active"):
                signal["closed_reason"] = "BREAKEVEN_SL"
            else:
                signal["closed_reason"] = "SL"
            signal["updated_at"] = iso_now()
            return True

        if not signal.get("tp1_hit") and price_hit(signal, current_price, "tp1"):
            partial_pct = safe_float(signal.get("components", {}).get("partial_pct"), 1.0 / 3.0)
            partial_target = safe_float(signal.get("tp1"))
            if partial_target is None or not close_piece(signal, partial_target, partial_pct, "tp1"):
                print(f"[MANAGE ERROR] {signal.get('symbol')}: V3 partial exit not booked; leaving trade active")
                return False
            entry = safe_float(signal.get("entry"))
            if entry is None:
                return False
            signal["sl"] = max(entry, safe_float(signal.get("sl"), entry) or entry)
            signal["trailing_active"] = True
            signal["updated_at"] = iso_now()
            return True
        return False

    # Stop is checked before new TP levels.
    if stop_hit(signal, current_price):
        remaining = safe_float(signal.get("remaining_fraction"), 0)
        if remaining > 0 and not close_piece(signal, current_price, remaining, "sl"):
            print(f"[MANAGE ERROR] {signal.get('symbol')}: stop exit not booked; keeping signal active")
            return False
        signal["status"] = "CLOSED"
        signal["closed_reason"] = "SL"
        signal["updated_at"] = iso_now()
        return True

    # This reference setup has one final target; the repeated TP fields are legacy compatibility.
    is_single_target = signal.get("components", {}).get("setup") in {
        "REFERENCE_EMA_RSI_STOCH_ATR_SESSION", "REFERENCE_RSI_STOCH_ATR_RR15"
    }
    if is_single_target and price_hit(signal, current_price, "tp1"):
        remaining = safe_float(signal.get("remaining_fraction"), 1.0)
        if remaining > 0 and not close_piece(signal, signal["tp1"], remaining, "tp1"):
            print(f"[MANAGE ERROR] {signal.get('symbol')}: final target exit not booked; keeping signal active")
            return False
        signal["status"] = "CLOSED"
        signal["closed_reason"] = "TP1"
        signal["updated_at"] = iso_now()
        return True

    if not signal.get("tp1_hit") and price_hit(signal, current_price, "tp1"):
        if not close_piece(signal, signal["tp1"], TP_WEIGHTS["tp1"], "tp1"):
            print(f"[MANAGE ERROR] {signal.get('symbol')}: TP1 exit not booked; leaving trade state unchanged")
            return changed
        # Move the remaining position's stop to breakeven only after TP1 is booked.
        signal["sl"] = signal["entry"]
        changed = True

    if not signal.get("tp2_hit") and price_hit(signal, current_price, "tp2"):
        if not close_piece(signal, signal["tp2"], TP_WEIGHTS["tp2"], "tp2"):
            print(f"[MANAGE ERROR] {signal.get('symbol')}: TP2 exit not booked; leaving remaining state unchanged")
            return changed
        changed = True

    if not signal.get("tp3_hit") and price_hit(signal, current_price, "tp3"):
        if not close_piece(signal, signal["tp3"], TP_WEIGHTS["tp3"], "tp3"):
            print(f"[MANAGE ERROR] {signal.get('symbol')}: TP3 exit not booked; leaving remaining state unchanged")
            return changed
        changed = True

    if signal.get("remaining_fraction", 0) <= 0.000001:
        signal["remaining_fraction"] = 0
        signal["status"] = "CLOSED"
        signal["closed_reason"] = "TP3"
        signal["updated_at"] = iso_now()
        changed = True

    return changed


def update_open_signals(state, manager):
    """Update active signals and return one Telegram message per new event."""
    events = []

    for signal in state.get("signals", []):
        if signal.get("status") != "ACTIVE":
            continue

        symbol = signal.get("symbol")
        exchange_name = signal.get("exchange")
        exchange = manager.get_exchange(exchange_name)
        if not exchange:
            print(f"[MANAGE SKIP] {symbol}: exchange {exchange_name!r} unavailable")
            continue
        if symbol not in getattr(exchange, "markets", {}):
            print(f"[MANAGE SKIP] {symbol}: not listed on {exchange_name}")
            continue

        price = manager.ticker_price(exchange, symbol)
        if price is None:
            print(f"[MANAGE SKIP] {symbol}: ticker price unavailable")
            continue

        before = {
            "status": signal.get("status"),
            "tp1_hit": bool(signal.get("tp1_hit")),
            "tp2_hit": bool(signal.get("tp2_hit")),
            "tp3_hit": bool(signal.get("tp3_hit")),
            "trailing_active": bool(signal.get("trailing_active")),
            "sl": safe_float(signal.get("sl")),
        }
        trailing_updated = False
        if (
            signal.get("components", {}).get("setup") == "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3"
            and signal.get("trailing_active")
        ):
            try:
                last_ts = safe_float(signal.get("last_trailing_candle_ts"))
                interval_ms = safe_float(exchange.parse_timeframe(TIMEFRAME), 14400.0) * 1000.0
                now_ms = utc_now().timestamp() * 1000.0
                # CCXT candle timestamps are candle-open times and the current
                # forming candle is discarded, so the next closed bar is due
                # roughly two intervals after the last stored open timestamp.
                candle_due = last_ts is None or now_ms >= last_ts + 2.0 * interval_ms
                if candle_due:
                    raw = manager.fetch_ohlcv(exchange, symbol, timeframe=TIMEFRAME, limit=OHLCV_LIMIT)
                    closed_df = closed_candles(raw)
                    trailing_updated = update_chandelier_stop(signal, closed_df)
            except Exception as exc:
                print(f"[TRAILING SKIP] {symbol}: {type(exc).__name__}: {exc}")

        changed = manage_signal(signal, price) or trailing_updated
        if not changed:
            continue

        side = signal.get("side", "BUY")
        header = f"<b>{symbol} — {side}</b>"
        if (
            not before["tp1_hit"] and signal.get("tp1_hit")
            and signal.get("components", {}).get("setup") != "REFERENCE_EMA_RSI_STOCH_ATR_SESSION"
        ):
            if signal.get("components", {}).get("setup") == "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3":
                events.append(
                    f"🎯 <b>برداشت جزئی V3 ثبت شد</b>\n{header}\n"
                    f"قیمت بررسی: <b>{fmt_price(price)}</b>\n"
                    f"یک‌سوم موقعیت در +0.75R ثبت شد؛ استاپ دوسوم باقی‌مانده به نقطه ورود منتقل شد.\n"
                    f"SL جدید: <b>{fmt_price(signal.get('sl'))}</b>"
                )
            else:
                events.append(
                    f"🎯 <b>TP1 فعال شد</b>\n{header}\n"
                    f"قیمت بررسی: <b>{fmt_price(price)}</b>\n"
                    f"۵۰٪ از موقعیت ثبت شد؛ حد ضرر باقی‌مانده به نقطه ورود منتقل شد.\n"
                    f"SL جدید: <b>{fmt_price(signal.get('sl'))}</b>"
                )
        if (
            signal.get("components", {}).get("setup") == "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3"
            and before["trailing_active"]
            and signal.get("status") == "ACTIVE"
            and before["sl"] is not None
            and safe_float(signal.get("sl")) is not None
            and safe_float(signal.get("sl")) > before["sl"]
        ):
            events.append(
                f"📈 <b>تریلینگ استاپ به‌روزرسانی شد</b>\n{header}\n"
                f"SL جدید: <b>{fmt_price(signal.get('sl'))}</b>\n"
                f"بالاترین کلوز ثبت‌شده: <b>{fmt_price(signal.get('highest_close_since_entry'))}</b>"
            )

        if not before["tp2_hit"] and signal.get("tp2_hit"):
            events.append(
                f"🥈 <b>TP2 فعال شد</b>\n{header}\n"
                f"قیمت بررسی: <b>{fmt_price(price)}</b>\n۳۰٪ دیگر از موقعیت ثبت شد."
            )
        if not before["tp3_hit"] and signal.get("tp3_hit"):
            events.append(
                f"🥉 <b>TP3 فعال شد</b>\n{header}\n"
                f"قیمت بررسی: <b>{fmt_price(price)}</b>\n۲۰٪ باقی‌مانده ثبت شد."
            )

        if before["status"] == "ACTIVE" and signal.get("status") == "CLOSED":
            reason = str(signal.get("closed_reason", "")).upper()
            if reason == "TRAILING_SL":
                title = "📉 <b>تریلینگ استاپ چَندلیر فعال شد</b>"
            elif reason == "BREAKEVEN_SL":
                title = "🟰 <b>استاپ سربه‌سر فعال شد</b>"
            elif reason == "SL":
                title = "🛑 <b>حد ضرر (SL) فعال شد</b>"
            elif reason == "TP3":
                title = "✅ <b>موقعیت با TP3 بسته شد</b>"
            elif reason == "TP1" and signal.get("components", {}).get("setup") == "REFERENCE_EMA_RSI_STOCH_ATR_SESSION":
                title = "✅ <b>هدف نهایی فعال شد؛ موقعیت کامل بسته شد</b>"
            else:
                title = "📕 <b>موقعیت بسته شد</b>"
            events.append(
                f"{title}\n{header}\n"
                f"قیمت بررسی/خروج: <b>{fmt_price(price)}</b>\n"
                f"دلیل: <b>{reason or 'نامشخص'}</b>\n"
                f"سود/زیان ثبت‌شده: <b>${safe_float(signal.get('realized_pnl'), 0):.2f}</b>\n"
                f"کارمزد ثبت‌شده: <b>${safe_float(signal.get('fees_paid'), 0):.2f}</b>"
            )
        elif before["status"] == "ACTIVE" and signal.get("status") == "EXPIRED":
            events.append(
                f"⌛ <b>اعتبار سیگنال تمام شد</b>\n{header}\n"
                f"قیمت بررسی/خروج: <b>{fmt_price(price)}</b>\n"
                f"سود/زیان ثبت‌شده: <b>${safe_float(signal.get('realized_pnl'), 0):.2f}</b>"
            )

    return events


# ============================================================
# REPORTING
# ============================================================

def refresh_historical_stats(state):
    """Rebuild cached historical_stats from signals; never trust stale counters."""
    signals = state.get("signals", [])
    closed = [s for s in signals if s.get("status") in {"CLOSED", "EXPIRED"}]
    stats = {
        "closed": len(closed), "tp3": 0, "tp2_then_sl": 0,
        "tp1_then_sl": 0, "sl_direct": 0, "trailing_stop": 0,
        "breakeven_stop": 0, "expired": 0,
        "no_risk": 0, "other": 0, "net_pnl": 0.0,
        "fees": 0.0, "sl_methods": {},
    }
    for s in closed:
        reason = str(s.get("closed_reason") or "").lower()
        tp1 = bool(s.get("tp1_hit"))
        tp2 = bool(s.get("tp2_hit"))
        tp3 = bool(s.get("tp3_hit"))
        if tp3 or reason == "tp3":
            stats["tp3"] += 1
        elif "expir" in reason:
            stats["expired"] += 1
        elif reason == "trailing_sl":
            stats["trailing_stop"] += 1
        elif reason == "breakeven_sl":
            stats["breakeven_stop"] += 1
        elif reason == "sl" and tp2:
            stats["tp2_then_sl"] += 1
        elif reason == "sl" and tp1:
            stats["tp1_then_sl"] += 1
        elif reason == "sl":
            stats["sl_direct"] += 1
        else:
            stats["other"] += 1
        if safe_float(s.get("initial_risk_per_unit", s.get("risk_per_unit"))) in (None, 0):
            stats["no_risk"] += 1
        stats["net_pnl"] += safe_float(s.get("realized_pnl"), 0.0) or 0.0
        stats["fees"] += safe_float(s.get("fees_paid"), 0.0) or 0.0
        method = s.get("sl_method")
        if method:
            stats["sl_methods"][str(method)] = stats["sl_methods"].get(str(method), 0) + 1
    state["historical_stats"] = stats
    return stats


def performance_report(state):
    # Only the new RSI/Stochastic futures setup is included in the current forward test.
    start_at = state.get("forward_test_started_at")
    signals = [
        s for s in state.get("signals", [])
        if (s.get("components", {}) or {}).get("setup") == "REFERENCE_RSI_STOCH_ATR_RR15"
    ]
    if start_at:
        signals = [
            s for s in signals
            if s.get("created_at") and s.get("created_at") >= start_at
        ]
    closed = [
        s for s in signals
        if s.get("status") in {"CLOSED", "EXPIRED"}
    ]

    if not closed:
        return "📊 <b>Performance</b>\nهنوز معامله بسته‌شده‌ای ثبت نشده."

    wins = sum(1 for s in closed if safe_float(s.get("realized_pnl"), 0) > 0)
    losses = len(closed) - wins

    pnl = sum(safe_float(s.get("realized_pnl"), 0) for s in closed)
    fees = sum(safe_float(s.get("fees_paid"), 0) for s in closed)

    win_rate = wins / len(closed) * 100

    return (
        "📊 <b>Performance</b>\n"
        f"معاملات بسته: {len(closed)}\n"
        f"برد: {wins} | باخت: {losses}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"Net PnL: ${pnl:.2f}\n"
        f"Fees: ${fees:.2f}"
    )


def format_signal(signal):
    """Create a clean Persian Telegram signal message."""
    side = str(signal.get("side", "BUY")).upper()
    is_long = side == "BUY"
    emoji = "🟢" if is_long else "🔴"
    side_fa = "خرید (لانگ)" if is_long else "فروش (شورت)"
    components = signal.get("components", {}) or {}
    setup = components.get("setup", "")
    is_v3 = setup == "BREAKOUT_ADX_EMA200_PARTIAL_CHANDELIER_V3"
    is_reference_futures = setup == "REFERENCE_RSI_STOCH_ATR_RR15"
    is_legacy_single_tp = setup == "REFERENCE_EMA_RSI_STOCH_ATR_SESSION" or is_reference_futures

    entry, sl = safe_float(signal.get("entry")), safe_float(signal.get("sl"))
    tp1 = safe_float(signal.get("tp1"))
    support, resistance = safe_float(signal.get("support")), safe_float(signal.get("resistance"))
    ema200, adx = safe_float(components.get("ema200")), safe_float(components.get("adx14"))
    risk_pct, rr = safe_float(signal.get("risk_pct")), safe_float(signal.get("rr"))
    actual_risk, risk_target = safe_float(signal.get("actual_risk_usd")), safe_float(signal.get("risk_target_usd"))

    if entry is not None:
        if support is not None and support >= entry:
            support = None
        if resistance is not None and resistance <= entry:
            resistance = None

    fundamentals = signal.get("fundamentals", {}) or {}
    funding, oi = safe_float(fundamentals.get("funding")), safe_float(fundamentals.get("open_interest"))
    funding_text = f"{funding * 100:.4f}٪" if funding is not None else "نامشخص"
    oi_text = f"{oi:,.2f}" if oi is not None else "نامشخص"

    news = signal.get("news", {}) or {}
    news_raw = str(news.get("label", ""))
    news_map = {"not_used_for_entry": "صرفاً جهت اطلاع؛ در صدور سیگنال نقشی ندارد",
                "unavailable": "در دسترس نیست", "neutral": "خنثی",
                "positive": "مثبت", "negative": "منفی"}
    news_label = html.escape(news_map.get(news_raw.lower(), news_raw or "نامشخص"))
    news_adjustment = safe_float(news.get("score_adjustment"), 0) or 0
    news_items = news.get("headlines", []) or []

    exchange_raw = str(signal.get("exchange", ""))
    exchange_map = {"bitget": "بیت‌گت", "bybit": "بای‌بیت", "binanceusdm": "بایننس فیوچرز",
                    "binance": "بایننس", "binanceus": "بایننس آمریکا",
                    "bitstamp": "بیت‌استمپ", "bitfinex": "بیت‌فینکس", "cryptocom": "کریپتو دات‌کام"}
    exchange = html.escape(exchange_map.get(exchange_raw.lower(), exchange_raw or "نامشخص"))
    symbol = html.escape(str(signal.get("symbol", "نامشخص")))
    trend_raw = str(signal.get("trend", ""))
    trend_map = {"BULL": "صعودی", "BEAR": "نزولی", "NEUTRAL": "خنثی", "UNKNOWN": "نامشخص",
                 "4H RSI/Stochastic reversal": "بازگشت RSI و استوکاستیک در تایم‌فریم ۴ ساعته"}
    trend = html.escape(trend_map.get(trend_raw, trend_raw or "نامشخص"))
    sl_raw = str(signal.get("sl_method", ""))
    sl_method = html.escape(sl_raw.replace("_", " ") if sl_raw else "ساختار بازار")

    lines = [
        f"{emoji} <b>{symbol} | {side_fa}</b>",
        f"صرافی: <b>{exchange}</b>  |  بازار: قرارداد دائمی تتر (USDT-M)",
        "━━━━━━━━━━━━━━━━━━",
        "📍 <b>نقاط ورود و خروج</b>",
        f"قیمت ورود: <code>{fmt_price(entry)}</code>",
        f"حد ضرر: <code>{fmt_price(sl)}</code>" + (f"  (فاصله {risk_pct:.2f}٪)" if risk_pct is not None else ""),
    ]

    if is_v3:
        lines.extend([f"🎯 هدف اول: <code>{fmt_price(tp1)}</code>  |  بستن یک‌سوم حجم در +۰٫۷۵R",
                      "📈 ادامه معامله: مدیریت دوسوم باقی‌مانده با حد ضرر شناور",
                      "🔒 پس از هدف اول: انتقال حد ضرر به نقطه ورود"])
    elif is_reference_futures:
        lines.append(f"🎯 حد سود: <code>{fmt_price(tp1)}</code>  |  بستن کل موقعیت در +۱٫۵R")
    elif is_legacy_single_tp:
        lines.append(f"🎯 هدف نهایی: <code>{fmt_price(tp1)}</code>  |  بستن کل موقعیت")
    else:
        lines.extend([f"🎯 هدف اول: <code>{fmt_price(tp1)}</code>  |  ۵۰٪ حجم",
                      f"🎯 هدف دوم: <code>{fmt_price(signal.get('tp2'))}</code>  |  ۳۰٪ حجم",
                      f"🎯 هدف سوم: <code>{fmt_price(signal.get('tp3'))}</code>  |  ۲۰٪ حجم"])

    levels = []
    if support is not None:
        levels.append(f"حمایت: <code>{fmt_price(support)}</code>")
    if resistance is not None:
        levels.append(f"مقاومت: <code>{fmt_price(resistance)}</code>")
    if levels:
        lines.extend(["", "🧭 <b>سطوح مهم بازار</b>", *levels])

    lines.extend(["", "📊 <b>وضعیت تحلیل</b>",
                  f"جهت روند: {trend}",
                  f"قدرت روند ADX(14): {f'{adx:.1f}' if adx is not None else 'محاسبه نشده'}"
                  + (f"  |  میانگین متحرک ۲۰۰: <code>{fmt_price(ema200)}</code>" if ema200 is not None else ""),
                  f"مبنای تعیین حد ضرر: {sl_method}",
                  f"نسبت سود به زیان برآوردی: {f'{rr:.2f}' if rr is not None else 'نامشخص'}"])
    if is_reference_futures:
        lines.append("وضعیت راهبرد: در مرحله ارزیابی؛ این پیام صرفاً سیگنال است و سفارشی ثبت نمی‌کند")
    if is_v3:
        lines.append("RR نهایی: متغیر؛ به عملکرد حد ضرر شناور و قیمت خروج بستگی دارد")
        lines.append("مدیریت خروج: تریلینگ (حد ضرر شناور)؛ نتیجه به حرکت قیمت بستگی دارد")

    lines.extend(["", "🛡️ <b>مدیریت ریسک</b>",
                  "بودجه ریسک هدف: " + (f"{risk_target:.2f} دلار" if risk_target is not None else "مشخص نشده")])
    if actual_risk is not None:
        lines.append(f"ریسک برآوردی با کارمزد و لغزش قیمت: <b>{actual_risk:.2f} دلار</b>")
    if is_reference_futures:
        lines.append(f"هزینه تخمینی هر سمت معامله: کارمزد {FEE_RATE*100:.3f}٪ + لغزش قیمت {SLIPPAGE_RATE*100:.3f}٪")

    lines.extend(["", "🌐 <b>اطلاعات بازار</b>",
                  f"نرخ فاندینگ: {funding_text}  |  حجم موقعیت‌های باز (OI): {oi_text}",
                  "اعتبار سیگنال: ۴۸ ساعت", "", "📰 <b>بررسی اخبار</b>",
                  f"وضعیت: {news_label}  |  تغییر امتیاز: {news_adjustment:+g}"])
    if news_items:
        lines.append("خبرهای مرتبط:")
        for item in news_items[:3]:
            title = html.escape(str(item.get("title", ""))[:140])
            if title:
                lines.append(f"• {title}")
    else:
        lines.append("در منابع بررسی‌شده خبر مرتبطی پیدا نشد؛ این به معنی نبود اخبار در کل بازار نیست.")
    return "\n".join(lines)


# ============================================================
# SCANNER
# ============================================================

def has_potential_long_setup(df):
    """Broad price-action prefilter; detailed S/R structure is evaluated after daily trend."""
    if df is None or len(df) < 205:
        return False
    work = add_indicators(df).dropna(subset=["atr", "ema20", "ema50", "ema200", "rsi"]).reset_index(drop=True)
    if len(work) < 80:
        return False
    last = work.iloc[-1]
    close, opn = safe_float(last["close"]), safe_float(last["open"])
    low, high = safe_float(last["low"]), safe_float(last["high"])
    atr, ema20 = safe_float(last["atr"]), safe_float(last["ema20"])
    ema50, ema200 = safe_float(last["ema50"]), safe_float(last["ema200"])
    rsi = safe_float(last["rsi"])
    if any(v is None for v in (close, opn, low, high, atr, ema20, ema50, ema200, rsi)) or atr <= 0:
        return False
    if abs(close-opn) > 2.2*atr or high-low > 3.2*atr:
        return False
    prior = work.iloc[-21:-1]
    close_loc = (close-low)/max(high-low, 1e-12)
    trend_ok = close > ema50 and ema50 > ema200
    breakout = trend_ok and close > float(prior["high"].max()) and close_loc >= 0.55
    pullback = trend_ok and low <= ema20 + 0.45*atr and close > opn and close_loc >= 0.55
    rejection = close > opn and close_loc >= 0.62 and low <= float(work.iloc[-60:-1]["low"].quantile(0.18)) + 0.55*atr
    failed_breakdown = low < float(work.iloc[-60:-1]["low"].quantile(0.12)) - 0.10*atr and close > opn and close_loc >= 0.60
    spike_pullback = (close > opn and close > ema20 and
                      int((work["close"].iloc[-9:-1] > work["open"].iloc[-9:-1]).sum()) >= 3)
    return bool(breakout or pullback or rejection or failed_breakdown or spike_pullback)

def scan_market(manager, state):
    candidates = manager.scan_symbols()
    headlines = fetch_news_context()

    if not candidates:
        raise RuntimeError("Market scan aborted: no futures candidates were discovered.")

    signals = []
    stats = {
        "candidates": len(candidates),
        "checked": 0,
        "signals": 0,
        "no_setup_or_score": 0,
        "unknown_trend": 0,
        "duplicates": 0,
        "rate_limited_exchanges": set(),
        "errors": 0,
    }
    rate_limited_exchanges = set()

    for idx, (preferred_exchange_name, symbol) in enumerate(candidates, 1):
        exchange_names = [preferred_exchange_name]
        for fallback_name in EXCHANGE_NAMES:
            if fallback_name not in exchange_names:
                fallback_exchange = manager.get_exchange(fallback_name)
                if fallback_exchange and symbol in fallback_exchange.markets:
                    exchange_names.append(fallback_name)

        for exchange_name in exchange_names:
            # Once a venue rate-limits us, avoid hammering it for every remaining symbol.
            if exchange_name in rate_limited_exchanges:
                continue
            exchange = manager.get_exchange(exchange_name)
            if not exchange:
                continue

            try:
                stats["checked"] += 1
                raw = manager.fetch_ohlcv(exchange, symbol)
                df = closed_candles(raw)
                if len(df) < 65:
                    stats["no_setup_or_score"] += 1
                    break

                # RSI/Stochastic is the sole entry admission rule; news context is informational.
                next_open = None
                if raw and len(raw) >= 2:
                    closed_ts = safe_float(df.iloc[-1].get("timestamp"))
                    current_ts = safe_float(raw[-1][0])
                    interval_ms = safe_float(exchange.parse_timeframe(TIMEFRAME), 14400.0) * 1000.0
                    now_ms = utc_now().timestamp() * 1000.0
                    # The scheduled scan runs shortly after the UTC 4H boundary.
                    # If Actions starts late, do not pretend the old candle open is executable.
                    if (closed_ts is not None and current_ts is not None
                            and abs(current_ts-closed_ts-interval_ms) < 1
                            and 0 <= now_ms-current_ts <= 10 * 60 * 1000):
                        next_open = safe_float(raw[-1][1])
                if next_open is not None and not is_tradable_symbol(symbol, entry_price=next_open):
                    stats["no_setup_or_score"] += 1
                    break
                signal = build_signal(symbol, exchange_name, df, next_open=next_open)
                if signal:
                    # Snapshot the current Binance funding rate for alert context.
                    # Historical funding cashflows are not yet accrued into PnL.
                    signal["fundamentals"] = fundamental_data(manager, symbol)

                if not signal:
                    stats["no_setup_or_score"] += 1
                    break

                if duplicate_exists(state, symbol, signal["side"]) or duplicate_on_candle_exists(state, signal):
                    stats["duplicates"] += 1
                    break

                signals.append(signal)
                stats["signals"] += 1
                print(
                    f"[SIGNAL] {symbol} {signal['side']} "
                    f"score={signal['score']} exchange={exchange_name} "
                    f"setup={signal.get('components', {}).get('setup', 'N/A')}"
                )
                break

            except Exception as exc:
                message = str(exc).lower()
                is_rate_limit = (
                    isinstance(exc, getattr(ccxt, "RateLimitExceeded", ()))
                    or "ratelimit" in message
                    or "rate limit" in message
                    or '11010' in message
                )
                if is_rate_limit:
                    rate_limited_exchanges.add(exchange_name)
                    stats["rate_limited_exchanges"].add(exchange_name)
                    print(
                        f"[RATE LIMIT] {exchange_name}: pausing this venue for the rest of this scan; "
                        f"will try fallback exchanges."
                    )
                else:
                    stats["errors"] += 1
                    print(
                        f"[SCAN FAIL] {exchange_name} {symbol}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                # Continue to a different configured venue for this symbol.
                continue

        if idx % 25 == 0 or idx == len(candidates):
            print(
                f"[SCAN PROGRESS] {idx}/{len(candidates)} symbols; "
                f"signals={stats['signals']}; rate_limited={','.join(sorted(rate_limited_exchanges)) or 'none'}"
            )
        # CCXT rate limiting handles request pacing; avoid extra per-symbol delay.

    if stats["checked"] == 0:
        raise RuntimeError(
            "Market scan failed: candidates were found but no OHLCV request succeeded."
        )
    signals.sort(key=lambda x: safe_float(x.get("rr"), 0.0) or 0.0, reverse=True)
    print(
        "[SCAN SUMMARY] "
        f"candidates={stats['candidates']} checked={stats['checked']} "
        f"signals={stats['signals']} no_setup_or_score={stats['no_setup_or_score']} "
        f"unknown_trend={stats['unknown_trend']} duplicates={stats['duplicates']} "
        f"errors={stats['errors']} "
        f"rate_limited={','.join(sorted(stats['rate_limited_exchanges'])) or 'none'}"
    )
    return signals


# ============================================================
# MAIN
# ============================================================

def main(manage_only=False):
    print("=" * 70)
    print("CRYPTO SIGNAL BOT — RSI/STOCH FUTURES" + (" — MANAGEMENT ONLY" if manage_only else ""))
    print("Started:", iso_now())
    print("=" * 70)

    state = load_state()

    # V4 starts the RSI/Stochastic USDT-M forward-test baseline without deleting history.
    if state.get("signal_history_reset_version", 0) < 4:
        state["forward_test_started_at"] = iso_now()
        state["signal_history_reset_version"] = 4
        refresh_historical_stats(state)
        save_state(state)
        print("RSI/Stochastic Futures forward-test baseline reset; historical and active signals preserved.")
        telegram_send(
            "♻️ <b>شروع تست مستقل RSI/Stochastic Futures</b>\n"
            "شمارندهٔ عملکرد از صفر شروع شد. تاریخچه و سیگنال‌های فعال حذف نشدند؛ "
            "گزارش عملکرد فقط معاملات استراتژی جدید را حساب می‌کند."
        )

    manager = ExchangeManager()

    required_exchanges = {
        str(signal.get("exchange"))
        for signal in state.get("signals", [])
        if signal.get("status") == "ACTIVE" and signal.get("exchange")
    }
    if not manager.build(required_exchanges=required_exchanges):
        telegram_send(
            "❌ <b>Crypto Signal Bot</b>\n"
            "هیچ صرافی قابل دسترسی نبود."
        )
        return

    # Manage existing signals first and notify Telegram about every new TP/SL/expiry event.
    management_events = update_open_signals(state, manager)
    if management_events:
        refresh_historical_stats(state)
        save_state(state)
        for event_message in management_events:
            telegram_send(event_message)
            time.sleep(0.25)
    else:
        # Keep the cached historical counters synchronized even if no signal changed.
        refresh_historical_stats(state)

    if manage_only:
        print("Management-only run complete; market scan skipped.")
        return

    new_signals = scan_market(manager, state)

    # Keep the number of stored signals reasonable.
    state["signals"].extend(new_signals)

    # Keep history but avoid unbounded growth.
    state["signals"] = state["signals"][-1000:]
    state["last_run"] = iso_now()
    refresh_historical_stats(state)

    save_state(state)

    if new_signals:
        telegram_send(
            f"🚨 <b>{len(new_signals)} سیگنال جدید</b>\n"
            f"زمان: {iso_now()}"
        )

        # Forward-test every emitted signal; do not silently omit signals beyond a batch cap.
        for signal in new_signals:
            telegram_send(format_signal(signal))
            time.sleep(0.4)
    else:
        telegram_send(
            "🔎 <b>Crypto Signal Bot</b>\n"
            "در این دور هیچ نمادی از تمام شرط‌های اجباری استراتژی عبور نکرد."
        )

    telegram_send(performance_report(state))

    active = sum(
        1 for s in state["signals"]
        if s.get("status") == "ACTIVE"
    )

    print("=" * 70)
    print(f"New signals: {len(new_signals)}")
    print(f"Active signals: {active}")
    print(f"Total stored: {len(state['signals'])}")
    print("Finished:", iso_now())
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Crypto spot signal bot")
    parser.add_argument("--manage-only", action="store_true", help="Manage active signals without scanning markets")
    args = parser.parse_args()
    try:
        main(manage_only=args.manage_only)
    except Exception:
        traceback.print_exc()
        telegram_send(
            "❌ <b>Crypto Signal Bot Error</b>\n"
            "اجرای Bot با خطا متوقف شد. لاگ GitHub Actions را بررسی کن."
        )
        raise
