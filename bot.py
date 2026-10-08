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
EXCHANGE_NAMES = ["binanceus", "bitstamp", "cryptocom"]
FUNDING_SOURCES = ["binanceus", "bitstamp"]

QUOTE = "USDT"
TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"

MAX_COINS = 300
MIN_SCORE = 11
MIN_RR = 1.50

SIGNAL_EXPIRY_HOURS = 48
DUPLICATE_COOLDOWN_HOURS = 24

CAPITAL_USD = 1000.0
RISK_PCT = 1.0
MAX_POSITION_PCT = 20.0

FEE_RATE = 0.001
SL_FILL_SLIPPAGE = 0.001

SL_MIN_DIST_PCT = 0.5
SL_MAX_DIST_PCT = 15.0

CLOSED_RETENTION_DAYS = 7
MAX_STORED_SIGNALS = 200

TP_WEIGHTS = {"tp1": 0.50, "tp2": 0.30, "tp3": 0.20}
TP_MIN_MULTS = [1.0, 1.8, 2.6]
TP_MAX_MULTS = [2.0, 3.5, 5.0]

MIN_STRUCTURE_TPS = 1
MAX_FAILS_PER_EXCHANGE = 3
QUICK_THRESHOLD = max(0, MIN_SCORE - 3)

DATA_FILE = "signals.json"
USERS_FILE = "users.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = (
    os.getenv("ADMIN_CHAT_ID", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID", "").strip()
)

REQUEST_TIMEOUT_MS = 15000
OHLCV_LIMIT = 260
DAILY_LIMIT = 120
SLEEP_BETWEEN_SYMBOLS = 0.12

STATS = {
    "evaluated": 0, "skipped_quick": 0, "daily_fetched": 0,
    "signal_created": 0, "rejected_rr": 0, "rejected_sl": 0,
    "rejected_no_structure": 0, "rejected_tp_order": 0,
}


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


def classify_error(exc):
    if isinstance(exc, (
        ccxt.RateLimitExceeded, ccxt.DDoSProtection,
        ccxt.NetworkError, ccxt.RequestTimeout,
        ccxt.ExchangeNotAvailable,
    )):
        return "exchange_fail"
    msg = str(exc).lower()
    if ("does not have market" in msg or "symbol not found" in msg
            or "invalid symbol" in msg or "does not exist" in msg):
        return "symbol_fail"
    return "exchange_fail"


# ============================================================
# USERS MANAGEMENT
# ============================================================
def load_users():
    default = {"users": {}, "last_update_id": 0}
    if not os.path.exists(USERS_FILE):
        return default
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        data.setdefault("users", {})
        data.setdefault("last_update_id", 0)
        return data
    except Exception:
        print("WARNING: users.json could not be read.")
        return default


def save_users(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)


def ensure_user(users, chat_id, name=None):
    cid = str(chat_id)
    if cid not in users["users"]:
        users["users"][cid] = {
            "name": name or "",
            "joined_at": iso_now(),
            "blocked": False,
        }
        return True
    if name and not users["users"][cid].get("name"):
        users["users"][cid]["name"] = name
    return False


def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID)


def is_blocked(users, chat_id):
    return users["users"].get(str(chat_id), {}).get("blocked", False)


def get_active_user_ids(users):
    result = []
    for cid, u in users.get("users", {}).items():
        if not u.get("blocked"):
            result.append(cid)
    return result


def telegram_send_to(chat_id, text):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
        return r.ok
    except Exception:
        return False


def telegram_broadcast(users, text):
    count = 0
    for cid in get_active_user_ids(users):
        if telegram_send_to(cid, text):
            count += 1
        time.sleep(0.05)
    return count


def telegram_get_updates(offset=0):
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
    try:
        r = requests.get(url, params={
            "offset": offset, "timeout": 0, "limit": 100
        }, timeout=20)
        if r.ok:
            return r.json().get("result", [])
    except Exception as e:
        print("[WARN] getUpdates: " + str(e)[:80])
    return []


WELCOME_MSG = (
    "👋 سلام {name}!\n\n"
    "به <b>Crypto Signal Pro</b> خوش اومدی 🚀\n\n"
    "🤖 این ربات هر ۴ ساعت بازار رو اسکن می‌کنه\n"
    "و سیگنال‌های باکیفیت رو برات می‌فرسته.\n\n"
    "⏱ برای دریافت اولین سیگنال صبر کن..."
)


def handle_admin_command(users, chat_id, text):
    if text == "/users":
        total = len(users["users"])
        active = len(get_active_user_ids(users))
        blocked = total - active
        lines = [
            "👥 <b>کاربران</b>",
            "کل: " + str(total),
            "فعال: " + str(active),
            "مسدود: " + str(blocked),
            "",
        ]
        for cid, u in list(users["users"].items())[:30]:
            status = "🚫" if u.get("blocked") else "✅"
            lines.append(
                status + " " + (u.get("name") or "?")
                + " (<code>" + cid + "</code>)"
            )
        telegram_send_to(chat_id, "\n".join(lines))
        return True

    if text == "/stats":
        msg = (
            "📊 <b>وضعیت ربات</b>\n"
            "کاربران: " + str(len(users["users"])) + "\n"
            "فعال: " + str(len(get_active_user_ids(users)))
        )
        telegram_send_to(chat_id, msg)
        return True

    if text.startswith("/block "):
        parts = text.split()
        if len(parts) >= 2:
            target = parts[1].strip()
            if target in users["users"]:
                users["users"][target]["blocked"] = True
                telegram_send_to(chat_id, "🚫 کاربر <code>" + target + "</code> مسدود شد.")
            else:
                telegram_send_to(chat_id, "❌ کاربر پیدا نشد.")
        return True

    if text.startswith("/unblock "):
        parts = text.split()
        if len(parts) >= 2:
            target = parts[1].strip()
            if target in users["users"]:
                users["users"][target]["blocked"] = False
                telegram_send_to(chat_id, "✅ کاربر <code>" + target + "</code> رفع مسدودی شد.")
            else:
                telegram_send_to(chat_id, "❌ کاربر پیدا نشد.")
        return True

    if text.startswith("/broadcast "):
        msg_text = text[len("/broadcast "):].strip()
        if msg_text:
            count = telegram_broadcast(users, "📣 " + msg_text)
            telegram_send_to(chat_id, "📣 پیام به " + str(count) + " کاربر ارسال شد.")
        return True

    if text == "/help":
        help_text = (
            "🛠 <b>دستورات مدیریتی</b>\n\n"
            "/users — لیست کاربران\n"
            "/stats — آمار\n"
            "/block &lt;id&gt; — مسدود\n"
            "/unblock &lt;id&gt; — رفع مسدودی\n"
            "/broadcast &lt;متن&gt; — پیام همگانی"
        )
        telegram_send_to(chat_id, help_text)
        return True

    return False


