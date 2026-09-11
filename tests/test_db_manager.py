"""db_manager 的讀寫測試——用 tmp_path 給獨立的 sqlite 檔案，不會碰到專案
真正的 history.db。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import db_manager


@dataclass
class _FakeResult:
    symbol: str
    spot: float
    max_pain: float
    call_wall: float
    put_wall: float
    gamma_flip: float | None
    gamma_flip_distance_pct: float | None
    zero_dte_summary: dict
    alert: str | None
    pinning: dict | None = None
    decision: dict | None = None
    data_quality: dict | None = None


def _make_result(
    symbol="TSLA", spot=311.21, alert=None, pinning=None, decision=None, data_quality=None,
) -> _FakeResult:
    return _FakeResult(
        symbol=symbol, spot=spot, max_pain=315.0, call_wall=330.0, put_wall=300.0,
        gamma_flip=317.0, gamma_flip_distance_pct=-1.9,
        zero_dte_summary={"total_net_gex": 1_000_000.0, "zero_dte_net_gex": 0.0,
                           "ex_zero_dte_net_gex": 1_000_000.0, "zero_dte_share_pct": 0.0},
        alert=alert, pinning=pinning, decision=decision, data_quality=data_quality,
    )


def test_save_and_retrieve_snapshot_round_trip(tmp_path):
    db_path = tmp_path / "history.db"
    result = _make_result()

    db_manager.save_snapshot(result, "2026-08-01", db_path=db_path)
    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "TSLA"
    assert rows[0]["spot"] == 311.21
    assert rows[0]["max_pain"] == 315.0
    assert rows[0]["alert"] is None


def test_save_snapshot_same_symbol_and_date_overwrites_not_duplicates(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_make_result(spot=300.0), "2026-08-01", db_path=db_path)
    db_manager.save_snapshot(_make_result(spot=311.21), "2026-08-01", db_path=db_path)  # 同一天重跑

    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    assert len(rows) == 1  # 沒有重複紀錄
    assert rows[0]["spot"] == 311.21  # 用最新一次的資料覆蓋


def test_get_recent_snapshots_orders_newest_first(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_make_result(), "2026-07-30", db_path=db_path)
    db_manager.save_snapshot(_make_result(), "2026-08-01", db_path=db_path)
    db_manager.save_snapshot(_make_result(), "2026-07-31", db_path=db_path)

    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    dates = [row["date"] for row in rows]
    assert dates == ["2026-08-01", "2026-07-31", "2026-07-30"]


def test_get_recent_snapshots_respects_limit(tmp_path):
    db_path = tmp_path / "history.db"
    for day in range(1, 6):
        db_manager.save_snapshot(_make_result(), f"2026-08-0{day}", db_path=db_path)

    rows = db_manager.get_recent_snapshots("TSLA", limit=2, db_path=db_path)
    assert len(rows) == 2


def test_different_symbols_do_not_collide(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_make_result(symbol="TSLA"), "2026-08-01", db_path=db_path)
    db_manager.save_snapshot(_make_result(symbol="NVDA"), "2026-08-01", db_path=db_path)

    tsla_rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    nvda_rows = db_manager.get_recent_snapshots("NVDA", db_path=db_path)
    assert len(tsla_rows) == 1
    assert len(nvda_rows) == 1


def test_save_snapshot_stores_alert_text(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_make_result(alert="⚠️ 做市商對沖賣壓風險高"), "2026-08-01", db_path=db_path)
    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    assert rows[0]["alert"] == "⚠️ 做市商對沖賣壓風險高"


def test_save_snapshot_stores_decision_brief(tmp_path):
    db_path = tmp_path / "history.db"
    decision = {
        "action": "區間上緣，避免追價", "confidence": "中",
        "summary": "正 Gamma 壓抑波動，價格接近區間上緣。",
        "upside_trigger": "站穩 Call Wall $330 才重新評估向上突破",
        "downside_trigger": "跌破 Gamma Flip $317 轉為防守",
    }

    db_manager.save_snapshot(
        _make_result(decision=decision), "2026-08-01", db_path=db_path,
    )
    row = db_manager.get_recent_snapshots("TSLA", db_path=db_path)[0]

    assert row["decision_action"] == decision["action"]
    assert row["decision_confidence"] == "中"
    assert row["decision_summary"] == decision["summary"]
    assert row["decision_upside_trigger"] == decision["upside_trigger"]
    assert row["decision_downside_trigger"] == decision["downside_trigger"]


def test_snapshot_rerun_without_decision_preserves_existing_decision(tmp_path):
    db_path = tmp_path / "history.db"
    decision = {
        "action": "事件前觀望", "confidence": "低", "summary": "等待事件",
        "upside_trigger": "等待公布", "downside_trigger": "等待公布",
    }
    db_manager.save_snapshot(
        _make_result(spot=300.0, decision=decision), "2026-08-01", db_path=db_path,
    )

    db_manager.save_snapshot(
        _make_result(spot=311.21, decision=None), "2026-08-01", db_path=db_path,
    )
    row = db_manager.get_recent_snapshots("TSLA", db_path=db_path)[0]

    assert row["spot"] == 311.21
    assert row["decision_action"] == "事件前觀望"
    assert row["decision_summary"] == "等待事件"


def test_save_snapshot_stores_data_quality_score(tmp_path):
    db_path = tmp_path / "history.db"
    quality = {"score": 72, "label": "降級", "reason": "到期日覆蓋不足"}

    db_manager.save_snapshot(
        _make_result(data_quality=quality), "2026-08-01", db_path=db_path,
    )
    row = db_manager.get_recent_snapshots("TSLA", db_path=db_path)[0]

    assert row["data_quality_score"] == 72
    assert row["data_quality_label"] == "降級"
    assert row["data_quality_reason"] == "到期日覆蓋不足"


def _make_legs():
    return [
        {"action": "SELL", "option_type": "PUT", "strike_price": 300.0},
        {"action": "BUY", "option_type": "PUT", "strike_price": 295.0},
    ]


def test_save_and_get_pending_strategy_recommendation_round_trip(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_strategy_recommendation(
        symbol="TSLA", recommended_date="2026-07-01", strategy_name="Bull Put Spread",
        strategy_type="credit", legs=_make_legs(), net_premium=150.0, max_loss=350.0,
        expiry_date="2026-08-01", db_path=db_path,
    )
    pending = db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)
    assert len(pending) == 1
    assert pending[0]["symbol"] == "TSLA"
    assert pending[0]["resolved"] == 0
    assert json.loads(pending[0]["legs_json"]) == _make_legs()


def test_get_pending_strategy_recommendations_excludes_future_expiry(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_strategy_recommendation(
        symbol="TSLA", recommended_date="2026-07-01", strategy_name="Bull Put Spread",
        strategy_type="credit", legs=_make_legs(), net_premium=150.0, max_loss=350.0,
        expiry_date="2026-09-01", db_path=db_path,
    )
    pending = db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)
    assert pending == []


def test_save_strategy_recommendation_same_day_same_strategy_does_not_duplicate(tmp_path):
    db_path = tmp_path / "history.db"
    for _ in range(2):
        db_manager.save_strategy_recommendation(
            symbol="TSLA", recommended_date="2026-07-01", strategy_name="Bull Put Spread",
            strategy_type="credit", legs=_make_legs(), net_premium=150.0, max_loss=350.0,
            expiry_date="2026-08-01", db_path=db_path,
        )
    pending = db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)
    assert len(pending) == 1


def test_mark_strategy_resolved_updates_record_and_track_record(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_strategy_recommendation(
        symbol="TSLA", recommended_date="2026-07-01", strategy_name="Bull Put Spread",
        strategy_type="credit", legs=_make_legs(), net_premium=150.0, max_loss=350.0,
        expiry_date="2026-08-01", db_path=db_path,
    )
    pending = db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)
    db_manager.mark_strategy_resolved(
        recommendation_id=pending[0]["id"], settlement_spot=310.0, outcome="WIN",
        realized_pnl=150.0, db_path=db_path,
    )

    assert db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path) == []
    track_record = db_manager.get_strategy_track_record("TSLA", db_path=db_path)
    assert len(track_record) == 1
    assert track_record[0]["outcome"] == "WIN"
    assert track_record[0]["realized_pnl"] == 150.0
    assert track_record[0]["settlement_spot"] == 310.0
    assert track_record[0]["max_loss_hit"] == 0


def test_mark_strategy_resolved_stores_max_loss_hit(tmp_path):
    """max_loss_hit（是否被完全壓穿最大虧損，不是普通小賠）先前算完就丟掉，
    沒有存進資料庫——這是實測抓到的缺漏，記分板少了這個資訊沒辦法評估
    策略引擎的風險控管品質。
    """
    db_path = tmp_path / "history.db"
    db_manager.save_strategy_recommendation(
        symbol="TSLA", recommended_date="2026-07-01", strategy_name="Bull Put Spread",
        strategy_type="credit", legs=_make_legs(), net_premium=150.0, max_loss=350.0,
        expiry_date="2026-08-01", db_path=db_path,
    )
    pending = db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)
    db_manager.mark_strategy_resolved(
        recommendation_id=pending[0]["id"], settlement_spot=280.0, outcome="LOSS",
        realized_pnl=-350.0, max_loss_hit=True, db_path=db_path,
    )

    track_record = db_manager.get_strategy_track_record("TSLA", db_path=db_path)
    assert track_record[0]["max_loss_hit"] == 1


def test_get_strategy_track_record_filters_by_symbol(tmp_path):
    db_path = tmp_path / "history.db"
    for symbol in ("TSLA", "NVDA"):
        db_manager.save_strategy_recommendation(
            symbol=symbol, recommended_date="2026-07-01", strategy_name="Bull Put Spread",
            strategy_type="credit", legs=_make_legs(), net_premium=150.0, max_loss=350.0,
            expiry_date="2026-08-01", db_path=db_path,
        )
    for record in db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path):
        db_manager.mark_strategy_resolved(record["id"], 310.0, "WIN", 150.0, db_path=db_path)

    tsla_record = db_manager.get_strategy_track_record("TSLA", db_path=db_path)
    all_records = db_manager.get_strategy_track_record(db_path=db_path)
    assert len(tsla_record) == 1
    assert len(all_records) == 2


def _make_oi_legs():
    return [
        {"strike": 100.0, "call_oi": 500.0, "put_oi": 300.0},
        {"strike": 105.0, "call_oi": 200.0, "put_oi": 150.0},
    ]


def test_save_and_get_oi_snapshot_round_trip(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_oi_snapshot("TSLA", "2026-08-01", _make_oi_legs(), db_path=db_path)

    snapshot = db_manager.get_oi_snapshot("TSLA", "2026-08-01", db_path=db_path)

    assert snapshot == {
        100.0: {"call_oi": 500.0, "put_oi": 300.0},
        105.0: {"call_oi": 200.0, "put_oi": 150.0},
    }


def test_get_oi_snapshot_returns_empty_dict_when_no_data(tmp_path):
    db_path = tmp_path / "history.db"
    assert db_manager.get_oi_snapshot("TSLA", "2026-08-01", db_path=db_path) == {}


def test_save_oi_snapshot_same_symbol_date_and_strike_overwrites_not_duplicates(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_oi_snapshot("TSLA", "2026-08-01", _make_oi_legs(), db_path=db_path)
    # 同一天同一個履約價重存（例如排程重跑），應該覆蓋成新數字，不是疊加成兩筆；
    # 沒有重存到的履約價（105）維持原樣，不會被清空。
    db_manager.save_oi_snapshot(
        "TSLA", "2026-08-01", [{"strike": 100.0, "call_oi": 999.0, "put_oi": 999.0}], db_path=db_path,
    )

    snapshot = db_manager.get_oi_snapshot("TSLA", "2026-08-01", db_path=db_path)
    assert snapshot == {
        100.0: {"call_oi": 999.0, "put_oi": 999.0},
        105.0: {"call_oi": 200.0, "put_oi": 150.0},
    }


def test_get_most_recent_oi_snapshot_date_finds_latest_before_given_date(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_oi_snapshot("TSLA", "2026-07-30", _make_oi_legs(), db_path=db_path)
    db_manager.save_oi_snapshot("TSLA", "2026-07-31", _make_oi_legs(), db_path=db_path)

    assert db_manager.get_most_recent_oi_snapshot_date("TSLA", "2026-08-01", db_path=db_path) == "2026-07-31"


def test_get_most_recent_oi_snapshot_date_returns_none_when_no_earlier_data(tmp_path):
    db_path = tmp_path / "history.db"
    assert db_manager.get_most_recent_oi_snapshot_date("TSLA", "2026-08-01", db_path=db_path) is None


def test_get_most_recent_oi_snapshot_date_skips_gap_days(tmp_path):
    """排程可能漏跑好幾天——要找『實際上一次真的存過』的日期，不是假設
    『昨天』一定有資料。
    """
    db_path = tmp_path / "history.db"
    db_manager.save_oi_snapshot("TSLA", "2026-07-20", _make_oi_legs(), db_path=db_path)

    assert db_manager.get_most_recent_oi_snapshot_date("TSLA", "2026-08-01", db_path=db_path) == "2026-07-20"


def test_save_snapshot_stores_pinning_fields(tmp_path):
    db_path = tmp_path / "history.db"
    pinning = {
        "pin_strike": 320.0, "oi_concentration_pct": 18.5, "in_positive_gamma": True,
        "score": 78, "regime": "PINNING",
    }
    db_manager.save_snapshot(_make_result(pinning=pinning), "2026-08-01", db_path=db_path)

    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    assert rows[0]["pin_strike"] == 320.0
    assert rows[0]["pinning_oi_concentration_pct"] == 18.5
    assert rows[0]["pinning_in_positive_gamma"] == 1
    assert rows[0]["pinning_score"] == 78
    assert rows[0]["pinning_regime"] == "PINNING"


def test_save_snapshot_stores_null_pinning_when_none(tmp_path):
    """result.pinning 是加分項，可能因為計算失敗或期權鏈為空而是 None——
    要存成 NULL，不是硬塞一個假分數，呼叫端才能正確分辨『沒資料』跟
    『確認算出某個分數』。
    """
    db_path = tmp_path / "history.db"
    db_manager.save_snapshot(_make_result(pinning=None), "2026-08-01", db_path=db_path)

    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    assert rows[0]["pin_strike"] is None
    assert rows[0]["pinning_score"] is None
    assert rows[0]["pinning_regime"] is None


def test_save_snapshot_migrates_pre_pinning_schema_database(tmp_path):
    """模擬 Railway Volume 上『加入 pinning 欄位之前』就已經存在的舊
    daily_snapshots 表——沒有這幾個欄位，插入舊格式的一筆資料，確認接著
    呼叫 save_snapshot() 不會因為『no such column』而炸掉，是安全的
    自我修復遷移，而不是需要手動介入的斷線問題。
    """
    import sqlite3

    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE daily_snapshots (
            symbol TEXT NOT NULL, date TEXT NOT NULL, spot REAL NOT NULL,
            max_pain REAL NOT NULL, call_wall REAL NOT NULL, put_wall REAL NOT NULL,
            gamma_flip REAL, gamma_flip_distance_pct REAL, total_net_gex REAL NOT NULL,
            zero_dte_net_gex REAL NOT NULL, zero_dte_share_pct REAL, alert TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (symbol, date)
        )
        """
    )
    conn.execute(
        "INSERT INTO daily_snapshots (symbol, date, spot, max_pain, call_wall, put_wall, "
        "total_net_gex, zero_dte_net_gex) VALUES ('TSLA', '2026-07-31', 300.0, 300.0, 320.0, 280.0, 1.0, 0.0)"
    )
    conn.commit()
    conn.close()

    db_manager.save_snapshot(_make_result(spot=311.21), "2026-08-01", db_path=db_path)

    rows = db_manager.get_recent_snapshots("TSLA", db_path=db_path)
    assert len(rows) == 2
    old_row = next(r for r in rows if r["date"] == "2026-07-31")
    assert old_row["pin_strike"] is None  # 舊資料補上的新欄位是 NULL，不是報錯
    assert old_row["decision_action"] is None
    new_row = next(r for r in rows if r["date"] == "2026-08-01")
    assert "decision_confidence" in new_row


