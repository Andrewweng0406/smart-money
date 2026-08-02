from dataclasses import dataclass
from math import isinf

import pytest

from smart_money import (
    DEATH_LOOP_ALERT_TEXT,
    compute_iv_skew,
    compute_market_maker_pressure_score,
    compute_put_call_ratio,
    detect_unusual_activity,
)


@dataclass
class FakeLeg:
    strike: float
    call_oi: float = 0
    call_iv: float = 0
    call_volume: float = 0
    put_oi: float = 0
    put_iv: float = 0
    put_volume: float = 0
    call_bid: float = 0
    call_ask: float = 0
    put_bid: float = 0
    put_ask: float = 0


def test_compute_iv_skew_averages_otm_calls_and_puts():
    legs = [
        FakeLeg(strike=85, put_iv=0.40),
        FakeLeg(strike=90, put_iv=0.30),
        FakeLeg(strike=100, call_iv=0.99, put_iv=0.99),
        FakeLeg(strike=105, call_iv=0.20),
        FakeLeg(strike=115, call_iv=0.30),
    ]

    assert compute_iv_skew(legs, spot=100) == pytest.approx(0.10)


def test_compute_iv_skew_includes_configured_boundaries():
    legs = [
        FakeLeg(strike=90, put_iv=0.20),
        FakeLeg(strike=110, call_iv=0.35),
    ]

    assert compute_iv_skew(legs, spot=100, otm_min_pct=0.10, otm_max_pct=0.10) == pytest.approx(
        -0.15
    )


def test_compute_iv_skew_returns_none_when_either_side_has_no_sample():
    legs = [FakeLeg(strike=110, call_iv=0.25)]

    assert compute_iv_skew(legs, spot=100) is None


def test_compute_put_call_ratio_normal_case():
    legs = [
        FakeLeg(strike=95, call_volume=100, put_volume=150, call_oi=200, put_oi=300),
        FakeLeg(strike=105, call_volume=300, put_volume=50, call_oi=600, put_oi=100),
    ]

    assert compute_put_call_ratio(legs) == {
        "volume_ratio": pytest.approx(0.5),
        "oi_ratio": pytest.approx(0.5),
    }


def test_compute_put_call_ratio_handles_zero_denominators():
    legs = [FakeLeg(strike=100, put_volume=50, put_oi=80)]

    assert compute_put_call_ratio(legs) == {"volume_ratio": None, "oi_ratio": None}


def test_compute_put_call_ratio_can_have_only_one_zero_denominator():
    legs = [FakeLeg(strike=100, call_volume=100, put_volume=25, put_oi=80)]

    assert compute_put_call_ratio(legs) == {
        "volume_ratio": pytest.approx(0.25),
        "oi_ratio": None,
    }


def test_detect_unusual_activity_finds_both_sides_and_sorts_by_ratio():
    legs = [
        FakeLeg(
            strike=100,
            call_volume=500,
            call_oi=200,
            put_volume=120,
            put_oi=200,
        ),
        FakeLeg(strike=105, call_volume=100, call_oi=400, put_volume=300, put_oi=100),
    ]

    result = detect_unusual_activity(legs)

    assert [(item["strike"], item["side"], item["ratio"]) for item in result] == [
        (105, "put", 3.0),
        (100, "call", 2.5),
        (100, "put", 0.6),
    ]


def test_detect_unusual_activity_treats_zero_oi_as_infinite_ratio():
    legs = [FakeLeg(strike=350, call_volume=100, call_oi=0)]

    result = detect_unusual_activity(legs)

    assert len(result) == 1
    assert result[0]["side"] == "call"
    assert isinf(result[0]["ratio"])


def test_detect_unusual_activity_applies_threshold_and_top_n():
    legs = [
        FakeLeg(strike=90, call_volume=99, call_oi=1),
        FakeLeg(strike=100, call_volume=100, call_oi=100),
        FakeLeg(strike=110, call_volume=300, call_oi=100),
    ]

    result = detect_unusual_activity(legs, min_volume_oi_ratio=1.0, top_n=1)

    assert result == [
        {"strike": 110, "side": "call", "volume": 300, "oi": 100, "ratio": 3.0}
    ]


def test_pressure_score_triggers_death_loop_alert():
    legs = [
        FakeLeg(strike=100, call_volume=600, call_oi=1000, put_volume=300, put_oi=800),
        FakeLeg(strike=105, call_volume=400, call_oi=500, put_volume=100, put_oi=200),
    ]

    result = compute_market_maker_pressure_score(
        spot=100, put_wall=99, in_negative_gamma=True, legs=legs
    )

    assert result == {
        "score": 90,
        "label": "極高",
        "is_death_loop_alert": True,
        "alert_text": DEATH_LOOP_ALERT_TEXT,
    }


def test_pressure_score_does_not_alert_outside_negative_gamma():
    legs = [FakeLeg(strike=100, call_volume=1000, call_oi=1000, put_volume=100)]

    result = compute_market_maker_pressure_score(
        spot=100, put_wall=90, in_negative_gamma=False, legs=legs
    )

    assert result == {
        "score": 30,
        "label": "中",
        "is_death_loop_alert": False,
        "alert_text": None,
    }


def test_pressure_score_uses_linear_put_wall_component_and_zero_call_oi():
    legs = [FakeLeg(strike=100, call_volume=500, call_oi=0, put_volume=10)]

    result = compute_market_maker_pressure_score(
        spot=100, put_wall=94, in_negative_gamma=False, legs=legs
    )

    assert result == {
        "score": 10,
        "label": "低",
        "is_death_loop_alert": False,
        "alert_text": None,
    }
