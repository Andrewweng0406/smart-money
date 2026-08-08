"""pinning_engine 是純計算層，不做 I/O，直接用合成數據測試即可。"""

from __future__ import annotations

import pytest

from pinning_engine import (
    PIN_CANDIDATE_MAX_DISTANCE_PCT,
    PIN_OI_CONCENTRATION_MIN_PCT,
    PIN_PROXIMITY_THRESHOLD_PCT,
    compute_pinning_analysis,
    find_pin_strike,
    score_pinning,
)


def _row(strike: float, call_oi: float, put_oi: float) -> dict:
    return {"strike": strike, "call_oi": call_oi, "put_oi": put_oi}


def test_find_pin_strike_picks_largest_combined_oi():
    rows = [_row(90, 100, 100), _row(100, 500, 600), _row(110, 200, 50)]
    assert find_pin_strike(rows) == 100


def test_find_pin_strike_empty_input_returns_none():
    assert find_pin_strike([]) is None


def test_find_pin_strike_without_spot_ignores_distance_and_can_pick_far_leap():
    """不傳 spot 時維持舊行為：純看誰的合計OI最大，不管距離。"""
    rows = [_row(100, 500, 500), _row(400, 5000, 5000)]
    assert find_pin_strike(rows) == 400


def test_find_pin_strike_with_spot_excludes_far_stale_leaps_oi():
    """實測抓到的真bug：彙總多個到期日後，遠端 LEAPS 履約價（這裡是 400，
    距現貨 328.58 約 21.7%，超過 PIN_CANDIDATE_MAX_DISTANCE_PCT）就算OI
    絕對值遠大於近端履約價，也不該被選成 Pin Strike——那筆OI是陳舊的
    長天期倉位，跟「現貨真的會被磁吸到哪裡」無關。傳入 spot 後應該只在
    現貨附近的候選裡挑，即使近端OI小很多。
    """
    spot = 328.58
    rows = [_row(330, 1000, 1000), _row(400, 50000, 50000)]
    assert find_pin_strike(rows, spot) == 330


def test_find_pin_strike_with_spot_falls_back_to_full_chain_when_nothing_nearby():
    """現貨附近完全沒有OI（極端情況）時，不要直接放棄——退回全鏈次佳解，
    讓 compute_pinning_analysis 的距離分量自然算出低分反映「離現貨太遠」，
    比直接回傳 None、整段 Pinning 資訊從報告消失更有用。
    """
    spot = 100.0
    rows = [_row(400, 5000, 5000)]
    assert find_pin_strike(rows, spot) == 400


def test_compute_pinning_analysis_ignores_far_leaps_oi_end_to_end():
    """跟 test_find_pin_strike_with_spot_excludes_far_stale_leaps_oi 同一個
    真實案例，但走完整條 compute_pinning_analysis，確認過濾邏輯真的接上了
    （而不是只有 find_pin_strike 單獨測試時對，整合起來卻沒生效）。
    """
    spot = 328.58
    rows = [_row(330, 1000, 1000), _row(400, 50000, 50000)]
    result = compute_pinning_analysis(
        rows, spot=spot, max_pain=330.0, call_wall=350.0, put_wall=300.0,
        in_positive_gamma=True,
    )
    assert result["pin_strike"] == 330
    assert result["distance_pct"] < PIN_CANDIDATE_MAX_DISTANCE_PCT


def test_compute_pinning_analysis_empty_chain_returns_none():
    assert (
        compute_pinning_analysis(
            [], spot=100.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
            in_positive_gamma=True,
        )
        is None
    )


def test_compute_pinning_analysis_zero_spot_returns_none():
    rows = [_row(100, 1000, 1000)]
    assert (
        compute_pinning_analysis(
            rows, spot=0.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
            in_positive_gamma=True,
        )
        is None
    )


