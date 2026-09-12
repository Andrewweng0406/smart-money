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
import db_manager
import run_watchlist


@pytest.fixture(autouse=True)
def _assume_trading_day(monkeypatch):
    """跟 test_analyze_resilience.py 同樣理由：預設一律當作是交易日，避免
    每個既有測試都要各自 mock，也避免測試真的打網路查 SPY。"""
    monkeypatch.setattr(data_fetcher, "is_market_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(data_fetcher, "current_trading_date_str", lambda: "2026-08-01")
    monkeypatch.setattr(db_manager, "get_recent_snapshots", lambda *a, **k: [])


def _fake_result(symbol, spot=100.0, pinning=None):
    return analyze.AnalysisResult(
        symbol=symbol, spot=spot, expiries_used=["2026-09-04"], gex_by_strike=[{"strike": 100, "net_gex": 1}],
        volume_by_strike={}, max_pain=100.0, call_wall=110.0, put_wall=90.0,
        gamma_flip=105.0, gamma_flip_distance_pct=-4.8,
        zero_dte_summary={"total_net_gex": 1, "zero_dte_net_gex": 0, "ex_zero_dte_net_gex": 1, "zero_dte_share_pct": 0.0},
        alert=None, pinning=pinning,
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


def test_run_one_symbol_persists_decision_with_daily_snapshot(monkeypatch, tmp_path):
    result = _fake_result("TSLA")
    captured = {}
    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda *a, **k: result)
    monkeypatch.setattr(db_manager, "save_snapshot", lambda saved, *a, **k: captured.update(decision=saved.decision))
    monkeypatch.setattr(analyze, "compute_strategy_recommendation", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "build_markdown_report", lambda *a, **k: None)

    row = run_watchlist.run_one_symbol(
        "TSLA", tmp_path, max_expiries=None, risk_free_rate=0.045, use_ai=False,
    )

    assert captured["decision"] == row["decision"]
    assert captured["decision"]["action"]


def test_compare_decisions_marks_confidence_gate_release():
    current = {"action": "區間應對，不追方向", "confidence": "中"}
    previous = {
        "date": "2026-07-31", "decision_action": "事件前觀望",
        "decision_confidence": "低",
    }

    change = run_watchlist.compare_decisions(current, previous)

    assert change["changed"] is True
    assert change["kind"] == "gate_released"
    assert "事件前觀望 → 區間應對，不追方向" in change["text"]


def test_compare_decisions_keeps_unchanged_posture_quiet():
    current = {"action": "區間應對，不追方向", "confidence": "中"}
    previous = {
        "date": "2026-07-31", "decision_action": "區間應對，不追方向",
        "decision_confidence": "中",
    }

    change = run_watchlist.compare_decisions(current, previous)

    assert change["changed"] is False
    assert change["kind"] == "unchanged"


def test_load_previous_decision_skips_same_day_rerun(monkeypatch):
    monkeypatch.setattr(
        db_manager, "get_recent_snapshots", lambda symbol, limit=10: [
            {"date": "2026-08-01", "decision_action": "區間應對，不追方向"},
            {"date": "2026-07-31", "decision_action": "事件前觀望"},
        ],
    )

    previous = run_watchlist.load_previous_decision_snapshot("TSLA", "2026-08-01")

    assert previous["date"] == "2026-07-31"


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


# ---------- 開盤盤中摘要（--intraday-summary） ----------

def test_build_intraday_summary_line_includes_pinning():
    result = _fake_result("TSLA", spot=328.58, pinning={
        "pin_strike": 350.0, "oi_concentration_pct": 4.5, "in_positive_gamma": True,
        "score": 35, "regime": "NEUTRAL",
    })
    line = run_watchlist.build_intraday_summary_line("TSLA", result)
    assert "TSLA" in line
    assert "328.58" in line
    assert "Pinning" in line
    assert "35/100" in line


def test_build_intraday_summary_line_without_pinning_omits_pinning_line():
    """pinning 是加分項，None 時整行 Pinning 資訊消失，不留 N/A 佔位。"""
    result = _fake_result("TSLA", pinning=None)
    line = run_watchlist.build_intraday_summary_line("TSLA", result)
    assert "Pinning" not in line


def test_run_intraday_summary_continues_when_one_symbol_fails(monkeypatch):
    def fake_fetch(symbol, max_expiries, risk_free_rate):
        if symbol == "NVDA":
            raise ConnectionError("Yahoo Finance 斷線")
        return _fake_result(symbol)

    monkeypatch.setattr(analyze, "fetch_and_aggregate", fake_fetch)

    text = run_watchlist.run_intraday_summary(["TSLA", "NVDA"], max_expiries=4, risk_free_rate=0.045)

    assert "TSLA" in text
    assert "NVDA" in text
    assert "分析失敗" in text


def test_run_intraday_summary_sends_telegram_when_notify(monkeypatch):
    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda symbol, max_expiries, risk_free_rate: _fake_result(symbol))

    import telegram_notifier
    sent = {}
    monkeypatch.setattr(telegram_notifier, "send_text_report", lambda text: sent.setdefault("text", text))

    run_watchlist.run_intraday_summary(["TSLA"], max_expiries=4, risk_free_rate=0.045, notify=True)

    assert "TSLA" in sent["text"]