def poll_and_handle_commands(users):
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = users.get("last_update_id", 0) + 1
    updates = telegram_get_updates(offset=offset)
    if not updates:
        return

    max_id = users.get("last_update_id", 0)

    for u in updates:
        uid = u.get("update_id", 0)
        if uid > max_id:
            max_id = uid

        msg = u.get("message")
        if not msg:
            continue

        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if not chat_id:
            continue

        text = (msg.get("text") or "").strip()
        from_user = msg.get("from", {})
        name = from_user.get("first_name", "") or from_user.get("username", "")
        chat_id_str = str(chat_id)

        ensure_user(users, chat_id, name)

        if text.startswith("/start"):
            welcome = WELCOME_MSG.format(name=name or "دوست عزیز")
            telegram_send_to(chat_id, welcome)
            if ADMIN_CHAT_ID and not is_admin(chat_id):
                notice = (
                    "👤 <b>کاربر جدید</b>\n"
                    "نام: " + (name or "?") + "\n"
                    "Chat ID: <code>" + chat_id_str + "</code>"
                )
                telegram_send_to(ADMIN_CHAT_ID, notice)
            continue

        if is_blocked(users, chat_id):
            continue

        if is_admin(chat_id):
            if handle_admin_command(users, chat_id, text):
                continue

    users["last_update_id"] = max_id


# ============================================================
# SIGNALS STATE
# ============================================================
def empty_historical_stats():
    return {
        "closed": 0, "tp3": 0, "tp2_then_sl": 0,
        "tp1_then_sl": 0, "sl_direct": 0, "expired": 0,
        "no_risk": 0, "other": 0,
        "net_pnl": 0.0, "fees": 0.0,
        "sl_methods": {},
    }


def _bump_sl_method_stats(hist, method, cat, pnl):
    if not isinstance(hist.get("sl_methods"), dict):
        hist["sl_methods"] = {}
    m = hist["sl_methods"].setdefault(
        method, {"total": 0, "wins": 0, "partials": 0, "losses": 0, "pnl": 0.0}
    )
    m["total"] += 1
    if cat == "TP3_FULL":
        m["wins"] += 1
    elif cat in ("TP1_THEN_SL", "TP2_THEN_SL",
                 "OPEN_AFTER_TP1", "OPEN_AFTER_TP2"):
        m["partials"] += 1
    elif cat in ("SL_DIRECT", "EXPIRED_NO_TP"):
        m["losses"] += 1
    m["pnl"] = round(m["pnl"] + pnl, 2)


def load_state():
    default = {
        "signals": [], "last_run": None,
        "last_event_report": None,
        "historical_stats": empty_historical_stats(),
    }
    if not os.path.exists(DATA_FILE):
        return default
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        data.setdefault("signals", [])
        data.setdefault("last_run", None)
        data.setdefault("last_event_report", None)
        if not isinstance(data.get("historical_stats"), dict):
            data["historical_stats"] = empty_historical_stats()
        for k, v in empty_historical_stats().items():
            data["historical_stats"].setdefault(k, v)
        return data
    except Exception:
        print("WARNING: signals.json could not be read.")
        return default


def save_state(state):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def migrate_old_signals(state):
    if not isinstance(state.get("historical_stats"), dict):
        state["historical_stats"] = empty_historical_stats()
    hist = state["historical_stats"]
    cutoff = utc_now() - timedelta(days=CLOSED_RETENTION_DAYS)

    new_signals = []
    migrated = 0

    for s in state.get("signals", []):
        if s.get("status") == "ACTIVE":
            new_signals.append(s)
            continue

        ts_str = s.get("closed_at")
        if not ts_str:
            new_signals.append(s)
            continue

        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            new_signals.append(s)
            continue

        if ts < cutoff:
            cat = categorize_outcome(s)
            hist["closed"] = hist.get("closed", 0) + 1
            if cat == "TP3_FULL":
                hist["tp3"] = hist.get("tp3", 0) + 1
            elif cat == "TP2_THEN_SL":
                hist["tp2_then_sl"] = hist.get("tp2_then_sl", 0) + 1
            elif cat == "TP1_THEN_SL":
                hist["tp1_then_sl"] = hist.get("tp1_then_sl", 0) + 1
            elif cat == "SL_DIRECT":
                hist["sl_direct"] = hist.get("sl_direct", 0) + 1
            elif cat in ("EXPIRED_NO_TP", "EXPIRED_WITH_TP"):
                hist["expired"] = hist.get("expired", 0) + 1
            elif cat == "NO_RISK_DATA":
                hist["no_risk"] = hist.get("no_risk", 0) + 1
            else:
                hist["other"] = hist.get("other", 0) + 1

            pnl = safe_float(s.get("realized_pnl"), 0)
            hist["net_pnl"] = round(safe_float(hist.get("net_pnl"), 0) + pnl, 2)
            hist["fees"] = round(
                safe_float(hist.get("fees"), 0)
                + safe_float(s.get("fees_paid"), 0), 2
            )

            method = s.get("sl_method") or "unknown"
            _bump_sl_method_stats(hist, method, cat, pnl)

            migrated += 1
        else:
            new_signals.append(s)

    state["signals"] = new_signals
    return migrated


def cap_signals(state):
    signals = state.get("signals", [])
    if len(signals) <= MAX_STORED_SIGNALS:
        return
    active = [s for s in signals if s.get("status") == "ACTIVE"]
    closed = [s for s in signals if s.get("status") != "ACTIVE"]
    closed.sort(key=lambda x: x.get("closed_at") or "", reverse=True)
    keep_closed = max(0, MAX_STORED_SIGNALS - len(active))
    state["signals"] = active + closed[:keep_closed]