def test_breakout_when_spot_above_call_wall():
    # Pin strike (100) is right at spot, positive gamma, fully concentrated —
    # every other signal says "pin", but spot already cleared the call wall,
    # so the wall breach must win.
    rows = [_row(100, 1000, 1000)]
    result = compute_pinning_analysis(
        rows, spot=106.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    assert result["regime"] == "BREAKOUT"
    assert result["has_broken_wall"] is True


def test_breakout_when_spot_below_put_wall():
    rows = [_row(100, 1000, 1000)]
    result = compute_pinning_analysis(
        rows, spot=94.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    assert result["regime"] == "BREAKOUT"


def test_pinning_when_close_concentrated_and_positive_gamma():
    # All open interest sits at strike 100; spot is essentially on top of it.
    rows = [_row(100, 5000, 5000), _row(90, 10, 10), _row(110, 10, 10)]
    result = compute_pinning_analysis(
        rows, spot=100.2, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    assert result["regime"] == "PINNING"
    assert result["pin_strike"] == 100
    assert result["pin_strike_matches_max_pain"] is True
    assert result["score"] >= 60


def test_neutral_when_negative_gamma_even_if_close_and_concentrated():
    rows = [_row(100, 5000, 5000), _row(90, 10, 10), _row(110, 10, 10)]
    result = compute_pinning_analysis(
        rows, spot=100.2, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=False,
    )
    assert result["regime"] == "NEUTRAL"
    assert result["in_positive_gamma"] is False


def test_neutral_when_far_from_pin_strike():
    rows = [_row(100, 5000, 5000), _row(90, 10, 10), _row(110, 10, 10)]
    result = compute_pinning_analysis(
        rows, spot=103.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    assert result["distance_pct"] > PIN_PROXIMITY_THRESHOLD_PCT
    assert result["regime"] == "NEUTRAL"


def test_neutral_when_oi_too_spread_out_despite_proximity():
    # Open interest spread evenly across many strikes -> no strike clears the
    # concentration floor even though spot sits right on the (weak) pin.
    rows = [_row(90 + i, 50, 50) for i in range(21)]  # 21 strikes, 5% each
    pin_strike = find_pin_strike(rows)
    result = compute_pinning_analysis(
        rows, spot=pin_strike, max_pain=pin_strike, call_wall=pin_strike + 20,
        put_wall=pin_strike - 20, in_positive_gamma=True,
    )
    assert result["oi_concentration_pct"] < PIN_OI_CONCENTRATION_MIN_PCT
    assert result["regime"] == "NEUTRAL"


def test_pin_strike_matches_max_pain_flag_false_when_different():
    rows = [_row(100, 5000, 5000), _row(120, 10, 10)]
    result = compute_pinning_analysis(
        rows, spot=100.0, max_pain=120.0, call_wall=125.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    assert result["pin_strike"] == 100
    assert result["pin_strike_matches_max_pain"] is False


def test_perfect_conditions_score_near_maximum():
    rows = [_row(100, 5000, 5000)]
    result = compute_pinning_analysis(
        rows, spot=100.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    assert result["score"] == 100


def test_score_is_zero_at_far_distance_negative_gamma_and_no_concentration():
    # Zero open interest everywhere (e.g. an illiquid chain) makes
    # concentration_score exactly 0 too, so all three components bottom out.
    rows = [_row(90, 0, 0), _row(100, 0, 0), _row(110, 0, 0)]
    result = compute_pinning_analysis(
        rows, spot=140.0, max_pain=90.0, call_wall=160.0, put_wall=70.0,
        in_positive_gamma=False,
    )
    assert result["oi_concentration_pct"] == 0.0
    assert result["score"] == 0
    assert result["regime"] == "NEUTRAL"


def test_score_pinning_matches_compute_pinning_analysis_with_same_inputs():
    """score_pinning() 是 compute_pinning_analysis() 內部真正在做評分的那段
    邏輯拆出來的獨立入口——直接用同一組 pin_strike/集中度呼叫，結果應該
    完全一致（intraday_watcher.py 用這支重用前一天存好的 pin_strike，不用
    每次都重抓整條期權鏈）。
    """
    rows = [_row(100, 5000, 5000)]
    via_full = compute_pinning_analysis(
        rows, spot=100.2, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=True,
    )
    via_direct = score_pinning(
        spot=100.2, pin_strike=100.0, oi_concentration_pct=100.0, max_pain=100.0,
        call_wall=105.0, put_wall=95.0, in_positive_gamma=True,
    )
    assert via_full == via_direct


def test_score_pinning_none_pin_strike_returns_none():
    assert (
        score_pinning(
            spot=100.0, pin_strike=None, oi_concentration_pct=50.0, max_pain=100.0,
            call_wall=105.0, put_wall=95.0, in_positive_gamma=True,
        )
        is None
    )


@pytest.mark.parametrize("in_positive_gamma", [True, False])
def test_gamma_score_is_binary_not_scaled(in_positive_gamma):
    rows = [_row(100, 5000, 5000)]
    result = compute_pinning_analysis(
        rows, spot=100.0, max_pain=100.0, call_wall=105.0, put_wall=95.0,
        in_positive_gamma=in_positive_gamma,
    )
    expected_score = 100 if in_positive_gamma else 70
    assert result["score"] == expected_score