def test_run_intraday_summary_skips_telegram_without_notify(monkeypatch):
    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda symbol, max_expiries, risk_free_rate: _fake_result(symbol))

    import telegram_notifier
    mock = MagicMock()
    monkeypatch.setattr(telegram_notifier, "send_text_report", mock)

    run_watchlist.run_intraday_summary(["TSLA"], max_expiries=4, risk_free_rate=0.045, notify=False)

    mock.assert_not_called()


def test_main_intraday_summary_flag_skips_full_daily_flow(monkeypatch, tmp_path, capsys):
    """--intraday-summary 應該只跑輕量摘要就結束，不該連帶跑歷史資料庫
    寫入/策略建議/圖表產生那整套每日流程——用 mock 斷言那些步驟完全沒被
    呼叫到，而不是只看有沒有丟例外。
    """
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA"]}), encoding="utf-8")

    monkeypatch.setattr(analyze, "fetch_and_aggregate", lambda symbol, max_expiries, risk_free_rate: _fake_result(symbol))

    save_snapshot_mock = MagicMock()
    monkeypatch.setattr("db_manager.save_snapshot", save_snapshot_mock)
    build_chart_mock = MagicMock()
    monkeypatch.setattr(analyze, "build_chart", build_chart_mock)

    monkeypatch.setattr(sys, "argv", [
        "run_watchlist.py", "--watchlist", str(watchlist_path), "--intraday-summary",
    ])

    run_watchlist.main()

    captured = capsys.readouterr()
    assert "TSLA" in captured.out
    save_snapshot_mock.assert_not_called()
    build_chart_mock.assert_not_called()


# ---------- 觀察名單 ----------

def _save_watch_event(db_path, symbol="TSLA", text="TSLA 現貨 $115.00 向上穿越 Call Wall $110"):
    return db_manager.save_signal_event(
        symbol, "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch",
        reason="正 Gamma 通常被壓回", signature="call_wall_breach",
        payload={"text": text}, db_path=db_path,
    )


def test_build_watch_section_is_empty_without_pending_events(tmp_path):
    db_path = tmp_path / "history.db"

    text, ids = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)

    assert text == ""
    assert ids == []


def test_build_watch_section_lists_pending_events(tmp_path):
    db_path = tmp_path / "history.db"
    event_id = _save_watch_event(db_path)

    text, ids = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)

    assert "觀察名單" in text
    assert "Call Wall" in text
    assert "正 Gamma" in text
    assert ids == [event_id]


