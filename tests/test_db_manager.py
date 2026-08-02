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


def _make_result(symbol="TSLA", spot=311.21, alert=None) -> _FakeResult:
    return _FakeResult(
        symbol=symbol, spot=spot, max_pain=315.0, call_wall=330.0, put_wall=300.0,
        gamma_flip=317.0, gamma_flip_distance_pct=-1.9,
        zero_dte_summary={"total_net_gex": 1_000_000.0, "zero_dte_net_gex": 0.0,
                           "ex_zero_dte_net_gex": 1_000_000.0, "zero_dte_share_pct": 0.0},
        alert=alert,
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
