"""測試 analyze.py 面對「部分到期日抓取失敗」與「完全抓不到資料」時的行為：
前者應該優雅降級（用剩下的資料照樣算完），後者應該是一個清楚的例外，
而不是把 NaN/IndexError 一路帶到最後讓程式莫名其妙地壞掉。
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import patch

import pytest

import analyze
import data_fetcher
import db_manager
import dashboard_generator
import line_notifier
import pinning_engine
import smart_money
from data_fetcher import StrikeLegRaw


@pytest.fixture(autouse=True)
def _assume_trading_day(monkeypatch):
    """main() 現在只有「今天是美股交易日」才寫歷史資料庫/策略追蹤——這裡
    預設一律當作是交易日，避免每個既有測試都要各自 mock 一次，也避免測試
    真的打網路查 SPY。個別測試要測「非交易日跳過寫入」的情境時，可以在
    測試本體裡再蓋一次 monkeypatch.setattr 成 False。
    """
    monkeypatch.setattr(data_fetcher, "is_market_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(data_fetcher, "current_trading_date_str", lambda: "2026-08-01")


def test_fetch_and_aggregate_succeeds_when_one_expiry_fails(monkeypatch):
    """模擬其中一個到期日的期權鏈查詢失敗（data_fetcher 內部已經接住例外、
    回傳空列表），確認彙總結果仍然用另一個到期日的資料算得出來。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02", "2026-01-09"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)

    def fake_get_option_chain_legs(symbol, expiry):
        if expiry == "2026-01-02":
            return []  # 模擬這個到期日查詢失敗
        return [StrikeLegRaw(
            expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
            put_oi=500.0, put_iv=0.5, put_volume=5.0,
        )]

    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", fake_get_option_chain_legs)

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)

    assert result.spot == 100.0
    assert len(result.gex_by_strike) == 1
    assert result.gex_by_strike[0]["call_oi"] == 1000.0


def test_fetch_and_aggregate_raises_when_no_expiries_available(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: [])

    with pytest.raises(RuntimeError, match="沒有可用的到期日"):
        analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)


def test_fetch_and_aggregate_raises_when_all_expiries_fail(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [])

    with pytest.raises(RuntimeError, match="沒有有效資料"):
        analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)


def test_main_exits_cleanly_instead_of_raw_traceback_on_total_failure(monkeypatch, tmp_path):
    """spot price 抓不到（Yahoo Finance 整個斷線）時，main() 應該用
    SystemExit(1) 收尾，而不是讓原始例外一路往外炸、印出嚇人的 traceback。
    """
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path)])

    def raise_connection_error(symbol):
        raise ConnectionError("Yahoo Finance 斷線")

    monkeypatch.setattr(data_fetcher, "get_spot_price", raise_connection_error)

    with pytest.raises(SystemExit) as exc_info:
        analyze.main()

    assert exc_info.value.code == 1


def test_main_notifies_failure_when_notify_flag_set(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path), "--notify"])
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: (_ for _ in ()).throw(ConnectionError("down")))

    with patch("telegram_notifier.send_failure_notice") as mock_notify:
        with pytest.raises(SystemExit):
            analyze.main()

    mock_notify.assert_called_once()


def _fake_result(spot=100.0, put_wall=90.0, call_wall=110.0, alert=None):
    return analyze.AnalysisResult(
        symbol="TSLA", spot=spot, expiries_used=["2026-09-04"], gex_by_strike=[{"strike": 100, "net_gex": 1}],
        volume_by_strike={}, max_pain=100.0, call_wall=call_wall, put_wall=put_wall,
        gamma_flip=None, gamma_flip_distance_pct=None,
        zero_dte_summary={"total_net_gex": 1, "zero_dte_net_gex": 0, "ex_zero_dte_net_gex": 1, "zero_dte_share_pct": 0.0},
        alert=alert,
    )


def test_compute_strategy_recommendation_returns_none_when_no_dte_expiry(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: None)
    result = analyze.compute_strategy_recommendation("TSLA", _fake_result())
    assert result is None


def test_compute_strategy_recommendation_returns_none_when_chain_empty(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: "2026-09-04")
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda *a, **k: [])
    result = analyze.compute_strategy_recommendation("TSLA", _fake_result())
    assert result is None


