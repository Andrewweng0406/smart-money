"""LINE Messaging API 推播——「白話文極端警報」專用的第二通道。

用「廣播（broadcast）」而不是指定對象的「推播（push）」——broadcast 不需要
知道對方的 User ID，直接發給這個官方帳號的所有好友。這是個人使用的交易
監控工具，官方帳號預期只有使用者自己一個好友，broadcast 效果等同於
「傳給我自己」，還省去了「在沒有設定 webhook 的情況下要怎麼查到自己的
User ID」這個麻煩——Messaging API 不像 Telegram Bot API 的 getUpdates
那樣有現成的拉取式端點可以查發送者是誰。

⚠️ 如果這個官方帳號之後加了其他好友，broadcast 會發給所有人，屆時要改成
指定對象的 push（POST /v2/bot/message/push，body 要帶對方的 User ID），
需要另外設定 webhook 才查得到 User ID，這裡先不做。

免費方案每月有訊息則數上限（撰寫時是500則/月），這裡只在「重大極端事件」
才觸發，正常用量不會超過。

對外只提供同步函式，跟 telegram_notifier.py 的設計原則一致；send_line_alert()
失敗都只會記警告、不會拋例外。
"""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("options_gex")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


def send_line_alert(message: str) -> bool:
    """發送一則 LINE 廣播訊息給官方帳號的所有好友。回傳是否成功——呼叫端
    可以用這個記錄結果，但這支函式本身絕不拋出例外（Token失效、服務出狀況
    都只是警告）。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("未設定 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE 推播")
        return False

    try:
        response = requests.post(
            LINE_BROADCAST_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning("LINE 推播失敗（狀態碼 %s）：%s", response.status_code, response.text)
            return False
        logger.info("已推播 LINE 極端警報")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("LINE 推播失敗：%s", exc)
        return False
