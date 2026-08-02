"""沒設定 Token/Chat ID，或 Telegram API 呼叫失敗時，都不該讓排程腳本掛掉——
推播本來就是錦上添花，report/chart 已經寫到磁碟上，這一步失敗不該回頭
影響前面已經完成的工作。
"""

from __future__ import annotations

from unittest.mock import patch

import telegram_notifier


def test_send_daily_report_skips_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_CHAT_ID", "")

    report_path = tmp_path / "report.md"
    report_path.write_text("test", encoding="utf-8")
    png_path = tmp_path / "chart.png"

    # 不應該拋出例外，即使 png 檔案根本不存在
    telegram_notifier.send_daily_report("TSLA", report_path, png_path)


def test_send_daily_report_swallows_telegram_api_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_CHAT_ID", "fake-chat-id")
    # 用普通函式取代 async 函式，避免建立出真正的 coroutine 物件卻沒被
    # await（那樣會觸發 Python 的 RuntimeWarning，雖然無害但會污染測試輸出）。
    monkeypatch.setattr(telegram_notifier, "_send_daily_report_async", lambda *a, **k: None)

    report_path = tmp_path / "report.md"
    report_path.write_text("test", encoding="utf-8")
    png_path = tmp_path / "chart.png"

    with patch("telegram_notifier.asyncio.run", side_effect=ConnectionError("Telegram 斷線")):
        telegram_notifier.send_daily_report("TSLA", report_path, png_path)  # 不應該拋出例外


def test_send_failure_notice_skips_without_credentials(monkeypatch):
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_CHAT_ID", "")

    telegram_notifier.send_failure_notice("TSLA", "some error")  # 不應該拋出例外


def test_send_text_report_skips_without_credentials(monkeypatch):
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_CHAT_ID", "")

    telegram_notifier.send_text_report("watchlist 摘要內容")  # 不應該拋出例外


def test_send_text_report_swallows_telegram_api_errors(monkeypatch):
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_CHAT_ID", "fake-chat-id")
    monkeypatch.setattr(telegram_notifier, "_send_text_async", lambda *a, **k: None)

    with patch("telegram_notifier.asyncio.run", side_effect=ConnectionError("Telegram 斷線")):
        telegram_notifier.send_text_report("watchlist 摘要內容")  # 不應該拋出例外
