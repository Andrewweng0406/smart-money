"""gex_engine 是純計算層，不做 I/O，直接用合成數據測試即可。"""

from __future__ import annotations

import math

import pytest

from gex_engine import (
    OptionLeg,
    _find_nearest_zero_crossing,
    black_scholes_delta,
    compute_net_gex_by_strike,
    compute_net_gex_curve,
    find_gamma_flip_point,
    gamma_flip_distance_pct,
    summarize_zero_dte_contribution,
)


def test_compute_net_gex_by_strike_call_only_is_positive():
    legs = [OptionLeg(strike=100, call_oi=1000, call_iv=0.5, put_oi=0, put_iv=0, time_to_expiry_years=30 / 365)]
    result = compute_net_gex_by_strike(legs, spot=100.0)
    assert len(result) == 1
    row = result[0]
    assert row["put_gex"] == 0.0
    assert row["call_gex"] > 0.0
    assert row["net_gex"] == row["call_gex"]


def test_compute_net_gex_by_strike_put_only_is_negative():
    legs = [OptionLeg(strike=100, call_oi=0, call_iv=0, put_oi=1000, put_iv=0.5, time_to_expiry_years=30 / 365)]
    result = compute_net_gex_by_strike(legs, spot=100.0)
    assert result[0]["net_gex"] < 0.0


def test_compute_net_gex_by_strike_aggregates_same_strike_across_expiries():
    """同一履約價橫跨兩個到期日的 legs 應該自然加總成一筆。"""
    legs = [
        OptionLeg(strike=100, call_oi=500, call_iv=0.5, put_oi=0, put_iv=0, time_to_expiry_years=7 / 365),
        OptionLeg(strike=100, call_oi=500, call_iv=0.5, put_oi=0, put_iv=0, time_to_expiry_years=30 / 365),
    ]
    result = compute_net_gex_by_strike(legs, spot=100.0)
    assert len(result) == 1  # 同一 strike 合併成一筆，不是兩筆


def test_compute_net_gex_by_strike_empty_input():
    assert compute_net_gex_by_strike([], spot=100.0) == []


def test_expired_or_zero_iv_contract_has_zero_gamma_not_nan():
    legs = [OptionLeg(strike=100, call_oi=1000, call_iv=0.0, put_oi=0, put_iv=0.0, time_to_expiry_years=0.0)]
    result = compute_net_gex_by_strike(legs, spot=100.0)
    assert result[0]["net_gex"] == 0.0
    assert not math.isnan(result[0]["net_gex"])


def test_find_nearest_zero_crossing_interpolates():
    # -50 -> 150，在 90~100 之間 1/4 處由負轉正
    crossing = _find_nearest_zero_crossing([90, 100], [-50, 150], reference_x=90)
    assert crossing == pytest.approx(92.5, abs=0.01)


def test_find_nearest_zero_crossing_no_sign_change_returns_none():
    assert _find_nearest_zero_crossing([90, 100], [100, 50], reference_x=95) is None


def test_find_nearest_zero_crossing_picks_nearest_to_reference():
    """曲線上有兩個交叉（10~20附近、250~300附近），要抓離 reference_x 最近
    的那個，不是掃描到的第一個——這是實測抓到的真實案例（彙總多個到期日
    後，遠離現貨的稀疏舊倉位在低履約價附近製造出對交易沒有意義的假交叉）
    背後真正在測的數學邏輯本身，跟 Black-Scholes 完全無關。
    """
    x = [10, 20, 200, 250, 300]
    y = [50, -100, 30, -60, 200]

    nearest_to_300 = _find_nearest_zero_crossing(x, y, reference_x=300)
    nearest_to_10 = _find_nearest_zero_crossing(x, y, reference_x=10)

    assert nearest_to_300 > 200
    assert nearest_to_10 < 20


def test_compute_net_gex_curve_call_only_is_nonnegative_everywhere():
    legs = [OptionLeg(strike=100, call_oi=1000, call_iv=0.4, put_oi=0, put_iv=0, time_to_expiry_years=30 / 365)]
    curve = compute_net_gex_curve(legs, spot=100.0)
    assert len(curve) > 0
    assert all(row["net_gex"] >= 0 for row in curve)


def test_compute_net_gex_curve_put_only_is_nonpositive_everywhere():
    legs = [OptionLeg(strike=100, call_oi=0, call_iv=0, put_oi=1000, put_iv=0.4, time_to_expiry_years=30 / 365)]
    curve = compute_net_gex_curve(legs, spot=100.0)
    assert all(row["net_gex"] <= 0 for row in curve)


def test_compute_net_gex_curve_empty_legs_returns_empty():
    assert compute_net_gex_curve([], spot=100.0) == []


def test_compute_net_gex_curve_spans_price_range_around_spot():
    legs = [OptionLeg(strike=100, call_oi=100, call_iv=0.4, put_oi=100, put_iv=0.4, time_to_expiry_years=30 / 365)]
    curve = compute_net_gex_curve(legs, spot=100.0, price_range_pct=0.2, grid_points=21)
    spots = [row["spot"] for row in curve]
    assert spots == sorted(spots)
    assert spots[0] == pytest.approx(80.0)
    assert spots[-1] == pytest.approx(120.0)