def test_compute_strategy_recommendation_returns_none_on_unexpected_exception(monkeypatch):
    """就算 data_fetcher 或 options_strategy_engine 出現預期外的例外，這也只是
    報告的加分項，不該讓整份報告連帶失敗——只記警告、回傳 None。
    """
    def raise_error(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", raise_error)
    result = analyze.compute_strategy_recommendation("TSLA", _fake_result())
    assert result is None


def test_main_survives_db_write_failure(monkeypatch, tmp_path):
    """歷史資料庫寫入失敗（例如磁碟權限問題）不該讓整支腳本掛掉——報告跟圖表
    已經是當天最重要的產出，不能因為資料庫這個加分項失敗而連帶不見。
    """
    monkeypatch.setattr(sys, "argv", [
        "analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path), "--no-ai", "--no-dashboard",
    ])
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: None)  # 略過策略建議這條路
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])  # 避免真的打網路查財報日期
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)  # 避免測試依賴 kaleido/Chrome

    def raise_disk_error(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(db_manager, "save_snapshot", raise_disk_error)

    analyze.main()  # 不應該拋出例外

    report_path = tmp_path / f"daily_report_TSLA_{datetime.now().strftime('%Y%m%d')}.md"
    assert report_path.exists()


def test_fetch_and_aggregate_survives_smart_money_failure(monkeypatch):
    """Smart Money 指標（IV Skew / PCR / 異常大單 / 壓力分數）是報告的加分項，
    計算過程出例外不該連累核心的 GEX/Wall/Max Pain 結果算不出來。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])

    def raise_error(*a, **k):
        raise ValueError("unexpected smart money bug")

    monkeypatch.setattr(smart_money, "compute_iv_skew", raise_error)

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)

    assert result.spot == 100.0  # 核心結果照樣算出來
    assert result.max_pain == 100.0
    assert result.iv_skew is None
    assert result.put_call_ratio == {}
    assert result.unusual_activity == []


def test_fetch_and_aggregate_survives_pinning_failure(monkeypatch):
    """Pinning 判斷是報告的加分項，計算過程出例外不該連累核心的
    GEX/Wall/Max Pain 結果算不出來（跟 Smart Money 指標同一套防禦慣例）。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])

    def raise_error(*a, **k):
        raise ValueError("unexpected pinning bug")

    monkeypatch.setattr(pinning_engine, "compute_pinning_analysis", raise_error)

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)

    assert result.spot == 100.0  # 核心結果照樣算出來
    assert result.max_pain == 100.0
    assert result.pinning is None


