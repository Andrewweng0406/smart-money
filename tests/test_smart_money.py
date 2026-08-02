from dataclasses import dataclass
from math import isinf

import pytest

from gex_engine import black_scholes_delta
from smart_money import (
    DEATH_LOOP_ALERT_TEXT,
    compute_delta_adjusted_call_volume,
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
    time_to_expiry_years: float = 30 / 365


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


def test_compute_iv_skew_excludes_zero_iv_noise_from_average():
    """IV=0 是 data_fetcher.py 清洗掉異常值後留下的缺值標記（真實選擇權的
    IV不可能是0），不是「這檔真的零波動」——混進平均會把 IV Skew 拉向
    錯誤方向，製造出假的偏斜訊號，這是實測抓到的真bug。
    """
    legs = [
        FakeLeg(strike=90, put_iv=0.30),
        FakeLeg(strike=88, put_iv=0.0),  # 缺值，應該被排除，不是「0波動的put」
        FakeLeg(strike=105, call_iv=0.20),
    ]

    assert compute_iv_skew(legs, spot=100) == pytest.approx(0.10)  # 0.30 - 0.20，不受put_iv=0污染


def test_compute_iv_skew_returns_none_when_only_zero_iv_samples_on_one_side():
    """一側全部都是缺值（IV<=0）時，等同那一側沒有任何有效樣本，應該回傳
    None，不是拿缺值硬湊出一個看起來有效但其實沒意義的平均。
    """
    legs = [
        FakeLeg(strike=90, put_iv=0.0),
        FakeLeg(strike=105, call_iv=0.20),
    ]

    assert compute_iv_skew(legs, spot=100) is None


# ---------- compute_delta_adjusted_call_volume ----------

def test_compute_delta_adjusted_call_volume_weighs_by_delta():
    """深度價外的Call delta接近0，加權後貢獻很小；平值附近delta較大，
    同樣張數應該貢獻更多——這是審查抓出的訊號品質問題：原始張數沒辦法
    分辨『一堆便宜的價外樂透單』跟『真正大戶在建立實質曝險』。
    """
    atm_leg = FakeLeg(strike=100, call_iv=0.4, call_volume=1000, time_to_expiry_years=30 / 365)
    deep_otm_leg = FakeLeg(strike=200, call_iv=0.4, call_volume=1000, time_to_expiry_years=30 / 365)

    atm_result = compute_delta_adjusted_call_volume([atm_leg], spot=100)
    deep_otm_result = compute_delta_adjusted_call_volume([deep_otm_leg], spot=100)

    assert atm_result > deep_otm_result


def test_compute_delta_adjusted_call_volume_matches_manual_delta_calculation():
    leg = FakeLeg(strike=100, call_iv=0.4, call_volume=500, time_to_expiry_years=30 / 365)
    expected_delta = abs(float(black_scholes_delta(
        spot=100, strike=100, time_to_expiry_years=30 / 365, iv=0.4, option_type="call",
    )))

    result = compute_delta_adjusted_call_volume([leg], spot=100)

    assert result == pytest.approx(500 * expected_delta)


def test_compute_delta_adjusted_call_volume_sums_across_legs():
    legs = [
        FakeLeg(strike=100, call_iv=0.4, call_volume=500, time_to_expiry_years=30 / 365),
        FakeLeg(strike=105, call_iv=0.4, call_volume=300, time_to_expiry_years=30 / 365),
    ]
    total = compute_delta_adjusted_call_volume(legs, spot=100)
    single_leg_totals = sum(compute_delta_adjusted_call_volume([leg], spot=100) for leg in legs)
    assert total == pytest.approx(single_leg_totals)


def test_compute_delta_adjusted_call_volume_skips_invalid_legs():
    legs = [
        FakeLeg(strike=100, call_iv=0.0, call_volume=500),  # IV=0，資料清洗雜訊
        FakeLeg(strike=105, call_iv=0.4, call_volume=0.0),  # 沒有成交量
        FakeLeg(strike=110, call_iv=0.4, call_volume=100, time_to_expiry_years=0.0),  # 已到期
    ]
    assert compute_delta_adjusted_call_volume(legs, spot=100) == 0.0


def test_compute_delta_adjusted_call_volume_empty_legs_returns_zero():
    assert compute_delta_adjusted_call_volume([], spot=100) == 0.0


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
        {"strike": 110, "side": "call", "volume": 300, "oi": 100, "ratio": 3.0, "likely_opening": None}
    ]


def test_detect_unusual_activity_likely_opening_none_without_previous_oi():
    legs = [FakeLeg(strike=100, call_volume=300, call_oi=100)]
    result = detect_unusual_activity(legs, min_volume_oi_ratio=1.0)
    assert result[0]["likely_opening"] is None


def test_detect_unusual_activity_likely_opening_true_when_oi_increased():
    legs = [FakeLeg(strike=100, call_volume=300, call_oi=100)]
    previous_oi = {100: {"call_oi": 50.0, "put_oi": 0.0}}  # 昨天call_oi只有50，今天漲到100 -> 淨新增部位

    result = detect_unusual_activity(legs, min_volume_oi_ratio=1.0, previous_oi_by_strike=previous_oi)

    assert result[0]["likely_opening"] is True


def test_detect_unusual_activity_likely_opening_false_when_oi_flat_or_lower():
    legs = [FakeLeg(strike=100, call_volume=300, call_oi=100)]
    previous_oi = {100: {"call_oi": 150.0, "put_oi": 0.0}}  # 昨天call_oi比今天還高 -> 比較像平倉/轉倉

    result = detect_unusual_activity(legs, min_volume_oi_ratio=1.0, previous_oi_by_strike=previous_oi)

    assert result[0]["likely_opening"] is False


def test_detect_unusual_activity_likely_opening_handles_strike_missing_from_previous_snapshot():
    """前一天完全沒有這個履約價的紀錄（例如新掛牌的履約價），視為前一天
    OI=0——今天只要有OI就代表是新增部位。
    """
    legs = [FakeLeg(strike=999, call_volume=300, call_oi=100)]
    result = detect_unusual_activity(legs, min_volume_oi_ratio=1.0, previous_oi_by_strike={})
    assert result[0]["likely_opening"] is True


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
        "delta_adjusted_call_volume": 0.0,  # 這裡的 FakeLeg 沒設定 call_iv（預設0），會被跳過不計入
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
        "delta_adjusted_call_volume": 0.0,
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
        "delta_adjusted_call_volume": 0.0,
    }


def test_pressure_score_includes_nonzero_delta_adjusted_call_volume_with_valid_iv():
    legs = [FakeLeg(strike=100, call_iv=0.4, call_volume=500, call_oi=1000, put_volume=10)]

    result = compute_market_maker_pressure_score(
        spot=100, put_wall=90, in_negative_gamma=False, legs=legs,
    )

    assert result["delta_adjusted_call_volume"] > 0.0
