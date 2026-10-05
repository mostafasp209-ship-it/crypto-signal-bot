import os
import ccxt
import pandas as pd
import numpy as np
import requests
import time
import json
from datetime import datetime

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SIGNALS_FILE = "signals.json"

def send(msg):
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

def fmt_price(p):
    if p == 0:
        return "0"
    if abs(p) < 0.0001:
        return "{:.1e}".format(p)
    if abs(p) < 1:
        return "{:.6f}".format(p)
    if abs(p) < 100:
        return "{:.4f}".format(p)
    return "{:.2f}".format(p)

# ============================================================
# ذخیره و بارگذاری سیگنال‌ها
# ============================================================
def load_signals():
    try:
        with open(SIGNALS_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_signals(signals):
    with open(SIGNALS_FILE, 'w') as f:
        json.dump(signals, f, indent=2)

def check_signal_status(signal, current_price):
    if signal.get('status') != 'ACTIVE':
        return signal, None
    
    entry = signal['entry']
    sl = signal['sl']
    tp1 = signal['tp1']
    tp2 = signal['tp2']
    tp3 = signal['tp3']
    sig_type = signal['type']
    
    pnl = None
    new_status = None
    
    if sig_type == 'BUY':
        if current_price <= sl:
            new_status = 'SL_HIT'
            pnl = ((sl - entry) / entry) * 100
        elif current_price >= tp3:
            new_status = 'TP3_HIT'
            pnl = ((tp3 - entry) / entry) * 100
        elif current_price >= tp2:
            new_status = 'TP2_HIT'
            pnl = ((tp2 - entry) / entry) * 100
        elif current_price >= tp1:
            new_status = 'TP1_HIT'
            pnl = ((tp1 - entry) / entry) * 100
    else:
        if current_price >= sl:
            new_status = 'SL_HIT'
            pnl = ((entry - sl) / entry) * 100
        elif current_price <= tp3:
            new_status = 'TP3_HIT'
            pnl = ((entry - tp3) / entry) * 100
        elif current_price <= tp2:
            new_status = 'TP2_HIT'
            pnl = ((entry - tp2) / entry) * 100
        elif current_price <= tp1:
            new_status = 'TP1_HIT'
            pnl = ((entry - tp1) / entry) * 100
    
    if new_status:
        signal['status'] = new_status
        signal['pnl'] = round(pnl, 2)
        signal['closed_at'] = datetime.now().isoformat()
    
    return signal, new_status

# ============================================================
# فاندامنتال
# ============================================================
def get_funding_from_binance(symbol):
    try:
        base = symbol.split('/')[0]
        binance_symbol = base + "USDT"
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        params = {'symbol': binance_symbol}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return float(data.get('lastFundingRate', 0)) * 100
    except:
        pass
    return None

def get_open_interest(symbol):
    try:
        base = symbol.split('/')[0]
        binance_symbol = base + "USDT"
        url = "https://fapi.binance.com/fapi/v1/openInterest"
        params = {'symbol': binance_symbol}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return float(data.get('openInterest', 0))
    except:
        pass
    return None

def get_long_short_ratio(symbol):
    try:
        base = symbol.split('/')[0]
        binance_symbol = base + "USDT"
        url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
        params = {'symbol': binance_symbol, 'period': '4h', 'limit': 1}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                return float(data[0].get('longShortRatio', 0))
    except:
        pass
    return None

# ============================================================
# کلاس ربات
# ============================================================
class Bot:
    def __init__(self):
        self.ex = None
        exchange_list = ['binanceus', 'bitstamp', 'bitfinex', 'cryptocom']
        for name in exchange_list:
            try:
                test_ex = getattr(ccxt, name)({'enableRateLimit': True, 'timeout': 30000})
                test_data = test_ex.fetch_ohlcv('BTC/USDT', '4h', 5)
                if test_data and len(test_data) > 0:
                    self.ex = test_ex
                    self.quote = 'USDT'
                    print("صرافی متصل: " + name.upper())
                    break
            except Exception as e:
                print("صرافی " + name + " کار نکرد: " + str(e)[:80])
        if self.ex is None:
            raise Exception("هیچ صرافی‌ای جواب نداد!")
    
    def ema(self, s, l):
        return s.ewm(span=l, adjust=False).mean()
    
    def rsi(self, s, l=14):
        d = s.diff()
        g = d.where(d > 0, 0).ewm(span=l, adjust=False).mean()
        ls = -d.where(d < 0, 0).ewm(span=l, adjust=False).mean()
        return 100 - (100 / (1 + g / ls))
    
    def atr(self, h, lo, c, l=14):
        tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(span=l, adjust=False).mean()
    
    def ichimoku(self, df):
        df = df.copy()
        df['tenkan'] = (df['h'].rolling(9).max() + df['l'].rolling(9).min()) / 2
        df['kijun'] = (df['h'].rolling(26).max() + df['l'].rolling(26).min()) / 2
        df['senkou_a'] = ((df['tenkan'] + df['kijun']) / 2).shift(26)
        df['senkou_b'] = ((df['h'].rolling(52).max() + df['l'].rolling(52).min()) / 2).shift(26)
        df['chikou'] = df['c'].shift(-26)
        return df
    
    def bollinger(self, df, l=20, std=2):
        df = df.copy()
        df['bb_mid'] = df['c'].rolling(l).mean()
        df['bb_std'] = df['c'].rolling(l).std()
        df['bb_upper'] = df['bb_mid'] + std * df['bb_std']
        df['bb_lower'] = df['bb_mid'] - std * df['bb_std']
        df['bb_position'] = (df['c'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])
        return df
    
    def fibonacci(self, df, lookback=50):
        recent = df.tail(lookback)
        high = recent['h'].max()
        low = recent['l'].min()
        diff = high - low
        if diff == 0:
            return None
        return {
            'high': high, 'low': low,
            'level_0': high, 'level_236': high - diff * 0.236,
            'level_382': high - diff * 0.382, 'level_500': high - diff * 0.5,
            'level_618': high - diff * 0.618, 'level_786': high - diff * 0.786,
            'level_100': low
        }
    
    def check_fib_support(self, price, fib, tol=0.02):
        if fib is None:
            return 0
        for lv in [fib['level_382'], fib['level_500'], fib['level_618'], fib['level_786']]:
            if lv > 0 and abs(price - lv) / lv < tol:
                return 3
        return 0
    
    def check_fib_resistance(self, price, fib, tol=0.02):
        if fib is None:
            return 0
        for lv in [fib['level_236'], fib['level_382'], fib['level_500']]:
            if lv > 0 and abs(price - lv) / lv < tol:
                return 3
        return 0
    
    def brooks_buy(self, df):
        last, p1, p2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]
        bull = 1 if last['c'] > last['o'] else 0
        rng = last['h'] - last['l']
        strong = 1 if rng > 0 and (last['c'] - last['l']) / rng > 0.84 else 0
        above = 1 if last['c'] > p1['h'] else 0
        two_bear = 1 if (p1['c'] < p1['o']) and (p2['c'] < p2['o']) else 0
        return bull + strong + above + two_bear
    
    def brooks_sell(self, df):
        last, p1, p2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]
        bear = 1 if last['c'] < last['o'] else 0
        rng = last['h'] - last['l']
        strong = 1 if rng > 0 and (last['h'] - last['c']) / rng > 0.84 else 0
        below = 1 if last['c'] < p1['l'] else 0
        two_bull = 1 if (p1['c'] > p1['o']) and (p2['c'] > p2['o']) else 0
        return bear + strong + below + two_bull
    
    def rtm_buy(self, df):
        last, p1 = df.iloc[-1], df.iloc[-2]
        rlow = df['l'].iloc[-10:].min()
        sweep = 1 if (p1['l'] <= rlow * 1.002) and (p1['c'] > rlow) and (last['c'] > p1['c']) else 0
        shigh = df['h'].iloc[-5:-1].max()
        mss = 1 if last['c'] > shigh else 0
        disc = 1 if last['c'] <= rlow * 1.01 else 0
        hdiv = 1 if (last['l'] > df['l'].iloc[-5]) and (last['rsi'] < df['rsi'].iloc[-5]) else 0
        return sweep + mss + disc + hdiv
    
    def rtm_sell(self, df):
        last, p1 = df.iloc[-1], df.iloc[-2]
        rhigh = df['h'].iloc[-10:].max()
        sweep = 1 if (p1['h'] >= rhigh * 0.998) and (p1['c'] < rhigh) and (last['c'] < p1['c']) else 0
        slow = df['l'].iloc[-5:-1].min()
        mss = 1 if last['c'] < slow else 0
        prem = 1 if last['c'] >= rhigh * 0.99 else 0
        hdiv = 1 if (last['h'] < df['h'].iloc[-5]) and (last['rsi'] > df['rsi'].iloc[-5]) else 0
        return sweep + mss + prem + hdiv
    
    def daily_trend(self, symbol):
        try:
            o = self.ex.fetch_ohlcv(symbol, '1d', 200)
            if not o or len(o) < 200:
                return None
            df = pd.DataFrame(o, columns=['ts','o','h','l','c','v'])
            for col in ['o','h','l','c','v']:
                df[col] = pd.to_numeric(df[col])
            df = self.ichimoku(df)
            last = df.iloc[-1]
            score = 0
            if last['c'] > max(last['senkou_a'], last['senkou_b']): score += 1
            if last['tenkan'] > last['kijun']: score += 1
            if last['senkou_a'] > last['senkou_b']: score += 1
            if len(df) > 27 and last['c'] > df['c'].iloc[-27]: score += 1
            return score
        except:
            return None
    
    def find_sr(self, df, lookback=50):
        recent = df.tail(lookback)
        highs, lows = [], []
        for i in range(2, len(recent) - 2):
            if (recent['h'].iloc[i] > recent['h'].iloc[i-1] and recent['h'].iloc[i] > recent['h'].iloc[i-2]
                and recent['h'].iloc[i] > recent['h'].iloc[i+1] and recent['h'].iloc[i] > recent['h'].iloc[i+2]):
                highs.append(recent['h'].iloc[i])
            if (recent['l'].iloc[i] < recent['l'].iloc[i-1] and recent['l'].iloc[i] < recent['l'].iloc[i-2]
                and recent['l'].iloc[i] < recent['l'].iloc[i+1] and recent['l'].iloc[i] < recent['l'].iloc[i+2]):
                lows.append(recent['l'].iloc[i])
        last_price = df['c'].iloc[-1]
        nearest_res = min([r for r in highs if r > last_price], default=None)
        nearest_sup = max([s for s in lows if s < last_price], default=None)
        return nearest_res, nearest_sup
    
    def analyze(self, symbol):
        try:
            o = self.ex.fetch_ohlcv(symbol, '4h', 300)
            if not o or len(o) < 200:
                return None
            df = pd.DataFrame(o, columns=['ts','o','h','l','c','v'])
            for col in ['o','h','l','c','v']:
                df[col] = pd.to_numeric(df[col])
            df['ema50'] = self.ema(df['c'], 50)
            df['ema200'] = self.ema(df['c'], 200)
            df['rsi'] = self.rsi(df['c'])
            df['atr'] = self.atr(df['h'], df['l'], df['c'])
            df['vma'] = df['v'].rolling(20).mean()
            df = self.ichimoku(df)
            df = self.bollinger(df)
            
            last = df.iloc[-1]
            daily_score = self.daily_trend(symbol)
            if daily_score is None:
                daily_score = 0
            
            brooks_b = self.brooks_buy(df)
            brooks_s = self.brooks_sell(df)
            rtm_b = self.rtm_buy(df)
            rtm_s = self.rtm_sell(df)
            nearest_res, nearest_sup = self.find_sr(df)
            fib = self.fibonacci(df)
            
            ichi_bull = 0
            if last['c'] > max(last['senkou_a'], last['senkou_b']): ichi_bull += 1
            if last['tenkan'] > last['kijun']: ichi_bull += 1
            if last['senkou_a'] > last['senkou_b']: ichi_bull += 1
            
            ichi_bear = 0
            if last['c'] < min(last['senkou_a'], last['senkou_b']): ichi_bear += 1
            if last['tenkan'] < last['kijun']: ichi_bear += 1
            if last['senkou_a'] < last['senkou_b']: ichi_bear += 1
            
            bb_pos = last['bb_position']
            bb_bull = 1 if bb_pos < 0.2 else 0
            bb_bear = 1 if bb_pos > 0.8 else 0
            
            fib_bull = self.check_fib_support(last['c'], fib)
            fib_bear = self.check_fib_resistance(last['c'], fib)
            
            # ===== BUY =====
            buy_filters = 0
            if daily_score >= 2: buy_filters += 1
            if brooks_b >= 2: buy_filters += 1
            if rtm_b >= 1: buy_filters += 1
            if ichi_bull >= 2: buy_filters += 1
            
            buy_bonus = 0
            if bb_bull: buy_bonus += 2
            if fib_bull: buy_bonus += 2
            if nearest_sup:
                dist = ((last['c'] - nearest_sup) / last['c']) * 100
                if dist < 3: buy_bonus += 2
                elif dist < 7: buy_bonus += 1
            
            if (buy_filters >= 3 and last['rsi'] < 45 and last['v'] > last['vma'] * 0.5):
                entry = last['c']
                atr = last['atr']
                recent_low = df['l'].iloc[-10:].min()
                
                ez_low = max(recent_low, entry - 0.5 * atr)
                ez_high = entry + 0.2 * atr
                sl = min(recent_low - 0.5 * atr, entry - 2 * atr)
                tp1 = entry + 1.0 * atr
                tp2 = entry + 2.0 * atr
                tp3 = entry + 3.0 * atr
                total = daily_score + brooks_b + rtm_b + ichi_bull + buy_bonus
                
                return {
                    'sym': symbol, 'type': 'BUY',
                    'entry': entry, 'ez_low': ez_low, 'ez_high': ez_high,
                    'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
                    'rsi': last['rsi'], 'vol': last['v'] / last['vma'],
                    'atr': atr, 'daily': daily_score,
                    'brooks': brooks_b, 'rtm': rtm_b,
                    'ichi': ichi_bull, 'bb': bb_bull, 'fib': fib_bull,
                    'total': total, 'score': buy_filters * 3 + buy_bonus,
                    'status': 'ACTIVE', 'created_at': datetime.now().isoformat(), 'pnl': None
                }
            
            # ===== SELL =====
            sell_filters = 0
            if daily_score <= 1: sell_filters += 1
            if brooks_s >= 2: sell_filters += 1
            if rtm_s >= 1: sell_filters += 1
            if ichi_bear >= 2: sell_filters += 1
            
            sell_bonus = 0
            if bb_bear: sell_bonus += 2
            if fib_bear: sell_bonus += 2
            if nearest_res:
                dist = ((nearest_res - last['c']) / last['c']) * 100
                if dist < 3: sell_bonus += 2
                elif dist < 7: sell_bonus += 1
            
            if (sell_filters >= 3 and last['rsi'] > 55 and last['v'] > last['vma'] * 0.5):
                entry = last['c']
                atr = last['atr']
                recent_high = df['h'].iloc[-10:].max()
                
                ez_low = entry - 0.2 * atr
                ez_high = min(recent_high, entry + 0.5 * atr)
                sl = max(recent_high + 0.5 * atr, entry + 2 * atr)
                tp1 = entry - 1.0 * atr
                tp2 = entry - 2.0 * atr
                tp3 = entry - 3.0 * atr
                total = daily_score + brooks_s + rtm_s + ichi_bear + sell_bonus
                
                return {
                    'sym': symbol, 'type': 'SELL',
                    'entry': entry, 'ez_low': ez_low, 'ez_high': ez_high,
                    'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
                    'rsi': last['rsi'], 'vol': last['v'] / last['vma'],
                    'atr': atr, 'daily': daily_score,
                    'brooks': brooks_s, 'rtm': rtm_s,
                    'ichi': ichi_bear, 'bb': bb_bear, 'fib': fib_bear,
                    'total': total, 'score': sell_filters * 3 + sell_bonus,
                    'status': 'ACTIVE', 'created_at': datetime.now().isoformat(), 'pnl': None
                }
            
            return None
        except:
            return None


