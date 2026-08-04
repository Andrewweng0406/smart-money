"""測試 run_watchlist.py 面對「清單裡某幾檔標的分析失敗」時的行為：應該
繼續跑完剩下的標的、把失敗的那幾檔記錄成錯誤列，只有在整份清單全部失敗
時才視為整體失敗（SystemExit）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import analyze
import data_fetcher
import run_watchlist


@pytest.fixture(autouse=True)
def _assume_trading_day(monkeypatch):
    """跟 test_analyze_resilience.py 同樣理由：預設一律當作是交易日，避免
    每個既有測試都要各自 mock，也避免測試真的打網路查 SPY。"""
    monkeypatch.setattr(data_fetcher, "is_market_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(data_fetcher, "current_trading_date_str", lambda: "2026-08-01")


def _fake_result(symbol, spot=100.0):
    return analyze.AnalysisResult(
        symbol=symbol, spot=spot, expiries_used=["2026-09-04"], gex_by_strike=[{"strike": 100, "net_gex": 1}],
        volume_by_strike={}, max_pain=100.0, call_wall=110.0, put_wall=90.0,
        gamma_flip=105.0, gamma_flip_distance_pct=-4.8,
        zero_dte_summary={"total_net_gex": 1, "zero_dte_net_gex": 0, "ex_zero_dte_net_gex": 1, "zero_dte_share_pct": 0.0},
        alert=None,
    )


def test_load_watchlist_reads_symbols(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"symbols": ["TSLA", "NVDA"]}), encoding="utf-8")
    assert run_watchlist.load_watchlist(path) == ["TSLA", "NVDA"]


def test_load_watchlist_raises_on_empty_list(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"symbols": []}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        run_watchlist.load_watchlist(path)


def test_run_one_symbol_returns_error_row_on_failure(monkeypatch, tmp_path):
    def raise_error(symbol, max_expiries, risk_free_rate):
        raise ConnectionError("Yahoo Finance 斷線")

    monkeypatch.setattr(analyze, "fetch_and_aggregate", raise_error)

    row = run_watchlist.run_one_symbol("TSLA", tmp_path, max_expiries=None, risk_free_rate=0.045, use_ai=False)

    assert row["symbol"] == "TSLA"
    assert "error" in row


def test_run_one_symbol_returns_summary_row_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda symbol, max_expiries, risk_free_rate: _fake_result(symbol))
    monkeypatch.setattr("db_manager.save_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "compute_strategy_recommendation", lambda symbol, result: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])  # 避免真的打網路/讀 macro_events.json
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "build_markdown_report", lambda *a, **k: None)

    row = run_watchlist.run_one_symbol("TSLA", tmp_path, max_expiries=None, risk_free_rate=0.045, use_ai=False)

    assert row["symbol"] == "TSLA"
    assert "error" not in row
    assert row["spot"] == 100.0


def test_main_continues_when_one_symbol_fails_others_succeed(monkeypatch, tmp_path):
    """三檔標的裡有一檔失敗，其餘兩檔應該照樣跑完、寫進綜合摘要，整體不視為失敗。"""
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA", "BAD", "NVDA"]}), encoding="utf-8")

    def fake_fetch(symbol, max_expiries, risk_free_rate):
        if symbol == "BAD":
            raise ConnectionError("下市或斷線")
        return _fake_result(symbol)

    monkeypatch.setattr(analyze, "fetch_and_aggregate", fake_fetch)
    monkeypatch.setattr("db_manager.save_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "compute_strategy_recommendation", lambda symbol, result: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])  # 避免真的打網路/讀 macro_events.json
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "build_markdown_report", lambda *a, **k: None)

    import strategy_resolver
    monkeypatch.setattr(strategy_resolver, "resolve_watchlist", lambda *a, **k: [])

    monkeypatch.setattr(sys, "argv", [
        "run_watchlist.py", "--watchlist", str(watchlist_path), "--output-dir", str(tmp_path), "--no-ai",
        "--no-dashboard",  # 避免測試把檔案寫到使用者真實的專案 dashboard/ 目錄
    ])

    run_watchlist.main()  # 不應該拋出例外（不是全部標的都失敗）

    summary_path = tmp_path / f"watchlist_summary_{datetime.now().strftime('%Y%m%d')}.md"
    content = summary_path.read_text(encoding="utf-8")
    assert "❌ BAD" in content
    assert "TSLA" in content
    assert "NVDA" in content


def test_main_resolves_strategy_scorecard_and_notifies(monkeypatch, tmp_path):
    """main() 現在會在每日流程裡自動結算策略追蹤記分板（過去需要另外手動
    執行 strategy_resolver.py，排程從沒真的觸發過這一步）——有結算到東西
    又帶 --notify 時，應該推播合併摘要。
    """
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA"]}), encoding="utf-8")

    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda symbol, max_expiries, risk_free_rate: _fake_result(symbol))
    monkeypatch.setattr("db_manager.save_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "compute_strategy_recommendation", lambda symbol, result: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "build_markdown_report", lambda *a, **k: None)

    import strategy_resolver
    fake_resolved = [{
        "symbol": "TSLA", "strategy_name": "Bull Put Spread", "expiry_date": "2026-08-01",
        "outcome": "WIN", "realized_pnl": 1.5, "settlement_spot": 310.0, "approximate": False,
    }]
    captured_watchlist_arg = {}

    def fake_resolve_watchlist(watchlist_path_arg):
        captured_watchlist_arg["path"] = watchlist_path_arg
        return fake_resolved

    monkeypatch.setattr(strategy_resolver, "resolve_watchlist", fake_resolve_watchlist)

    import telegram_notifier
    send_text_report_mock = MagicMock()
    monkeypatch.setattr(telegram_notifier, "send_text_report", send_text_report_mock)

    monkeypatch.setattr(sys, "argv", [
        "run_watchlist.py", "--watchlist", str(watchlist_path), "--output-dir", str(tmp_path), "--no-ai",
        "--no-dashboard", "--notify",
    ])

    run_watchlist.main()

    assert captured_watchlist_arg["path"] == str(watchlist_path)
    # 兩次呼叫：一次是策略記分板結算摘要，一次是每日watchlist綜合摘要
    assert send_text_report_mock.call_count == 2
    scorecard_call_text = send_text_report_mock.call_args_list[0].args[0]
    assert "Bull Put Spread" in scorecard_call_text


def test_main_exits_with_error_when_all_symbols_fail(monkeypatch, tmp_path):
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA", "NVDA"]}), encoding="utf-8")

    def raise_error(symbol, max_expiries, risk_free_rate):
        raise ConnectionError("Yahoo Finance 整個斷線")

    monkeypatch.setattr(analyze, "fetch_and_aggregate", raise_error)

    import strategy_resolver
    monkeypatch.setattr(strategy_resolver, "resolve_watchlist", lambda *a, **k: [])

    monkeypatch.setattr(sys, "argv", [
        "run_watchlist.py", "--watchlist", str(watchlist_path), "--output-dir", str(tmp_path), "--no-ai",
        "--no-dashboard",
    ])

    with pytest.raises(SystemExit):
        run_watchlist.main()
