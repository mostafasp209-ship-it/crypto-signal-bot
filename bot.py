import os
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send(msg):
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})

send("✅ تست موفق! ربات آماده است.")
print("Done!")
