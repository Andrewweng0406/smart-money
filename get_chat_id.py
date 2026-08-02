#!/usr/bin/env python3
"""用 .env 裡的 TELEGRAM_BOT_TOKEN 呼叫 getUpdates，列出目前查得到的 Chat ID。

執行前必須先用你自己的 Telegram 帳號傳一則訊息給你的機器人（隨便打字即可），
getUpdates 才會查得到——Telegram 不會主動推播歷史訊息給沒互動過的機器人。

只會印出 Chat ID 跟寄件人資訊，不會印出完整 Bot Token。
"""

from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

if not TOKEN:
    print("❌ .env 裡沒有 TELEGRAM_BOT_TOKEN")
    raise SystemExit(1)

resp = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10)
data = resp.json()

if not data.get("ok"):
    print(f"❌ Telegram API 回應錯誤：{data}")
    raise SystemExit(1)

results = data.get("result", [])
if not results:
    print("⚠️ 查不到任何訊息紀錄——請先用你的 Telegram 帳號傳一則訊息給機器人，再重新執行這支腳本。")
    raise SystemExit(0)

seen = set()
for item in results:
    message = item.get("message") or item.get("channel_post")
    if not message:
        continue
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id in seen:
        continue
    seen.add(chat_id)
    name = chat.get("title") or chat.get("username") or chat.get("first_name") or "(無名稱)"
    print(f"Chat ID: {chat_id}　來源：{chat.get('type')}　{name}")

print("\n把上面對應你自己的那組 Chat ID 填進 .env 的 TELEGRAM_CHAT_ID。")
