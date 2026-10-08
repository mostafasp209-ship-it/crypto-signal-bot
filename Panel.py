"""
panel.py - پنل دکمه‌ای ادمین و کاربران برای bot.py

استفاده در bot.py:
    import panel
    ...
    # به‌جای poll_and_handle_commands(users):
    panel.poll_and_handle(users, state)

اجرای مستقل (هر چند دقیقه با workflow جدا):
    python panel.py --once
"""
import os
import sys
import html
import json
import time
import requests

import bot  # توابع و تنظیمات بات اصلی

API = "https://api.telegram.org/bot"

# متن دکمه‌ها
B_A_USERS = "👥 کاربران"
B_A_STATS = "📊 آمار سیگنال‌ها"
B_A_BCAST = "📢 ارسال همگانی"
B_A_BLOCK = "🚫 مسدودسازی"
B_A_STATUS = "🔎 وضعیت ربات"
B_A_SCAN = "🔍 اجرای اسکن"

B_U_NEW = "📡 سیگنال‌های جدید"
B_U_PERF = "📈 عملکرد سیگنال‌ها"
B_U_NOTIF = "🔔 تنظیم اعلان‌ها"
B_U_HELP = "📖 راهنما"

HELP_TEXT = (
    "📖 <b>راهنما</b>\n\n"
    "• هر ۴ ساعت بازار اسکن می‌شود و سیگنال‌های باکیفیت ارسال می‌شود.\n"
    "• هر سیگنال سه هدف (TP1/TP2/TP3) و یک حد ضرر دارد.\n"
    "• بعد از رسیدن به TP1، حد ضرر به نقطه ورود می‌رود.\n"
    "• سیگنال‌ها ۴۸ ساعت اعتبار دارند.\n\n"
    "⚠️ ربات در مرحله آزمایشی است؛ با احتیاط استفاده کنید."
)


# ---------------- Telegram helpers ----------------
def tg(method, **payload):
    if not bot.TELEGRAM_BOT_TOKEN:
        return None
    try:
        r = requests.post(API + bot.TELEGRAM_BOT_TOKEN + "/" + method,
                          json=payload, timeout=20)
        return r.json() if r.ok else None
    except Exception:
        return None


def send(chat_id, text, markup=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return tg("sendMessage", **p)


def reply_kb(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows],
            "resize_keyboard": True}


def admin_menu():
    return reply_kb([
        [B_A_USERS, B_A_STATS],
        [B_A_BCAST, B_A_BLOCK],
        [B_A_STATUS, B_A_SCAN],
        [B_U_NEW, B_U_HELP],
    ])


def user_menu():
    return reply_kb([
        [B_U_NEW, B_U_PERF],
        [B_U_NOTIF, B_U_HELP],
    ])


def menu_for(chat_id):
    return admin_menu() if bot.is_admin(chat_id) else user_menu()


# ---------------- actions ----------------
def show_new_signals(chat_id, state):
    active = [s for s in state.get("signals", [])
              if s.get("status") == "ACTIVE"]
    if not active:
        send(chat_id, "📭 فعلاً سیگنال فعالی وجود ندارد.")
        return
    for s in active[-5:]:
        send(chat_id, bot.format_signal(s))
        time.sleep(0.2)


def show_perf(chat_id, state):
    send(chat_id, bot.performance_report(state))


def toggle_notif(users, chat_id):
    u = users["users"].setdefault(str(chat_id), {"name": "", "blocked": False})
    u["notify"] = not u.get("notify", True)
    send(chat_id, "🔔 اعلان‌ها " + ("فعال شد ✅" if u["notify"]
                                    else "غیرفعال شد 🔕"))


def admin_users(chat_id, users):
    total = len(users["users"])
    active = len(bot.get_active_user_ids(users))
    send(chat_id, "👥 <b>کاربران</b>\nکل: %d\nفعال: %d\nمسدود: %d"
         % (total, active, total - active))


PAGE = 8


