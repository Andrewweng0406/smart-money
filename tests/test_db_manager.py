"""db_manager 的讀寫測試——用 tmp_path 給獨立的 sqlite 檔案，不會碰到專案
真正的 history.db。
"""

from __future__ import annotations

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
