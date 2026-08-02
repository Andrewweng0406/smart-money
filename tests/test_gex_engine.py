"""gex_engine 是純計算層，不做 I/O，直接用合成數據測試即可。"""

from __future__ import annotations

import math

import pytest

from gex_engine import (
    OptionLeg,
    black_scholes_delta,
    compute_net_gex_by_strike,
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


def test_find_gamma_flip_point_interpolates_between_strikes():
    # 累計 Net GEX：-50 -> 100，在 90~100 之間 1/3 處由負轉正
    gex_by_strike = [
        {"strike": 90, "net_gex": -50},
        {"strike": 100, "net_gex": 150},
    ]
    flip = find_gamma_flip_point(gex_by_strike)
    assert flip == pytest.approx(93.333, abs=0.01)


def test_find_gamma_flip_point_no_sign_change_returns_none():
    gex_by_strike = [{"strike": 90, "net_gex": 100}, {"strike": 100, "net_gex": 50}]
    assert find_gamma_flip_point(gex_by_strike) is None


def test_find_gamma_flip_point_insufficient_data_returns_none():
    assert find_gamma_flip_point([{"strike": 100, "net_gex": 100}]) is None


def test_find_gamma_flip_point_picks_crossing_nearest_spot_not_first_from_bottom():
    """實測過的真實案例：彙總多個到期日後，遠低於現貨價的深度價外履約價
    （LEAPS 留下的稀疏舊倉位）理論 Gamma 幾乎是 0 但不精確等於 0，會在完全
    不影響避險行為的低履約價附近製造出「假交叉」。曲線上第一個交叉點（在
    $20 附近）沒有參考價值，離現貨價 $300 最近的交叉點（在 $250 附近）才是
    業界講的「Gamma 翻轉點」。
    """
    gex_by_strike = [
        {"strike": 15, "net_gex": 50},      # 累計：50（假交叉的起點雜訊）
        {"strike": 20, "net_gex": -100},    # 累計：-50 -> 遠低於現貨的假交叉
        {"strike": 200, "net_gex": 30},     # 累計：-20（維持負值，直到接近現貨才轉正）
        {"strike": 250, "net_gex": -60},    # 累計：-80
        {"strike": 300, "net_gex": 200},    # 累計：120 -> 離現貨最近的真交叉
    ]
    flip_near_spot = find_gamma_flip_point(gex_by_strike, spot=300.0)
    flip_first_from_bottom = find_gamma_flip_point(gex_by_strike)  # 沒傳 spot：舊行為

    assert flip_first_from_bottom < 20  # 舊行為：抓到低履約價的假交叉
    assert flip_near_spot > 200  # 新行為：抓到離現貨最近、有意義的交叉


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
