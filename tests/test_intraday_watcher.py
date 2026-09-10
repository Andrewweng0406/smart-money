"""intraday_watcher.py 測試——市場時間判斷用時區明確的合成時間測試，
牆位/異常大單檢查用合成資料跟 monkeypatch，完全不用連網路或等真實時間。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import data_fetcher
import db_manager
import intraday_watcher
import signal_tiering
import smart_money
from data_fetcher import StrikeLegRaw

US_EASTERN = ZoneInfo("America/New_York")


def _et(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=US_EASTERN)


# ---------- is_market_hours ----------

def test_is_market_hours_true_during_regular_session():
    assert intraday_watcher.is_market_hours(_et(2026, 8, 3, 10, 0))  # 週一 10:00 ET


def test_is_market_hours_false_during_premarket():
    assert not intraday_watcher.is_market_hours(_et(2026, 8, 3, 4, 30))  # 週一 04:30 ET


def test_is_market_hours_false_before_regular_open():
    assert not intraday_watcher.is_market_hours(_et(2026, 8, 3, 9, 29))


def test_is_market_hours_true_at_exact_regular_open():
    assert intraday_watcher.is_market_hours(_et(2026, 8, 3, 9, 30))


def test_is_market_hours_true_at_exact_close():
    assert intraday_watcher.is_market_hours(_et(2026, 8, 3, 16, 0))


def test_is_market_hours_false_after_close():
    assert not intraday_watcher.is_market_hours(_et(2026, 8, 3, 16, 1))


def test_is_market_hours_false_on_saturday():
    assert not intraday_watcher.is_market_hours(_et(2026, 8, 1, 10, 0))  # 假設是週六


# ---------- is_regular_market_hours ----------

def test_is_regular_market_hours_true_during_regular_session():
    assert intraday_watcher.is_regular_market_hours(_et(2026, 8, 3, 10, 0))  # 週一 10:00 ET


def test_is_regular_market_hours_false_during_premarket():
    """09:30 之前（含盤前）一律不算，期權監控只在正式交易時段評估。"""
    assert not intraday_watcher.is_regular_market_hours(_et(2026, 8, 3, 9, 0))


def test_is_regular_market_hours_true_at_exact_open():
    assert intraday_watcher.is_regular_market_hours(_et(2026, 8, 3, 9, 30))


def test_is_regular_market_hours_false_after_close():
    assert not intraday_watcher.is_regular_market_hours(_et(2026, 8, 3, 16, 1))


def test_is_regular_market_hours_false_on_saturday():
    assert not intraday_watcher.is_regular_market_hours(_et(2026, 8, 1, 10, 0))


# ---------- check_wall_breach ----------

@dataclass
class _FakeResult:
    symbol: str
    spot: float
    max_pain: float = 100.0
    call_wall: float = 110.0
    put_wall: float = 90.0
    gamma_flip: float | None = None
    gamma_flip_distance_pct: float | None = None
    zero_dte_summary: dict = None
    alert: str | None = None
    pinning: dict | None = None

    def __post_init__(self):
        if self.zero_dte_summary is None:
            self.zero_dte_summary = {"total_net_gex": 0, "zero_dte_net_gex": 0, "ex_zero_dte_net_gex": 0, "zero_dte_share_pct": 0.0}


def _save_walls(db_path, call_wall=110.0, put_wall=90.0):
    db_manager.save_snapshot(
        _FakeResult(symbol="TSLA", spot=100.0, call_wall=call_wall, put_wall=put_wall),
        "2026-08-01", db_path=db_path,
    )


def test_check_wall_breach_returns_none_without_history(tmp_path):
    db_path = tmp_path / "history.db"
    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=100.0, prev_spot=99.0, db_path=db_path,
    ) is None


def test_check_wall_breach_detects_upward_call_wall_crossing(tmp_path):
    db_path = tmp_path / "history.db"
    _save_walls(db_path)

    breach = intraday_watcher.check_wall_breach(
        "TSLA", spot=115.0, prev_spot=105.0, db_path=db_path,
    )

    assert breach is not None
    assert breach["kind"] == signal_tiering.KIND_CALL_WALL_BREACH
    assert breach["wall_price"] == 110.0
    assert "Call Wall" in breach["text"]


def test_check_wall_breach_detects_downward_put_wall_crossing(tmp_path):
    db_path = tmp_path / "history.db"
    _save_walls(db_path)

    breach = intraday_watcher.check_wall_breach(
        "TSLA", spot=85.0, prev_spot=95.0, db_path=db_path,
    )

    assert breach is not None
    assert breach["kind"] == signal_tiering.KIND_PUT_WALL_BREACH
    assert "Put Wall" in breach["text"]


def test_check_wall_breach_ignores_price_already_above_wall(tmp_path):
    """問題二的迴歸測試：價格整天待在牆上不是持續的『突破事件』。

    沒有這個測試，價格不動地停在 Call Wall 上方會每次檢查都回報突破，
    60 分鐘冷卻仍會每小時推一次，六個半小時吃掉大半日推播預算。
    """
    db_path = tmp_path / "history.db"
    _save_walls(db_path)

    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=115.0, prev_spot=114.0, db_path=db_path,
    ) is None


def test_check_wall_breach_without_prev_spot_never_triggers(tmp_path):
    """當日首次檢查或狀態檔遺失時寧可漏報，也不要把既有狀態誤報成新突破。"""
    db_path = tmp_path / "history.db"
    _save_walls(db_path)

    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=115.0, prev_spot=None, db_path=db_path,
    ) is None


def test_check_wall_breach_no_crossing_within_range(tmp_path):
    db_path = tmp_path / "history.db"
    _save_walls(db_path)

    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=100.0, prev_spot=101.0, db_path=db_path,
    ) is None


def test_current_trading_date_uses_eastern_not_utc():
    """UTC 已跨日但美東還是前一天時，交易日必須是美東日期。"""
    utc_after_midnight = datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc)

    assert intraday_watcher.current_trading_date(utc_after_midnight) == "2026-09-09"


# ---------- check_pinning_alert ----------

def _pinning_snapshot_result(
    symbol="TSLA", spot=100.0, pin_strike=100.0, oi_concentration_pct=100.0,
    in_positive_gamma=True, call_wall=110.0, put_wall=90.0,
):
    return _FakeResult(
        symbol=symbol, spot=spot, max_pain=pin_strike, call_wall=call_wall, put_wall=put_wall,
        pinning={
            "pin_strike": pin_strike, "oi_concentration_pct": oi_concentration_pct,
            "in_positive_gamma": in_positive_gamma, "score": 99, "regime": "PINNING",
        },
    )


def test_check_pinning_alert_returns_none_without_history(tmp_path):
    db_path = tmp_path / "history.db"
    assert intraday_watcher.check_pinning_alert("TSLA", spot=100.0, db_path=db_path) is None


def test_check_pinning_alert_returns_none_when_no_pinning_data_stored(tmp_path):
    """前一天 pinning 加分項失敗（pin_strike 存成 NULL），優雅跳過。"""
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_FakeResult(symbol="TSLA", spot=100.0, pinning=None), "2026-08-01", db_path=db_path)
    assert intraday_watcher.check_pinning_alert("TSLA", spot=100.0, db_path=db_path) is None


def test_check_pinning_alert_triggers_when_score_exceeds_threshold(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_pinning_snapshot_result(), "2026-08-01", db_path=db_path)

    alert = intraday_watcher.check_pinning_alert("TSLA", spot=100.2, db_path=db_path)
    assert alert is not None
    assert "Pinning" in alert
    assert "TSLA" in alert


def test_check_pinning_alert_none_when_score_at_or_below_threshold(tmp_path):
    db_path = tmp_path / "history.db"
    # 負Gamma時最高只能拿到70分（40近距離+0Gamma+30集中度），不會超過門檻80。
    db_manager.save_snapshot(
        _pinning_snapshot_result(in_positive_gamma=False), "2026-08-01", db_path=db_path,
    )
    assert intraday_watcher.check_pinning_alert("TSLA", spot=100.2, db_path=db_path) is None


def test_check_pinning_alert_uses_live_spot_not_stored_score(tmp_path):
    """驗證分數是用『現在』的即時現貨價重新算，不是直接沿用昨天存的分數
    ——現貨已經遠離 Pin Strike 時，就算昨天存的分數很高，也不該再觸發。
    """
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_pinning_snapshot_result(), "2026-08-01", db_path=db_path)

    assert intraday_watcher.check_pinning_alert("TSLA", spot=130.0, db_path=db_path) is None


# ---------- check_unusual_activity ----------

def test_check_unusual_activity_returns_empty_when_no_expiries(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: [])
    assert intraday_watcher.check_unusual_activity("TSLA") == []


def test_check_unusual_activity_applies_strict_thresholds(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_all_expiries", lambda symbol, max_expiries=None: ["2026-08-03"])
    monkeypatch.setattr(data_fetcher, "get_option_chain_legs", lambda symbol, expiry: [
        StrikeLegRaw(expiry=expiry, strike=310.0, call_oi=500.0, call_iv=0.5, call_volume=5000.0,
                     put_oi=100.0, put_iv=0.5, put_volume=50.0),  # ratio=10, volume=5000 -> 符合門檻
        StrikeLegRaw(expiry=expiry, strike=320.0, call_oi=100.0, call_iv=0.5, call_volume=350.0,
                     put_oi=100.0, put_iv=0.5, put_volume=10.0),  # ratio=3.5 但 volume 350 < 3000 -> 不符合
    ])

    result = intraday_watcher.check_unusual_activity("TSLA")
    assert len(result) == 1
    assert result[0]["strike"] == 310.0


# ---------- run_check ----------

def test_run_check_records_error_when_spot_price_fails(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: (_ for _ in ()).throw(ConnectionError("down")))
    result = intraday_watcher.run_check("TSLA")
    assert result["error"] is not None
    assert result["spot"] is None


def test_run_check_continues_when_wall_breach_check_fails(monkeypatch, tmp_path):
    """牆位檢查失敗不該連累異常大單檢查——兩個子檢查互相獨立。"""
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(intraday_watcher, "check_wall_breach", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setattr(intraday_watcher, "check_unusual_activity", lambda symbol: [{"strike": 100.0, "side": "call", "volume": 5000, "oi": 100, "ratio": 50.0}])

    result = intraday_watcher.run_check("TSLA")
    assert result["wall_breach"] is None
    assert len(result["unusual_activity"]) == 1


def test_run_check_evaluates_pinning_during_regular_hours(monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(intraday_watcher, "check_pinning_alert", lambda *a, **k: "TSLA Pinning 分數達 99/100")
    monkeypatch.setattr(intraday_watcher, "check_wall_breach", lambda *a, **k: None)
    monkeypatch.setattr(intraday_watcher, "check_unusual_activity", lambda symbol: [])

    result = intraday_watcher.run_check("TSLA", now=_et(2026, 8, 3, 10, 0))
    assert result["pinning_alert"] == "TSLA Pinning 分數達 99/100"


def test_run_check_skips_pinning_outside_regular_hours(monkeypatch):
    """盤前（09:30 之前）不評估 Pinning 分數——就算底層資料會判定觸發，
    也不該在這個時段被評估到，見 is_regular_market_hours() 的理由。
    """
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(intraday_watcher, "check_pinning_alert", lambda *a, **k: "不該被呼叫到")
    monkeypatch.setattr(intraday_watcher, "check_wall_breach", lambda *a, **k: None)
    monkeypatch.setattr(intraday_watcher, "check_unusual_activity", lambda symbol: [])

    result = intraday_watcher.run_check("TSLA", now=_et(2026, 8, 3, 8, 0))
    assert result["pinning_alert"] is None


def test_run_check_continues_when_pinning_check_fails(monkeypatch):
    """Pinning 檢查失敗不該連累其他子檢查——跟牆位/異常大單同一套獨立性。"""
    monkeypatch.setattr(data_fetcher, "get_spot_price", lambda symbol: 100.0)
    monkeypatch.setattr(intraday_watcher, "check_pinning_alert", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setattr(intraday_watcher, "check_wall_breach", lambda *a, **k: None)
    monkeypatch.setattr(intraday_watcher, "check_unusual_activity", lambda symbol: [{"strike": 100.0, "side": "call", "volume": 5000, "oi": 100, "ratio": 50.0}])

    result = intraday_watcher.run_check("TSLA", now=_et(2026, 8, 3, 10, 0))
    assert result["pinning_alert"] is None
    assert len(result["unusual_activity"]) == 1


# ---------- build_alert_text ----------

def test_build_alert_text_returns_none_when_nothing_triggered():
    result = {"symbol": "TSLA", "wall_breach": None, "unusual_activity": [], "spot": 100.0, "error": None}
    assert intraday_watcher.build_alert_text(result) is None


def test_build_alert_text_formats_wall_breach_and_unusual_activity():
    result = {
        "symbol": "TSLA", "spot": 115.0, "error": None,
        "wall_breach": {
            "kind": signal_tiering.KIND_CALL_WALL_BREACH, "wall_price": 110.0, "spot": 115.0,
            "text": "TSLA 現貨 $115.00 向上穿越 Call Wall $110（潛在壓力位失守）",
        },
        "unusual_activity": [{"strike": 320.0, "side": "call", "volume": 5000.0, "oi": 500.0, "ratio": 10.0}],
    }
    text = intraday_watcher.build_alert_text(result)
    assert text is not None
    assert intraday_watcher.INTRADAY_ALERT_PREFIX in text
    assert "Call Wall" in text
    assert "320" in text


def test_build_alert_text_includes_pinning_alert():
    result = {
        "symbol": "TSLA", "spot": 100.2, "error": None, "wall_breach": None,
        "pinning_alert": "TSLA Pinning 分數達 99/100（現貨 $100.20 貼近 Pin Strike $100，做市商磁吸/卡價效應極強）",
        "unusual_activity": [],
    }
    text = intraday_watcher.build_alert_text(result)
    assert text is not None
    assert "Pinning" in text


def test_build_alert_text_shows_infinity_symbol_not_python_inf():
    result = {
        "symbol": "TSLA", "spot": 100.0, "error": None, "wall_breach": None,
        "unusual_activity": [{"strike": 400.0, "side": "put", "volume": 5000.0, "oi": 0.0, "ratio": float("inf")}],
    }
    text = intraday_watcher.build_alert_text(result)
    assert "∞" in text
    assert "inf" not in text.lower()


# ---------- 警示去重/冷卻機制 ----------

def _wall_breach_result(symbol="TSLA", spot=280.0):
    return {
        "symbol": symbol, "spot": spot, "error": None,
        "wall_breach": {
            "kind": signal_tiering.KIND_PUT_WALL_BREACH, "wall_price": 300.0, "spot": spot,
            "text": f"{symbol} 現貨 ${spot:.2f} 向下穿越 Put Wall $300（潛在支撐位失守）",
        },
        "unusual_activity": [],
    }


def test_build_alert_signature_ignores_spot_price_changes():
    """同一個持續中的突破，現貨價格每次檢查都會變，但簽章應該保持一致
    ——不然去重機制會把同一個事件每次都當成新事件。
    """
    sig1 = intraday_watcher.build_alert_signature(_wall_breach_result(spot=280.0))
    sig2 = intraday_watcher.build_alert_signature(_wall_breach_result(spot=275.0))
    assert sig1 == sig2


def test_build_alert_signature_differs_for_different_wall():
    call_breach = {
        "symbol": "TSLA", "spot": 320.0, "error": None,
        "wall_breach": {
            "kind": signal_tiering.KIND_CALL_WALL_BREACH, "wall_price": 310.0, "spot": 320.0,
            "text": "TSLA 現貨 $320.00 向上穿越 Call Wall $310（潛在壓力位失守）",
        },
        "unusual_activity": [],
    }
    put_breach = _wall_breach_result()
    assert intraday_watcher.build_alert_signature(call_breach) != intraday_watcher.build_alert_signature(put_breach)


def test_build_alert_signature_includes_pinning_token():
    base = {"symbol": "TSLA", "spot": 100.0, "error": None, "wall_breach": None, "unusual_activity": []}
    with_pinning = {**base, "pinning_alert": "TSLA Pinning 分數達 99/100"}
    without_pinning = {**base, "pinning_alert": None}
    assert intraday_watcher.build_alert_signature(with_pinning) != intraday_watcher.build_alert_signature(without_pinning)


def test_build_alert_signature_stable_for_sustained_pinning_alert():
    """跟牆位突破一樣，Pinning 警報的簽章不該受現貨小幅波動影響——持續中
    的同一個事件不該每次都被當成新事件重新推播。
    """
    result1 = {"symbol": "TSLA", "spot": 100.1, "error": None, "wall_breach": None,
               "unusual_activity": [], "pinning_alert": "TSLA Pinning 分數達 99/100"}
    result2 = {"symbol": "TSLA", "spot": 100.3, "error": None, "wall_breach": None,
               "unusual_activity": [], "pinning_alert": "TSLA Pinning 分數達 95/100"}
    assert intraday_watcher.build_alert_signature(result1) == intraday_watcher.build_alert_signature(result2)


def test_should_send_alert_true_when_no_prior_state(tmp_path):
    state_path = tmp_path / "state.json"
    assert intraday_watcher.should_send_alert(
        "TSLA", "put_wall_breach", "sig", state_path=state_path,
    ) is True


def test_should_send_alert_false_within_cooldown_for_same_signature(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "put_wall_breach", "sig", now=now, state_path=state_path,
    )

    still_cooling = now + timedelta(minutes=30)
    assert intraday_watcher.should_send_alert(
        "TSLA", "put_wall_breach", "sig",
        now=still_cooling, state_path=state_path, cooldown_minutes=60,
    ) is False


def test_should_send_alert_true_after_cooldown_expires(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "put_wall_breach", "sig", now=now, state_path=state_path,
    )

    after_cooldown = now + timedelta(minutes=61)
    assert intraday_watcher.should_send_alert(
        "TSLA", "put_wall_breach", "sig",
        now=after_cooldown, state_path=state_path, cooldown_minutes=60,
    ) is True


def test_should_send_alert_true_when_signature_changes_even_within_cooldown(tmp_path):
    """同一種訊號但內容變了（簽章不同）不受冷卻限制，一定要重新推播。"""
    state_path = tmp_path / "state.json"
    now = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "unusual_activity", "call:100", now=now, state_path=state_path,
    )

    moments_later = now + timedelta(minutes=1)
    assert intraday_watcher.should_send_alert(
        "TSLA", "unusual_activity", "call:120",
        now=moments_later, state_path=state_path, cooldown_minutes=60,
    ) is True


def test_should_send_alert_survives_corrupted_state_file(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("不是合法的JSON{{{", encoding="utf-8")
    assert intraday_watcher.should_send_alert(
        "TSLA", "put_wall_breach", "sig", state_path=state_path,
    ) is True


def test_run_watch_cycle_suppresses_duplicate_notification_within_cooldown(monkeypatch, tmp_path):
    """整合測試：同一個突破事件連續兩次檢查週期，第二次在冷卻時間內應該
    只印出/記log，不該真的再推播一次 Telegram。
    """
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(intraday_watcher, "ALERT_STATE_PATH", state_path)
    monkeypatch.setattr(intraday_watcher, "run_check", lambda symbol: _wall_breach_result(symbol))

    import telegram_notifier
    send_mock = MagicMock()
    monkeypatch.setattr(telegram_notifier, "send_text_report", send_mock)

    intraday_watcher.run_watch_cycle(["TSLA"], notify=True, force=True)
    intraday_watcher.run_watch_cycle(["TSLA"], notify=True, force=True)

    send_mock.assert_called_once()


# ---------- load_symbols ----------

def test_load_symbols_returns_single_symbol_when_given():
    assert intraday_watcher.load_symbols("TSLA", "watchlist.json") == ["TSLA"]


def test_load_symbols_reads_watchlist_file(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"symbols": ["TSLA", "NVDA"]}), encoding="utf-8")
    assert intraday_watcher.load_symbols(None, str(path)) == ["TSLA", "NVDA"]


# ---------- main ----------

def test_main_skips_outside_market_hours(monkeypatch):
    monkeypatch.setattr(intraday_watcher, "is_market_hours", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["intraday_watcher.py", "--symbol", "TSLA"])

    with patch("intraday_watcher.run_check") as mock_run_check:
        intraday_watcher.main()

    mock_run_check.assert_not_called()


def test_run_watch_cycle_skips_premarket_without_fetching_data(monkeypatch):
    monkeypatch.setattr(intraday_watcher, "is_market_hours", lambda *a, **k: False)

    with patch("intraday_watcher.run_check") as mock_run_check:
        intraday_watcher.run_watch_cycle(["TSLA"], notify=True, force=False)

    mock_run_check.assert_not_called()


def test_main_force_flag_bypasses_market_hours_check(monkeypatch):
    monkeypatch.setattr(intraday_watcher, "is_market_hours", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["intraday_watcher.py", "--symbol", "TSLA", "--force"])
    monkeypatch.setattr(intraday_watcher, "run_check", lambda symbol: {
        "symbol": symbol, "wall_breach": None, "unusual_activity": [], "spot": 100.0, "error": None,
    })

    intraday_watcher.main()  # 不應該拋出例外，且應該真的執行了 run_check（透過上面 monkeypatch 驗證不會crash即可）


# ---------- 冷卻：每訊號種類獨立 ----------

def test_cooldown_blocks_same_kind_within_window(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "sig-a", now=now, state_path=state_path,
    )

    assert intraday_watcher.should_send_alert(
        "TSLA", "call_wall_breach", "sig-a",
        now=now + timedelta(minutes=30), state_path=state_path,
    ) is False


def test_cooldown_expires_after_window(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "sig-a", now=now, state_path=state_path,
    )

    assert intraday_watcher.should_send_alert(
        "TSLA", "call_wall_breach", "sig-a",
        now=now + timedelta(minutes=61), state_path=state_path,
    ) is True


def test_different_kinds_have_independent_cooldowns(tmp_path):
    """問題一的迴歸測試。

    原本所有訊號共用一個簽章：異常大單的履約價盤中會 churn，簽章一變就
    穿透冷卻，導致最吵的訊號擁有最高推播頻率，且牆位突破被綁在同一個簽章
    裡跟著重發。分開之後，異常大單怎麼變都不能影響牆位突破的冷卻。
    """
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "wall-sig", now=now, state_path=state_path,
    )

    # 異常大單是全新的種類，不該被牆位突破的冷卻擋住
    assert intraday_watcher.should_send_alert(
        "TSLA", "unusual_activity", "strike-123",
        now=now + timedelta(minutes=1), state_path=state_path,
    ) is True

    # 而牆位突破自己仍然在冷卻中——不受異常大單的簽章變動影響
    assert intraday_watcher.should_send_alert(
        "TSLA", "call_wall_breach", "wall-sig",
        now=now + timedelta(minutes=1), state_path=state_path,
    ) is False


def test_same_kind_different_symbols_are_independent(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "sig-a", now=now, state_path=state_path,
    )

    assert intraday_watcher.should_send_alert(
        "MU", "call_wall_breach", "sig-a",
        now=now + timedelta(minutes=1), state_path=state_path,
    ) is True
