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

from config import (
    ADMIN_CHAT_ID, DAILY_LIMIT, DATA_FILE, EXCHANGE_NAMES, FEE_RATE,
    FUNDING_SOURCES, MAX_POSITION_USD, MIN_RR, NEWS_FEEDS,
    NEWS_LOOKBACK_HOURS, NEWS_TIMEOUT_SECONDS, OHLCV_LIMIT, RISK_USD,
    SIGNAL_EXPIRY_HOURS, SLIPPAGE_RATE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TIMEFRAME, TP_WEIGHTS, TREND_TIMEFRAME, USERS_FILE,
)
from exchange import ExchangeManager
from indicators import add_indicators
from market_data import closed_candles
from notifications import telegram_recipients, telegram_send
from risk import calculate_risk_plan
from strategy import (
    _candle_rejection, _count_h1_h2, _find_breakout_retest, _market_regime,
    _trendline_context, atr, bb_score, brooks_score, calculate_crypto_strategy_gates,
    calculate_levels, calculate_reference_strategy_gates, ema_score, fibonacci_score,
    find_support_resistance, ichimoku_score, rsi, rsi_score, rtm_score, sr_score,
    stochastic,
)
from safety_filters import MAX_RISK_PCT, is_tradable_symbol
from storage import load_state as _load_state, save_state as _save_state
from trade_manager import (
    close_piece, duplicate_exists, duplicate_on_candle_exists, manage_signal,
    price_hit, stop_hit, update_chandelier_stop,
)
from utils import safe_float


# ============================================================
# UTILITIES
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


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
    """Backward-compatible state loader used by the bot and Telegram panel."""
    return _load_state(DATA_FILE)


def save_state(state):
    """Backward-compatible state writer used by the bot and Telegram panel."""
    return _save_state(state, DATA_FILE)


# ============================================================
# TECHNICAL SCORE COMPONENTS
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

def build_signal(symbol, exchange_name, df, next_open=None, trend=None, fundamentals=None, news_context=None):
    """Create a USDT-linear perpetual 4H signal using the next candle open."""
    st = calculate_reference_strategy_gates(df)
    if not st["signal_long"] and not st["signal_short"]:
        return None
    raw_entry, atr_now = safe_float(next_open), safe_float(st["atr"])
    if raw_entry is None or raw_entry <= 0 or atr_now is None or atr_now <= 0:
        print(f"[SIGNAL SKIP] {symbol}: next 4H open unavailable; no close-price fallback")
        return None
    side = "BUY" if st["signal_long"] else "SELL"
    entry = raw_entry * (1+SLIPPAGE_RATE) if side == "BUY" else raw_entry * (1-SLIPPAGE_RATE)
    if not is_tradable_symbol(symbol, entry_price=entry, require_quote_volume=False):
        print(f"[SIGNAL SKIP] {symbol}: entry failed price/symbol safety validation")
        return None
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
    risk_plan = calculate_risk_plan(
        side=side, entry=entry, stop=stop, target=target,
        risk_budget_usd=RISK_USD, max_position_usd=MAX_POSITION_USD,
        fee_rate=FEE_RATE, slippage_rate=SLIPPAGE_RATE,
    )
    if risk_plan is None:
        print(f"[SIGNAL SKIP] {symbol}: invalid risk or execution-cost plan")
        return None
    net_rr = risk_plan["net_rr"]
    if net_rr < MIN_RR:
        print(f"[SIGNAL SKIP] {symbol}: net RR {net_rr:.3f} below MIN_RR={MIN_RR:.2f}")
        return None
    raw_qty = risk_plan["raw_quantity"]
    qty = risk_plan["quantity"]
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
        "risk_target_usd": RISK_USD, "actual_risk_usd": risk_plan["actual_risk_usd"],
        "position_quantity": qty, "quantity": qty, "position_value": risk_plan["position_value_usd"],
        "position_capped": bool(risk_plan["position_capped"]), "signal_candle_timestamp": st["signal_timestamp"],
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
                if next_open is not None and not is_tradable_symbol(symbol, entry_price=next_open, require_quote_volume=False):
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
    if not manager.build(
        required_exchanges=required_exchanges,
        require_scanners=not manage_only,
    ):
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
    parser = argparse.ArgumentParser(description="USDT-margined perpetual crypto signal bot")
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
