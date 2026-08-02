"""呼叫 Claude API，把 GEX 籌碼面 + 當日新聞濃縮成一段盤後綜合評語。

只有設定 ANTHROPIC_API_KEY 時才會執行；沒設定或呼叫失敗都回傳 None——這是
報告裡的加分項，不該因為它掛掉就讓整份報告產不出來（呼叫端 analyze.py 本來
就把它當可選欄位處理）。
"""

from __future__ import annotations

import logging
import os

import anthropic
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("options_gex")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = "claude-opus-5"


def _fetch_headlines(symbol: str, limit: int = 5) -> list[str]:
    """抓最近幾則新聞標題+摘要，餵給 Claude 當背景資訊。"""
    try:
        news = yf.Ticker(symbol).news or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("抓取 %s 新聞失敗：%s", symbol, exc)
        return []

    headlines = []
    for item in news[:limit]:
        content = item.get("content", {})
        title = content.get("title")
        summary = content.get("summary", "")
        if title:
            headlines.append(f"- {title}：{summary}" if summary else f"- {title}")
    return headlines


def generate_commentary(
    symbol: str, spot: float, max_pain: float, call_wall: float, put_wall: float,
    gamma_flip: float | None, alert: str | None,
) -> str | None:
    """回傳 200 字內的繁體中文『盤後籌碼與新聞綜合評語』；無法產生時回傳 None。"""
    if not ANTHROPIC_API_KEY:
        logger.info("未設定 ANTHROPIC_API_KEY，略過 AI 綜合評語")
        return None

    headlines = _fetch_headlines(symbol)
    news_block = "\n".join(headlines) if headlines else "（今日無相關新聞）"
    flip_text = f"${gamma_flip:.0f}" if gamma_flip is not None else "無法計算"

    prompt = f"""你是一位專業的美股期權籌碼面分析師。請根據以下當日 {symbol} 的籌碼面數據與新聞，
用繁體中文寫一段 200 字以內的『盤後籌碼與新聞綜合評語』，說明籌碼結構與新聞事件可能對後續股價的影響。
不要條列，寫成一段自然的分析文字。

籌碼面數據：
- 現貨價：${spot:.2f}
- Max Pain：${max_pain:.0f}
- Call Wall：${call_wall:.0f}
- Put Wall：${put_wall:.0f}
- Gamma 翻轉點：{flip_text}
- 系統警示：{alert or '無'}

近期新聞：
{news_block}
"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        # 刻意接住所有例外，不只是 anthropic.APIStatusError / APIConnectionError——
        # 實測過 .env 裡如果不慎留著範例佔位字串（含中文字元）當 API Key，
        # httpx 組 header 時會直接丟出 UnicodeEncodeError，這種發生在送出
        # 請求「之前」的錯誤不屬於任何一種 anthropic SDK 的例外類別。這段是
        # 報告的加分項，任何失敗都該優雅跳過，不能讓整支 analyze.py 掛掉。
        logger.warning("Claude API 呼叫失敗：%s", exc)
        return None

    if response.stop_reason == "refusal":
        logger.warning("Claude 拒絕產生評語（stop_reason=refusal）")
        return None

    text = next((block.text for block in response.content if block.type == "text"), "")
    return text.strip() or None