def format_signal(s):
    emoji = "🟢" if s['type'] == 'BUY' else "🔴"
    name = "خرید" if s['type'] == 'BUY' else "فروش"
    
    if s['total'] >= 12:
        strength = "🔥🔥🔥 قوی"
    elif s['total'] >= 8:
        strength = "🔥🔥 متوسط"
    else:
        strength = "🔥 ضعیف"
    
    entry = s['entry']
    sl = s['sl']
    risk = abs(entry - sl)
    
    sl_pct = ((sl - entry) / entry) * 100
    tp1_pct = ((s['tp1'] - entry) / entry) * 100
    tp2_pct = ((s['tp2'] - entry) / entry) * 100
    tp3_pct = ((s['tp3'] - entry) / entry) * 100
    
    rr1 = abs(s['tp1'] - entry) / risk if risk > 0 else 0
    rr2 = abs(s['tp2'] - entry) / risk if risk > 0 else 0
    rr3 = abs(s['tp3'] - entry) / risk if risk > 0 else 0
    size = 10 / risk if risk > 0 else 0
    
    funding = get_funding_from_binance(s['sym'])
    oi = get_open_interest(s['sym'])
    ls_ratio = get_long_short_ratio(s['sym'])
    
    if funding is not None:
        if funding < -0.01:
            funding_txt = str(round(funding, 4)) + "% 🟢 فرصت خرید"
        elif funding > 0.05:
            funding_txt = str(round(funding, 4)) + "% 🔴 ریسک"
        else:
            funding_txt = str(round(funding, 4)) + "% ⚪ نرمال"
    else:
        funding_txt = "نامشخص"
    
    oi_txt = "{:,.0f} واحد".format(oi) if oi is not None else "نامشخص"
    
    if ls_ratio is not None:
        if ls_ratio > 2.0:
            ls_txt = str(round(ls_ratio, 2)) + " 🔴 لانگ زیاد"
        elif ls_ratio > 1.5:
            ls_txt = str(round(ls_ratio, 2)) + " ⚠️ لانگ متوسط"
        else:
            ls_txt = str(round(ls_ratio, 2)) + " ✅ متعادل"
    else:
        ls_txt = "نامشخص"
    
    msg = emoji + " <b>سیگنال " + name + ": " + s['sym'] + "</b>\n"
    msg += "⚡ قدرت: " + strength + " (" + str(s['total']) + ")\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "📍 <b>محدوده ورود:</b>\n"
    msg += fmt_price(s['ez_low']) + " تا " + fmt_price(s['ez_high']) + "\n"
    msg += "🎯 بهینه: " + fmt_price(entry) + "\n\n"
    msg += "🛑 <b>حد ضرر:</b> " + fmt_price(sl) + " (" + str(round(sl_pct, 2)) + "%)\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "🎯 <b>اهداف سود:</b>\n\n"
    msg += "🥇 TP1: " + fmt_price(s['tp1']) + " (" + str(round(tp1_pct, 2)) + "%) | R/R: 1:" + str(round(rr1, 2)) + "\n"
    msg += "   ↳ بستن ۵۰٪\n\n"
    msg += "🥈 TP2: " + fmt_price(s['tp2']) + " (" + str(round(tp2_pct, 2)) + "%) | R/R: 1:" + str(round(rr2, 2)) + "\n"
    msg += "   ↳ بستن ۳۰٪\n\n"
    msg += "🥉 TP3: " + fmt_price(s['tp3']) + " (" + str(round(tp3_pct, 2)) + "%) | R/R: 1:" + str(round(rr3, 2)) + "\n"
    msg += "   ↳ بستن ۲۰٪\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "📊 <b>تکنیکال:</b>\n"
    msg += "• Daily: " + str(s['daily']) + "/4\n"
    msg += "• Brooks: " + str(s['brooks']) + "/4\n"
    msg += "• RTM: " + str(s['rtm']) + "/4\n"
    msg += "• Ichimoku: " + str(s['ichi']) + "/3\n"
    msg += "• Bollinger: " + ("✅" if s['bb'] else "❌") + "\n"
    msg += "• Fibonacci: " + ("✅" if s['fib'] else "❌") + "\n"
    msg += "• RSI: " + str(round(s['rsi'], 1)) + " | حجم: " + str(round(s['vol'], 2)) + "x\n"
    msg += "• ATR: " + fmt_price(s['atr']) + "\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "📈 <b>فاندامنتال:</b>\n"
    msg += "• فاندینگ: " + funding_txt + "\n"
    msg += "• Open Interest: " + oi_txt + "\n"
    msg += "• نسبت L/S: " + ls_txt + "\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "💡 <b>مدیریت سرمایه (با ۱۰۰۰ دلار):</b>\n"
    msg += "• ریسک ۱٪ = ۱۰ دلار\n"
    msg += "• حجم: " + str(round(size, 4)) + " واحد\n\n"
    msg += "⏰ اعتبار: ۴۸ ساعت"
    
    return msg