def test_fetch_and_aggregate_pinning_uses_confirmed_positive_gamma_only(monkeypatch):
    """gamma_flip 算不出來（回傳 None）時，正 Gamma 條件必須保守判定為
    False（未確認），不能直接沿用 in_negative_gamma 取反——那個變數在
    gamma_flip 是 None 時預設 False，取反會誤把「無法判斷」當成
    「確認正Gamma」，讓 Pinning 在資料不足時被錯誤地判定為可能成立。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])
    # gamma_flip 找不到交叉點（例如整條曲線都同號），find_gamma_flip_point 回傳 None。
    monkeypatch.setattr(analyze, "find_gamma_flip_point", lambda *a, **k: None)

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)

    assert result.gamma_flip is None
    assert result.pinning is not None
    assert result.pinning["in_positive_gamma"] is False


def test_fetch_and_aggregate_enriches_unusual_activity_with_previous_day_oi(monkeypatch, tmp_path):
    """串接測試：昨天存過的OI快照要能正確傳進 detect_unusual_activity，
    讓異常大單多一個 likely_opening 判斷（新開倉 vs 平倉/轉倉）。
    """
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(data_fetcher, "current_trading_date_str", lambda: "2026-08-01")
    db_manager.save_oi_snapshot("TSLA", "2026-07-31", [{"strike": 100.0, "call_oi": 50.0, "put_oi": 0.0}], db_path=db_path)

    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-09-04"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 30 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=600.0,  # OI比昨天(50)高很多
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045, db_path=db_path)

    assert len(result.unusual_activity) >= 1
    call_item = next(item for item in result.unusual_activity if item["side"] == "call")
    assert call_item["likely_opening"] is True  # 今天call_oi(1000) > 昨天(50)，判斷為新開倉


def test_negative_gamma_flag_is_independent_of_put_wall_breach(monkeypatch):
    """現貨跌破 Put Wall 但仍高於 Gamma Flip（做市商仍是淨多Gamma）時，
    傳給 compute_market_maker_pressure_score 的 in_negative_gamma 應該是
    False——不能因為 alert 被 Put Wall 觸發就誤判成負Gamma。這是實測抓到
    的真bug：混用會讓莊家壓力分數平白多加50分，也可能誤觸死亡Loop警示。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    # put_oi 在 105 這檔最大 -> put_wall=105 > spot=100，現貨已跌破 Put Wall，觸發 alert。
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [
        StrikeLegRaw(expiry=expiry, strike=100.0, call_oi=100.0, call_iv=0.5, call_volume=10.0,
                     put_oi=50.0, put_iv=0.5, put_volume=5.0),
        StrikeLegRaw(expiry=expiry, strike=105.0, call_oi=50.0, call_iv=0.5, call_volume=5.0,
                     put_oi=200.0, put_iv=0.5, put_volume=20.0),
    ])
    # gamma_flip=90 < spot=100，現貨仍在 Gamma Flip 之上，做市商仍是淨多Gamma。
    monkeypatch.setattr(analyze, "find_gamma_flip_point", lambda *a, **k: 90.0)

    captured = {}

    def fake_pressure(spot, put_wall, in_negative_gamma, legs):
        captured["in_negative_gamma"] = in_negative_gamma
        return None

    monkeypatch.setattr(smart_money, "compute_market_maker_pressure_score", fake_pressure)

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)

    assert result.put_wall == 105.0
    assert result.spot < result.put_wall  # 確認 alert 真的是被 Put Wall 突破觸發
    assert result.alert is not None
    assert captured["in_negative_gamma"] is False
    assert result.mm_pressure is None