# ---------- signal_events ----------

def test_save_and_read_undelivered_watch_event(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch",
        reason="正 Gamma", signature="call_wall", payload={"wall": 110.0},
        db_path=db_path,
    )

    rows = db_manager.get_undelivered_watch_events("TSLA", db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["kind"] == "call_wall_breach"
    assert rows[0]["payload"]["wall"] == 110.0
    assert rows[0]["reason"] == "正 Gamma"


def test_silent_events_are_never_returned_as_watch(tmp_path):
    """靜默紀錄只落地，永遠不該被 drain 出來推播。"""
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "pinning_high",
        classified_tier="silent", delivered_tier="silent",
        reason="分數不足", signature="pinning", payload={"score": 40},
        db_path=db_path,
    )

    assert db_manager.get_undelivered_watch_events("TSLA", db_path=db_path) == []


def test_unusual_activity_updates_same_contract_instead_of_inserting_duplicates(tmp_path):
    db_path = tmp_path / "history.db"
    common = dict(
        symbol="SPCX", trading_date="2026-09-09", kind="unusual_activity",
        classified_tier="watch", delivered_tier="watch", reason="盤中無法判定開平倉",
        signature="put:150", db_path=db_path,
    )
    first_id = db_manager.save_signal_event(
        detected_at="2026-09-09T14:00:00+00:00",
        payload={"strike": 150, "side": "put", "volume": 25000, "ratio": 3.7, "text": "舊值"},
        **common,
    )
    second_id = db_manager.save_signal_event(
        detected_at="2026-09-09T15:00:00+00:00",
        payload={"strike": 150, "side": "put", "volume": 35000, "ratio": 5.0, "text": "最新值"},
        **common,
    )

    rows = db_manager.get_undelivered_watch_events("SPCX", db_path=db_path)

    assert second_id == first_id
    assert len(rows) == 1
    assert rows[0]["payload"]["volume"] == 35000
    assert rows[0]["payload"]["ratio"] == 5.0
    assert rows[0]["payload"]["text"] == "最新值"