def test_watch_events_are_not_repeated_after_delivery(tmp_path):
    """drain 之後標記已送，下一個摘要時間點不得重複出現。"""
    db_path = tmp_path / "history.db"
    _save_watch_event(db_path)

    _, ids = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)
    db_manager.mark_events_delivered(
        ids, "2026-09-09T20:30:00+00:00", "daily_report", db_path=db_path,
    )

    text, ids_again = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)

    assert text == ""
    assert ids_again == []


def test_build_watch_section_spans_multiple_symbols(tmp_path):
    db_path = tmp_path / "history.db"
    _save_watch_event(db_path, symbol="TSLA", text="TSLA 穿越 Call Wall")
    _save_watch_event(db_path, symbol="MU", text="MU 穿越 Call Wall")

    text, ids = run_watchlist.build_watch_section(["TSLA", "MU"], db_path=db_path)

    assert "TSLA" in text and "MU" in text
    assert len(ids) == 2


def _save_unusual_watch_event(db_path, strike, ratio, detected_at):
    return db_manager.save_signal_event(
        "SPCX", detected_at, "2026-09-09", "unusual_activity",
        classified_tier="watch", delivered_tier="watch",
        reason="盤中無法判定開倉/平倉故不升級", signature=f"call:{strike}",
        payload={
            "strike": strike, "side": "call", "volume": ratio * 1000,
            "ratio": ratio, "text": f"SPCX ${strike} CALL 累積成交量（{ratio:.1f}x）",
        }, db_path=db_path,
    )


def test_build_watch_section_caps_unusual_contracts_and_keeps_strongest(tmp_path):
    db_path = tmp_path / "history.db"
    ids = [
        _save_unusual_watch_event(db_path, 150 + i, 3.0 + i, f"2026-09-09T14:0{i}:00+00:00")
        for i in range(6)
    ]

    text, delivered_ids = run_watchlist.build_watch_section(["SPCX"], db_path=db_path)

    assert text.count("SPCX $") == 5
    assert "$150" not in text
    assert "$155" in text
    assert set(delivered_ids) == set(ids)


def _summary_row(
    symbol, strategy_name="Bull Put Spread", macro_warnings=None,
    oi_data_quality=None, data_quality=None,
):
    return {
        "symbol": symbol, "spot": 100.0, "max_pain": 100.0,
        "call_wall": 110.0, "put_wall": 90.0, "gamma_flip": 95.0,
        "alert": None, "strategy_name": strategy_name, "mm_pressure": None,
        "macro_warnings": macro_warnings or [], "risk": None,
        "oi_data_quality": oi_data_quality,
        "data_quality": data_quality,
    }


def test_watchlist_summary_deduplicates_shared_macro_warning():
    warning = "⚠️ 距離 CPI 數據公布僅剩 1 天"

    text = run_watchlist.build_watchlist_summary([
        _summary_row("TSLA", macro_warnings=[warning]),
        _summary_row("SOXL", macro_warnings=[warning]),
    ])

    assert text.count(warning) == 1


def test_watchlist_summary_explains_unavailable_strategy_without_contradiction():
    text = run_watchlist.build_watchlist_summary([
        _summary_row("SOXL", strategy_name="無建議（Bear Call Spread）"),
    ])

    assert "建議策略：無建議" not in text
    assert "候選方向：Bear Call Spread" not in text
    assert "模型候選" not in text


def test_watchlist_summary_surfaces_unusable_oi_quality():
    text = run_watchlist.build_watchlist_summary([
        _summary_row("SPCX", oi_data_quality={
            "usable": False, "total_oi": 100, "total_volume": 10000,
            "reason": "OI 僅為成交量的 1.00%",
        }),
    ])

    assert "OI 資料可信度低" in text
    assert "1.00%" in text