def admin_block_list(chat_id, users, page=0, message_id=None):
    items = [(c, u) for c, u in users["users"].items()
             if not bot.is_admin(c)]
    if not items:
        send(chat_id, "کاربری وجود ندارد.")
        return
    pages = (len(items) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = []
    for cid, u in items[page * PAGE:(page + 1) * PAGE]:
        mark = "🚫" if u.get("blocked") else "✅"
        rows.append([{
            "text": "%s %s (%s)" % (mark, u.get("name") or "?", cid),
            "callback_data": "tgl:%s:%d" % (cid, page),
        }])
    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": "pg:%d" % (page - 1)})
    nav.append({"text": "%d/%d" % (page + 1, pages),
                "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "▶️", "callback_data": "pg:%d" % (page + 1)})
    rows.append(nav)
    text = "🚫 برای مسدود/آزاد کردن روی کاربر بزن:"
    if message_id:
        tg("editMessageText", chat_id=chat_id, message_id=message_id,
           text=text, reply_markup={"inline_keyboard": rows})
    else:
        send(chat_id, text, {"inline_keyboard": rows})


def admin_status(chat_id, state, users):
    lr = state.get("last_run") or "—"
    act = sum(1 for s in state.get("signals", [])
              if s.get("status") == "ACTIVE")
    send(chat_id, "🔎 <b>وضعیت ربات</b>\nآخرین اجرا: <code>%s</code>\n"
         "سیگنال فعال: %d\nکاربران فعال: %d"
         % (lr, act, len(bot.get_active_user_ids(users))))


def admin_scan(chat_id):
    """نیاز به GH_PAT (توکن با دسترسی workflow) و GH_REPO مثل user/repo"""
    pat = os.getenv("GH_PAT", "").strip()
    repo = os.getenv("GH_REPO", "").strip()
    wf = os.getenv("GH_WORKFLOW", "bot.yml").strip()
    if not pat or not repo:
        send(chat_id, "❌ GH_PAT و GH_REPO تنظیم نشده‌اند.")
        return
    try:
        r = requests.post(
            "https://api.github.com/repos/%s/actions/workflows/%s/dispatches"
            % (repo, wf),
            headers={"Authorization": "Bearer " + pat,
                     "Accept": "application/vnd.github+json"},
            json={"ref": "main"}, timeout=20)
        send(chat_id, "🔍 اسکن درخواست شد ✅" if r.status_code == 204
             else "❌ خطا: %s" % r.status_code)
    except Exception as e:
        send(chat_id, "❌ " + str(e)[:80])


# ---------------- update handlers ----------------
def handle_message(users, state, msg):
    chat_id = msg["chat"]["id"]
    cid = str(chat_id)
    text = (msg.get("text") or "").strip()
    frm = msg.get("from", {})
    name = frm.get("first_name", "") or frm.get("username", "")
    safe_name = html.escape(name)

    is_new = bot.ensure_user(users, chat_id, name)

    if bot.is_blocked(users, chat_id):
        return

    if text.startswith("/start"):
        send(chat_id,
             bot.WELCOME_MSG.format(name=safe_name or "دوست عزیز"),
             menu_for(chat_id))
        if is_new and bot.ADMIN_CHAT_ID and not bot.is_admin(chat_id):
            bot.telegram_send_to(
                bot.ADMIN_CHAT_ID,
                "👤 <b>کاربر جدید</b>\nنام: %s\nChat ID: <code>%s</code>"
                % (safe_name or "?", cid))
        return

    admin = bot.is_admin(chat_id)

    # ادمین: منتظر متن همگانی
    if admin and users["users"][cid].get("pending") == "broadcast":
        users["users"][cid].pop("pending", None)
        if text in (B_A_BCAST, "/cancel"):
            send(chat_id, "لغو شد.", admin_menu())
            return
        n = bot.telegram_broadcast(users, "📣 " + text)
        send(chat_id, "📣 برای %d کاربر ارسال شد." % n, admin_menu())
        return

    # دکمه‌های مشترک
    if text == B_U_NEW:
        show_new_signals(chat_id, state)
    elif text == B_U_PERF:
        show_perf(chat_id, state)
    elif text == B_U_NOTIF:
        toggle_notif(users, chat_id)
    elif text == B_U_HELP or text == "/help":
        send(chat_id, HELP_TEXT)
    # دکمه‌های ادمین (حتماً چک ادمین)
    elif admin and text == B_A_USERS:
        admin_users(chat_id, users)
    elif admin and text == B_A_STATS:
        show_perf(chat_id, state)
        rep = bot.sl_method_report(state)
        if rep:
            send(chat_id, rep)
    elif admin and text == B_A_BCAST:
        users["users"][cid]["pending"] = "broadcast"
        send(chat_id, "📢 متن پیام همگانی را بفرست (/cancel برای لغو):")
    elif admin and text == B_A_BLOCK:
        admin_block_list(chat_id, users)
    elif admin and text == B_A_STATUS:
        admin_status(chat_id, state, users)
    elif admin and text == B_A_SCAN:
        admin_scan(chat_id)
    elif admin and text.startswith("/"):
        bot.handle_admin_command(users, chat_id, text)


def handle_callback(users, cq):
    chat_id = cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]
    data = cq.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if bot.is_blocked(users, chat_id) or not bot.is_admin(chat_id):
        return
    if data.startswith("pg:"):
        admin_block_list(chat_id, users, int(data[3:] or 0), mid)
    elif data.startswith("tgl:"):
        _, target, page = data.split(":")
        if target in users["users"] and not bot.is_admin(target):
            u = users["users"][target]
            u["blocked"] = not u.get("blocked", False)
        admin_block_list(chat_id, users, int(page), mid)


# ---------------- polling ----------------
def poll_and_handle(users, state):
    if not bot.TELEGRAM_BOT_TOKEN:
        return
    offset = users.get("last_update_id", 0) + 1
    updates = bot.telegram_get_updates(offset=offset)
    max_id = users.get("last_update_id", 0)
    for u in updates:
        max_id = max(max_id, u.get("update_id", 0))
        try:
            if u.get("message"):
                handle_message(users, state, u["message"])
            elif u.get("callback_query"):
                handle_callback(users, u["callback_query"])
        except Exception as e:
            print("[PANEL ERR] " + str(e)[:100])
    users["last_update_id"] = max_id


if __name__ == "__main__":
    # حالت مستقل: فقط پیام‌های در انتظار را پردازش کن
    st = bot.load_state()
    us = bot.load_users()
    if bot.ADMIN_CHAT_ID:
        bot.ensure_user(us, bot.ADMIN_CHAT_ID, "Admin")
    poll_and_handle(us, st)
    bot.save_users(us)
    if "--loop" in sys.argv:  # برای سرور همیشه روشن
        while True:
            poll_and_handle(us, st)
            bot.save_users(us)
            st = bot.load_state()
            time.sleep(2)
