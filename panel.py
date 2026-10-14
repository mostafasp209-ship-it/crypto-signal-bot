"""Telegram control panel for the spot crypto-signal bot.

Compatible with the bot.py currently provided in this project.

Run once (GitHub Actions scheduled workflow):
    python panel.py --once

Run continuously (only on an always-on host):
    python panel.py --loop

Required environment:
    TELEGRAM_BOT_TOKEN
    ADMIN_CHAT_ID (recommended; falls back to TELEGRAM_CHAT_ID in bot.py)
Optional for the admin Scan button:
    GH_PAT, GH_REPO=owner/repo, GH_WORKFLOW=bot.yml, GH_BRANCH=main
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
import bot

API = "https://api.telegram.org/bot"
TOKEN = str(getattr(bot, "TELEGRAM_BOT_TOKEN", "") or "").strip()
ADMIN_ID = str(getattr(bot, "ADMIN_CHAT_ID", "") or getattr(bot, "TELEGRAM_CHAT_ID", "") or "").strip()
USERS_FILE = Path(os.getenv("USERS_FILE", getattr(bot, "USERS_FILE", "users.json")))
STATE_FILE = Path(getattr(bot, "DATA_FILE", "signals.json"))
PAGE_SIZE = 6

# Reply keyboard labels
B_USERS = "👥 کاربران"
B_STATS = "📊 آمار و عملکرد"
B_BROADCAST = "📢 پیام همگانی"
B_BLOCK = "🚫 مدیریت کاربران"
B_STATUS = "🔎 وضعیت ربات"
B_SCAN = "🔍 اجرای اسکن"
B_SIGNALS = "📡 سیگنال‌های فعال"
B_HISTORY = "🗂 تاریخچه سیگنال‌ها"
B_PERF = "📈 عملکرد"
B_NOTIF = "🔔 اعلان‌ها"
B_HELP = "📖 راهنما"
B_MENU = "🏠 منو"

HELP_TEXT = (
    "📖 <b>راهنمای ربات</b>\n\n"
    "• این پنل وضعیت و سیگنال‌های ثبت‌شده در signals.json را نمایش می‌دهد.\n"
    "• مدیریت TP/SL توسط اجرای اصلی bot.py انجام می‌شود؛ پنل خودش قیمت بازار را مدیریت نمی‌کند.\n"
    "• اعتبار پیش‌فرض سیگنال‌ها ۴۸ ساعت است.\n"
    "• بعد از TP1، منطق فعلی bot.py حد ضرر را به نقطه ورود منتقل می‌کند.\n\n"
    "⚠️ ربات آزمایشی است؛ سیگنال‌ها توصیه مالی نیستند."
)


def atomic_json_save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_users() -> dict:
    if not USERS_FILE.exists():
        return {"users": {}, "last_update_id": 0}
    try:
        with USERS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("users", {})
        data.setdefault("last_update_id", 0)
        if isinstance(data["users"], list):
            converted = {}
            for item in data["users"]:
                if isinstance(item, dict):
                    cid = item.get("chat_id", item.get("telegram_id", item.get("id")))
                    if cid is not None:
                        converted[str(cid)] = item
                elif isinstance(item, (int, str)):
                    converted[str(item)] = {"name": "", "blocked": False, "notify": True}
            data["users"] = converted
        if not isinstance(data["users"], dict):
            data["users"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        # Do not silently overwrite a malformed users file.
        raise RuntimeError(f"فایل {USERS_FILE} قابل خواندن نیست؛ برای جلوگیری از پاک‌شدن اطلاعات، اصلاحش کن.")


def load_state() -> dict:
    try:
        return bot.load_state()
    except Exception:
        if not STATE_FILE.exists():
            return {"signals": [], "last_run": None}
        raise


def save_users(users: dict) -> None:
    atomic_json_save(USERS_FILE, users)


def is_admin(chat_id: Any) -> bool:
    return bool(ADMIN_ID) and str(chat_id) == ADMIN_ID


def get_user(users: dict, chat_id: Any) -> dict:
    cid = str(chat_id)
    return users["users"].setdefault(cid, {"name": "", "blocked": False, "notify": True})


def active_users(users: dict) -> list[str]:
    result = []
    for cid, user in users.get("users", {}).items():
        if isinstance(user, dict) and not user.get("blocked") and user.get("notify", True):
            result.append(str(cid))
    return result


def api(method: str, **payload) -> dict | None:
    if not TOKEN:
        print("[PANEL] TELEGRAM_BOT_TOKEN is missing")
        return None
    try:
        response = requests.post(API + TOKEN + "/" + method, json=payload, timeout=20)
        try:
            data = response.json()
        except ValueError:
            data = {"ok": False, "description": response.text[:200]}
        if not response.ok or not data.get("ok", False):
            print(f"[PANEL API] {method}: {response.status_code} {str(data)[:250]}")
        return data if isinstance(data, dict) else None
    except requests.RequestException as exc:
        print(f"[PANEL API] {method}: {exc}")
        return None


def send(chat_id: Any, text: str, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    api("sendMessage", **payload)


def edit(chat_id: Any, message_id: Any, text: str, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    api("editMessageText", **payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
    api("answerCallbackQuery", **payload)


def reply_keyboard(rows: list[list[str]]) -> dict:
    return {"keyboard": [[{"text": label} for label in row] for row in rows], "resize_keyboard": True, "is_persistent": True}


def admin_menu() -> dict:
    return reply_keyboard([[B_USERS, B_STATS], [B_BLOCK, B_BROADCAST], [B_STATUS, B_SCAN], [B_SIGNALS, B_HISTORY], [B_HELP]])


def user_menu() -> dict:
    return reply_keyboard([[B_SIGNALS, B_HISTORY], [B_PERF, B_NOTIF], [B_HELP]])


def menu_for(chat_id: Any) -> dict:
    return admin_menu() if is_admin(chat_id) else user_menu()


def safe_num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def active_signals(state: dict) -> list[dict]:
    return [s for s in state.get("signals", []) if isinstance(s, dict) and s.get("status") == "ACTIVE"]


def signal_short(s: dict, index: int) -> str:
    side = html.escape(str(s.get("side", "?")))
    symbol = html.escape(str(s.get("symbol", "?")))
    status = "فعال" if s.get("status") == "ACTIVE" else html.escape(str(s.get("status", "?")))
    entry = html.escape(str(s.get("entry", "—")))
    sl = html.escape(str(s.get("sl", "—")))
    return f"{index}. <b>{symbol} — {side}</b> | {status}\nEntry: <code>{entry}</code> | SL: <code>{sl}</code>"


def show_signals(chat_id: Any, state: dict, history: bool = False) -> None:
    signals = state.get("signals", [])
    if history:
        chosen = [s for s in signals if s.get("status") != "ACTIVE"][-8:]
        title = "🗂 <b>آخرین سیگنال‌های بسته‌شده/منقضی</b>"
    else:
        chosen = active_signals(state)[-8:]
        title = "📡 <b>سیگنال‌های فعال</b>"
    if not chosen:
        send(chat_id, title + "\n\nموردی برای نمایش نیست.", menu_for(chat_id))
        return
    send(chat_id, title, menu_for(chat_id))
    for s in reversed(chosen):
        try:
            body = bot.format_signal(s) if s.get("status") == "ACTIVE" else signal_short(s, 1)
        except Exception:
            body = signal_short(s, 1)
        send(chat_id, body)
        time.sleep(0.1)


def show_performance(chat_id: Any, state: dict) -> None:
    try:
        report = bot.performance_report(state)
    except Exception as exc:
        report = "گزارش عملکرد در دسترس نیست: " + html.escape(str(exc)[:150])
    signals = state.get("signals", [])
    active = sum(s.get("status") == "ACTIVE" for s in signals)
    closed = [s for s in signals if s.get("status") in ("CLOSED", "EXPIRED")]
    report += f"\n\nسیگنال فعال: <b>{active}</b>\nکل تاریخچه: <b>{len(signals)}</b>"
    send(chat_id, report, menu_for(chat_id))


def show_users(chat_id: Any, users: dict) -> None:
    all_users = users.get("users", {})
    blocked = sum(isinstance(u, dict) and u.get("blocked", False) for u in all_users.values())
    send(chat_id, "👥 <b>آمار کاربران</b>\n"
         f"کل ثبت‌شده: <b>{len(all_users)}</b>\n"
         f"دریافت اعلان فعال: <b>{len(active_users(users))}</b>\n"
         f"مسدود: <b>{blocked}</b>", admin_menu())


def show_status(chat_id: Any, state: dict, users: dict) -> None:
    last_run = html.escape(str(state.get("last_run") or "هنوز ثبت نشده"))
    total = len(state.get("signals", []))
    active = len(active_signals(state))
    send(chat_id, "🔎 <b>وضعیت ربات</b>\n"
         f"آخرین اجرای ثبت‌شده: <code>{last_run}</code>\n"
         f"سیگنال فعال: <b>{active}</b>\n"
         f"کل سیگنال‌ها: <b>{total}</b>\n"
         f"کاربران ثبت‌شده: <b>{len(users.get('users', {}))}</b>\n"
         f"توکن تلگرام: {'تنظیم شده ✅' if TOKEN else 'تنظیم نشده ❌'}", admin_menu())


def block_keyboard(users: dict, page: int = 0) -> dict:
    items = [(cid, u) for cid, u in users.get("users", {}).items() if not is_admin(cid)]
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = []
    for cid, u in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        if not isinstance(u, dict):
            u = {}
        marker = "🚫" if u.get("blocked") else "✅"
        name = str(u.get("name") or "بدون نام")[:22]
        rows.append([{"text": f"{marker} {name} · {cid}"[:60], "callback_data": f"user:{cid}:{page}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"page:{page-1}"})
    nav.append({"text": f"{page+1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "بعدی ▶️", "callback_data": f"page:{page+1}"})
    rows.append(nav)
    return {"inline_keyboard": rows}


def show_block_list(chat_id: Any, users: dict, page: int = 0, message_id: Any = None) -> None:
    if not users.get("users"):
        send(chat_id, "هنوز کاربری ثبت نشده است.", admin_menu())
        return
    text = "🚫 <b>مدیریت کاربران</b>\nروی نام کاربر بزن تا مسدود/آزاد شود."
    markup = block_keyboard(users, page)
    if message_id:
        edit(chat_id, message_id, text, markup)
    else:
        send(chat_id, text, markup)


def broadcast_users(users: dict, message: str) -> tuple[int, int]:
    ok = failed = 0
    for cid, u in users.get("users", {}).items():
        if is_admin(cid) or not isinstance(u, dict) or u.get("blocked") or not u.get("notify", True):
            continue
        result = api("sendMessage", chat_id=cid, text=message, parse_mode="HTML", disable_web_page_preview=True)
        if result and result.get("ok"):
            ok += 1
        else:
            failed += 1
        time.sleep(0.06)
    return ok, failed


def trigger_scan(chat_id: Any) -> None:
    pat = os.getenv("GH_PAT", "").strip()
    repo = os.getenv("GH_REPO", "").strip()
    workflow = os.getenv("GH_WORKFLOW", "bot.yml").strip()
    branch = os.getenv("GH_BRANCH", "main").strip() or "main"
    if not pat or not repo:
        send(chat_id, "❌ برای اجرای اسکن از پنل، متغیرهای <code>GH_PAT</code> و <code>GH_REPO</code> را در Secrets تنظیم کن.", admin_menu())
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    try:
        response = requests.post(url, headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, json={"ref": branch}, timeout=20)
        if response.status_code == 204:
            send(chat_id, f"🔍 اجرای workflow <code>{html.escape(workflow)}</code> برای شاخه <code>{html.escape(branch)}</code> درخواست شد ✅", admin_menu())
        else:
            send(chat_id, f"❌ GitHub خطا داد: HTTP {response.status_code}\n<code>{html.escape(response.text[:350])}</code>", admin_menu())
    except requests.RequestException as exc:
        send(chat_id, "❌ اتصال به GitHub ناموفق بود: " + html.escape(str(exc)[:180]), admin_menu())


def handle_message(users: dict, state: dict, msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return
    cid = str(chat_id)
    frm = msg.get("from", {})
    name = str(frm.get("first_name") or frm.get("username") or "کاربر")[:80]
    user = get_user(users, chat_id)
    user.setdefault("name", name)
    user.setdefault("blocked", False)
    user.setdefault("notify", True)
    user["last_seen"] = int(time.time())
    text = (msg.get("text") or "").strip()

    if user.get("blocked") and not is_admin(chat_id):
        return
    if text.startswith("/start"):
        send(chat_id, f"سلام {html.escape(name)} 👋\nبه پنل سیگنال خوش آمدی.", menu_for(chat_id))
        return
    if text in ("/menu", B_MENU):
        send(chat_id, "منوی اصلی:", menu_for(chat_id))
        return
    if text in ("/help", B_HELP):
        send(chat_id, HELP_TEXT, menu_for(chat_id))
        return

    # Broadcast confirmation state is persisted in users.json, so it survives Actions runs.
    if is_admin(chat_id) and user.get("pending") == "broadcast":
        if text in ("/cancel", B_BROADCAST):
            user.pop("pending", None)
            send(chat_id, "ارسال همگانی لغو شد.", admin_menu())
            return
        user.pop("pending", None)
        escaped = html.escape(text)
        ok, failed = broadcast_users(users, "📣 <b>پیام مدیریت</b>\n\n" + escaped)
        send(chat_id, f"📢 ارسال تمام شد.\nموفق: <b>{ok}</b>\nناموفق: <b>{failed}</b>", admin_menu())
        return

    if text == B_SIGNALS:
        show_signals(chat_id, state, False)
    elif text == B_HISTORY:
        show_signals(chat_id, state, True)
    elif text in (B_PERF, B_STATS):
        show_performance(chat_id, state)
    elif text == B_NOTIF:
        user["notify"] = not user.get("notify", True)
        send(chat_id, "اعلان‌ها " + ("فعال شد 🔔" if user["notify"] else "غیرفعال شد 🔕"), menu_for(chat_id))
    elif is_admin(chat_id) and text == B_USERS:
        show_users(chat_id, users)
    elif is_admin(chat_id) and text == B_BLOCK:
        show_block_list(chat_id, users)
    elif is_admin(chat_id) and text == B_STATUS:
        show_status(chat_id, state, users)
    elif is_admin(chat_id) and text == B_SCAN:
        trigger_scan(chat_id)
    elif is_admin(chat_id) and text == B_BROADCAST:
        user["pending"] = "broadcast"
        send(chat_id, "متن پیام همگانی را ارسال کن. برای لغو بنویس <code>/cancel</code>.", admin_menu())
    elif text.startswith("/"):
        send(chat_id, "این دستور در پنل فعلی پشتیبانی نمی‌شود. از دکمه‌های منو استفاده کن.", menu_for(chat_id))
    else:
        send(chat_id, "یکی از گزینه‌های منو را انتخاب کن.", menu_for(chat_id))


def handle_callback(users: dict, cq: dict) -> None:
    callback_id = cq.get("id", "")
    message = cq.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    data = str(cq.get("data", ""))
    if chat_id is None:
        answer_callback(callback_id)
        return
    if not is_admin(chat_id):
        answer_callback(callback_id, "این بخش فقط برای ادمین است.")
        return
    if data == "noop":
        answer_callback(callback_id)
    elif data.startswith("page:"):
        answer_callback(callback_id)
        try:
            show_block_list(chat_id, users, int(data.split(":", 1)[1]), message_id)
        except ValueError:
            pass
    elif data.startswith("user:"):
        answer_callback(callback_id)
        try:
            _, target, page = data.split(":", 2)
            if target in users.get("users", {}) and not is_admin(target):
                u = users["users"][target]
                u["blocked"] = not bool(u.get("blocked", False))
                status = "مسدود شد 🚫" if u["blocked"] else "آزاد شد ✅"
                send(chat_id, f"کاربر <code>{html.escape(target)}</code> {status}.")
            show_block_list(chat_id, users, int(page), message_id)
        except (ValueError, TypeError):
            pass
    else:
        answer_callback(callback_id)


def poll_and_handle(users: dict, state: dict) -> int:
    if not TOKEN:
        print("[PANEL] No token; skipped polling")
        return 0
    offset = int(users.get("last_update_id", 0)) + 1
    result = api("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"])
    if not result or not result.get("ok"):
        return 0
    updates = result.get("result", [])
    max_id = int(users.get("last_update_id", 0))
    handled = 0
    for update in updates:
        update_id = int(update.get("update_id", 0))
        try:
            if update.get("message"):
                handle_message(users, state, update["message"])
            elif update.get("callback_query"):
                handle_callback(users, update["callback_query"])
            handled += 1
        except Exception as exc:
            print("[PANEL HANDLER ERROR]", type(exc).__name__, str(exc)[:180])
        max_id = max(max_id, update_id)
    users["last_update_id"] = max_id
    return handled


def main() -> None:
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set")
        sys.exit(2)
    users = load_users()
    if ADMIN_ID:
        admin = get_user(users, ADMIN_ID)
        admin["name"] = admin.get("name") or "Admin"
        admin["blocked"] = False
        admin["notify"] = False
    loop = "--loop" in sys.argv
    try:
        while True:
            state = load_state()
            handled = poll_and_handle(users, state)
            save_users(users)
            print(f"[PANEL] handled={handled}, users={len(users.get('users', {}))}")
            if not loop:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        save_users(users)
        print("[PANEL] stopped")


if __name__ == "__main__":
    main()
