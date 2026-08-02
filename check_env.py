#!/usr/bin/env python3
"""檢查 .env 裡的金鑰格式對不對——只顯示遮罩後的片段（前幾碼+後幾碼），
不會把完整金鑰印出來或留在終端機捲動紀錄裡，可以放心執行、甚至截圖問人。

用法：
    python check_env.py
"""

from __future__ import annotations

import os
import re

from dotenv import load_dotenv

load_dotenv()


def _mask(value: str) -> str:
    if len(value) <= 4:
        return f"{value[:1]}***（長度{len(value)}，可能太短或是貼錯）"
    if len(value) <= 10:
        return f"{value[:2]}...{value[-2:]}（長度{len(value)}）"
    return f"{value[:6]}...{value[-4:]}（長度{len(value)}）"


def _check(name: str, value: str, pattern: str, required: bool, hint: str) -> None:
    if not value:
        status = "❌ 未設定" if required else "⚠️  未設定（選填，會自動跳過對應功能）"
        print(f"{name}: {status}")
        return
    ok = re.match(pattern, value) is not None
    mark = "✅" if ok else "❌ 格式看起來不對"
    print(f"{name}: {mark}　{_mask(value)}")
    if not ok:
        print(f"  提示：{hint}")


if __name__ == "__main__":
    _check(
        "TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        r"^\d{6,}:[A-Za-z0-9_-]{30,}$", required=True,
        hint="格式應為「數字:英數字混合」，例如 123456789:ABC-defGhIJKlmNoPQRstuVwxyZ（跟 @BotFather 拿的）",
    )
    _check(
        "TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        r"^-?\d+$", required=True,
        hint="應該是純數字（群組聊天室通常是負數），從 getUpdates 回傳 JSON 裡的 chat.id 複製",
    )
    _check(
        "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        r"^sk-ant-", required=False,
        hint="應該以 sk-ant- 開頭（在 console.anthropic.com 的 API Keys 頁面建立）",
    )
    _check(
        "LINE_CHANNEL_ACCESS_TOKEN", os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip(),
        r"^[A-Za-z0-9+/]{50,}={0,2}$", required=False,
        hint="LINE Messaging API 的 Channel Access Token，通常是100碼以上的base64字串"
             "（在 developers.line.biz/console 的 Messaging API 頻道設定頁「發行」）。",
    )
