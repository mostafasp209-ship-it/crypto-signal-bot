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


# ============================================================
# CONFIG
# ============================================================

EXCHANGE_NAMES = [
    "binanceus",
    "bitstamp",
    "bitfinex",
    "cryptocom",
    "bitget",
]

FUNDING_SOURCES = [
    "binanceus",
    "bitstamp",
]

QUOTE = "USDT"
TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"

MAX_COINS = None  # Scan every eligible USDT spot symbol across configured exchanges
MIN_SCORE = 12
MIN_RR = 1.50

SIGNAL_EXPIRY_HOURS = 48
RISK_USD = 10.0
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
FEE_RATE = 0.001

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

    def build(self):
        for name in EXCHANGE_NAMES:
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

        return bool(self.exchanges)

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

    def scan_symbols(self):
        # Prefer Binance.US, then fill with fallback exchanges.
        ordered = []
        seen = set()

        for name in EXCHANGE_NAMES:
            ex = self.exchanges.get(name)
            if not ex:
                continue

            symbols = self.market_symbols(ex)
            print(f"{name}: {len(symbols)} {QUOTE} spot symbols")

            for symbol in symbols:
                if symbol not in seen:
                    ordered.append((name, symbol))
                    seen.add(symbol)

                if MAX_COINS is not None and len(ordered) >= MAX_COINS:
                    return ordered

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

    positive = returns.tail(4).sum()
    negative = returns.tail(4).sum()

    if positive > 0.025:
        return True, False, 1
    if negative < -0.025:
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

def calculate_levels(side, entry, atr, sr_support, sr_resistance):
    entry = safe_float(entry)
    atr = safe_float(atr)

    if not entry or not atr or atr <= 0:
        return None

    # ATR-based stop with a safety floor.
    stop_distance = max(atr * 1.8, entry * 0.015)

    if side == "BUY":
        sl = entry - stop_distance
        tp1 = entry + stop_distance * 1.0
        tp2 = entry + stop_distance * 1.8
        tp3 = entry + stop_distance * 2.6
    else:
        sl = entry + stop_distance
        tp1 = entry - stop_distance * 1.0
        tp2 = entry - stop_distance * 1.8
        tp3 = entry - stop_distance * 2.6

    # Don't use S/R to move SL into an unsafe position.
    if side == "BUY" and sr_support:
        candidate = sr_support * 0.995
        if candidate < entry and candidate > sl:
            sl = candidate
    elif side == "SELL" and sr_resistance:
        candidate = sr_resistance * 1.005
        if candidate > entry and candidate < sl:
            sl = candidate

    # Recalculate all targets from the FINAL stop distance. Previously, S/R
    # could move the stop without moving targets, making rr1/rr2 inconsistent.
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if side == "BUY":
        tp1, tp2, tp3 = entry + risk * 1.0, entry + risk * 1.8, entry + risk * 2.6
    else:
        tp1, tp2, tp3 = entry - risk * 1.0, entry - risk * 1.8, entry - risk * 2.6
    reward = abs(tp3 - entry)
    rr = reward / risk

    if rr < MIN_RR:
        return None

    return {
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_per_unit": risk,
        "rr": rr,
    }


