import os
import json
import time
import math
import traceback
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
]

FUNDING_SOURCES = [
    "binanceus",
    "bitstamp",
]

QUOTE = "USDT"
TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"

MAX_COINS = 300
MIN_SCORE = 12
MIN_RR = 1.50

SIGNAL_EXPIRY_HOURS = 48
RISK_USD = 10.0
FEE_RATE = 0.001

TP_WEIGHTS = {
    "tp1": 0.50,
    "tp2": 0.30,
    "tp3": 0.20,
}

DATA_FILE = "signals.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

REQUEST_TIMEOUT_MS = 15000
OHLCV_LIMIT = 260
DAILY_LIMIT = 120
SLEEP_BETWEEN_SYMBOLS = 0.08


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

def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are missing; message skipped.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:500])
            return False
        return True
    except Exception as exc:
        print("Telegram exception:", exc)
        return False


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

                if len(ordered) >= MAX_COINS:
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
        print(f"daily trend failed {symbol}: {exc}")
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

    risk = abs(entry - sl)
    reward = abs(tp3 - entry)
    rr = reward / risk if risk > 0 else 0

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


def build_signal(symbol, exchange_name, df, trend, fundamentals):
    df = add_indicators(df)
    if len(df) < 205:
        return None

    last = df.iloc[-1]

    components = {}
    bull_total = 0
    bear_total = 0

    checks = [
        ("ema", ema_score(last)),
        ("rsi", rsi_score(last)),
        ("bb", bb_score(last)),
        ("ichi", ichimoku_score(last)),
        ("fib", fibonacci_score(df)),
        ("sr", sr_score(df)),
        ("brooks", brooks_score(df)),
        ("rtm", rtm_score(df)),
    ]

    for name, (bull, bear, score) in checks:
        components[name] = score
        if bull:
            bull_total += score
        if bear:
            bear_total += score

    if trend == "BULL":
        bull_total += 2
    elif trend == "BEAR":
        bear_total += 2

    buy_fund = fundamental_score(fundamentals, "BUY")
    sell_fund = fundamental_score(fundamentals, "SELL")

    bull_total += buy_fund
    bear_total += sell_fund

    if bull_total >= bear_total:
        side = "BUY"
        score = bull_total
    else:
        side = "SELL"
        score = bear_total

    if score < MIN_SCORE:
        return None

    close = safe_float(last["close"])
    atr = safe_float(last["atr"])

    recent = df.tail(60)
    support = safe_float(recent["low"].quantile(0.10))
    resistance = safe_float(recent["high"].quantile(0.90))

    levels = calculate_levels(
        side,
        close,
        atr,
        support,
        resistance,
    )

    if not levels:
        return None

    risk_pct = abs(levels["entry"] - levels["sl"]) / levels["entry"] * 100

    # Reject absurd stops.
    if risk_pct > 15:
        return None

    strength = (
        "قوی" if score >= 18
        else "متوسط" if score >= 15
        else "ضعیف"
    )

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
        "risk_usd": RISK_USD,
        "components": components,
        "trend": trend,
        "fundamentals": fundamentals,
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

    # Position size is derived from fixed USD risk.
    risk_per_unit = abs(entry - safe_float(signal["sl"]))
    if risk_per_unit <= 0:
        return

    quantity = RISK_USD / risk_per_unit
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
    changed = False

    for signal in state.get("signals", []):
        if signal.get("status") != "ACTIVE":
            continue

        symbol = signal.get("symbol")
        exchange_name = signal.get("exchange")
        exchange = manager.get_exchange(exchange_name)

        if not exchange:
            continue

        if symbol not in exchange.markets:
            continue

        price = manager.ticker_price(exchange, symbol)
        if price is None:
            continue

        before_status = signal.get("status")
        before_tp1 = signal.get("tp1_hit")
        before_tp2 = signal.get("tp2_hit")
        before_tp3 = signal.get("tp3_hit")

        manage_signal(signal, price)

        if (
            signal.get("status") != before_status
            or signal.get("tp1_hit") != before_tp1
            or signal.get("tp2_hit") != before_tp2
            or signal.get("tp3_hit") != before_tp3
        ):
            changed = True

    return changed


