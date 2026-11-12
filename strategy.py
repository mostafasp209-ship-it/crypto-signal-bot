"""Deterministic strategy features, gates and structural level calculations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import FEE_RATE, MIN_RR, TP_WEIGHTS
from utils import safe_float


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
