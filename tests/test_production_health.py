"""生產健康判斷只吃合成快照，不碰真實 Volume。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import production_health


ET = ZoneInfo("America/New_York")


def _row(**overrides):
    row = {
        "date": "2026-09-11", "spot": 100.0, "data_quality_score": 99,
        "decision_action": "區間應對，不追方向", "decision_confidence": "中",
        "decision_gamma_regime": "positive", "decision_price_zone": "inside_walls",
        "decision_event_regime": "normal", "decision_zero_dte_regime": "normal",
        "decision_data_regime": "usable",
    }
    row.update(overrides)
    return row


def test_expected_snapshot_date_uses_friday_during_weekend():
    now = datetime(2026, 9, 13, 12, 0, tzinfo=ET)

    assert production_health.expected_snapshot_date(now).isoformat() == "2026-09-11"


def test_expected_snapshot_date_uses_prior_session_before_daily_run():
    now = datetime(2026, 9, 14, 10, 0, tzinfo=ET)

    assert production_health.expected_snapshot_date(now).isoformat() == "2026-09-11"


def test_expected_snapshot_date_uses_today_after_daily_run():
    now = datetime(2026, 9, 14, 16, 31, tzinfo=ET)

    assert production_health.expected_snapshot_date(now).isoformat() == "2026-09-14"


def test_assess_symbol_health_accepts_complete_snapshot_and_oi():
    result = production_health.assess_symbol_health(
        "TSLA", _row(), "2026-09-11", oi_strike_count=120,
    )

    assert result["healthy"] is True
    assert result["issues"] == []


def test_assess_symbol_health_reports_all_material_gaps():
    result = production_health.assess_symbol_health(
        "TSLA", _row(
            spot=float("nan"), data_quality_score=float("nan"),
            decision_event_regime=None,
        ),
        "2026-09-11", oi_strike_count=0,
    )

    assert result["healthy"] is False
    assert "現貨價格無效" in result["issues"]
    assert "資料健康分數無效" in result["issues"]
    assert "決策情境缺失" in result["issues"]
    assert "OI 快照缺失" in result["issues"]


def test_assess_symbol_health_rejects_stale_snapshot():
    result = production_health.assess_symbol_health(
        "TSLA", _row(date="2026-09-10"), "2026-09-11", oi_strike_count=100,
    )

    assert result["healthy"] is False
    assert any("缺少 2026-09-11 快照" in issue for issue in result["issues"])