def test_marking_delivered_removes_from_queue(tmp_path):
    db_path = tmp_path / "history.db"
    event_id = db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch",
        reason="正 Gamma", signature="call_wall", payload={},
        db_path=db_path,
    )

    db_manager.mark_events_delivered(
        [event_id], "2026-09-09T20:30:00+00:00", "daily_report", db_path=db_path,
    )

    assert db_manager.get_undelivered_watch_events("TSLA", db_path=db_path) == []


def test_count_urgent_delivered_is_global_across_symbols(tmp_path):
    """每日推播預算是跨所有標的合計，不是每檔各算一份。"""
    db_path = tmp_path / "history.db"
    for symbol in ("TSLA", "MU", "SPCX"):
        db_manager.save_signal_event(
            symbol, "2026-09-09T14:00:00+00:00", "2026-09-09", "put_wall_breach",
            classified_tier="urgent", delivered_tier="urgent",
            reason="保護優先", signature="put_wall", payload={},
            db_path=db_path,
        )

    assert db_manager.count_urgent_delivered("2026-09-09", db_path=db_path) == 3
    assert db_manager.count_urgent_delivered("2026-09-10", db_path=db_path) == 0


def test_demoted_event_keeps_classified_tier_urgent(tmp_path):
    """預算擠掉的訊號 delivered_tier 降級，但 classified_tier 必須維持
    urgent——否則日後績效統計會被當日到達順序污染。"""
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "put_wall_breach",
        classified_tier="urgent", delivered_tier="watch",
        reason="超出當日推播預算", signature="put_wall", payload={},
        db_path=db_path,
    )

    rows = db_manager.get_undelivered_watch_events("TSLA", db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["classified_tier"] == "urgent"
    assert db_manager.count_urgent_delivered("2026-09-09", db_path=db_path) == 0