# ============================================================
# EXCHANGE MANAGER
# ============================================================
class ExchangeManager:
    def __init__(self):
        self.exchanges = {}
        self.fail_counts = {}
        self.disabled = set()
        self.name_by_id = {}

    def build(self):
        for name in EXCHANGE_NAMES:
            try:
                cls = getattr(ccxt, name)
                ex = cls({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT_MS})
                ex.load_markets()
                self.exchanges[name] = ex
                self.fail_counts[name] = 0
                self.name_by_id[id(ex)] = name
                print("[OK] " + name + ": " + str(len(ex.markets)) + " markets")
            except Exception as exc:
                print("[FAIL] " + name + ": " + str(exc)[:100])
        return bool(self.exchanges)

    def get_exchange(self, name):
        return self.exchanges.get(name)

    def _name_of(self, exchange):
        return self.name_by_id.get(id(exchange))

    def mark_ok(self, exchange):
        name = self._name_of(exchange)
        if name:
            self.fail_counts[name] = 0

    def mark_fail(self, exchange):
        name = self._name_of(exchange)
        if not name:
            return
        self.fail_counts[name] = self.fail_counts.get(name, 0) + 1
        if self.fail_counts[name] >= MAX_FAILS_PER_EXCHANGE:
            self.disabled.add(name)
            print("[DISABLED] " + name)

    def is_disabled(self, exchange):
        name = self._name_of(exchange)
        return name in self.disabled if name else False

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
        ordered = []
        seen = set()
        for name in EXCHANGE_NAMES:
            ex = self.exchanges.get(name)
            if not ex:
                continue
            symbols = self.market_symbols(ex)
            print(name + ": " + str(len(symbols)) + " " + QUOTE + " spot symbols")
            for symbol in symbols:
                if symbol not in seen:
                    ordered.append((name, symbol))
                    seen.add(symbol)
                if len(ordered) >= MAX_COINS:
                    return ordered
        return ordered

    def fetch_ohlcv(self, exchange, symbol, timeframe=TIMEFRAME, limit=OHLCV_LIMIT):
        if self.is_disabled(exchange):
            return None
        try:
            data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            self.mark_ok(exchange)
            return data
        except Exception as exc:
            if classify_error(exc) == "exchange_fail":
                self.mark_fail(exchange)
            raise

    def ticker_price(self, exchange, symbol):
        if self.is_disabled(exchange):
            return None
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = safe_float(ticker.get("last"))
            self.mark_ok(exchange)
            return price
        except Exception as exc:
            if classify_error(exc) == "exchange_fail":
                self.mark_fail(exchange)
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
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    df["bb_mid"] = mid
    df["bb_upper"] = mid + 2 * std
    df["bb_lower"] = mid - 2 * std

    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)

    df["tenkan"] = tenkan
    df["kijun"] = kijun
    df["span_a"] = span_a
    df["span_b"] = span_b
    return df


def closed_candles(raw):
    if not raw or len(raw) < 30:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=[
        "timestamp", "open", "high", "low", "close", "volume"
    ])
    df = df.iloc[:-1].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(inplace=True)
    return df


def ema_score(last):
    s = 0
    if last["close"] > last["ema20"]:
        s += 1
    if last["ema20"] > last["ema50"]:
        s += 1
    if last["ema50"] > last["ema200"]:
        s += 1
    return (s >= 2), (s <= 1), s


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
    top = max(span_a, span_b)
    bot = min(span_a, span_b)
    if close > top and tenkan > kijun:
        return True, False, 2
    if close < bot and tenkan < kijun:
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
    if distance >= 0.04:
        return False, False, 0
    mid = lo + span * 0.5
    if close < mid:
        return True, False, 1
    if close > mid:
        return False, True, 1
    return False, False, 0


def sr_score(df):
    recent = df.tail(60)
    close = df.iloc[-1]["close"]
    resistance = recent["high"].quantile(0.90)
    support = recent["low"].quantile(0.10)
    if close <= support * 1.03:
        return True, False, 1
    if close >= resistance * 0.97:
        return False, True, 1
    return False, False, 0


def brooks_score(df):
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
    if len(df) < 12:
        return False, False, 0
    recent = df.tail(12)
    returns = recent["close"].pct_change().dropna()
    if len(returns) < 4:
        return False, False, 0
    last4 = returns.tail(4)
    positive = last4[last4 > 0].sum()
    negative = last4[last4 < 0].sum()
    if positive > 0.025:
        return True, False, 1
    if negative < -0.025:
        return False, True, 1
    return False, False, 0


def compute_4h_score(df):
    last = df.iloc[-1]
    components = {}
    bull = 0
    bear = 0
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
    for name, (is_bull, is_bear, score) in checks:
        components[name] = score
        if is_bull:
            bull += score
        if is_bear:
            bear += score
    return bull, bear, components


def daily_trend(manager, exchange, symbol):
    try:
        raw = manager.fetch_ohlcv(
            exchange, symbol,
            timeframe=TREND_TIMEFRAME,
            limit=DAILY_LIMIT
        )
        if not raw:
            return "UNKNOWN"
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
        print("[WARN] daily trend: " + str(exc)[:80])
        return "UNKNOWN"


def fundamental_data(manager, symbol):
    result = {
        "funding": None,
        "open_interest": None,
        "long_short": None,
        "source": None,
    }
    for name in FUNDING_SOURCES:
        exchange = manager.get_exchange(name)
        if not exchange or symbol not in exchange.markets:
            continue
        market = exchange.markets[symbol]
        if not (market.get("swap") or market.get("future")
                or market.get("contract")):
            continue
        try:
            if exchange.has.get("fetchFundingRate"):
                data = exchange.fetch_funding_rate(symbol)
                result["funding"] = safe_float(data.get("fundingRate"))
                result["source"] = name
        except Exception:
            pass
        try:
            if exchange.has.get("fetchOpenInterest"):
                oi = exchange.fetch_open_interest(symbol)
                result["open_interest"] = safe_float(
                    oi.get("openInterestValue")
                    or oi.get("openInterestAmount")
                )
        except Exception:
            pass
        if (result["funding"] is not None
                or result["open_interest"] is not None):
            break
    return result


def fundamental_score(data, side):
    funding = data.get("funding")
    if funding is None:
        return 0
    if side == "BUY" and funding < 0:
        return 1
    if side == "SELL" and funding > 0:
        return 1
    return 0


# ============================================================
# SL / TP ENGINES
# ============================================================
def find_swing_points(df, lookback=80, wing=3):
    recent = df.tail(lookback).reset_index(drop=True)
    highs = []
    lows = []
    for i in range(wing, len(recent) - wing):
        h = safe_float(recent["high"].iloc[i])
        l = safe_float(recent["low"].iloc[i])
        if h is None or l is None:
            continue
        is_high = True
        is_low = True
        for j in range(1, wing + 1):
            if (recent["high"].iloc[i] <= recent["high"].iloc[i - j]
                    or recent["high"].iloc[i]
                    <= recent["high"].iloc[i + j]):
                is_high = False
            if (recent["low"].iloc[i] >= recent["low"].iloc[i - j]
                    or recent["low"].iloc[i]
                    >= recent["low"].iloc[i + j]):
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            highs.append(h)
        if is_low:
            lows.append(l)
    return highs, lows


def find_tp_levels(side, entry, risk, atr, df):
    highs, lows = find_swing_points(df, lookback=80, wing=3)

    if side == "BUY":
        levels = sorted([h for h in highs if h > entry * 1.002])
    else:
        levels = sorted(
            [l for l in lows if l < entry * 0.998],
            reverse=True
        )

    tps = []
    methods = []
    last_tp = entry

    for i in range(3):
        min_m = TP_MIN_MULTS[i]
        max_m = TP_MAX_MULTS[i]

        if side == "BUY":
            lo_bound = max(entry + risk * min_m, last_tp * 1.002)
            hi_bound = entry + risk * max_m
        else:
            lo_bound = entry - risk * max_m
            hi_bound = min(entry - risk * min_m, last_tp * 0.998)

        chosen = None
        for lv in levels:
            if side == "BUY" and lo_bound <= lv <= hi_bound:
                chosen = lv
                break
            if side == "SELL" and lo_bound <= lv <= hi_bound:
                chosen = lv
                break

        if chosen is not None:
            tps.append(chosen)
            methods.append("structure")
            if side == "BUY":
                levels = [lv for lv in levels if lv > chosen * 1.002]
            else:
                levels = [lv for lv in levels if lv < chosen * 0.998]
            last_tp = chosen
        else:
            if side == "BUY":
                tps.append(lo_bound)
            else:
                tps.append(hi_bound)
            methods.append("atr")
            last_tp = tps[-1]

    return tps[0], tps[1], tps[2], methods