# ============================================================
# REPORTING
# ============================================================

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

    components = signal.get("components", {})

    return (
        f"{emoji} <b>{signal['symbol']} — {side}</b>\n"
        f"صرافی: {signal['exchange']}\n"
        f"قدرت: <b>{signal['strength']}</b> | Score: <b>{signal['score']}</b>\n"
        f"روند روزانه: {signal['trend']}\n\n"

        f"🎯 Entry: <b>{fmt_price(signal['entry'])}</b>\n"
        f"🛑 SL: <b>{fmt_price(signal['sl'])}</b> "
        f"({signal['risk_pct']:.2f}%)\n"
        f"🥇 TP1: <b>{fmt_price(signal['tp1'])}</b> — 50%\n"
        f"🥈 TP2: <b>{fmt_price(signal['tp2'])}</b> — 30%\n"
        f"🥉 TP3: <b>{fmt_price(signal['tp3'])}</b> — 20%\n"
        f"RR: <b>{signal['rr']:.2f}</b>\n\n"

        f"📐 EMA: {components.get('ema', 0)}\n"
        f"RSI: {components.get('rsi', 0)}\n"
        f"BB: {components.get('bb', 0)}\n"
        f"Ichimoku: {components.get('ichi', 0)}\n"
        f"Fibonacci: {components.get('fib', 0)}\n"
        f"S/R: {components.get('sr', 0)}\n"
        f"Brooks: {components.get('brooks', 0)}\n"
        f"RTM: {components.get('rtm', 0)}\n\n"

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

    if not candidates:
        print("No markets found.")
        return []

    signals = []

    for idx, (preferred_exchange_name, symbol) in enumerate(candidates, 1):
        # Try the preferred venue first, then any other configured venue
        # that actually lists the same symbol.
        exchange_names = [preferred_exchange_name]
        for fallback_name in EXCHANGE_NAMES:
            if fallback_name not in exchange_names:
                fallback_exchange = manager.get_exchange(fallback_name)
                if fallback_exchange and symbol in fallback_exchange.markets:
                    exchange_names.append(fallback_name)

        signal_created = False

        for exchange_name in exchange_names:
            exchange = manager.get_exchange(exchange_name)
            if not exchange:
                continue

            try:
                raw = manager.fetch_ohlcv(exchange, symbol)
                df = closed_candles(raw)

                if len(df) < 210:
                    continue

                trend = daily_trend(exchange, symbol)
                fundamentals = fundamental_data(manager, symbol)

                signal = build_signal(
                    symbol,
                    exchange_name,
                    df,
                    trend,
                    fundamentals,
                )

                if not signal:
                    # A valid market but no qualifying setup; don't need
                    # to retry the same symbol on every venue.
                    signal_created = True
                    break

                if duplicate_exists(state, symbol, signal["side"]):
                    signal_created = True
                    break

                signals.append(signal)

                print(
                    f"[SIGNAL] {symbol} {signal['side']} "
                    f"score={signal['score']} exchange={exchange_name}"
                )

                signal_created = True
                break

            except Exception as exc:
                print(
                    f"[SCAN FAIL] {exchange_name} {symbol}: "
                    f"{type(exc).__name__}: {exc}"
                )
                # Try the next exchange for this symbol.

        time.sleep(SLEEP_BETWEEN_SYMBOLS)

    signals.sort(
        key=lambda x: (x["score"], x["rr"]),
        reverse=True,
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

    # First manage previous active trades.
    if update_open_signals(state, manager):
        save_state(state)

    new_signals = scan_market(manager, state)

    # Keep the number of stored signals reasonable.
    state["signals"].extend(new_signals)

    # Keep history but avoid unbounded growth.
    state["signals"] = state["signals"][-1000:]
    state["last_run"] = iso_now()

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
