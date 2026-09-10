"""signal_auditor.py 測試——用合成 daily_snapshots 驗證 Telegram 實戰訊號
審核的統計定義，不連網、不讀真實 history.db。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import db_manager
import signal_auditor


@dataclass
class _FakeResult:
    symbol: str
    spot: float
    max_pain: float = 100.0
    call_wall: float = 110.0
    put_wall: float = 90.0
    gamma_flip: float | None = None
    gamma_flip_distance_pct: float | None = None
    zero_dte_summary: dict | None = None
    alert: str | None = None
    pinning: dict | None = None

    def __post_init__(self):
        if self.zero_dte_summary is None:
            self.zero_dte_summary = {
                "total_net_gex": 0,
                "zero_dte_net_gex": 0,
                "ex_zero_dte_net_gex": 0,
                "zero_dte_share_pct": 0.0,
            }


def _save(
    db_path,
    date_str,
    spot,
    *,
    symbol="TSLA",
    call_wall=110.0,
    put_wall=90.0,
    gamma_flip=None,
    pinning_score=None,
    alert=None,
):
    pinning = None
    if pinning_score is not None:
        pinning = {
            "pin_strike": spot,
            "oi_concentration_pct": 10.0,
            "in_positive_gamma": True,
            "score": pinning_score,
            "regime": "PINNING",
        }
    db_manager.save_snapshot(
        _FakeResult(
            symbol=symbol,
            spot=spot,
            call_wall=call_wall,
            put_wall=put_wall,
            gamma_flip=gamma_flip,
            alert=alert,
            pinning=pinning,
        ),
        date_str,
        db_path=db_path,
    )


def test_audit_signal_performance_counts_call_wall_continuation(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 115.0, call_wall=110.0)
    _save(db_path, "2026-08-04", 120.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["call_wall_break"][1]
    assert stat["sample_size"] == 1
    assert stat["success_rate_pct"] == 100.0
    assert stat["avg_return_pct"] == pytest.approx(4.3478, 0.01)


def test_audit_signal_performance_counts_put_wall_continuation(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 85.0, put_wall=90.0)
    _save(db_path, "2026-08-04", 80.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["put_wall_break"][1]
    assert stat["sample_size"] == 1
    assert stat["success_rate_pct"] == 100.0
    assert stat["avg_return_pct"] == pytest.approx(-5.8823, 0.01)


def test_audit_signal_performance_gamma_flip_level_hold(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 101.0, gamma_flip=100.0)
    _save(db_path, "2026-08-04", 102.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["gamma_flip_touch"][1]
    assert stat["sample_size"] == 1
    assert stat["success_rate_pct"] == 100.0


def test_audit_signal_performance_pinning_high_measures_narrow_move(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 100.0, pinning_score=90)
    _save(db_path, "2026-08-04", 101.0)
    _save(db_path, "2026-08-05", 104.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1, 2))

    assert audit["signals"]["pinning_high"][1]["success_rate_pct"] == 100.0
    assert audit["signals"]["pinning_high"][2]["success_rate_pct"] == 0.0


def test_audit_signal_performance_alert_day_has_returns_without_success_rate(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 100.0, alert="做市商對沖賣壓風險高")
    _save(db_path, "2026-08-04", 95.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["alert_day"][1]
    assert stat["sample_size"] == 1
    assert stat["success_rate_pct"] is None
    assert stat["avg_return_pct"] == pytest.approx(-5.0)


def _save_series(
    db_path,
    spots,
    *,
    call_wall=110.0,
    put_wall=90.0,
    gamma_flip=None,
    alerts=None,
    pinning_scores=None,
):
    """存一串連續交易日的快照，日期從 2026-08-03 起遞增。

    alerts / pinning_scores 用「索引 -> 值」的 dict 指定哪幾天要觸發，
    方便測試「連續觸發」跟「間隔觸發」的差別。
    """
    alerts = alerts or {}
    pinning_scores = pinning_scores or {}
    for i, spot in enumerate(spots):
        _save(
            db_path,
            f"2026-08-{3 + i:02d}",
            spot,
            call_wall=call_wall,
            put_wall=put_wall,
            gamma_flip=gamma_flip,
            alert=alerts.get(i),
            pinning_score=pinning_scores.get(i),
        )


def test_excess_return_is_zero_when_signal_has_no_edge_in_trending_market(tmp_path):
    """多頭漂移下，沒有預測力的訊號其超額報酬必須是 0。

    這是這個模組最重要的性質：價格每天固定漲 1%，任何一天進場的 1D 報酬
    都是 +1%，所以不管訊號挑哪幾天，它的平均報酬都等於無條件基準——
    excess_return_pct 必須歸零，不能因為大盤在漲就顯示成「訊號有效」。
    """
    db_path = tmp_path / "history.db"
    spots = [100 * (1.01 ** i) for i in range(12)]
    _save_series(db_path, spots, alerts={i: "測試警報" for i in (0, 2, 4, 6, 8)})

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["alert_day"][1]
    assert stat["sample_size"] == 5
    assert stat["avg_return_pct"] == pytest.approx(1.0)
    assert stat["baseline_avg_return_pct"] == pytest.approx(1.0)
    assert stat["excess_return_pct"] == pytest.approx(0.0, abs=1e-9)


def test_consecutive_trigger_days_collapse_into_one_episode(tmp_path):
    """連續觸發的同一訊號只算一段 episode。

    spot 連續 8 天待在 Call Wall 上方是「一段行情」，不是 8 個獨立事件。
    不去重的話有效樣本會被嚴重高估，勝率的信賴區間會假性收窄。
    """
    db_path = tmp_path / "history.db"
    _save_series(db_path, [115, 116, 117, 118, 119, 120, 121, 122], call_wall=110.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["call_wall_break"][1]
    assert stat["trigger_days"] == 8
    assert stat["sample_size"] == 1


def test_non_consecutive_triggers_count_as_separate_episodes(tmp_path):
    """中間有一天沒觸發，就算兩段獨立的 episode。"""
    db_path = tmp_path / "history.db"
    # 索引 0,1 在 Call Wall 上方，索引 2 掉下來，索引 3,4 再回到上方
    _save_series(db_path, [115, 116, 105, 115, 116, 117], call_wall=110.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["call_wall_break"][1]
    assert stat["trigger_days"] == 5
    assert stat["sample_size"] == 2


def test_sample_below_minimum_is_flagged_insufficient(tmp_path):
    """樣本數低於門檻時要標記為不足，不能只丟一個百分比出去。

    原本 n=1 也會印「成功率 0%」，那個數字沒有任何統計意義卻看起來像結論。
    """
    db_path = tmp_path / "history.db"
    _save_series(db_path, [85, 80, 81, 82], put_wall=90.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["put_wall_break"][1]
    assert stat["sample_size"] < signal_auditor.MIN_SAMPLE_SIZE
    assert stat["sufficient_sample"] is False


def test_baseline_success_rate_is_reported_for_comparison(tmp_path):
    """勝率必須附上同期的無條件基準，否則無法判斷訊號有沒有加值。"""
    db_path = tmp_path / "history.db"
    spots = [100 * (1.01 ** i) for i in range(12)]
    _save_series(db_path, spots, call_wall=110.0)

    audit = signal_auditor.audit_signal_performance("TSLA", db_path=db_path, horizons=(1,))

    stat = audit["signals"]["call_wall_break"][1]
    # 每天都在漲，所以無條件上漲比例是 100%
    assert stat["baseline_success_rate_pct"] == pytest.approx(100.0)
    assert stat["baseline_sample_size"] > 0


def test_report_shows_baseline_comparison(tmp_path):
    """報告文字要並排顯示基準，讓噪音訊號無所遁形。"""
    db_path = tmp_path / "history.db"
    spots = [100 * (1.01 ** i) for i in range(12)]
    _save_series(db_path, spots, alerts={i: "測試警報" for i in (0, 2, 4, 6, 8)})

    report = signal_auditor.build_signal_audit_report("TSLA", db_path=db_path, horizons=(1,))

    assert "基準" in report
    assert "超額" in report


def test_report_does_not_print_percentage_for_insufficient_sample(tmp_path):
    """樣本不足時報告不能印出成功率百分比。"""
    db_path = tmp_path / "history.db"
    _save_series(db_path, [85, 80, 81, 82, 83, 84], put_wall=90.0)

    report = signal_auditor.build_signal_audit_report("TSLA", db_path=db_path, horizons=(1,))

    put_wall_section = report.split("Put Wall 跌破續跌")[1].split("\n\n")[0]
    assert "成功率" not in put_wall_section
    assert "樣本不足" in put_wall_section


def test_build_signal_audit_report_handles_small_sample(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 100.0)

    report = signal_auditor.build_signal_audit_report("TSLA", db_path=db_path)

    assert "樣本太少" in report
    assert "TSLA" in report


def test_build_signal_audit_report_includes_signal_sections(tmp_path):
    """樣本足夠時，報告要印出成功率區塊。

    價格在 Call Wall 上下交替，讓每次突破都是獨立的一段 episode，
    湊到門檻以上的樣本數（連續觸發會被收斂成一段，印不出百分比）。
    """
    db_path = tmp_path / "history.db"
    _save_series(
        db_path,
        [115, 105, 115, 105, 115, 105, 115, 105, 115, 105, 115, 120],
        call_wall=110.0,
    )

    report = signal_auditor.build_signal_audit_report("TSLA", db_path=db_path, horizons=(1,))

    assert "Call Wall 突破續漲" in report
    assert "成功率" in report
    assert "定義" in report