def test_unusual_activity_excludes_far_otm_noise(monkeypatch):
    """實測過的真實現象：遠價外的 LEAPS 履約價（現價20%以外）OI=0 很正常，
    不該被算成「異常大單」擠掉現價附近真正值得注意的訊號。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)

    def fake_legs(symbol, expiry):
        return [
            # 現價附近，真正值得注意的異常大單
            StrikeLegRaw(expiry=expiry, strike=105.0, call_oi=100.0, call_iv=0.5, call_volume=500.0,
                         put_oi=100.0, put_iv=0.5, put_volume=10.0),
            # 遠價外（現價70%以外）的雜訊：OI=0、只有一點點成交量
            StrikeLegRaw(expiry=expiry, strike=170.0, call_oi=0.0, call_iv=0.0, call_volume=200.0,
                         put_oi=0.0, put_iv=0.0, put_volume=0.0),
        ]

    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", fake_legs)

    result = analyze.fetch_and_aggregate("TSLA", max_expiries=None, risk_free_rate=0.045)

    strikes_flagged = [item["strike"] for item in result.unusual_activity]
    assert 105.0 in strikes_flagged
    assert 170.0 not in strikes_flagged


def test_watch_flag_delegates_to_intraday_watcher_without_running_full_analysis(monkeypatch):
    """--watch 應該直接委派給 intraday_watcher，完全不跑完整的每日分析流程
    （不該去抓所有到期日、算GEX、產報告——那些跟『盤中即時檢查』的目標無關）。
    """
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--symbol", "TSLA", "--watch", "--notify"])

    def fail_if_called(*a, **k):
        raise AssertionError("不該跑到完整分析流程")

    monkeypatch.setattr(analyze, "fetch_and_aggregate", fail_if_called)

    import intraday_watcher
    with patch.object(intraday_watcher, "run_watch_cycle") as mock_cycle:
        analyze.main()

    mock_cycle.assert_called_once_with(["TSLA"], notify=True)


def test_main_generates_dashboard_at_specified_path(monkeypatch, tmp_path):
    dashboard_path = tmp_path / "dashboard" / "index.html"
    monkeypatch.setattr(sys, "argv", [
        "analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path), "--no-ai",
        "--dashboard-path", str(dashboard_path),
    ])
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(db_manager, "save_snapshot", lambda *a, **k: None)

    analyze.main()

    assert dashboard_path.exists()
    assert "TSLA" in dashboard_path.read_text(encoding="utf-8")


def test_main_survives_dashboard_generation_failure(monkeypatch, tmp_path):
    """儀表板產生失敗（例如磁碟權限問題）不該讓 Markdown 報告連帶產不出來。"""
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path), "--no-ai"])
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(db_manager, "save_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(dashboard_generator, "generate_dashboard", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    analyze.main()  # 不應該拋出例外

    report_path = tmp_path / f"daily_report_TSLA_{datetime.now().strftime('%Y%m%d')}.md"
    assert report_path.exists()


def test_send_line_alert_if_extreme_calls_notifier_when_triggered(monkeypatch):
    result = _fake_result(spot=115.0, call_wall=110.0, put_wall=90.0)  # 突破 Call Wall

    with patch.object(line_notifier, "send_line_alert") as mock_send:
        analyze.send_line_alert_if_extreme(result)

    mock_send.assert_called_once()
    assert "TSLA" in mock_send.call_args.args[0]


def test_send_line_alert_if_extreme_skips_when_no_extreme_event(monkeypatch):
    result = _fake_result(spot=100.0, call_wall=110.0, put_wall=90.0)  # 兩道牆都沒突破

    with patch.object(line_notifier, "send_line_alert") as mock_send:
        analyze.send_line_alert_if_extreme(result)

    mock_send.assert_not_called()


def test_send_line_alert_if_extreme_survives_unexpected_exception(monkeypatch):
    """LINE警報是額外的加分通道，判斷或推播失敗都不該讓呼叫端（main()）跟著壞掉。"""
    result = _fake_result(spot=115.0, call_wall=110.0, put_wall=90.0)
    monkeypatch.setattr(line_notifier, "send_line_alert", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))

    analyze.send_line_alert_if_extreme(result)  # 不應該拋出例外


def test_main_triggers_line_alert_when_notify_and_extreme_event(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [
        "analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path), "--no-ai",
        "--no-dashboard", "--notify",
    ])
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    # 讓 call_oi 遠大於 put_oi，Call Wall 落在現貨附近以下，觸發「突破 Call Wall」
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=95.0, call_oi=1000.0, call_iv=0.5, call_volume=10.0,
        put_oi=500.0, put_iv=0.5, put_volume=5.0,
    )])
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(db_manager, "save_snapshot", lambda *a, **k: None)

    import telegram_notifier
    with patch.object(telegram_notifier, "send_daily_report"), patch.object(line_notifier, "send_line_alert") as mock_line:
        analyze.main()

    mock_line.assert_called_once()  # 現貨 $100 > Call Wall $95，應該觸發


def test_main_resolves_strategy_scorecard_and_notifies(monkeypatch, tmp_path):
    """main() 現在會在單一標的排程模式（./run.sh TSLA）也自動結算到期的
    策略建議——過去只有手動執行 strategy_resolver.py 才會結算，排程從沒
    真的觸發過。
    """
    monkeypatch.setattr(sys, "argv", [
        "analyze.py", "--symbol", "TSLA", "--output-dir", str(tmp_path), "--no-ai",
        "--no-dashboard", "--notify",
    ])
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-01-02"])
    monkeypatch.setattr(data_fetcher, "time_to_expiry_years", lambda expiry: 7 / 365)
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [StrikeLegRaw(
        expiry=expiry, strike=100.0, call_oi=100.0, call_iv=0.5, call_volume=10.0,
        put_oi=100.0, put_iv=0.5, put_volume=10.0,
    )])
    monkeypatch.setattr(data_fetcher, "get_expiry_by_dte", lambda *a, **k: None)
    monkeypatch.setattr(analyze, "get_macro_warnings", lambda symbol: [])
    monkeypatch.setattr(analyze, "build_chart", lambda *a, **k: None)
    monkeypatch.setattr(db_manager, "save_snapshot", lambda *a, **k: None)

    import strategy_resolver
    fake_resolved = [{
        "symbol": "TSLA", "strategy_name": "Bull Put Spread", "expiry_date": "2026-08-01",
        "outcome": "WIN", "realized_pnl": 1.5, "settlement_spot": 310.0, "approximate": False,
    }]
    captured = {}

    def fake_resolve_pending(symbol):
        captured["symbol"] = symbol
        return fake_resolved

    monkeypatch.setattr(strategy_resolver, "resolve_pending", fake_resolve_pending)

    import telegram_notifier
    with patch.object(telegram_notifier, "send_daily_report"), patch.object(telegram_notifier, "send_text_report") as mock_text:
        analyze.main()

    assert captured["symbol"] == "TSLA"
    mock_text.assert_called_once()
    assert "Bull Put Spread" in mock_text.call_args.args[0]


def test_aggregate_smart_money_legs_sums_oi_volume_across_expiries():
    raw_legs = [
        StrikeLegRaw(expiry="2026-08-07", strike=100.0, call_oi=100.0, call_iv=0.5, call_volume=10.0,
                     put_oi=50.0, put_iv=0.4, put_volume=5.0),
        StrikeLegRaw(expiry="2026-08-14", strike=100.0, call_oi=200.0, call_iv=0.6, call_volume=20.0,
                     put_oi=80.0, put_iv=0.45, put_volume=8.0),
    ]
    legs = analyze._aggregate_smart_money_legs(raw_legs)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.call_oi == 300.0  # 100+200，跟 GEX 彙總邏輯一致
    assert leg.call_volume == 30.0
    assert leg.call_iv == pytest.approx(0.55)  # (0.5+0.6)/2


def test_aggregate_smart_money_legs_excludes_zero_iv_noise_from_average():
    """實測過的真實現象：近到期、無流動性合約 IV 常被 data_fetcher 清成 0
    （雜訊，不是真實報價）。這種 0 不該被當成一個有效樣本拉低平均 IV。
    """
    raw_legs = [
        StrikeLegRaw(expiry="2026-08-07", strike=100.0, call_oi=10.0, call_iv=0.0, call_volume=1.0,
                     put_oi=0.0, put_iv=0.0, put_volume=0.0),  # 雜訊：IV 被清成 0
        StrikeLegRaw(expiry="2026-08-14", strike=100.0, call_oi=200.0, call_iv=0.6, call_volume=20.0,
                     put_oi=0.0, put_iv=0.0, put_volume=0.0),
    ]
    legs = analyze._aggregate_smart_money_legs(raw_legs)
    leg = legs[0]
    assert leg.call_iv == pytest.approx(0.6)  # 只取有效樣本平均，不是 (0+0.6)/2


def test_save_oi_snapshot_if_trading_day_persists_strike_level_oi(tmp_path):
    db_path = tmp_path / "history.db"
    result = analyze.AnalysisResult(
        symbol="TSLA", spot=100.0,
        expiries_used=["2026-09-04"],
        gex_by_strike=[
            {"strike": 100.0, "net_gex": 1.0, "call_oi": 500.0, "put_oi": 300.0},
            {"strike": 105.0, "net_gex": 1.0, "call_oi": 200.0, "put_oi": 150.0},
        ],
        volume_by_strike={}, max_pain=100.0, call_wall=110.0, put_wall=90.0,
        gamma_flip=None, gamma_flip_distance_pct=None,
        zero_dte_summary={"total_net_gex": 1, "zero_dte_net_gex": 0, "ex_zero_dte_net_gex": 1, "zero_dte_share_pct": 0.0},
        alert=None,
    )

    analyze.save_oi_snapshot_if_trading_day("TSLA", result, "2026-08-01", db_path=db_path)

    snapshot = db_manager.get_oi_snapshot("TSLA", "2026-08-01", db_path=db_path)
    assert snapshot == {
        100.0: {"call_oi": 500.0, "put_oi": 300.0},
        105.0: {"call_oi": 200.0, "put_oi": 150.0},
    }


def test_save_oi_snapshot_if_trading_day_survives_malformed_gex_by_strike(tmp_path):
    """gex_by_strike 缺少 call_oi/put_oi 欄位（例如上游資料格式意外改變）
    不該讓整個報告流程炸掉——這是加分項，寫入失敗只記警告。
    """
    db_path = tmp_path / "history.db"
    result = _fake_result()  # gex_by_strike 只有 strike/net_gex，沒有 call_oi/put_oi

    analyze.save_oi_snapshot_if_trading_day("TSLA", result, "2026-08-01", db_path=db_path)  # 不應該拋出例外
