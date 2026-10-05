import os
import ccxt
import pandas as pd
import numpy as np
import requests
import time

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send(msg):
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

class Bot:
    def __init__(self):
        # اول Binance، اگه نشد OKX
        self.ex = None
        for name in ['binance', 'okx', 'bybit']:
            try:
                test_ex = getattr(ccxt, name)({'enableRateLimit': True, 'timeout': 30000})
                test_data = test_ex.fetch_ohlcv('BTC/USDT', '4h', 5)
                if test_data and len(test_data) > 0:
                    self.ex = test_ex
                    print("صرافی متصل: " + name.upper())
                    break
            except Exception as e:
                print("صرافی " + name + " کار نکرد: " + str(e)[:100])
        
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
    
    def analyze(self, symbol):
        try:
            o = self.ex.fetch_ohlcv(symbol, '4h', 300)
            if not o or len(o) < 200:
                return None
            
            df = pd.DataFrame(o, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
            for col in ['o', 'h', 'l', 'c', 'v']:
                df[col] = pd.to_numeric(df[col])
            
            df['ema50'] = self.ema(df['c'], 50)
            df['ema200'] = self.ema(df['c'], 200)
            df['rsi'] = self.rsi(df['c'])
            df['atr'] = self.atr(df['h'], df['l'], df['c'])
            df['vma'] = df['v'].rolling(20).mean()
            
            last = df.iloc[-1]
            
            # شرط خرید (نرم)
            if (last['ema50'] > last['ema200'] * 0.98 and 
                last['rsi'] < 55 and 
                last['v'] > last['vma'] * 0.3):
                
                entry = last['c']
                sl = entry - 2 * last['atr']
                tp = entry + 3 * last['atr']
                
                return {
                    'sym': symbol, 'type': 'BUY',
                    'entry': entry, 'sl': sl, 'tp': tp,
                    'rsi': last['rsi'], 'vol': last['v'] / last['vma']
                }
            
            # شرط فروش (نرم)
            if (last['ema50'] < last['ema200'] * 1.02 and 
                last['rsi'] > 45 and 
                last['v'] > last['vma'] * 0.3):
                
                entry = last['c']
                sl = entry + 2 * last['atr']
                tp = entry - 3 * last['atr']
                
                return {
                    'sym': symbol, 'type': 'SELL',
                    'entry': entry, 'sl': sl, 'tp': tp,
                    'rsi': last['rsi'], 'vol': last['v'] / last['vma']
                }
            
            return None
        except:
            return None


print("شروع اسکن...")
bot = Bot()

markets = bot.ex.load_markets()
symbols = [s for s, m in markets.items() if m['quote'] == 'USDT' and m['spot'] and m['active']]

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

if signals:
    signals_sorted = sorted(signals, key=lambda x: x['rsi'] if x['type'] == 'BUY' else -x['rsi'])
    
    summary = "<b>سیگنال های جدید</b>\n"
    summary += "تعداد: " + str(len(signals)) + "\n\n"
    for i, s in enumerate(signals_sorted[:20], 1):
        summary += str(i) + ". " + s['sym'] + " (" + s['type'] + ")\n"
    send(summary)
    
    time.sleep(1)
    
    for s in signals_sorted[:10]:
        emoji = "🟢" if s['type'] == 'BUY' else "🔴"
        msg = emoji + " <b>" + s['sym'] + "</b> (" + s['type'] + ")\n"
        msg += "━━━━━━━━━━━━━━━\n"
        msg += "ورود: <code>" + str(round(s['entry'], 4)) + "</code>\n"
        msg += "حد ضرر: <code>" + str(round(s['sl'], 4)) + "</code>\n"
        msg += "حد سود: <code>" + str(round(s['tp'], 4)) + "</code>\n"
        msg += "━━━━━━━━━━━━━━━\n"
        msg += "RSI: " + str(round(s['rsi'], 1)) + " | حجم: " + str(round(s['vol'], 2)) + "x"
        send(msg)
        time.sleep(1)
    
    print("ارسال شد: " + str(len(signals)) + " سیگنال")
else:
    send("هیچ سیگنالی پیدا نشد.")
    print("سیگنالی نیست")

print("Done!")