# ============================================================
# اجرا
# ============================================================
print("شروع اسکن...")
bot = Bot()

# ===== بررسی سیگنال‌های قبلی =====
old_signals = load_signals()
print("سیگنال‌های قبلی: " + str(len(old_signals)))

active_count = 0
for old_sig in old_signals:
    if old_sig.get('status') == 'ACTIVE':
        active_count += 1
        try:
            ticker = bot.ex.fetch_ticker(old_sig['sym'])
            current_price = ticker['last']
            updated, new_status = check_signal_status(old_sig, current_price)
            old_sig.clear()
            old_sig.update(updated)
            if new_status:
                emoji = "✅" if 'TP' in new_status else "❌"
                msg = emoji + " <b>" + old_sig['sym'] + "</b>\n"
                msg += "وضعیت جدید: <b>" + new_status + "</b>\n"
                msg += "سود/ضرر: " + str(old_sig['pnl']) + "%\n"
                msg += "قیمت فعلی: " + fmt_price(current_price)
                send(msg)
                time.sleep(1)
        except:
            pass

print("سیگنال‌های فعال: " + str(active_count))

# ===== اسکن بازار =====
markets = bot.ex.load_markets()
symbols = [s for s, m in markets.items() if m['quote'] == bot.quote and m['spot'] and m['active']]
print("تعداد کل ارزها: " + str(len(symbols)))

