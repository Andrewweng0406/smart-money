"""決策審核測試：只用合成快照，不碰專案真正的 history.db。"""

from __future__ import annotations

import decision_auditor
import pytest


def _row(
    date, spot, action=None, confidence="中", put_wall=90.0,
    call_wall=110.0, gamma_flip=100.0,
):
    return {
        "date": date, "spot": spot, "put_wall": put_wall, "call_wall": call_wall,
        "gamma_flip": gamma_flip,
        "decision_action": action, "decision_confidence": confidence,
        "decision_summary": "測試摘要",
    }


def test_breakout_decision_counts_confirmation_and_invalidation():
    rows = [
        _row("2026-09-01", 112, "突破觀察，等待站穩"),
        _row("2026-09-02", 114),
        _row("2026-09-03", 113, "突破觀察，等待站穩"),
        _row("2026-09-04", 108),
    ]

    audit = decision_auditor.audit_decision_rows(rows, horizon=1, min_sample_size=1)
    stats = audit["actions"]["突破觀察，等待站穩"]

    assert stats["sample_size"] == 2
    assert stats["success_count"] == 1
    assert stats["success_rate_pct"] == 50.0
    assert [event["outcome"] for event in stats["events"]] == ["confirmed", "invalidated"]


def test_observation_decision_never_manufactures_a_win_rate():
    rows = [
        _row("2026-09-01", 100, "事件前觀望", confidence="低"),
        _row("2026-09-02", 106),
    ]

    audit = decision_auditor.audit_decision_rows(rows, horizon=1, min_sample_size=1)
    stats = audit["actions"]["事件前觀望"]

    assert stats["sample_size"] == 1
    assert stats["success_rate_pct"] is None
    assert stats["avg_abs_return_pct"] == 6.0
    assert stats["events"][0]["outcome"] == "observed"


def test_consecutive_same_decisions_are_one_episode():
    rows = [
        _row("2026-09-01", 100, "區間應對，不追方向"),
        _row("2026-09-02", 101, "區間應對，不追方向"),
        _row("2026-09-03", 102, "區間應對，不追方向"),
        _row("2026-09-04", 103),
    ]

    audit = decision_auditor.audit_decision_rows(rows, horizon=1, min_sample_size=1)

    assert audit["decision_days"] == 3
    assert audit["episode_count"] == 1
    assert audit["actions"]["區間應對，不追方向"]["sample_size"] == 1


def test_latest_decision_is_pending_until_future_snapshot_exists():
    rows = [_row("2026-09-01", 100, "區間上緣，避免追價")]

    audit = decision_auditor.audit_decision_rows(rows, horizon=1)

    assert audit["matured_episodes"] == 0
    assert audit["pending_episodes"] == 1
    assert audit["recent"][0]["outcome"] == "pending"


def test_evaluate_decision_returns_same_result_used_by_audit():
    previous = _row("2026-09-01", 112, "突破觀察，等待站穩")

    event = decision_auditor.evaluate_decision(
        previous, future_spot=114, future_date="2026-09-02",
    )

    assert event["outcome"] == "confirmed"
    assert event["success"] is True
    assert event["return_pct"] == pytest.approx(1.7857, rel=1e-3)


def test_report_refuses_percentage_below_minimum_sample(monkeypatch):
    monkeypatch.setattr(
        decision_auditor.db_manager, "get_recent_snapshots",
        lambda *a, **k: [
            _row("2026-09-01", 112, "突破觀察，等待站穩"),
            _row("2026-09-02", 114),
        ],
    )

    text = decision_auditor.build_decision_audit_report("TSLA")

    assert "同類歷史樣本不足（1D 1/5段；3D 0/5段；5D 0/5段）" in text
    assert "成功率 100%" not in text
    assert "觀望不會被算成命中" in text


def test_multi_horizon_audit_matures_each_horizon_independently():
    rows = [
        _row("2026-09-01", 112, "突破觀察，等待站穩", confidence="高"),
        _row("2026-09-02", 114),
        _row("2026-09-03", 109),
        _row("2026-09-04", 108),
    ]

    audit = decision_auditor.audit_decision_horizons(
        rows, horizons=(1, 3, 5), min_sample_size=1,
    )

    assert audit["horizons"][1]["actions"]["突破觀察，等待站穩"]["success_rate_pct"] == 100.0
    assert audit["horizons"][3]["actions"]["突破觀察，等待站穩"]["success_rate_pct"] == 0.0
    assert audit["horizons"][5]["pending_episodes"] == 1


def test_confidence_calibration_keeps_levels_separate():
    rows = [
        _row("2026-09-01", 112, "突破觀察，等待站穩", confidence="高"),
        _row("2026-09-02", 114),
        _row("2026-09-03", 112, "突破觀察，等待站穩", confidence="中"),
        _row("2026-09-04", 108),
    ]

    audit = decision_auditor.audit_decision_rows(rows, horizon=1, min_sample_size=1)

    assert audit["confidence"]["高"]["success_rate_pct"] == 100.0
    assert audit["confidence"]["中"]["success_rate_pct"] == 0.0