def compute_sl_candidates(side, entry, atr, df, support, resistance):
    candidates = {}
    if not entry or not atr or atr <= 0:
        return candidates

    if len(df) >= 20:
        window = df.tail(20)
        if side == "BUY":
            swing = safe_float(window["low"].min())
            if swing and swing > 0:
                candidates["structure"] = swing - atr * 0.3
        else:
            swing = safe_float(window["high"].max())
            if swing and swing > 0:
                candidates["structure"] = swing + atr * 0.3

    if side == "BUY" and support:
        candidates["sr"] = support - atr * 0.3
    elif side == "SELL" and resistance:
        candidates["sr"] = resistance + atr * 0.3

    if side == "BUY":
        candidates["atr"] = entry - atr * 1.8
    else:
        candidates["atr"] = entry + atr * 1.8

    if len(df) >= 20:
        std = safe_float(df["close"].tail(20).std())
        if std and std > 0:
            if side == "BUY":
                candidates["volatility"] = entry - std * 2.5
            else:
                candidates["volatility"] = entry + std * 2.5

    cleaned = {}
    for name, price in candidates.items():
        p = safe_float(price)
        if p is None or p <= 0:
            continue
        if side == "BUY" and p >= entry:
            continue
        if side == "SELL" and p <= entry:
            continue
        cleaned[name] = p
    return cleaned


def score_sl_candidates(side, entry, atr, candidates, df):
    scored = []
    atr_dist_pct = (atr / entry) * 100 if entry > 0 else 0
    base_scores = {
        "structure": 3, "sr": 3,
        "atr": 2, "volatility": 1,
    }

    for name, sl_price in candidates.items():
        dist_pct = abs(entry - sl_price) / entry * 100
        if dist_pct < SL_MIN_DIST_PCT or dist_pct > SL_MAX_DIST_PCT:
            continue
        score = base_scores.get(name, 0)
        if atr_dist_pct > 0 and dist_pct < atr_dist_pct * 0.8:
            score -= 2
        if len(df) >= 50:
            last = df.iloc[-1]
            if side == "BUY" and last["close"] > last["ema50"]:
                score += 1
            elif side == "SELL" and last["close"] < last["ema50"]:
                score += 1
        if dist_pct > SL_MAX_DIST_PCT * 0.7:
            score -= 2
        scored.append((name, sl_price, dist_pct, score))

    scored.sort(key=lambda x: (x[3], -x[2]), reverse=True)
    return scored


def pick_best_sl(side, entry, atr, candidates, df):
    scored = score_sl_candidates(side, entry, atr, candidates, df)
    if not scored:
        return None, None, None
    best = scored[0]
    return best[0], best[1], {
        "score": best[3],
        "dist_pct": round(best[2], 3),
        "all_scored": [
            {
                "method": n,
                "price": round(p, 8),
                "dist_pct": round(d, 3),
                "score": s,
            }
            for (n, p, d, s) in scored
        ],
    }


def calculate_levels(side, entry, atr, sr_support, sr_resistance, df):
    entry = safe_float(entry)
    atr = safe_float(atr)
    if not entry or not atr or atr <= 0:
        return None

    sl_candidates = compute_sl_candidates(
        side, entry, atr, df, sr_support, sr_resistance
    )
    sl_method, sl, sl_meta = pick_best_sl(
        side, entry, atr, sl_candidates, df
    )
    if sl is None:
        STATS["rejected_sl"] += 1
        return None

    risk = abs(entry - sl)
    if risk <= 0:
        STATS["rejected_sl"] += 1
        return None

    tp1, tp2, tp3, tp_methods = find_tp_levels(
        side, entry, risk, atr, df
    )

    structure_count = sum(1 for m in tp_methods if m == "structure")
    if structure_count < MIN_STRUCTURE_TPS:
        STATS["rejected_no_structure"] += 1
        return None

    if side == "BUY":
        if not (tp1 > entry and tp2 > tp1 and tp3 > tp2):
            STATS["rejected_tp_order"] += 1
            return None
    else:
        if not (tp1 < entry and tp2 < tp1 and tp3 < tp2):
            STATS["rejected_tp_order"] += 1
            return None

    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    rr3 = abs(tp3 - entry) / risk

    weighted_rr = (
        TP_WEIGHTS["tp1"] * rr1
        + TP_WEIGHTS["tp2"] * rr2
        + TP_WEIGHTS["tp3"] * rr3
    )

    if weighted_rr < MIN_RR:
        STATS["rejected_rr"] += 1
        return None

    risk_target_usd = CAPITAL_USD * RISK_PCT / 100.0
    max_position_value = CAPITAL_USD * MAX_POSITION_PCT / 100.0

    quantity = risk_target_usd / risk
    position_value = quantity * entry
    capped = False
    actual_risk_usd = risk_target_usd

    if position_value > max_position_value:
        quantity = max_position_value / entry
        position_value = quantity * entry
        actual_risk_usd = quantity * risk
        capped = True

    return {
        "entry": entry,
        "sl": sl,
        "original_sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp_methods": tp_methods,
        "risk_per_unit": risk,
        "rr1": rr1,
        "rr2": rr2,
        "rr3": rr3,
        "weighted_rr": weighted_rr,
        "sl_method": sl_method,
        "sl_candidates": {
            k: round(v, 8) for k, v in sl_candidates.items()
        },
        "sl_meta": sl_meta,
        "quantity": quantity,
        "position_value": position_value,
        "position_capped": capped,
        "risk_target_usd": risk_target_usd,
        "actual_risk_usd": actual_risk_usd,
    }