def test_find_gamma_flip_point_finds_crossing_between_put_and_call_strikes():
    """put_oi 集中在低履約價、call_oi 集中在高履約價，現貨介於中間——這是
    典型會產生 Gamma 翻轉點的部位結構：假設現貨夠低時 Put Gamma 主導（淨空
    Gamma，避險行為傾向放大波動），假設現貨夠高時 Call Gamma 主導（淨多
    Gamma，避險行為抑制波動），中間某處交叉，這才是業界定義的 Gamma 翻轉點。
    """
    legs = [
        OptionLeg(strike=90, call_oi=0, call_iv=0.4, put_oi=1000, put_iv=0.4, time_to_expiry_years=30 / 365),
        OptionLeg(strike=110, call_oi=1000, call_iv=0.4, put_oi=0, put_iv=0.4, time_to_expiry_years=30 / 365),
    ]
    flip = find_gamma_flip_point(legs, spot=100.0)
    assert flip is not None
    assert 90 < flip < 110


def test_find_gamma_flip_point_shifts_lower_when_call_oi_dominates():
    """call_oi 遠大於 put_oi 時，做市商在更大範圍的假設現貨價都是淨多
    Gamma，翻轉點應該往下移（更靠近 put 履約價——現貨要非常接近 put 履約價、
    put gamma 局部飆升，才蓋得過遠大的 call 曝險）。
    """
    def _flip_with_call_oi(call_oi: float) -> float:
        legs = [
            OptionLeg(strike=90, call_oi=0, call_iv=0.4, put_oi=1000, put_iv=0.4, time_to_expiry_years=30 / 365),
            OptionLeg(strike=110, call_oi=call_oi, call_iv=0.4, put_oi=0, put_iv=0.4, time_to_expiry_years=30 / 365),
        ]
        return find_gamma_flip_point(legs, spot=100.0)

    balanced_flip = _flip_with_call_oi(1000)
    call_heavy_flip = _flip_with_call_oi(10_000)

    assert call_heavy_flip < balanced_flip


def test_find_gamma_flip_point_no_crossing_returns_none():
    # 只有put、沒有call，Net GEX 在整個網格上都是負值，沒有交叉
    legs = [OptionLeg(strike=100, call_oi=0, call_iv=0, put_oi=1000, put_iv=0.4, time_to_expiry_years=30 / 365)]
    assert find_gamma_flip_point(legs, spot=100.0) is None


def test_find_gamma_flip_point_empty_legs_returns_none():
    assert find_gamma_flip_point([], spot=100.0) is None


def test_gamma_flip_distance_pct_above_flip_is_positive():
    assert gamma_flip_distance_pct(spot=110, gamma_flip=100) == 10.0


def test_gamma_flip_distance_pct_below_flip_is_negative():
    assert gamma_flip_distance_pct(spot=90, gamma_flip=100) == -10.0


def test_gamma_flip_distance_pct_none_when_no_flip_point():
    assert gamma_flip_distance_pct(spot=100, gamma_flip=None) is None


def test_summarize_zero_dte_contribution_computes_share():
    total = [{"strike": 100, "net_gex": 100}, {"strike": 105, "net_gex": 100}]
    zero_dte = [{"strike": 100, "net_gex": 50}]
    summary = summarize_zero_dte_contribution(total, zero_dte)
    assert summary["total_net_gex"] == 200
    assert summary["zero_dte_net_gex"] == 50
    assert summary["ex_zero_dte_net_gex"] == 150
    assert summary["zero_dte_share_pct"] == 25.0


def test_summarize_zero_dte_contribution_no_zero_dte_legs():
    total = [{"strike": 100, "net_gex": 100}]
    summary = summarize_zero_dte_contribution(total, [])
    assert summary["zero_dte_net_gex"] == 0.0
    assert summary["zero_dte_share_pct"] == 0.0


def test_summarize_zero_dte_contribution_near_zero_total_is_na():
    """分母接近 0 時佔比沒有意義，回傳 None 而不是誇張的巨大數字或 inf。"""
    total = [{"strike": 100, "net_gex": 1e-12}]
    summary = summarize_zero_dte_contribution(total, [])
    assert summary["zero_dte_share_pct"] is None


def test_black_scholes_delta_call_is_between_0_and_1():
    delta = float(black_scholes_delta(spot=100, strike=100, time_to_expiry_years=30 / 365, iv=0.5, option_type="call"))
    assert 0.0 < delta < 1.0


def test_black_scholes_delta_put_is_between_minus_1_and_0():
    delta = float(black_scholes_delta(spot=100, strike=100, time_to_expiry_years=30 / 365, iv=0.5, option_type="put"))
    assert -1.0 < delta < 0.0


def test_black_scholes_delta_deep_itm_call_approaches_1():
    delta = float(black_scholes_delta(spot=200, strike=100, time_to_expiry_years=30 / 365, iv=0.3, option_type="call"))
    assert delta > 0.9


def test_black_scholes_delta_expired_contract_is_nan():
    delta = float(black_scholes_delta(spot=100, strike=100, time_to_expiry_years=0.0, iv=0.5, option_type="call"))
    assert math.isnan(delta)
