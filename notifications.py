"""Telegram notification delivery for signal lifecycle events."""
from __future__ import annotations

import json
import os
import time

import requests

from config import ADMIN_CHAT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, USERS_FILE


def telegram_recipients():
    """Return admin plus opted-in users from users.json, without duplicate chat IDs.

    Supported users.json shapes:
      {"users": [{"chat_id": 123, "blocked": false, "notify": true}]}
      [{"chat_id": 123, "blocked": false, "notify": true}]
      {"123": {"blocked": false, "notify": true}, "456": true}
      {"users": [123, 456]}
    """
    recipients = []
    seen = set()

    def add(chat_id):
        if chat_id is None:
            return
        chat_id = str(chat_id).strip()
        if not chat_id or chat_id in seen:
            return
        seen.add(chat_id)
        recipients.append(chat_id)

    # Keep the configured administrator/main chat as a recipient.
    add(ADMIN_CHAT_ID or TELEGRAM_CHAT_ID)

    if not os.path.exists(USERS_FILE):
        print(f"[TELEGRAM] {USERS_FILE} not found; only the admin chat will receive messages.")
        return recipients

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[TELEGRAM] Could not read {USERS_FILE}: {exc}; using admin only.")
        return recipients

    users = data.get("users", data) if isinstance(data, dict) else data
    if isinstance(users, dict):
        entries = []
        for key, value in users.items():
            # A dict keyed by chat ID is supported as well as a wrapper object.
            if key == "users" and isinstance(value, list):
                entries.extend(value)
            else:
                entries.append((key, value))
    elif isinstance(users, list):
        entries = users
    else:
        entries = []

    for entry in entries:
        chat_id = None
        settings = {}
        if isinstance(entry, (str, int)):
            chat_id = entry
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            chat_id, settings = entry
            if not isinstance(settings, dict):
                settings = {"notify": bool(settings)}
        elif isinstance(entry, dict):
            settings = entry
            chat_id = entry.get("chat_id", entry.get("telegram_id", entry.get("user_id", entry.get("id"))))
        if settings.get("blocked") is True or settings.get("is_blocked") is True:
            continue
        if settings.get("notify") is False or settings.get("notifications") is False or settings.get("active") is False:
            continue
        add(chat_id)

    print(f"[TELEGRAM] recipients loaded: {len(recipients)}")
    return recipients


def telegram_send(text):
    """Send a message to the administrator and all subscribed users."""
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram bot token is missing; message skipped.")
        return False

    recipients = telegram_recipients()
    if not recipients:
        print("No Telegram recipients configured; message skipped.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    delivered = 0
    failed = 0

    for chat_id in recipients:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if r.ok:
                delivered += 1
            else:
                failed += 1
                # Telegram often returns 403 when a user has not started the bot or blocked it.
                print(f"[TELEGRAM FAIL] chat_id={chat_id}: HTTP {r.status_code} {r.text[:250]}")
        except Exception as exc:
            failed += 1
            print(f"[TELEGRAM FAIL] chat_id={chat_id}: {type(exc).__name__}: {exc}")
        time.sleep(0.05)

    print(f"[TELEGRAM] delivered={delivered} failed={failed} total={len(recipients)}")
    return delivered > 0
