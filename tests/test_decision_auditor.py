"""決策審核測試：只用合成快照，不碰專案真正的 history.db。"""

from __future__ import annotations

import decision_auditor
import pytest


def _row(date, spot, action=None, confidence="中", put_wall=90.0, call_wall=110.0):
    return {
        "date": date, "spot": spot, "put_wall": put_wall, "call_wall": call_wall,
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

    assert "樣本不足（1 段）" in text
    assert "成功率 100%" not in text
    assert "觀望不會被算成命中" in text
