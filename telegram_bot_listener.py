#!/usr/bin/env python3
"""互動式 Telegram 機器人——長輪詢（polling）常駐執行，讓使用者能隨時在
Telegram 裡打指令主動要資料，不用等每天排程、也不用自己開終端機跑腳本。

跟其他排程型腳本（run.sh/analyze.py）的關鍵差異：這支「絕對不能中途死掉」
——常駐服務死了，使用者不會馬上發現（不像排程腳本失敗當天日誌就看得出來），
所以額外做了兩層防護：

1. 最外層 supervisor 迴圈：run_polling() 內部本來就會重試 Telegram API的
   暫時性錯誤（網路微斷線、逾時），但萬一發生更嚴重的例外導致整個
   run_polling() 直接掛掉、跳出來，這裡會記警告、等5秒、重新建立一個全新
   的 Application 再跑一次，而不是讓程式就此死掉、需要人工重開機。
2. 每個指令處理都用 asyncio.to_thread() 把耗時的分析工作（GEX計算、畫圖、
   Claude API）丟到背景執行緒——analyze.py 的核心函式都是同步（blocking）
   寫的，直接在 async handler 裡呼叫會卡住整個事件迴圈，讓其他指令（甚至
   同一個使用者的下一句話）都要排隊等好幾秒才有反應。這個「絕不在事件
   迴圈上做阻塞呼叫」的慣例，這個專案在 yfinance_client.py 就已經立下過，
   這裡延續同一個做法。指令一進來會先秒回一句「正在分析...」，處理完才
   把結果送出，避免使用者以為 Bot 沒反應。

除了 /report、/watchlist、/backtest 這些精確指令，也支援直接打一般口語
（例如「幫我看一下特斯拉」「TSLA現在怎麼樣」）——用 Claude 判斷使用者想
做哪一種操作、標的代號是什麼，再導去跟指令共用的同一段處理邏輯
（_handle_report/_handle_watchlist/_handle_backtest）。沒設定
ANTHROPIC_API_KEY 或判斷失敗時，會請使用者改用斜線指令，不會讓整個
Bot 掛掉。

用法：
    python telegram_bot_listener.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import analyze
import backtester
import run_watchlist

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")

# httpx 預設的 INFO log 會把完整請求URL印出來——Telegram Bot API 的網址
# 本身就長 https://api.telegram.org/bot<TOKEN>/xxx，token 直接寫在路徑裡。
# 這代表只要開著 INFO log 等級不管，Bot Token 就會整組被印進日誌檔／終端機，
# 等同於把密鑰外洩。這裡把 httpx（以及底層的 httpcore）調到 WARNING，
# 避免這個管道洩漏 token。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
OUTPUT_DIR = Path("reports")
INTENT_MODEL = "claude-opus-5"

HELP_TEXT = (
    "嗨！我是 stock.agent 的互動機器人，可以直接跟我口語對話（例如「幫我看一下"
    "特斯拉」「TSLA現在怎麼樣」），也可以用精確指令：\n\n"
    "/report <代號> - 立即分析單一標的（不指定代號預設 TSLA）\n"
    "/watchlist - 立即分析整份 watchlist.json\n"
    "/backtest <代號> - 歷史籌碼模型回測統計\n"
    "/help - 顯示這則說明"
)


class BotIntent(BaseModel):
    """自然語言意圖判斷結果——Claude 把使用者的口語訊息轉成結構化的動作。"""
    action: Literal["report", "watchlist", "backtest", "help", "unknown"]
    symbol: Optional[str] = None


INTENT_SYSTEM_PROMPT = (
    "你是 stock.agent 這個美股期權分析機器人的指令判讀助手。使用者會用一般口語"
    "跟你說話，你要判斷他想做哪一種操作：\n"
    "- report：查詢單一標的的GEX籌碼分析（例如「幫我看一下TSLA」「特斯拉現在"
    "怎麼樣」）。symbol欄位填美股代號，中文公司名稱要轉換成代號（特斯拉→TSLA、"
    "輝達→NVDA、蘋果→AAPL等），看不出來要查哪支就留空。\n"
    "- watchlist：查詢整份追蹤清單的綜合摘要（例如「幫我看一下整份清單」）。\n"
    "- backtest：查詢某標的的歷史回測統計（例如「TSLA的歷史勝率如何」）。\n"
    "- help：使用者在問這個機器人能做什麼、怎麼用。\n"
    "- unknown：看不出來是上面哪一種，或者他在閒聊、問跟股票分析無關的問題。\n"
    "只要判斷意圖，不要自己回答股票問題。"
)


def _run_report_sync(symbol: str) -> tuple[str, Path]:
    """同步跑一次完整分析，回傳（報告文字, 圖表PNG路徑）。這支函式本身是
    同步的，故意設計成用 asyncio.to_thread() 丟到背景執行緒呼叫，不要在
    async handler 裡直接呼叫，否則會卡住事件迴圈。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = analyze.fetch_and_aggregate(symbol, max_expiries=8, risk_free_rate=0.045)

    try:
        import db_manager
        db_manager.save_snapshot(result, datetime.now().strftime("%Y-%m-%d"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 寫入歷史資料庫失敗：%s", symbol, exc)

    strategy = analyze.compute_strategy_recommendation(symbol, result)
    macro_warnings = analyze.get_macro_warnings(symbol)

    import ai_analyst
    ai_commentary = ai_analyst.generate_commentary(
        symbol=symbol, spot=result.spot, max_pain=result.max_pain,
        call_wall=result.call_wall, put_wall=result.put_wall,
        gamma_flip=result.gamma_flip, alert=result.alert,
    )

    date_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    chart_path = OUTPUT_DIR / f"gex_chart_{symbol}_{date_tag}_bot"
    report_path = OUTPUT_DIR / f"daily_report_{symbol}_{date_tag}_bot.md"
    analyze.build_chart(result, chart_path)
    analyze.build_markdown_report(
        result, report_path, ai_commentary=ai_commentary, strategy=strategy, macro_warnings=macro_warnings,
    )
    return report_path.read_text(encoding="utf-8"), chart_path.with_suffix(".png")


def _run_watchlist_sync() -> str:
    """同步跑一次整份 watchlist，回傳綜合摘要文字。跟排程用的
    run_watchlist.main() 不同：這裡 notify=False（機器人的回覆本身就是
    通知，不需要再觸發一次獨立的 Telegram/LINE 推播）、dashboard_dir=None
    （即時查詢不需要順便更新儀表板檔案）。
    """
    symbols = run_watchlist.load_watchlist(Path("watchlist.json"))
    summaries = [
        run_watchlist.run_one_symbol(
            symbol, output_dir=OUTPUT_DIR, max_expiries=8, risk_free_rate=0.045,
            use_ai=True, dashboard_dir=None, notify=False,
        )
        for symbol in symbols
    ]
    return run_watchlist.build_watchlist_summary(summaries)


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...（內容過長，已截斷）"


async def _handle_report(update: Update, symbol: str) -> None:
    """/report 指令跟自然語言的「查某標的」共用這段——精確指令跟口語判斷出來
    的意圖，最後都是同一套分析流程，不該各寫一份。
    """
    await update.message.reply_text(f"⏳ 正在分析 {symbol}，請稍候...")

    try:
        report_text, chart_path = await asyncio.to_thread(_run_report_sync, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("分析 %s 失敗：%s", symbol, exc)
        await update.message.reply_text(f"❌ 分析 {symbol} 失敗：{exc}")
        return

    await update.message.reply_text(_truncate(report_text))
    if chart_path.exists():
        with chart_path.open("rb") as f:
            await update.message.reply_photo(photo=f, caption=f"{symbol} GEX 圖表")


async def _handle_watchlist(update: Update) -> None:
    await update.message.reply_text("⏳ 正在分析整份 watchlist，請稍候（標的較多時可能需要一點時間）...")

    try:
        summary_text = await asyncio.to_thread(_run_watchlist_sync)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchlist 分析失敗：%s", exc)
        await update.message.reply_text(f"❌ Watchlist 分析失敗：{exc}")
        return

    await update.message.reply_text(_truncate(summary_text))


async def _handle_backtest(update: Update, symbol: str) -> None:
    await update.message.reply_text(f"⏳ 正在查詢 {symbol} 的歷史回測統計...")

    try:
        report_text = await asyncio.to_thread(backtester.generate_backtest_report, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查詢 %s 回測失敗：%s", symbol, exc)
        await update.message.reply_text(f"❌ 查詢失敗：{exc}")
        return

    await update.message.reply_text(_truncate(report_text))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = context.args[0].upper() if context.args else "TSLA"
    await _handle_report(update, symbol)


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_watchlist(update)


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = context.args[0].upper() if context.args else "TSLA"
    await _handle_backtest(update, symbol)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("看不懂這個指令，輸入 /help 查看可用指令。")


async def interpret_intent(text: str) -> BotIntent:
    """用 Claude 把口語訊息轉成結構化的 BotIntent。沒設定 API Key 或呼叫
    失敗都回傳 action="unknown"，讓呼叫端統一導去「請用指令」的提示，
    不會讓自然語言判斷這個加分功能拖垮整個 Bot。
    """
    if not ANTHROPIC_API_KEY:
        return BotIntent(action="unknown")

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.parse(
            model=INTENT_MODEL, max_tokens=200,
            system=INTENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
            output_format=BotIntent,
        )
        return response.parsed_output
    except Exception as exc:  # noqa: BLE001
        logger.warning("自然語言意圖判斷失敗：%s", exc)
        return BotIntent(action="unknown")


async def natural_language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """處理一般口語訊息（非斜線指令）——先問 Claude 這句話是想做哪一種操作，
    再導去跟指令共用的同一套處理函式。
    """
    text = (update.message.text or "").strip()
    if not text:
        return

    intent = await interpret_intent(text)

    if intent.action == "report":
        await _handle_report(update, (intent.symbol or "TSLA").upper())
    elif intent.action == "watchlist":
        await _handle_watchlist(update)
    elif intent.action == "backtest":
        await _handle_backtest(update, (intent.symbol or "TSLA").upper())
    elif intent.action == "help":
        await update.message.reply_text(HELP_TEXT)
    else:
        await update.message.reply_text(
            "不太確定你想查哪支標的或想做什麼～可以直接說「幫我看一下TSLA」"
            "「特斯拉現在怎麼樣」，或輸入 /help 看所有指令。"
        )


def _build_and_run_once(token: str) -> None:
    """建立一個全新的 Application 並開始長輪詢——每次 supervisor 迴圈重啟
    都會重新呼叫這支函式，不共用上一次可能已經壞掉的 Application 實例。

    這裡刻意在建立 Application 之前先 new 一個全新的 asyncio event loop：
    run_polling() 內部是用 asyncio.get_event_loop() 去抓「目前這條執行緒的
    loop」，而且結束時（不管是正常收工還是中途炸掉）預設會把這個 loop
    關掉（close_loop=True）。如果 supervisor 迴圈重試時沿用同一條 loop，
    第二次呼叫 run_polling() 抓到的會是上一輪已經被關閉的 loop，任何操作
    都會立刻死在「Event loop is closed」——這是實測抓到的真實 bug，不是
    猜測。每輪重試前手動建一個乾淨的新 loop 並設為目前執行緒的 loop，
    就能讓每一輪重試都拿到乾淨可用的 loop。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("backtest", backtest_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    # 放在所有 CommandHandler 之後——filters.COMMAND 那個 handler 已經攔截了
    # 所有斜線指令（包括打錯的），這裡只會接到不是斜線指令的一般口語訊息。
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_handler))

    logger.info("Telegram 互動機器人啟動，開始長輪詢...")
    application.run_polling(drop_pending_updates=True)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("未設定 TELEGRAM_BOT_TOKEN，無法啟動互動機器人")
        raise SystemExit(1)

    while True:
        try:
            _build_and_run_once(TELEGRAM_BOT_TOKEN)
            break  # run_polling() 正常返回（例如收到終止訊號優雅關閉），不用重啟
        except KeyboardInterrupt:
            logger.info("收到中斷訊號，機器人停止")
            break
        except Exception as exc:  # noqa: BLE001
            # run_polling() 內部已經會重試網路微斷線/API暫時性錯誤，會跑到
            # 這裡代表是更嚴重的未預期例外，5秒後從頭重建整個Application
            # 再試一次，而不是讓常駐服務就此死掉、需要人工重開機。
            logger.error("Telegram 機器人發生未預期例外，5秒後重新啟動：%s", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
