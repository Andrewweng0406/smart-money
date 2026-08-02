"""把每日報告推播到 Telegram —— 封裝 python-telegram-bot 的非同步 API，
對外只提供同步函式，讓 analyze.py 不用理會 asyncio。

Token/Chat ID 一律從環境變數讀取（見 .env.example），絕不寫死在程式碼裡。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

logger = logging.getLogger("options_gex")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


async def _send_daily_report_async(symbol: str, report_path: Path, png_path: Path) -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    report_text = report_path.read_text(encoding="utf-8")

    # Telegram 文字訊息上限 4096 字元，報告太長時截斷並提示完整檔案位置，
    # 避免 API 直接因超長而報錯。
    if len(report_text) > 4000:
        report_text = report_text[:4000] + f"\n\n...（完整內容請見 {report_path}）"

    async with bot:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=report_text)
        if png_path.exists():
            with png_path.open("rb") as f:
                await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=f, caption=f"{symbol} GEX 圖表")


def send_daily_report(symbol: str, report_path: Path, png_path: Path) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過推播")
        return

    try:
        asyncio.run(_send_daily_report_async(symbol, report_path, png_path))
        logger.info("已推播 %s 報告到 Telegram", symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram 推播失敗：%s", exc)


async def _send_failure_notice_async(symbol: str, error_message: str) -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async with bot:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ {symbol} 分析排程失敗：{error_message}",
        )


def send_failure_notice(symbol: str, error_message: str) -> None:
    """分析失敗時推播一則簡短警示——排程如果整晚沒收到報告，使用者才知道
    是「腳本掛了」而不是「今天沒有訊號」，兩者需要的反應完全不同。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        asyncio.run(_send_failure_notice_async(symbol, error_message))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram 失敗通知推播失敗：%s", exc)


async def _send_text_async(text: str) -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    if len(text) > 4000:
        text = text[:4000] + "\n\n...（內容過長，已截斷，完整內容請見 reports/ 目錄）"
    async with bot:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)


def send_text_report(text: str) -> None:
    """推播純文字報告，不附圖——給多標的 watchlist 綜合摘要用，跟
    send_daily_report（單一標的、附圖）是分開的用途，不強行共用同一支函式。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過推播")
        return

    try:
        asyncio.run(_send_text_async(text))
        logger.info("已推播文字報告到 Telegram")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram 推播失敗：%s", exc)
