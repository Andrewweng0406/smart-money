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


def test_build_signal_audit_report_handles_small_sample(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 100.0)

    report = signal_auditor.build_signal_audit_report("TSLA", db_path=db_path)

    assert "樣本太少" in report
    assert "TSLA" in report


def test_build_signal_audit_report_includes_signal_sections(tmp_path):
    db_path = tmp_path / "history.db"
    _save(db_path, "2026-08-03", 115.0, call_wall=110.0)
    _save(db_path, "2026-08-04", 120.0)
    _save(db_path, "2026-08-05", 121.0)
    _save(db_path, "2026-08-06", 122.0)
    _save(db_path, "2026-08-07", 123.0)
    _save(db_path, "2026-08-10", 124.0)

    report = signal_auditor.build_signal_audit_report("TSLA", db_path=db_path, horizons=(1,))

    assert "Call Wall 突破續漲" in report
    assert "成功率" in report
    assert "定義" in report
