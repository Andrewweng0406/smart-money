"""測試 run_watchlist.py 面對「清單裡某幾檔標的分析失敗」時的行為：應該
繼續跑完剩下的標的、把失敗的那幾檔記錄成錯誤列，只有在整份清單全部失敗
時才視為整體失敗（SystemExit）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest

import analyze
import run_watchlist


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

    monkeypatch.setattr(sys, "argv", [
        "run_watchlist.py", "--watchlist", str(watchlist_path), "--output-dir", str(tmp_path), "--no-ai",
        "--no-dashboard",  # 避免測試把檔案寫到使用者真實的 ~/Desktop/stock.agent/dashboard/
    ])

    run_watchlist.main()  # 不應該拋出例外（不是全部標的都失敗）

    summary_path = tmp_path / f"watchlist_summary_{datetime.now().strftime('%Y%m%d')}.md"
    content = summary_path.read_text(encoding="utf-8")
    assert "❌ BAD" in content
    assert "TSLA" in content
    assert "NVDA" in content


def test_main_exits_with_error_when_all_symbols_fail(monkeypatch, tmp_path):
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA", "NVDA"]}), encoding="utf-8")

    def raise_error(symbol, max_expiries, risk_free_rate):
        raise ConnectionError("Yahoo Finance 整個斷線")

    monkeypatch.setattr(analyze, "fetch_and_aggregate", raise_error)
    monkeypatch.setattr(sys, "argv", [
        "run_watchlist.py", "--watchlist", str(watchlist_path), "--output-dir", str(tmp_path), "--no-ai",
    ])

    with pytest.raises(SystemExit):
        run_watchlist.main()