def test_decision_evidence_refuses_rate_when_same_action_sample_is_small():
    rows = [
        _row("2026-09-01", 112, "突破觀察，等待站穩"),
        _row("2026-09-02", 114),
    ]

    evidence = decision_auditor.build_decision_evidence(
        rows, "突破觀察，等待站穩", horizons=(1, 3, 5), min_sample_size=5,
    )

    assert evidence["sufficient_sample"] is False
    assert evidence["text"] == (
        "同類歷史樣本不足（1D 1/5段；3D 0/5段；5D 0/5段），暫不估計成功率"
    )
    assert "100%" not in evidence["text"]


def test_decision_evidence_reports_all_mature_horizons_with_enough_samples():
    rows = []
    for day in range(1, 16, 3):
        rows.extend([
            _row(f"2026-09-{day:02d}", 112, "突破觀察，等待站穩"),
            _row(f"2026-09-{day + 1:02d}", 114),
            _row(f"2026-09-{day + 2:02d}", 116),
        ])

    evidence = decision_auditor.build_decision_evidence(
        rows, "突破觀察，等待站穩", horizons=(1, 3, 5), min_sample_size=2,
    )

    assert evidence["sufficient_sample"] is True
    assert "1D 100%（5段）" in evidence["text"]
    assert "3D" in evidence["text"]
    assert "5D" in evidence["text"]
    assert "1D路徑 5確認/0失效/0未觸發" in evidence["text"]


def test_default_audit_report_shows_multi_horizon_and_confidence_calibration(monkeypatch):
    rows = []
    for day in range(1, 16, 3):
        rows.extend([
            _row(f"2026-09-{day:02d}", 112, "突破觀察，等待站穩", confidence="中"),
            _row(f"2026-09-{day + 1:02d}", 114),
            _row(f"2026-09-{day + 2:02d}", 116),
        ])
    monkeypatch.setattr(
        decision_auditor.db_manager, "get_recent_snapshots", lambda *a, **k: rows,
    )

    text = decision_auditor.build_decision_audit_report("TSLA")

    assert "多期限驗證" in text
    assert "1D 100%（5段）" in text
    assert "3D" in text
    assert "5D" in text
    assert "信心校準（1D）" in text
    assert "中：100%（5/5段）" in text


def test_breakout_path_keeps_first_confirmation_even_if_horizon_close_fails():
    decision = _row("2026-09-01", 112, "突破觀察，等待站穩")
    future_rows = [
        _row("2026-09-02", 114),
        _row("2026-09-03", 108),
        _row("2026-09-04", 99),
    ]

    path = decision_auditor.evaluate_decision_path(decision, future_rows)

    assert path["outcome"] == "confirmed_first"
    assert path["resolved_date"] == "2026-09-02"
    assert path["max_upside_excursion_pct"] == pytest.approx(1.7857, rel=1e-3)
    assert path["max_downside_excursion_pct"] == pytest.approx(-11.6071, rel=1e-3)


def test_breakout_path_records_invalidation_before_later_confirmation():
    decision = _row("2026-09-01", 112, "突破觀察，等待站穩")
    future_rows = [
        _row("2026-09-02", 99),
        _row("2026-09-03", 115),
    ]

    path = decision_auditor.evaluate_decision_path(decision, future_rows)

    assert path["outcome"] == "invalidated_first"
    assert path["resolved_date"] == "2026-09-02"


def test_path_excursions_use_zero_when_price_never_moves_that_direction():
    decision = _row("2026-09-01", 100, "事件前觀望")

    only_up = decision_auditor.evaluate_decision_path(
        decision, [_row("2026-09-02", 102), _row("2026-09-03", 105)],
    )
    only_down = decision_auditor.evaluate_decision_path(
        decision, [_row("2026-09-02", 98), _row("2026-09-03", 95)],
    )

    assert only_up["max_downside_excursion_pct"] == 0.0
    assert only_down["max_upside_excursion_pct"] == 0.0


def test_range_path_cannot_hide_an_intermediate_wall_break():
    decision = _row("2026-09-01", 100, "區間應對，不追方向")
    future_rows = [
        _row("2026-09-02", 112),
        _row("2026-09-03", 100),
    ]

    path = decision_auditor.evaluate_decision_path(decision, future_rows)

    assert path["outcome"] == "invalidated_first"
    assert path["resolved_date"] == "2026-09-02"


def test_audit_event_contains_full_horizon_path_not_only_endpoint():
    rows = [
        _row("2026-09-01", 100, "區間應對，不追方向"),
        _row("2026-09-02", 112),
        _row("2026-09-03", 100),
    ]

    audit = decision_auditor.audit_decision_rows(rows, horizon=2, min_sample_size=1)
    event = audit["actions"]["區間應對，不追方向"]["events"][0]

    assert event["outcome"] == "confirmed"
    assert event["path"]["outcome"] == "invalidated_first"
    assert audit["actions"]["區間應對，不追方向"]["path_invalidated_count"] == 1


def test_report_exposes_path_counts_without_claiming_a_rate(monkeypatch):
    monkeypatch.setattr(
        decision_auditor.db_manager, "get_recent_snapshots",
        lambda *a, **k: [
            _row("2026-09-01", 100, "區間應對，不追方向"),
            _row("2026-09-02", 112),
            _row("2026-09-03", 100),
        ],
    )

    text = decision_auditor.build_decision_audit_report("TSLA", horizon=2)

    assert "期間路徑：先確認 0｜先失效 1｜未觸發 0" in text
    assert "路徑成功率" not in text
