# Crypto Signal Bot

ربات سیگنال‌گیری اسپات؛ اجرای اسکن بازار در GitHub Actions و پنل تلگرام نیازمند اجرای جداگانه است.

## فایل‌ها
- `bot.py`: اسکن بازار، ساخت سیگنال، مدیریت TP/SL و ارسال پیام.
- `panel.py`: پنل تعاملی تلگرام.
- `signals.json`: تاریخچه و وضعیت سیگنال‌ها.
- `users.json`: کاربران پنل و شناسهٔ آخرین آپدیت تلگرام.

## تنظیمات GitHub Actions
در **Settings → Secrets and variables → Actions** این Secrets را تنظیم کن:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ADMIN_CHAT_ID` (اختیاری؛ اگر تنظیم نشود از `TELEGRAM_CHAT_ID` استفاده می‌شود)

Workflow موجود `bot.yml` اسکن ربات را هر چهار ساعت اجرا می‌کند. این اجرا برای پنل تعاملی کافی نیست.

## اجرای پنل تعاملی
برای پاسخ فوری به دکمه‌ها، `panel.py` باید روی یک سرویس همیشه‌روشن اجرا شود؛ مثلاً VPS یا یک سرویس Python worker. چون پنل معمولاً روی میزبان جدا از GitHub Actions است، برای نمایش آخرین سیگنال‌ها `GH_REPO` و `GH_BRANCH` را هم تنظیم کن؛ پنل در این حالت `signals.json` را از شاخهٔ GitHub می‌خواند و در صورت قطع دسترسی از فایل محلی استفاده می‌کند. متغیرهای محیطی زیر را تنظیم کن:
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_CHAT_ID`
- `GH_PAT` (اختیاری، فقط اگر دکمهٔ «اجرای اسکن» باید GitHub Actions را راه‌اندازی کند)
- `GH_REPO=mostafasp209-ship-it/crypto-signal-bot`
- `GH_WORKFLOW=bot.yml`
- `GH_BRANCH=main`

دستور اجرا:
```bash
python -u panel.py --loop
```

در این حالت worker باید دائماً روشن بماند. **پنل را به‌عنوان job زمان‌بندی‌شدهٔ کوتاه اجرا نکن**؛ اجرای یک‌باره فقط آپدیت‌های موجود در همان لحظه را می‌خواند و دکمه‌ها تا اجرای بعدی پاسخ نمی‌گیرند. اجرای چند نمونهٔ `panel.py --loop` با یک توکن هم‌زمان نیز باعث تداخل `getUpdates` می‌شود.

اگر قبلاً webhook برای همین توکن تنظیم شده، polling با `getUpdates` کار نخواهد کرد؛ فقط وقتی مطمئنی برنامهٔ دیگری از webhook استفاده نمی‌کند، آن را حذف کن.

## نکات ایمنی
- توکن تلگرام یا PAT را در کد یا commit قرار نده.
- تغییرات معاملاتی را قبل از استفادهٔ واقعی روی داده‌های تاریخی و حساب آزمایشی بررسی کن.
- این ربات توصیهٔ مالی نیست.


## گزارش خودکار علت معاملات زیان‌ده

Workflow مستقل `Automated Signal Postmortem` هر روز اجرا می‌شود و با اجرای دستی هم قابل شروع است. این workflow:
- نتایج معاملات بسته‌شده را بر اساس setup، روند روزانه، رژیم بازار، spike، نزدیکی خط روند، فضای مقاومت، تعداد برخورد با حمایت و امتیاز سیگنال گروه‌بندی می‌کند.
- نرخ برد، سود/زیان خالص، ضریب سود، پوشش `entry_snapshot` و لغزش ثبت‌شدهٔ حد ضرر را گزارش می‌کند.
- گزارش JSON را به‌صورت Artifact در صفحهٔ اجرای GitHub Actions برای ۳۰ روز نگه می‌دارد.

این گزارش فقط‌خواندنی است و استراتژی زنده، حدها یا فایل `signals.json` را تغییر نمی‌دهد. گروه‌های کمتر از ۵ معامله صرفاً اکتشافی‌اند؛ هیچ فیلتری نباید صرفاً بر اساس همبستگی چند معامله به استراتژی زنده اضافه شود.


## Historical candle replay (read-only)

The daily/manual `Automated Signal Postmortem` workflow also downloads public 1-minute OHLCV candles for closed signals when the exchange still provides them, then publishes a separate `historical-replay-<run id>` artifact. It compares recorded PnL with a conservative, approximate replay and lists unavailable data/errors per signal. OHLCV cannot prove intraminute ordering or real fills, so the replay is diagnostic evidence—not a promise of realized results.

The workflow does not modify `bot.py`, entry filters, stop-loss/target rules, or `signals.json`. Any strategy adjustment must be separately tested out-of-sample and reviewed before deployment; this automation never self-edits live rules.