def test_watchlist_summary_surfaces_chain_health_score_and_reason():
    text = run_watchlist.build_watchlist_summary([
        _summary_row("SPCX", data_quality={
            "usable": False, "score": 65, "label": "不完整",
            "reason": "到期日覆蓋 50%，低於 75% 門檻",
        }),
    ])

    assert "資料 65/100（不完整）" in text
    assert "到期日覆蓋 50%" in text


def test_watchlist_summary_puts_decision_before_raw_levels():
    row = _summary_row("TSLA")
    row["decision"] = {
        "action": "區間上緣，避免追價", "confidence": "中",
        "summary": "正 Gamma 壓抑波動，價格接近區間上緣。",
        "upside_trigger": "站穩 Call Wall $110 才重新評估向上突破",
        "downside_trigger": "跌破 Gamma Flip $95 轉為防守",
        "why": [],
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "TSLA $100.00｜區間上緣，避免追價｜信心 中" in text
    assert "Max Pain" not in text
    assert "觸發：" in text
    assert "失效：" in text


def test_watchlist_summary_does_not_recommend_strategy_during_low_confidence_observation():
    row = _summary_row("TSLA", strategy_name="Bull Put Spread")
    row["decision"] = {
        "action": "事件前觀望", "confidence": "低",
        "summary": "事件前不判斷方向", "upside_trigger": "等待事件",
        "downside_trigger": "等待事件", "why": [],
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "建議策略：Bull Put Spread" not in text
    assert "模型候選：Bull Put Spread" not in text


def test_watchlist_summary_does_not_execute_strategy_at_medium_confidence():
    row = _summary_row("TSLA", strategy_name="Bear Call Spread")
    row["decision"] = {
        "action": "突破觀察，等待站穩", "confidence": "中",
        "summary": "等待確認", "upside_trigger": "站穩 $110",
        "downside_trigger": "跌回 $100", "why": [],
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "建議策略：Bear Call Spread" not in text
    assert "模型候選：Bear Call Spread" not in text


def test_watchlist_summary_leads_with_all_observe_conclusion():
    rows = [_summary_row("TSLA"), _summary_row("SOXL")]
    for row in rows:
        row["decision"] = {
            "action": "事件前觀望", "confidence": "低", "summary": "等待事件",
            "upside_trigger": "等待事件", "downside_trigger": "等待事件", "why": [],
        }

    text = run_watchlist.build_watchlist_summary(rows)

    assert "今日結論：全部觀望，沒有符合執行條件的標的" in text
    assert text.index("今日結論") < text.index("◆ TSLA")


def test_watchlist_summary_leads_with_actionable_symbols():
    tsla = _summary_row("TSLA")
    tsla["decision"] = {
        "action": "區間下緣，等待止跌", "confidence": "中", "summary": "接近支撐",
        "upside_trigger": "站回 $100", "downside_trigger": "跌破 $90", "why": [],
    }
    soxl = _summary_row("SOXL")
    soxl["decision"] = {
        "action": "事件前觀望", "confidence": "低", "summary": "等待事件",
        "upside_trigger": "等待", "downside_trigger": "等待", "why": [],
    }

    text = run_watchlist.build_watchlist_summary([tsla, soxl])

    assert "今日重點觀察：TSLA（區間下緣，等待止跌）" in text
    assert "SOXL（事件前觀望）" not in text.split("◆ TSLA", 1)[0]


def test_watchlist_summary_highlights_only_changed_decisions_at_top():
    tsla = _summary_row("TSLA")
    tsla["decision_change"] = {
        "changed": True, "kind": "gate_released",
        "text": "閘門解除：事件前觀望 → 區間應對，不追方向",
    }
    soxl = _summary_row("SOXL")
    soxl["decision_change"] = {
        "changed": False, "kind": "unchanged", "text": "維持原判斷",
    }

    text = run_watchlist.build_watchlist_summary([tsla, soxl])
    top = text.split("◆ TSLA", 1)[0]

    assert "今日決策變化" in top
    assert "TSLA：閘門解除" in top
    assert "SOXL：維持原判斷" not in top


def test_watchlist_summary_survives_missing_decision_change():
    row = _summary_row("TSLA")
    row["decision"] = None
    row["decision_change"] = None

    text = run_watchlist.build_watchlist_summary([row])

    assert "TSLA" in text
    assert "今日決策變化" not in text


def test_watchlist_summary_surfaces_matured_decision_result_at_top():
    row = _summary_row("SPCX")
    row["decision_outcome"] = {
        "outcome": "confirmed", "action": "突破觀察，等待站穩",
        "return_pct": 2.1, "date": "2026-09-10", "future_date": "2026-09-11",
    }

    text = run_watchlist.build_watchlist_summary([row])
    top = text.split("◆ SPCX", 1)[0]

    assert "前次決策驗證" in top
    assert "SPCX：確認成立" in top
    assert "+2.1%" in top


def test_watchlist_summary_moves_general_history_to_decisions_command():
    row = _summary_row("TSLA")
    row["decision"] = {
        "action": "突破觀察，等待站穩", "confidence": "中", "summary": "等待確認",
        "upside_trigger": "站穩 $110", "downside_trigger": "跌回 $100", "why": [],
    }
    row["decision_evidence"] = {
        "sufficient_sample": False,
        "text": "同類歷史樣本不足（2/5 段），暫不估計成功率",
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "歷史證據：" not in text
    assert "決策記分：/decisions TSLA" in text


def test_watchlist_summary_displays_current_market_context():
    row = _summary_row("TSLA")
    row["decision"] = {
        "action": "區間應對，不追方向", "confidence": "中", "summary": "等待",
        "upside_trigger": "向上", "downside_trigger": "向下", "why": [],
        "context": {
            "gamma_regime": "positive", "price_zone": "inside_walls",
            "event_regime": "normal", "zero_dte_regime": "high",
            "data_regime": "usable",
        },
    }
    row["context_evidence"] = {
        "applicability": "樣本不足", "sample_size": 2,
        "text": "同情境樣本不足（2/20 段）",
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "環境：正 Gamma／Wall 區間內／一般交易日／高 0DTE" in text
    assert "適用性：樣本不足；同情境樣本不足（2/20 段）" in text


def test_watchlist_summary_is_an_action_brief_not_a_metric_dump():
    row = _summary_row("TSLA")
    row["data_quality"] = {"usable": True, "score": 99, "label": "完整", "reason": "正常"}
    row["decision"] = {
        "action": "區間應對，不追方向", "confidence": "中",
        "summary": "正 Gamma 環境偏向均值回歸。",
        "upside_trigger": "站穩 Call Wall $110 才重新評估向上突破",
        "downside_trigger": "跌破 Gamma Flip $95 轉為防守",
        "why": [],
        "context": {
            "gamma_regime": "positive", "price_zone": "inside_walls",
            "event_regime": "normal", "zero_dte_regime": "normal",
            "data_regime": "usable",
        },
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "TSLA $100.00｜區間應對，不追方向｜信心 中" in text
    assert "觸發：站穩 Call Wall $110" in text
    assert "失效：跌破 Gamma Flip $95" in text
    assert "有效至下一交易日收盤" in text
    assert "資料 99/100" in text
    assert "Max Pain" not in text
    assert "莊家收割壓力" not in text
    assert "完整分析：/report TSLA" in text


def test_watchlist_summary_omits_unscored_previous_observation():
    row = _summary_row("TSLA")
    row["decision_outcome"] = {
        "outcome": "observed", "action": "事件前觀望", "return_pct": 3.0,
        "date": "2026-09-10", "future_date": "2026-09-11",
    }

    text = run_watchlist.build_watchlist_summary([row])

    assert "前次決策驗證" not in text
    assert "僅記錄波動" not in text
