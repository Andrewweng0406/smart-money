"""telegram_bot_listener.py 測試——同步輔助函式用一般 mock 測，async
指令處理函式用 asyncio.run() 執行（不需要 pytest-asyncio 外掛），
supervisor 迴圈用受控次數的 side_effect 測試「壞掉後會重啟」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import telegram_bot_listener as bot


def _run(coro):
    return asyncio.run(coro)


def _fake_update_and_context(args=None):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    context = MagicMock()
    context.args = args or []
    return update, context


# ---------- _truncate ----------

def test_truncate_leaves_short_text_unchanged():
    assert bot._truncate("hello") == "hello"


def test_truncate_cuts_long_text_with_notice():
    text = "a" * 5000
    result = bot._truncate(text, limit=100)
    assert len(result) < 5000
    assert "已截斷" in result


# ---------- _run_report_sync / _run_watchlist_sync (同步輔助函式) ----------

def test_run_report_sync_returns_text_and_chart_path(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "OUTPUT_DIR", tmp_path)
    fake_result = MagicMock(spot=100.0, max_pain=100.0, call_wall=110.0, put_wall=90.0, gamma_flip=None, alert=None)

    import analyze
    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda *a, **k: fake_result)
    monkeypatch.setattr(analyze, "compute_strategy_recommendation", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda *a, **k: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "build_markdown_report", lambda result, path, **k: path.write_text("報告內容", encoding="utf-8"))

    import db_manager
    monkeypatch.setattr(db_manager, "save_snapshot", lambda *a, **k: None)

    import ai_analyst
    monkeypatch.setattr(ai_analyst, "generate_commentary", lambda **k: None)

    report_text, chart_path = bot._run_report_sync("TSLA")

    assert report_text == "報告內容"
    assert chart_path.suffix == ".png"


def test_run_report_sync_survives_db_write_failure(monkeypatch, tmp_path):
    """歷史資料庫寫入失敗不該讓 /report 指令整個失敗——這是加分項，機器人
    指令的核心是「回傳分析結果」，不是「順便寫資料庫」。
    """
    monkeypatch.setattr(bot, "OUTPUT_DIR", tmp_path)
    fake_result = MagicMock(spot=100.0, max_pain=100.0, call_wall=110.0, put_wall=90.0, gamma_flip=None, alert=None)

    import analyze
    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda *a, **k: fake_result)
    monkeypatch.setattr(analyze, "compute_strategy_recommendation", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda *a, **k: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "build_markdown_report", lambda result, path, **k: path.write_text("報告內容", encoding="utf-8"))

    import db_manager
    monkeypatch.setattr(db_manager, "save_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    import ai_analyst
    monkeypatch.setattr(ai_analyst, "generate_commentary", lambda **k: None)

    report_text, chart_path = bot._run_report_sync("TSLA")  # 不應該拋出例外
    assert report_text == "報告內容"


def test_run_watchlist_sync_aggregates_summaries(monkeypatch, tmp_path):
    import run_watchlist
    monkeypatch.setattr(run_watchlist, "load_watchlist", lambda path: ["TSLA", "NVDA"])
    monkeypatch.setattr(run_watchlist, "run_one_symbol", lambda symbol, **k: {
        "symbol": symbol, "spot": 100.0, "max_pain": 100.0, "call_wall": 110.0, "put_wall": 90.0,
        "gamma_flip": None, "alert": None, "strategy_name": "N/A", "mm_pressure": None,
    })
    monkeypatch.setattr(run_watchlist, "build_watchlist_summary", lambda summaries: f"共{len(summaries)}檔標的")

    result = bot._run_watchlist_sync()
    assert result == "共2檔標的"


# ---------- async command handlers ----------

def test_report_command_defaults_to_tsla_and_sends_text_and_photo(monkeypatch, tmp_path):
    chart_path = tmp_path / "chart.png"
    chart_path.write_bytes(b"fake png bytes")
    monkeypatch.setattr(bot, "_run_report_sync", lambda symbol: (f"{symbol} 報告內容", chart_path))

    update, context = _fake_update_and_context(args=[])
    _run(bot.report_command(update, context))

    # 第一次呼叫是「正在分析」提示，第二次才是真正的報告內容
    assert update.message.reply_text.call_count == 2
    first_call_text = update.message.reply_text.call_args_list[0].args[0]
    second_call_text = update.message.reply_text.call_args_list[1].args[0]
    assert "TSLA" in first_call_text
    assert "TSLA 報告內容" == second_call_text
    update.message.reply_photo.assert_called_once()


def test_report_command_uses_symbol_from_args(monkeypatch, tmp_path):
    captured = {}

    def fake_run_report_sync(symbol):
        captured["symbol"] = symbol
        return "報告", tmp_path / "nonexistent.png"

    monkeypatch.setattr(bot, "_run_report_sync", fake_run_report_sync)

    update, context = _fake_update_and_context(args=["nvda"])
    _run(bot.report_command(update, context))

    assert captured["symbol"] == "NVDA"  # 應該自動轉大寫
    update.message.reply_photo.assert_not_called()  # 檔案不存在就不該嘗試傳圖


def test_report_command_replies_error_on_failure_without_raising(monkeypatch):
    monkeypatch.setattr(bot, "_run_report_sync", lambda symbol: (_ for _ in ()).throw(ConnectionError("Yahoo斷線")))

    update, context = _fake_update_and_context(args=["TSLA"])
    _run(bot.report_command(update, context))  # 不應該拋出例外

    last_call_text = update.message.reply_text.call_args_list[-1].args[0]
    assert "失敗" in last_call_text


def test_watchlist_command_sends_summary(monkeypatch):
    monkeypatch.setattr(bot, "_run_watchlist_sync", lambda: "watchlist摘要內容")

    update, context = _fake_update_and_context()
    _run(bot.watchlist_command(update, context))

    last_call_text = update.message.reply_text.call_args_list[-1].args[0]
    assert last_call_text == "watchlist摘要內容"


def test_watchlist_command_replies_error_on_failure_without_raising(monkeypatch):
    monkeypatch.setattr(bot, "_run_watchlist_sync", lambda: (_ for _ in ()).throw(ValueError("boom")))

    update, context = _fake_update_and_context()
    _run(bot.watchlist_command(update, context))  # 不應該拋出例外

    last_call_text = update.message.reply_text.call_args_list[-1].args[0]
    assert "失敗" in last_call_text


def test_backtest_command_defaults_to_tsla(monkeypatch):
    captured = {}

    def fake_generate_report(symbol):
        captured["symbol"] = symbol
        return "回測報告內容"

    import backtester
    monkeypatch.setattr(backtester, "generate_backtest_report", fake_generate_report)

    update, context = _fake_update_and_context(args=[])
    _run(bot.backtest_command(update, context))

    assert captured["symbol"] == "TSLA"
    last_call_text = update.message.reply_text.call_args_list[-1].args[0]
    assert last_call_text == "回測報告內容"


# ---------- 自然語言意圖判斷（interpret_intent） ----------

def test_interpret_intent_returns_unknown_without_api_key(monkeypatch):
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", "")
    result = _run(bot.interpret_intent("幫我看一下TSLA"))
    assert result.action == "unknown"


def test_interpret_intent_returns_unknown_on_api_failure(monkeypatch):
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", "fake-key")
    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(side_effect=ConnectionError("斷線"))

    with patch("telegram_bot_listener.anthropic.AsyncAnthropic", return_value=fake_client):
        result = _run(bot.interpret_intent("幫我看一下TSLA"))  # 不應該拋出例外

    assert result.action == "unknown"


def test_interpret_intent_returns_parsed_result_on_success(monkeypatch):
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", "fake-key")
    fake_response = MagicMock(parsed_output=bot.BotIntent(action="report", symbol="TSLA"))
    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    with patch("telegram_bot_listener.anthropic.AsyncAnthropic", return_value=fake_client):
        result = _run(bot.interpret_intent("幫我看一下特斯拉"))

    assert result.action == "report"
    assert result.symbol == "TSLA"


# ---------- 自然語言意圖分派（natural_language_handler） ----------

def test_natural_language_handler_dispatches_to_report(monkeypatch):
    monkeypatch.setattr(bot, "interpret_intent", AsyncMock(return_value=bot.BotIntent(action="report", symbol="NVDA")))
    handle_report_mock = AsyncMock()
    monkeypatch.setattr(bot, "_handle_report", handle_report_mock)

    update, context = _fake_update_and_context()
    update.message.text = "幫我看一下輝達"
    _run(bot.natural_language_handler(update, context))

    handle_report_mock.assert_called_once_with(update, "NVDA")


def test_natural_language_handler_dispatches_to_report_default_symbol(monkeypatch):
    """Claude 判斷是 report 意圖但抓不出代號時，預設查 TSLA（跟指令版一致）。"""
    monkeypatch.setattr(bot, "interpret_intent", AsyncMock(return_value=bot.BotIntent(action="report", symbol=None)))
    handle_report_mock = AsyncMock()
    monkeypatch.setattr(bot, "_handle_report", handle_report_mock)

    update, context = _fake_update_and_context()
    update.message.text = "幫我看一下現在的行情"
    _run(bot.natural_language_handler(update, context))

    handle_report_mock.assert_called_once_with(update, "TSLA")


def test_natural_language_handler_dispatches_to_watchlist(monkeypatch):
    monkeypatch.setattr(bot, "interpret_intent", AsyncMock(return_value=bot.BotIntent(action="watchlist")))
    handle_watchlist_mock = AsyncMock()
    monkeypatch.setattr(bot, "_handle_watchlist", handle_watchlist_mock)

    update, context = _fake_update_and_context()
    update.message.text = "幫我看一下整份清單"
    _run(bot.natural_language_handler(update, context))

    handle_watchlist_mock.assert_called_once_with(update)


def test_natural_language_handler_dispatches_to_backtest(monkeypatch):
    monkeypatch.setattr(bot, "interpret_intent", AsyncMock(return_value=bot.BotIntent(action="backtest", symbol="TSLA")))
    handle_backtest_mock = AsyncMock()
    monkeypatch.setattr(bot, "_handle_backtest", handle_backtest_mock)

    update, context = _fake_update_and_context()
    update.message.text = "TSLA的歷史勝率如何"
    _run(bot.natural_language_handler(update, context))

    handle_backtest_mock.assert_called_once_with(update, "TSLA")


def test_natural_language_handler_dispatches_to_help(monkeypatch):
    monkeypatch.setattr(bot, "interpret_intent", AsyncMock(return_value=bot.BotIntent(action="help")))

    update, context = _fake_update_and_context()
    update.message.text = "你能做什麼"
    _run(bot.natural_language_handler(update, context))

    update.message.reply_text.assert_called_once_with(bot.HELP_TEXT)


def test_natural_language_handler_replies_hint_on_unknown_intent(monkeypatch):
    monkeypatch.setattr(bot, "interpret_intent", AsyncMock(return_value=bot.BotIntent(action="unknown")))

    update, context = _fake_update_and_context()
    update.message.text = "今天天氣真好"
    _run(bot.natural_language_handler(update, context))

    reply_text = update.message.reply_text.call_args.args[0]
    assert "/help" in reply_text


def test_natural_language_handler_ignores_empty_text(monkeypatch):
    interpret_mock = AsyncMock()
    monkeypatch.setattr(bot, "interpret_intent", interpret_mock)

    update, context = _fake_update_and_context()
    update.message.text = "   "
    _run(bot.natural_language_handler(update, context))

    interpret_mock.assert_not_called()


def test_unknown_command_replies_help_hint():
    update, context = _fake_update_and_context()
    _run(bot.unknown_command(update, context))
    update.message.reply_text.assert_called_once()
    assert "/help" in update.message.reply_text.call_args.args[0]


def test_start_command_sends_help_text():
    update, context = _fake_update_and_context()
    _run(bot.start_command(update, context))
    update.message.reply_text.assert_called_once_with(bot.HELP_TEXT)


# ---------- supervisor 迴圈（main） ----------

def test_main_exits_without_token(monkeypatch):
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit):
        bot.main()


def test_main_restarts_after_unexpected_exception(monkeypatch):
    """_build_and_run_once 第一次拋出例外、第二次正常返回，main() 應該
    自動重試一次而不是直接讓程式掛掉；重試前會 sleep(5)。
    """
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "fake-token")
    call_count = {"n": 0}

    def fake_build_and_run(token):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("網路掛了")
        return None  # 第二次正常結束

    monkeypatch.setattr(bot, "_build_and_run_once", fake_build_and_run)

    with patch("telegram_bot_listener.time.sleep") as mock_sleep:
        bot.main()

    assert call_count["n"] == 2
    mock_sleep.assert_called_once_with(5)


def test_main_stops_cleanly_on_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(bot, "_build_and_run_once", lambda token: (_ for _ in ()).throw(KeyboardInterrupt()))

    with patch("telegram_bot_listener.time.sleep") as mock_sleep:
        bot.main()  # 不應該拋出例外，應該乾淨結束

    mock_sleep.assert_not_called()  # KeyboardInterrupt不該觸發重試等待
