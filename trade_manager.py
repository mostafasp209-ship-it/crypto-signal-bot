"""Deterministic signal lifecycle and simulated position accounting."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from config import FEE_RATE, RISK_USD, SIGNAL_EXPIRY_HOURS, SLIPPAGE_RATE, TP_WEIGHTS
from utils import safe_float


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


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