# ============================================================
# SIGNAL BUILDING
# ============================================================
def try_build_signal(symbol, exchange_name, df_4h, manager, exchange, state):
    if len(df_4h) < 205:
        return None

    df = add_indicators(df_4h)
    bull_4h, bear_4h, components = compute_4h_score(df)
    max_4h = max(bull_4h, bear_4h)

    if max_4h < QUICK_THRESHOLD:
        STATS["skipped_quick"] += 1
        return None

    trend = daily_trend(manager, exchange, symbol)
    STATS["daily_fetched"] += 1

    bull_total = bull_4h
    bear_total = bear_4h
    if trend == "BULL":
        bull_total += 2
    elif trend == "BEAR":
        bear_total += 2

    fundamentals = fundamental_data(manager, symbol)
    bull_total += fundamental_score(fundamentals, "BUY")
    bear_total += fundamental_score(fundamentals, "SELL")

    if bull_total >= bear_total:
        side = "BUY"
        score = bull_total
    else:
        side = "SELL"
        score = bear_total

    if score < MIN_SCORE:
        return None

    last = df.iloc[-1]
    close = safe_float(last["close"])
    atr = safe_float(last["atr"])
    recent = df.tail(60)
    support = safe_float(recent["low"].quantile(0.10))
    resistance = safe_float(recent["high"].quantile(0.90))

    levels = calculate_levels(
        side, close, atr, support, resistance, df
    )
    if not levels:
        return None

    risk_pct_of_entry = (
        abs(levels["entry"] - levels["sl"])
        / levels["entry"] * 100
    )

    if duplicate_exists(state, symbol, side):
        return None

    if score >= 18:
        strength = "قوی"
    elif score >= 15:
        strength = "متوسط"
    else:
        strength = "ضعیف"

    STATS["signal_created"] += 1
    now_iso = iso_now()

    return {
        "id": (symbol.replace("/", "_") + "_" + side
               + "_" + str(int(time.time()))),
        "symbol": symbol,
        "exchange": exchange_name,
        "side": side,
        "status": "ACTIVE",
        "strength": strength,
        "score": int(score),
        "created_at": now_iso,
        "updated_at": now_iso,
        "expiry_at": (
            utc_now() + timedelta(hours=SIGNAL_EXPIRY_HOURS)
        ).isoformat(),
        "entry": levels["entry"],
        "sl": levels["sl"],
        "original_sl": levels["original_sl"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp3": levels["tp3"],
        "tp_methods": levels["tp_methods"],
        "rr": levels["weighted_rr"],
        "rr1": levels["rr1"],
        "rr2": levels["rr2"],
        "rr3": levels["rr3"],
        "risk_pct": risk_pct_of_entry,
        "risk_per_unit": levels["risk_per_unit"],
        "sl_method": levels["sl_method"],
        "sl_candidates": levels["sl_candidates"],
        "sl_meta": levels.get("sl_meta"),
        "quantity": levels["quantity"],
        "position_value": levels["position_value"],
        "actual_risk_usd": levels["actual_risk_usd"],
        "risk_target_usd": levels["risk_target_usd"],
        "position_capped": levels["position_capped"],
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
        "event_history": [
            {"event": "ENTRY", "price": levels["entry"], "time": now_iso}
        ],
    }


def duplicate_exists(state, symbol, side):
    cutoff = utc_now() - timedelta(hours=DUPLICATE_COOLDOWN_HOURS)
    for sig in state.get("signals", []):
        if sig.get("symbol") != symbol or sig.get("side") != side:
            continue
        if sig.get("status") == "ACTIVE":
            return True
        ts_str = sig.get("updated_at") or sig.get("created_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts > cutoff:
                return True
        except Exception:
            continue
    return False


# ============================================================
# EVENTS / POSITION MANAGEMENT
# ============================================================
def add_event(signal, event_name, price=None, extra=None):
    if ("event_history" not in signal
            or not isinstance(signal["event_history"], list)):
        signal["event_history"] = []
    ev = {"event": event_name, "time": iso_now()}
    if price is not None:
        ev["price"] = price
    if extra:
        ev.update(extra)
    signal["event_history"].append(ev)


def close_piece(signal, exit_price, fraction, label):
    entry = safe_float(signal.get("entry"))
    exit_price = safe_float(exit_price)
    if (entry is None or exit_price is None
            or entry <= 0 or exit_price <= 0):
        print("[WARN] close_piece bad entry/exit "
              + str(signal.get("symbol")))
        signal[label + "_hit"] = True
        signal["updated_at"] = iso_now()
        return

    stored_qty = safe_float(signal.get("quantity"))
    if stored_qty is not None and stored_qty > 0:
        quantity = stored_qty
    else:
        risk_per_unit = safe_float(signal.get("risk_per_unit"))
        if risk_per_unit is None or risk_per_unit <= 0:
            original_sl = safe_float(signal.get("original_sl"))
            if (original_sl and original_sl > 0
                    and abs(entry - original_sl) > 1e-12):
                risk_per_unit = abs(entry - original_sl)
        if risk_per_unit is None or risk_per_unit <= 0:
            print("[ERROR] close_piece no risk "
                  + str(signal.get("symbol")))
            signal["remaining_fraction"] = 0
            signal[label + "_hit"] = True
            signal["status"] = "CLOSED"
            signal["closed_reason"] = (
                label.upper() + "_NO_RISK_DATA"
            )
            signal["closed_at"] = iso_now()
            signal["updated_at"] = iso_now()
            add_event(signal, label.upper() + "_NO_RISK_DATA")
            return
        quantity = (CAPITAL_USD * RISK_PCT / 100.0) / risk_per_unit

    piece_qty = quantity * fraction
    if signal["side"] == "BUY":
        gross_piece = (exit_price - entry) * piece_qty
    else:
        gross_piece = (entry - exit_price) * piece_qty

    entry_fee = entry * piece_qty * FEE_RATE
    exit_fee = exit_price * piece_qty * FEE_RATE
    fees = entry_fee + exit_fee
    net = gross_piece - fees

    signal["realized_pnl"] = (
        safe_float(signal.get("realized_pnl"), 0) + net
    )
    signal["fees_paid"] = (
        safe_float(signal.get("fees_paid"), 0) + fees
    )
    signal["remaining_fraction"] = max(
        0,
        safe_float(signal.get("remaining_fraction"), 1) - fraction,
    )
    signal[label + "_hit"] = True
    signal["updated_at"] = iso_now()

    add_event(signal, label.upper(), price=exit_price, extra={
        "fraction": fraction,
        "pnl_piece": round(net, 4),
        "fees": round(fees, 4),
    })


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


def compute_sl_fill_price(signal, current_price):
    sl = safe_float(signal.get("sl"))
    if sl is None or sl <= 0:
        return current_price
    side = signal["side"]
    if side == "BUY":
        overshoot = (sl - current_price) / sl
    else:
        overshoot = (current_price - sl) / sl
    if overshoot <= SL_FILL_SLIPPAGE:
        return current_price
    if side == "BUY":
        return sl * (1 - SL_FILL_SLIPPAGE)
    return sl * (1 + SL_FILL_SLIPPAGE)


def manage_signal(signal, current_price):
    if signal.get("status") != "ACTIVE":
        return False
    changed = False

    created = signal.get("created_at")
    if created:
        try:
            created_dt = datetime.fromisoformat(created)
            if (utc_now() - created_dt
                    >= timedelta(hours=SIGNAL_EXPIRY_HOURS)):
                remaining = safe_float(
                    signal.get("remaining_fraction"), 0
                )
                if remaining > 0:
                    close_piece(
                        signal, current_price, remaining, "expiry"
                    )
                if signal.get("status") != "CLOSED":
                    signal["status"] = "EXPIRED"
                    signal["closed_reason"] = "48h expiry"
                signal["closed_at"] = iso_now()
                signal["updated_at"] = iso_now()
                return True
        except Exception:
            pass

    if stop_hit(signal, current_price):
        remaining = safe_float(signal.get("remaining_fraction"), 0)
        if remaining > 0:
            sl_fill_price = compute_sl_fill_price(
                signal, current_price
            )
            close_piece(signal, sl_fill_price, remaining, "sl")
        if signal.get("status") != "CLOSED":
            signal["status"] = "CLOSED"
            signal["closed_reason"] = "SL"
        signal["closed_at"] = iso_now()
        signal["updated_at"] = iso_now()
        return True

    if (not signal.get("tp1_hit")
            and price_hit(signal, current_price, "tp1")):
        close_piece(signal, signal["tp1"], TP_WEIGHTS["tp1"], "tp1")
        if signal.get("status") == "CLOSED":
            return True
        old_sl = signal.get("sl")
        signal["sl"] = signal["entry"]
        add_event(
            signal, "SL_MOVED_TO_BE",
            price=signal["entry"],
            extra={"old_sl": old_sl},
        )
        changed = True

    if signal.get("status") == "CLOSED":
        return changed

    if (not signal.get("tp2_hit")
            and price_hit(signal, current_price, "tp2")):
        close_piece(signal, signal["tp2"], TP_WEIGHTS["tp2"], "tp2")
        if signal.get("status") == "CLOSED":
            return True
        changed = True

    if signal.get("status") == "CLOSED":
        return changed

    if (not signal.get("tp3_hit")
            and price_hit(signal, current_price, "tp3")):
        close_piece(signal, signal["tp3"], TP_WEIGHTS["tp3"], "tp3")
        changed = True

    if signal.get("remaining_fraction", 0) <= 0.000001:
        signal["remaining_fraction"] = 0
        signal["status"] = "CLOSED"
        signal["closed_reason"] = "TP3"
        signal["closed_at"] = iso_now()
        signal["updated_at"] = iso_now()
        changed = True

    return changed


def update_open_signals(state, manager):
    changed = False
    for signal in state.get("signals", []):
        if signal.get("status") != "ACTIVE":
            continue
        symbol = signal.get("symbol")
        exchange = manager.get_exchange(signal.get("exchange"))
        if not exchange or symbol not in exchange.markets:
            continue
        price = manager.ticker_price(exchange, symbol)
        if price is None:
            continue
        before = (
            signal.get("status"),
            signal.get("tp1_hit"),
            signal.get("tp2_hit"),
            signal.get("tp3_hit"),
        )
        manage_signal(signal, price)
        after = (
            signal.get("status"),
            signal.get("tp1_hit"),
            signal.get("tp2_hit"),
            signal.get("tp3_hit"),
        )
        if before != after:
            changed = True
    return changed


# ============================================================
# REPORTS
# ============================================================
def categorize_outcome(signal):
    status = signal.get("status")
    closed_reason = signal.get("closed_reason") or ""
    tp1 = signal.get("tp1_hit", False)
    tp2 = signal.get("tp2_hit", False)
    tp3 = signal.get("tp3_hit", False)
    if status == "ACTIVE":
        if tp1 and not tp2:
            return "OPEN_AFTER_TP1"
        if tp2 and not tp3:
            return "OPEN_AFTER_TP2"
        return "OPEN"
    if status == "EXPIRED":
        if tp1 or tp2 or tp3:
            return "EXPIRED_WITH_TP"
        return "EXPIRED_NO_TP"
    if "NO_RISK_DATA" in closed_reason:
        return "NO_RISK_DATA"
    if closed_reason in ("SL", "SL_HIT"):
        if tp2:
            return "TP2_THEN_SL"
        if tp1:
            return "TP1_THEN_SL"
        return "SL_DIRECT"
    if closed_reason == "TP3":
        return "TP3_FULL"
    return "OTHER"


def performance_report(state):
    signals = state.get("signals", [])
    hist = state.get("historical_stats", empty_historical_stats())

    buckets = {
        "OPEN": 0, "OPEN_AFTER_TP1": 0, "OPEN_AFTER_TP2": 0,
        "SL_DIRECT": 0, "TP1_THEN_SL": 0, "TP2_THEN_SL": 0,
        "TP3_FULL": 0, "EXPIRED_NO_TP": 0, "EXPIRED_WITH_TP": 0,
        "NO_RISK_DATA": 0, "OTHER": 0,
    }
    for s in signals:
        cat = categorize_outcome(s)
        buckets[cat] = buckets.get(cat, 0) + 1

    cur_open = (
        buckets["OPEN"]
        + buckets["OPEN_AFTER_TP1"]
        + buckets["OPEN_AFTER_TP2"]
    )
    cur_closed = len(signals) - cur_open
    cur_pnl = sum(
        safe_float(s.get("realized_pnl"), 0) for s in signals
    )
    cur_fees = sum(
        safe_float(s.get("fees_paid"), 0) for s in signals
    )

    hist_closed = hist.get("closed", 0)
    hist_pnl = safe_float(hist.get("net_pnl"), 0)
    hist_fees = safe_float(hist.get("fees"), 0)

    total_closed = cur_closed + hist_closed
    total_pnl = cur_pnl + hist_pnl
    total_fees = cur_fees + hist_fees

    lines = ["📊 <b>Performance</b>"]
    if total_closed == 0 and cur_open == 0:
        lines.append("هنوز سیگنالی ثبت نشده.")
        return "\n".join(lines)

    lines.append("باز: " + str(cur_open))
    lines.append("بسته‌شده (اخیر): " + str(cur_closed))
    if hist_closed > 0:
        lines.append("بسته‌شده (تاریخی): " + str(hist_closed))
    lines.append("")

    tp3 = buckets["TP3_FULL"] + hist.get("tp3", 0)
    tp2sl = buckets["TP2_THEN_SL"] + hist.get("tp2_then_sl", 0)
    tp1sl = buckets["TP1_THEN_SL"] + hist.get("tp1_then_sl", 0)
    sld = buckets["SL_DIRECT"] + hist.get("sl_direct", 0)
    exp = (
        buckets["EXPIRED_NO_TP"]
        + buckets["EXPIRED_WITH_TP"]
        + hist.get("expired", 0)
    )

    lines.append("<b>مسیر نتایج:</b>")
    lines.append("❌ SL مستقیم: " + str(sld))
    lines.append("🟡 TP1 → SL/BE: " + str(tp1sl))
    lines.append("🟠 TP2 → SL/BE: " + str(tp2sl))
    lines.append("🏆 TP3 کامل: " + str(tp3))
    lines.append("⏰ Expiry: " + str(exp))
    lines.append("")
    lines.append("💰 Net PnL: $" + str(round(total_pnl, 2)))
    lines.append("💸 Fees: $" + str(round(total_fees, 2)))
    return "\n".join(lines)


def sl_method_report(state):
    signals = state.get("signals", [])
    hist = state.get("historical_stats", empty_historical_stats())

    groups = {}
    for s in signals:
        if s.get("status") == "ACTIVE":
            continue
        method = s.get("sl_method") or "unknown"
        groups.setdefault(method, []).append(s)

    hist_methods = hist.get("sl_methods", {})
    if not isinstance(hist_methods, dict):
        hist_methods = {}

    if not groups and not hist_methods:
        return None

    lines = ["🧠 <b>SL Method Performance</b>", ""]

    all_methods = set(groups.keys()) | set(hist_methods.keys())
    for method in sorted(all_methods):
        trades = groups.get(method, [])
        h = hist_methods.get(method, {})

        cur_total = len(trades)
        cur_wins = 0
        cur_partials = 0
        cur_losses = 0
        for s in trades:
            cat = categorize_outcome(s)
            if cat == "TP3_FULL":
                cur_wins += 1
            elif cat in (
                "TP1_THEN_SL", "TP2_THEN_SL",
                "OPEN_AFTER_TP1", "OPEN_AFTER_TP2",
            ):
                cur_partials += 1
            elif cat in ("SL_DIRECT", "EXPIRED_NO_TP"):
                cur_losses += 1
        cur_pnl = sum(
            safe_float(s.get("realized_pnl"), 0) for s in trades
        )

        total = cur_total + h.get("total", 0)
        wins = cur_wins + h.get("wins", 0)
        partials = cur_partials + h.get("partials", 0)
        losses = cur_losses + h.get("losses", 0)
        pnl = round(cur_pnl + safe_float(h.get("pnl"), 0), 2)
        wr = (wins / total * 100) if total > 0 else 0

        lines.append(
            "🔹 <b>" + method + "</b>\n"
            + "  کل: " + str(total)
            + " | 🏆 " + str(wins)
            + " | 🟡 " + str(partials)
            + " | ❌ " + str(losses) + "\n"
            + "  Win Rate: " + str(round(wr, 1))
            + "% | PnL: $" + str(round(pnl, 2))
        )
    return "\n".join(lines)


# ============================================================
# SIGNAL FORMATTER
# ============================================================
def format_signal(signal):
    side = signal["side"]
    emoji = "🟢" if side == "BUY" else "🔴"
    f = signal.get("fundamentals", {})
    funding = f.get("funding")
    oi = f.get("open_interest")
    funding_text = (
        str(round(funding * 100, 4)) + "%"
        if funding is not None else "N/A"
    )
    oi_text = (
        format(oi, ",.2f") if oi is not None else "N/A"
    )
    components = signal.get("components", {})
    sl_method = signal.get("sl_method", "?")
    qty = safe_float(signal.get("quantity"), 0)
    pos_val = safe_float(signal.get("position_value"), 0)
    capped = signal.get("position_capped", False)
    actual_risk = safe_float(signal.get("actual_risk_usd"), 0)
    target_risk = safe_float(signal.get("risk_target_usd"), 0)
    tp_methods = signal.get("tp_methods", [])
    cap_note = " ⚠️ capped" if capped else ""

    tp_str = ""
    for i, m in enumerate(tp_methods[:3], 1):
        src = "S" if m == "structure" else "A"
        tp_str += "TP" + str(i) + ":" + src + " "

    return (
        emoji + " <b>" + signal["symbol"] + " — " + side + "</b>\n"
        + "صرافی: " + signal["exchange"] + "\n"
        + "قدرت: <b>" + signal["strength"]
        + "</b> | Score: <b>" + str(signal["score"]) + "</b>\n"
        + "روند روزانه: " + signal["trend"] + "\n"
        + "SL Method: <b>" + sl_method + "</b>\n"
        + "TP Sources: " + tp_str + "\n\n"
        + "🎯 Entry: <b>" + fmt_price(signal["entry"]) + "</b>\n"
        + "🛑 SL: <b>" + fmt_price(signal["sl"]) + "</b> ("
        + str(round(signal["risk_pct"], 2)) + "%)\n"
        + "🥇 TP1: <b>" + fmt_price(signal["tp1"]) + "</b> — 50%\n"
        + "🥈 TP2: <b>" + fmt_price(signal["tp2"]) + "</b> — 30%\n"
        + "🥉 TP3: <b>" + fmt_price(signal["tp3"]) + "</b> — 20%\n"
        + "RR وزنی: <b>" + str(round(signal["rr"], 2)) + "</b>\n\n"
        + "📐 EMA: " + str(components.get("ema", 0))
        + " | RSI: " + str(components.get("rsi", 0))
        + " | BB: " + str(components.get("bb", 0)) + "\n"
        + "Ichi: " + str(components.get("ichi", 0))
        + " | Fib: " + str(components.get("fib", 0))
        + " | S/R: " + str(components.get("sr", 0)) + "\n"
        + "Brooks: " + str(components.get("brooks", 0))
        + " | RTM: " + str(components.get("rtm", 0)) + "\n\n"
        + "💰 Funding: " + funding_text + " | OI: " + oi_text + "\n"
        + "━━━━━━━━━━━━━━━\n"
        + "💼 حجم: <b>" + str(round(qty, 6))
        + "</b> واحد" + cap_note + "\n"
        + "💵 ارزش پوزیشن: $" + str(round(pos_val, 2)) + "\n"
        + "🎯 ریسک هدف: $" + str(round(target_risk, 2)) + "\n"
        + "⚠️ ریسک واقعی: $" + str(round(actual_risk, 2)) + "\n"
        + "⏱ Expiry: 48h"
    )


# ============================================================
# EVENTS BROADCAST
# ============================================================
def send_new_events_only(state, users):
    last_report = state.get("last_event_report")
    cutoff_iso = (
        last_report if last_report
        else (utc_now() - timedelta(hours=24)).isoformat()
    )

    new_events = []
    for signal in state.get("signals", []):
        for ev in signal.get("event_history", []):
            ev_time = ev.get("time")
            if not ev_time or ev_time <= cutoff_iso:
                continue
            if ev.get("event") == "ENTRY":
                continue
            new_events.append((ev_time, signal, ev))

    new_events.sort(key=lambda x: x[0])
    if not new_events:
        state["last_event_report"] = iso_now()
        return 0

    emoji_map = {
        "TP1": "🥇", "TP2": "🥈", "TP3": "🏆",
        "SL": "❌", "EXPIRY": "⏰", "SL_MOVED_TO_BE": "🛡",
    }
    title_map = {
        "TP1": "TP1 خورد — ۵۰٪ بسته شد",
        "TP2": "TP2 خورد — ۳۰٪ بسته شد",
        "TP3": "TP3 کامل — معامله بسته شد",
        "SL": "استاپ خورد",
        "EXPIRY": "منقضی شد",
        "SL_MOVED_TO_BE": "SL → Breakeven",
    }

    blocks = ["📋 <b>آپدیت پوزیشن‌ها</b>", "━" * 15]

    for (_, signal, ev) in new_events[-15:]:
        event_name = ev.get("event", "?")
        emoji = emoji_map.get(event_name, "🔔")
        title = title_map.get(event_name, event_name)
        side_emoji = (
            "🟢" if signal.get("side") == "BUY" else "🔴"
        )
        block = [
            emoji + " " + side_emoji + " <b>"
            + str(signal.get("symbol", "?")) + "</b>",
            title,
        ]
        if ev.get("price") is not None:
            block.append(
                "قیمت: <b>" + fmt_price(ev["price"]) + "</b>"
            )
        if event_name in ("TP1", "TP2", "TP3"):
            pp = ev.get("pnl_piece")
            if pp is not None:
                block.append(
                    "PnL بخش: $" + str(round(pp, 2))
                )
        if event_name in ("TP1", "TP2", "TP3", "SL"):
            total = safe_float(signal.get("realized_pnl"), 0)
            block.append(
                "PnL تجمعی: $" + str(round(total, 2))
            )
        blocks.append("\n".join(block))
        blocks.append("────────────")

    blocks.append(
        "📊 <b>" + str(len(new_events)) + " رخداد جدید</b>"
    )
    full_msg = "\n".join(blocks)

    sent_count = telegram_broadcast(users, full_msg)
    if sent_count > 0:
        state["last_event_report"] = iso_now()
    return len(new_events) if sent_count > 0 else 0


# ============================================================
# MARKET SCANNER
# ============================================================
def scan_market(manager, state):
    candidates = manager.scan_symbols()
    if not candidates:
        return []
    signals = []
    for preferred_exchange_name, symbol in candidates:
        exchange_names = [preferred_exchange_name]
        for fallback_name in EXCHANGE_NAMES:
            if fallback_name not in exchange_names:
                fb = manager.get_exchange(fallback_name)
                if fb and symbol in fb.markets:
                    exchange_names.append(fallback_name)
        for exchange_name in exchange_names:
            exchange = manager.get_exchange(exchange_name)
            if not exchange or manager.is_disabled(exchange):
                continue
            try:
                raw = manager.fetch_ohlcv(exchange, symbol)
                df_4h = closed_candles(raw)
                if len(df_4h) < 210:
                    break
                STATS["evaluated"] += 1
                signal = try_build_signal(
                    symbol, exchange_name, df_4h,
                    manager, exchange, state,
                )
                if signal:
                    signals.append(signal)
                    print(
                        "[SIGNAL] " + symbol + " " + signal["side"]
                        + " score=" + str(signal["score"])
                        + " sl=" + str(signal["sl_method"])
                        + " tps=" + str(signal.get("tp_methods"))
                        + " rr=" + str(round(signal["rr"], 2))
                    )
                    break
                break
            except Exception as exc:
                print("[SCAN FAIL] " + symbol + ": "
                      + str(exc)[:80])
                continue
        time.sleep(SLEEP_BETWEEN_SYMBOLS)
    signals.sort(key=lambda x: (x["score"], x["rr"]), reverse=True)
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
    users = load_users()

    if ADMIN_CHAT_ID:
        ensure_user(users, ADMIN_CHAT_ID, "Admin")

    manager = ExchangeManager()
    if not manager.build():
        if ADMIN_CHAT_ID:
            telegram_send_to(
                ADMIN_CHAT_ID,
                "❌ <b>Crypto Signal Bot</b>\n"
                "هیچ صرافی قابل دسترسی نبود."
            )
        return

    try:
        poll_and_handle_commands(users)
        save_users(users)
    except Exception as e:
        print("[WARN] poll: " + str(e)[:100])

    migrated = migrate_old_signals(state)
    if migrated > 0:
        print("[RETENTION] migrated " + str(migrated))

    if update_open_signals(state, manager):
        save_state(state)

    n_events = send_new_events_only(state, users)
    print("[EVENTS] " + str(n_events))
    save_state(state)

    if ADMIN_CHAT_ID:
        telegram_send_to(
            ADMIN_CHAT_ID,
            "🔎 <b>در حال جستجوی سیگنال...</b>\n"
            "بازار در حال بررسی است، لطفاً منتظر بمانید."
        )

    new_signals = scan_market(manager, state)
    state["signals"].extend(new_signals)
    cap_signals(state)
    state["last_run"] = iso_now()
    save_state(state)

    if new_signals:
        header = (
            "🚨 <b>" + str(len(new_signals)) + " سیگنال جدید</b>\n"
            + iso_now()
        )
        telegram_broadcast(users, header)
        for signal in new_signals[:10]:
            telegram_broadcast(users, format_signal(signal))
            time.sleep(0.3)
    else:
        if ADMIN_CHAT_ID:
            telegram_send_to(
                ADMIN_CHAT_ID,
                "❌ <b>سیگنال جدیدی یافت نشد.</b>\n\n"
                "بازار بررسی شد اما هیچ سیگنالی با شرایط "
                "فعلی پیدا نشد."
            )

    if ADMIN_CHAT_ID:
        closed_count = sum(
            1 for s in state["signals"]
            if s.get("status") in {"CLOSED", "EXPIRED"}
        )
        hist_closed = state.get(
            "historical_stats", {}
        ).get("closed", 0)
        if (closed_count + hist_closed) >= 3:
            telegram_send_to(
                ADMIN_CHAT_ID, performance_report(state)
            )
            sl_rep = sl_method_report(state)
            if sl_rep:
                telegram_send_to(ADMIN_CHAT_ID, sl_rep)

    active = sum(
        1 for s in state["signals"]
        if s.get("status") == "ACTIVE"
    )
    total_users = len(users.get("users", {}))
    active_users = len(get_active_user_ids(users))

    print("=" * 70)
    print("New signals: " + str(len(new_signals)))
    print("New events: " + str(n_events))
    print("Migrated: " + str(migrated))
    print("Active signals: " + str(active))
    print("Total stored signals: " + str(len(state["signals"])))
    print("Total users: " + str(total_users))
    print("Active users: " + str(active_users))
    print("--- stats ---")
    print("Evaluated: " + str(STATS["evaluated"]))
    print("Skipped quick: " + str(STATS["skipped_quick"]))
    print("Daily fetched: " + str(STATS["daily_fetched"]))
    print("Signals created: " + str(STATS["signal_created"]))
    print("Rejected SL: " + str(STATS["rejected_sl"]))
    print("Rejected RR: " + str(STATS["rejected_rr"]))
    print("Rejected no-structure: "
          + str(STATS["rejected_no_structure"]))
    print("Rejected TP order: "
          + str(STATS["rejected_tp_order"]))
    print("Finished:", iso_now())
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        if ADMIN_CHAT_ID:
            telegram_send_to(
                ADMIN_CHAT_ID,
                "❌ <b>Crypto Signal Bot Error</b>\n"
                "اجرای Bot با خطا متوقف شد."
            )
        raise
