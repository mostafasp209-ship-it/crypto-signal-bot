# Crypto Signal Bot (Binance USDⓈ-M Futures)

ربات **سیگنال‌گیری** برای بازار **Binance USDⓈ-M Perpetual Futures**.
سفارش واقعی روی صرافی ثبت نمی‌شود؛ فقط سیگنال و مدیریت سطوح را در تلگرام و `signals.json` نگه می‌دارد.

> این ربات توصیه مالی نیست. قبل از هر استفاده واقعی، بک‌تست تاریخی و حساب آزمایشی الزامی است.

## استراتژی مرجع فعلی

| مورد | مقدار |
|------|--------|
| بازار | فقط Binance `binanceusdm` (USDT-M linear perpetual) |
| تایم‌فریم سیگنال | 4H |
| جهت | Long و Short |
| منطق | RSI recovery 30/70 + کراس واقعی Stochastic + حجم ≥ 1.12× میانگین 20 |
| ورود | Open کندل 4H بعدی (بدون fallback به کلوز) |
| RR | 1.5 |
| استاپ | سوینگ 5 کندل در برابر 1.32×ATR(14) |
| هزینه مدل‌شده | Fee 0.05% + Slippage 0.02% |
| وضعیت اعتبارسنجی | `NOT_VALIDATED_ON_REAL_MARKET_DATA` |

ورود سیگنال **جدید** فقط از Futures است. صرافی‌های Spot قدیمی فقط برای مدیریت سیگنال‌های فعال قدیمی در صورت وجود بارگذاری می‌شوند.

## فایل‌های اصلی

- `bot.py` — اسکن Futures، ساخت سیگنال، مدیریت TP/SL، ارسال تلگرام
- `panel.py` — پنل تعاملی تلگرام
- `signals.json` — تاریخچه و وضعیت سیگنال‌ها
- `users.json` — کاربران پنل
- `research/reference_futures_backtest.py` — بک‌تست تحقیقاتی آفلاین (خواندنی)
- `tests/` — تست‌های واحد استراتژی و ایمنی

## Secrets مورد نیاز (GitHub Actions)

در **Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ADMIN_CHAT_ID` (اختیاری؛ در غیر این صورت از `TELEGRAM_CHAT_ID` استفاده می‌شود)

## زمان‌بندی Workflow (`bot.yml`)

- اسکن بازار: حدود ۲ دقیقه بعد از هر باز شدن کندل 4H (UTC)
- مدیریت سیگنال‌های فعال + polling پنل: ساعتی
- concurrency سریالی برای جلوگیری از تداخل نوشتن در `signals.json`

## اجرای پنل تعاملی (همیشه‌روشن)

```bash
python -u panel.py --loop
```

متغیرها: `TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID`, و در صورت نیاز `GH_REPO`, `GH_BRANCH`, `GH_PAT`.

پنل را به‌صورت job کوتاه زمان‌بندی‌شده اجرا نکن. چند نمونه هم‌زمان با یک توکن باعث تداخل `getUpdates` می‌شود.

## تست‌ها

```bash
pip install -r requirements.txt
python -m py_compile bot.py panel.py
python -m unittest discover -s tests -v
```

Workflow `test-strategy.yml` روی هر Pull Request به `main` و pushهای مرتبط اجرا می‌شود.

## بک‌تست تحقیقاتی (آفلاین)

```bash
python research/reference_futures_backtest.py --symbols BTCUSDT,ETHUSDT --since 2022-01-01 --out research/out
```

این اسکریپت:
- از API عمومی Binance Futures کندل 4H می‌گیرد
- ورود next-open، fee/slippage، برخورد محافظه‌کارانه SL/TP و Funding تقریبی را مدل می‌کند
- **هیچ عدد سودآوری را به‌عنوان نتیجه زنده ادعا نمی‌کند**

## گزارش دستی سیگنال‌های ثبت‌شده

```bash
python analyze_signals.py --input signals.json --out outcome-report.json
python postmortem_report.py --input signals.json --output postmortem-report.json
python historical_replay.py --input signals.json --output historical-replay-report.json
```

این گزارش‌ها فقط‌خواندنی‌اند و منطق ربات را تغییر نمی‌دهند.

## نکات ایمنی

- توکن یا PAT را در کد commit نکن
- تا وقتی تست واحد و بک‌تست تاریخی پاس نشده، پارامتر استراتژی را برای «بهبود حدسی» عوض نکن
- ربات signal-only است؛ سفارش واقعی غیرفعال است