MAX_COINS = 500
signals = []
for i, sym in enumerate(symbols[:MAX_COINS]):
    sig = bot.analyze(sym)
    if sig:
        signals.append(sig)
        print("سیگنال: " + sym + " (" + sig['type'] + ")")
    if (i + 1) % 100 == 0:
        print("پیشرفت: " + str(i + 1) + "/" + str(min(MAX_COINS, len(symbols))) + " | سیگنال: " + str(len(signals)))
    time.sleep(0.05)

print("اسکن کامل شد. سیگنال: " + str(len(signals)))

# ===== ذخیره سیگنال‌های جدید =====
all_signals = old_signals + signals
save_signals(all_signals)
print("سیگنال‌های ذخیره شده: " + str(len(all_signals)))

# ===== ارسال =====
if signals:
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    summary = "<b>سیگنال های جدید</b>\n"
    summary += "تعداد: " + str(len(signals)) + "\n\n"
    for i, s in enumerate(signals_sorted[:20], 1):
        emoji = "🟢" if s['type'] == 'BUY' else "🔴"
        summary += str(i) + ". " + emoji + " " + s['sym'] + " (" + s['type'] + ")\n"
    send(summary)
    time.sleep(2)
    
    for s in signals_sorted[:10]:
        msg = format_signal(s)
        send(msg)
        time.sleep(2)
    
    print("ارسال شد: " + str(len(signals)) + " سیگنال")
else:
    print("سیگنال جدیدی نیست")

# ===== گزارش عملکرد =====
closed = [s for s in all_signals if s.get('status') in ['TP1_HIT', 'TP2_HIT', 'TP3_HIT', 'SL_HIT']]
if closed:
    wins = [s for s in closed if 'TP' in s.get('status', '')]
    losses = [s for s in closed if s.get('status') == 'SL_HIT']
    total = len(closed)
    win_rate = (len(wins) / total * 100) if total > 0 else 0
    
    report = "📊 <b>گزارش عملکرد</b>\n\n"
    report += "• کل معاملات بسته شده: " + str(total) + "\n"
    report += "• برد: " + str(len(wins)) + "\n"
    report += "• باخت: " + str(len(losses)) + "\n"
    report += "• وین ریت: " + str(round(win_rate, 1)) + "%\n"
    send(report)
    print("گزارش ارسال شد. وین ریت: " + str(round(win_rate, 1)) + "%")

print("Done!")