def build_signal(symbol, exchange_name, df, trend, fundamentals, news_context=None):
    """Hybrid price-action strategy: trend + breakout/retest or EMA pullback.

    Uses only the latest closed candle supplied by the scanner. The rest of the
    signal schema and trade-management code intentionally remain unchanged.
    """
    if df is None or len(df) < 205:
        return None

    df = add_indicators(df).dropna(subset=["atr", "ema20", "ema50", "ema200", "rsi"]).reset_index(drop=True)
    if len(df) < 60:
        return None

    i = len(df) - 1
    last = df.iloc[i]
    prev = df.iloc[i - 1]
    close = safe_float(last["close"])
    opn = safe_float(last["open"])
    high = safe_float(last["high"])
    low = safe_float(last["low"])
    atr = safe_float(last["atr"])
    volume = safe_float(last.get("volume"), 0.0) or 0.0
    if not close or not atr or atr <= 0 or not opn:
        return None

    # Compare the signal candle only with candles that preceded it.
    prior = df.iloc[i-20:i]
    prior_high = safe_float(prior["high"].max())
    prior_low = safe_float(prior["low"].min())
    vol_base = safe_float(df["volume"].iloc[max(0, i-20):i].median(), 0.0) or 0.0
    candle_range = max(high - low, 1e-12)
    close_location = (close - low) / candle_range
    body = abs(close - opn)
    rsi = safe_float(last["rsi"], 50.0)

    bullish_trend = (
        trend == "BULL"
        and close > safe_float(last["ema50"])
        and safe_float(last["ema50"]) > safe_float(last["ema200"])
    )
    bearish_trend = (
        trend == "BEAR"
        and close < safe_float(last["ema50"])
        and safe_float(last["ema50"]) < safe_float(last["ema200"])
    )

    # Breakout-and-hold: prior range is calculated without the current candle.
    buy_breakout = (
        bullish_trend and close > prior_high
        and close > opn and close_location >= 0.65
        and body >= 0.35 * atr and 42 <= rsi <= 72
    )
    sell_breakout = (
        bearish_trend and close < prior_low
        and close < opn and close_location <= 0.35
        and body >= 0.35 * atr and 28 <= rsi <= 58
    )

    # Trend pullback: price tests EMA20 and closes back in trend direction.
    buy_pullback = (
        bullish_trend and low <= safe_float(last["ema20"]) + 0.20 * atr
        and close > safe_float(last["ema20"]) and close > opn
        and close_location >= 0.60 and 42 <= rsi <= 68
    )
    sell_pullback = (
        bearish_trend and high >= safe_float(last["ema20"]) - 0.20 * atr
        and close < safe_float(last["ema20"]) and close < opn
        and close_location <= 0.40 and 32 <= rsi <= 58
    )

    # Avoid entries in exceptionally quiet or explosive candles.
    if body > 2.2 * atr or (high - low) > 3.2 * atr:
        return None

    buy_setup = "BREAKOUT_RETEST" if buy_breakout else ("EMA_PULLBACK" if buy_pullback else None)
    sell_setup = "BREAKDOWN_RETEST" if sell_breakout else ("EMA_PULLBACK" if sell_pullback else None)
    if buy_setup:
        side, setup = "BUY", buy_setup
    elif sell_setup:
        # This project is spot-long only. A SELL entry is a short position and
        # must not be advertised as an executable spot signal.
        print(f"[FILTER] {symbol}: bearish setup ignored (spot-long only)")
        return None
    else:
        return None

    # Score is a transparent quality score, not a promise of win probability.
    score = 8
    components = {"setup": setup, "daily_trend": trend}
    if (side == "BUY" and close > last["ema20"] > last["ema50"]) or (side == "SELL" and close < last["ema20"] < last["ema50"]):
        score += 2; components["ema_alignment"] = 2
    if (side == "BUY" and close_location >= 0.75) or (side == "SELL" and close_location <= 0.25):
        score += 1; components["close_strength"] = 1
    if vol_base > 0 and volume >= vol_base * 1.2:
        score += 1; components["volume_confirmation"] = 1
    if (side == "BUY" and 48 <= rsi <= 65) or (side == "SELL" and 35 <= rsi <= 52):
        score += 1; components["momentum"] = 1
    fund_score = fundamental_score(fundamentals, side)
    if fund_score:
        score += 1; components["fundamental_confirmation"] = fund_score
    news_context = news_context or {"score_adjustment": 0, "label": "unavailable", "headlines": []}
    news_adjustment = int(news_context.get("score_adjustment", 0))
    score += news_adjustment
    components["news_context"] = news_context.get("label", "unavailable")
    components["news_score_adjustment"] = news_adjustment
    if score < MIN_SCORE:
        print(f"[FILTER] {symbol} {side} setup={setup} score={score} < MIN_SCORE={MIN_SCORE}")
        return None

    recent = df.tail(60)
    support = safe_float(recent["low"].quantile(0.10))
    resistance = safe_float(recent["high"].quantile(0.90))
    levels = calculate_levels(side, close, atr, support, resistance)
    if not levels:
        return None
    risk_pct = abs(levels["entry"] - levels["sl"]) / levels["entry"] * 100
    if risk_pct > 15:
        return None

    strength = "قوی" if score >= 13 else "متوسط" if score >= 11 else "ضعیف"
    risk_per_unit = abs(levels["entry"] - levels["sl"])
    if risk_per_unit <= 0:
        return None
    risk_sized_quantity = RISK_USD / risk_per_unit
    cap_quantity = MAX_POSITION_USD / levels["entry"] if MAX_POSITION_USD > 0 else risk_sized_quantity
    position_quantity = min(risk_sized_quantity, cap_quantity)
    position_value = position_quantity * levels["entry"]
    actual_risk_usd = position_quantity * risk_per_unit
    return {
        "id": f"{symbol.replace('/', '_')}_{side}_{int(time.time())}",
        "symbol": symbol,
        "exchange": exchange_name,
        "side": side,
        "status": "ACTIVE",
        "strength": strength,
        "score": int(score),
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "expiry_at": (utc_now() + timedelta(hours=SIGNAL_EXPIRY_HOURS)).isoformat(),
        "entry": levels["entry"],
        "sl": levels["sl"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp3": levels["tp3"],
        "rr": levels["rr"],
        "risk_pct": risk_pct,
        # Preserve the original stop distance for correct partial PnL after SL moves to breakeven.
        "initial_risk_per_unit": abs(levels["entry"] - levels["sl"]),
        "initial_sl": levels["sl"],
        "risk_usd": RISK_USD,
        "risk_target_usd": RISK_USD,
        "actual_risk_usd": actual_risk_usd,
        "position_quantity": position_quantity,
        "quantity": position_quantity,
        "position_value": position_value,
        "position_capped": position_quantity + 1e-12 < risk_sized_quantity,
        "components": components,
        "trend": trend,
        "fundamentals": fundamentals,
        "news": news_context,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "remaining_fraction": 1.0,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "closed_reason": None,
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


def close_piece(signal, exit_price, fraction, label):
    entry = safe_float(signal["entry"])
    exit_price = safe_float(exit_price)

    if not entry or not exit_price:
        return

    # Position size must use the ORIGINAL stop distance. The active SL may move
    # to entry after TP1, which must not make remaining partial exits unaccounted.
    risk_per_unit = safe_float(signal.get("initial_risk_per_unit"))
    if not risk_per_unit or risk_per_unit <= 0:
        original_sl = safe_float(signal.get("initial_sl"))
        if original_sl is None:
            # Compatibility for older signals: TP1 is one original risk unit away.
            tp1 = safe_float(signal.get("tp1"))
            risk_per_unit = abs(tp1 - entry) if tp1 is not None else 0.0
        else:
            risk_per_unit = abs(entry - original_sl)
    if risk_per_unit <= 0:
        print(f"[PNL ERROR] Cannot determine initial risk for {signal.get('symbol')}")
        return

    quantity = safe_float(signal.get("position_quantity"))
    if quantity is None or quantity <= 0:
        quantity = RISK_USD / risk_per_unit
        signal["position_quantity"] = quantity
    gross = (
        (exit_price - entry) * quantity
        if signal["side"] == "BUY"
        else (entry - exit_price) * quantity
    )

    piece_qty = quantity * fraction
    gross_piece = gross * fraction

    entry_fee = entry * piece_qty * FEE_RATE
    exit_fee = exit_price * piece_qty * FEE_RATE
    fees = entry_fee + exit_fee

    net = gross_piece - fees

    signal["realized_pnl"] = safe_float(signal.get("realized_pnl"), 0) + net
    signal["fees_paid"] = safe_float(signal.get("fees_paid"), 0) + fees
    signal["remaining_fraction"] = max(
        0,
        safe_float(signal.get("remaining_fraction"), 1) - fraction,
    )

    signal[f"{label}_hit"] = True
    signal["updated_at"] = iso_now()


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
                if remaining > 0:
                    close_piece(signal, current_price, remaining, "expiry")
                signal["status"] = "EXPIRED"
                signal["closed_reason"] = "48h expiry"
                signal["updated_at"] = iso_now()
                return True
        except Exception:
            pass

    # Stop is checked before new TP levels.
    if stop_hit(signal, current_price):
        remaining = safe_float(signal.get("remaining_fraction"), 0)
        if remaining > 0:
            close_piece(signal, current_price, remaining, "sl")
        signal["status"] = "CLOSED"
        signal["closed_reason"] = "SL"
        signal["updated_at"] = iso_now()
        return True

    if not signal.get("tp1_hit") and price_hit(signal, current_price, "tp1"):
        close_piece(signal, signal["tp1"], TP_WEIGHTS["tp1"], "tp1")
        # Breakeven after TP1.
        signal["sl"] = signal["entry"]
        changed = True

    if not signal.get("tp2_hit") and price_hit(signal, current_price, "tp2"):
        close_piece(signal, signal["tp2"], TP_WEIGHTS["tp2"], "tp2")
        changed = True

    if not signal.get("tp3_hit") and price_hit(signal, current_price, "tp3"):
        close_piece(signal, signal["tp3"], TP_WEIGHTS["tp3"], "tp3")
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
            "sl": safe_float(signal.get("sl")),
        }
        changed = manage_signal(signal, price)
        if not changed:
            continue

        side = signal.get("side", "BUY")
        header = f"<b>{symbol} — {side}</b>"
        if not before["tp1_hit"] and signal.get("tp1_hit"):
            events.append(
                f"🎯 <b>TP1 فعال شد</b>\n{header}\n"
                f"قیمت بررسی: <b>{fmt_price(price)}</b>\n"
                f"۵۰٪ از موقعیت ثبت شد؛ حد ضرر باقی‌مانده به نقطه ورود منتقل شد.\n"
                f"SL جدید: <b>{fmt_price(signal.get('sl'))}</b>"
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
            if reason == "SL":
                title = "🛑 <b>حد ضرر (SL) فعال شد</b>"
            elif reason == "TP3":
                title = "✅ <b>موقعیت با TP3 بسته شد</b>"
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
        "tp1_then_sl": 0, "sl_direct": 0, "expired": 0,
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
    signals = state.get("signals", [])
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
    side = signal["side"]
    emoji = "🟢" if side == "BUY" else "🔴"

    f = signal.get("fundamentals", {})
    funding = f.get("funding")
    oi = f.get("open_interest")

    funding_text = (
        f"{funding * 100:.4f}%"
        if funding is not None else "N/A"
    )
    oi_text = (
        f"{oi:,.2f}"
        if oi is not None else "N/A"
    )

    return (
        f"{emoji} <b>{signal['symbol']} — {side}</b>\n"
        f"صرافی: {signal['exchange']}\n"
        f"قدرت: <b>{signal['strength']}</b> | Score: <b>{signal['score']}</b>\n"
        f"روند روزانه: {signal['trend']}\n"
        f"📰 وضعیت خبر: {signal.get('news', {}).get('label', 'N/A')} "
        f"(تعدیل امتیاز: {signal.get('news', {}).get('score_adjustment', 0)})\n"
        + "".join(
            f"• {html.escape(str(item.get('title', ''))[:180])}\n"
            for item in signal.get("news", {}).get("headlines", [])[:2]
        )
        + "\\n"

        f"🎯 Entry: <b>{fmt_price(signal['entry'])}</b>\n"
        f"🛑 SL: <b>{fmt_price(signal['sl'])}</b> "
        f"({signal['risk_pct']:.2f}%)\n"
        f"🥇 TP1: <b>{fmt_price(signal['tp1'])}</b> — 50%\n"
        f"🥈 TP2: <b>{fmt_price(signal['tp2'])}</b> — 30%\n"
        f"🥉 TP3: <b>{fmt_price(signal['tp3'])}</b> — 20%\n"
        f"RR: <b>{signal['rr']:.2f}</b>\n\n"


        f"💰 Funding: {funding_text}\n"
        f"📊 OI: {oi_text}\n"
        f"⏱ Expiry: 48h\n"
        f"⚖️ ریسک اسمی: ${signal['risk_usd']:.2f}"
    )


# ============================================================
# SCANNER
# ============================================================

def scan_market(manager, state):
    candidates = manager.scan_symbols()
    headlines = fetch_news_context()

    if not candidates:
        print("No markets found.")
        return []

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
                if len(df) < 210:
                    stats["no_setup_or_score"] += 1
                    break

                trend = daily_trend(exchange, symbol)
                if trend == "RATE_LIMIT":
                    rate_limited_exchanges.add(exchange_name)
                    stats["rate_limited_exchanges"].add(exchange_name)
                    print(f"[RATE LIMIT] {exchange_name}: skipping this venue for remaining scan")
                    continue
                if trend == "UNKNOWN":
                    stats["unknown_trend"] += 1
                    # Try another exchange for this symbol when the daily feed failed.
                    continue

                fundamentals = fundamental_data(manager, symbol)
                news_context = news_context_for_symbol(symbol, headlines)
                signal = build_signal(symbol, exchange_name, df, trend, fundamentals, news_context)

                if not signal:
                    stats["no_setup_or_score"] += 1
                    break

                if duplicate_exists(state, symbol, signal["side"]):
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
        time.sleep(SLEEP_BETWEEN_SYMBOLS)

    signals.sort(key=lambda x: (x["score"], x["rr"]), reverse=True)
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

def main():
    print("=" * 70)
    print("CRYPTO SIGNAL BOT")
    print("Started:", iso_now())
    print("=" * 70)

    state = load_state()

    manager = ExchangeManager()

    if not manager.build():
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

        # Send strongest signals first.
        for signal in new_signals[:10]:
            telegram_send(format_signal(signal))
            time.sleep(0.4)
    else:
        telegram_send(
            "🔎 <b>Crypto Signal Bot</b>\n"
            "این دور سیگنال جدیدی با امتیاز کافی پیدا نشد."
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
    try:
        main()
    except Exception:
        traceback.print_exc()
        telegram_send(
            "❌ <b>Crypto Signal Bot Error</b>\n"
            "اجرای Bot با خطا متوقف شد. لاگ GitHub Actions را بررسی کن."
        )
        raise
