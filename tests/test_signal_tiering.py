"""signal_tiering.py 測試——純函式，全部用合成 payload/regime，不碰 I/O。"""

from __future__ import annotations

import pytest

import signal_tiering as st


def _regime(negative_gamma: bool) -> dict:
    return st.build_regime(
        spot=90.0 if negative_gamma else 110.0, gamma_flip=100.0, total_net_gex=1.0,
    )


def test_build_regime_uses_gamma_flip_when_available():
    regime = st.build_regime(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)
    assert regime["negative_gamma"] is True
    assert regime["source"] == "gamma_flip"


def test_build_regime_falls_back_to_net_gex_when_gamma_flip_missing():
    regime = st.build_regime(spot=90.0, gamma_flip=None, total_net_gex=-5.0)
    assert regime["negative_gamma"] is True
    assert regime["source"] == "net_gex_fallback"


def test_put_wall_breach_is_urgent_in_positive_gamma():
    tier, reason = st.classify(st.KIND_PUT_WALL_BREACH, {}, _regime(negative_gamma=False))
    assert tier == "urgent"
    assert reason


def test_put_wall_breach_is_urgent_in_negative_gamma():
    tier, _ = st.classify(st.KIND_PUT_WALL_BREACH, {}, _regime(negative_gamma=True))
    assert tier == "urgent"


def test_call_wall_breach_is_urgent_only_in_negative_gamma():
    assert st.classify(st.KIND_CALL_WALL_BREACH, {}, _regime(True))[0] == "urgent"
    assert st.classify(st.KIND_CALL_WALL_BREACH, {}, _regime(False))[0] == "watch"


def test_mm_pressure_is_urgent_only_in_negative_gamma():
    assert st.classify(st.KIND_MM_PRESSURE, {}, _regime(True))[0] == "urgent"
    assert st.classify(st.KIND_MM_PRESSURE, {}, _regime(False))[0] == "watch"


@pytest.mark.parametrize("ratio", [3.0, 6.0, 100.0, float("inf")])
def test_unusual_activity_is_never_urgent(ratio):
    """盤中無法區分開倉/平倉（OI 隔夜才結算），所以永遠不得升 urgent。"""
    tier, _ = st.classify(
        st.KIND_UNUSUAL_ACTIVITY, {"ratio": ratio, "likely_opening": True}, _regime(True),
    )
    assert tier == "watch"


def test_unusual_activity_below_ratio_is_silent():
    tier, _ = st.classify(st.KIND_UNUSUAL_ACTIVITY, {"ratio": 2.9}, _regime(True))
    assert tier == "silent"


def test_unusual_activity_at_ratio_boundary_is_watch():
    tier, _ = st.classify(st.KIND_UNUSUAL_ACTIVITY, {"ratio": 3.0}, _regime(True))
    assert tier == "watch"


def test_pinning_at_threshold_is_watch():
    assert st.classify(st.KIND_PINNING_HIGH, {"score": 70}, _regime(False))[0] == "watch"


def test_pinning_below_threshold_is_silent():
    assert st.classify(st.KIND_PINNING_HIGH, {"score": 69}, _regime(False))[0] == "silent"


def test_reason_notes_fallback_regime_source():
    """用退化路徑判斷 regime 時，理由必須寫明，否則日後稽核分不出來。"""
    regime = st.build_regime(spot=90.0, gamma_flip=None, total_net_gex=-5.0)
    _, reason = st.classify(st.KIND_CALL_WALL_BREACH, {}, regime)
    assert "net_gex" in reason


def test_urgent_priority_orders_put_wall_first():
    assert st.urgent_priority(st.KIND_PUT_WALL_BREACH) < st.urgent_priority(st.KIND_MM_PRESSURE)
    assert st.urgent_priority(st.KIND_MM_PRESSURE) < st.urgent_priority(st.KIND_CALL_WALL_BREACH)
