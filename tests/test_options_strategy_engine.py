"""options_strategy_engine 是純計算層，用合成的期權鏈資料測試即可，不用碰
yfinance。合成資料刻意讓 8%~15% OTM 安全墊範圍內剛好有兩檔履約價可選
（短腿+長腿），這樣賣方價差/鐵鷹策略才組得出來。
"""

from __future__ import annotations

import pytest

from data_fetcher import StrikeLegRaw
from options_strategy_engine import (
    build_credit_spread,
    build_iron_condor,
    build_long_strangle,
    select_credit_spread_strikes,
    select_strategy,
)

SPOT = 100.0
TTE = 35 / 365


def _leg(strike, call_oi=100.0, call_iv=0.4, call_volume=10.0, put_oi=100.0, put_iv=0.45, put_volume=10.0,
         call_bid=0.0, call_ask=0.0, put_bid=0.0, put_ask=0.0) -> StrikeLegRaw:
    return StrikeLegRaw(
        expiry="2026-09-04", strike=strike, call_oi=call_oi, call_iv=call_iv, call_volume=call_volume,
        put_oi=put_oi, put_iv=put_iv, put_volume=put_volume,
        call_bid=call_bid, call_ask=call_ask, put_bid=put_bid, put_ask=put_ask,
    )


def _synthetic_chain() -> list[StrikeLegRaw]:
    """spot=100 附近的合成期權鏈——put 短腿在 90（10% OTM）、長腿在 85（15% OTM）；
    call 短腿在 110（10% OTM）、長腿在 115（15% OTM）；買方突圍用的價外 Call/Put
    在 105/95（5% OTM，滿足 LONG_STRANGLE_MIN_OTM_PCT=3%）。價格離現貨越遠越便宜，
    符合真實市場「越價外越便宜」的方向。
    """
    return [
        _leg(80, put_bid=0.25, put_ask=0.35),
        _leg(85, put_bid=0.50, put_ask=0.60),
        _leg(90, put_bid=1.00, put_ask=1.10),
        _leg(95, put_bid=2.00, put_ask=2.10),
        _leg(100),
        _leg(105, call_bid=2.00, call_ask=2.10),
        _leg(110, call_bid=1.00, call_ask=1.10),
        _leg(115, call_bid=0.50, call_ask=0.60),
        _leg(120, call_bid=0.25, call_ask=0.35),
    ]


def test_select_credit_spread_strikes_picks_nearest_within_safety_margin():
    legs = _synthetic_chain()
    picked = select_credit_spread_strikes(legs, SPOT, "put")
    assert picked is not None
    short_leg, long_leg = picked
    assert short_leg.strike == 90.0  # 離現價最近、仍在8~15%安全墊內
    assert long_leg.strike == 85.0  # 短腿再往外一檔


def test_select_credit_spread_strikes_returns_none_when_no_candidates_in_range():
    legs = [_leg(99), _leg(101)]  # OTM% 都遠低於 8%
    assert select_credit_spread_strikes(legs, SPOT, "put") is None


def test_build_credit_spread_put_side_has_positive_credit():
    legs = _synthetic_chain()
    result = build_credit_spread(legs, SPOT, "put", TTE)
    assert result is not None
    assert result.max_profit > 0
    assert result.max_loss > 0
    assert result.legs[0].action == "SELL"
    assert result.legs[0].strike_price == 90.0
    assert result.legs[1].action == "BUY"
    assert result.legs[1].strike_price == 85.0
    assert "%" in result.win_rate_bucket


def test_build_credit_spread_call_side_has_positive_credit():
    legs = _synthetic_chain()
    result = build_credit_spread(legs, SPOT, "call", TTE)
    assert result is not None
    assert result.legs[0].strike_price == 110.0
    assert result.legs[1].strike_price == 115.0


def test_build_credit_spread_returns_none_when_no_liquidity():
    """bid/ask 都是 0（沒人報價）時，這種價差實際上下不了單，該回傳 None。"""
    legs = [_leg(85), _leg(90), _leg(100)]  # 都沒填 bid/ask，預設 0
    assert build_credit_spread(legs, SPOT, "put", TTE) is None


def test_build_iron_condor_combines_both_sides():
    legs = _synthetic_chain()
    result = build_iron_condor(legs, SPOT, TTE)
    assert result is not None
    assert len(result.legs) == 4  # 兩組價差各兩腳
    assert result.leg_win_rates is not None
    assert "put" in result.leg_win_rates and "call" in result.leg_win_rates


def test_build_long_strangle_picks_nearest_qualifying_strikes():
    legs = _synthetic_chain()
    result = build_long_strangle(legs, SPOT)
    assert result is not None
    assert result.legs[0].strike_price == 105.0  # 最近的合格價外 Call
    assert result.legs[1].strike_price == 95.0  # 最近的合格價外 Put
    assert result.total_debit == pytest.approx((2.10 + 2.10) * 100, abs=0.01)
    assert result.breakeven_low < 95.0 < SPOT < 105.0 < result.breakeven_high


def test_build_long_strangle_returns_none_without_liquid_otm_legs():
    legs = [_leg(100)]  # 只有 ATM，沒有任何合格的價外履約價
    assert build_long_strangle(legs, SPOT) is None


def test_select_strategy_picks_long_strangle_when_alert_triggered():
    legs = _synthetic_chain()
    rec = select_strategy(legs, SPOT, TTE, put_wall=90.0, call_wall=110.0, alert="⚠️ 做市商對沖賣壓風險高")
    assert "突圍" in rec.strategy_name or "Strangle" in rec.strategy_name
    assert "大負GEX" in rec.rationale or "波動" in rec.rationale


def test_select_strategy_picks_iron_condor_when_walls_balanced():
    legs = _synthetic_chain()
    # put_wall/call_wall 距現貨都是 10%，判斷為盤整格局
    rec = select_strategy(legs, SPOT, TTE, put_wall=90.0, call_wall=110.0, alert=None)
    assert "Iron Condor" in rec.strategy_name


def test_select_strategy_picks_bull_put_spread_when_put_wall_closer():
    legs = _synthetic_chain()
    # put_wall 距現貨只有 5%，call_wall 距現貨 20%，下方支撐明顯較近
    rec = select_strategy(legs, SPOT, TTE, put_wall=95.0, call_wall=120.0, alert=None)
    assert "Bull Put Spread" in rec.strategy_name


def test_select_strategy_picks_bear_call_spread_when_call_wall_closer():
    legs = _synthetic_chain()
    rec = select_strategy(legs, SPOT, TTE, put_wall=80.0, call_wall=105.0, alert=None)
    assert "Bear Call Spread" in rec.strategy_name


def test_select_strategy_degrades_gracefully_when_no_valid_strikes():
    """完全找不到符合條件履約價（例如流動性太差）時，回傳清楚的『無建議』，
    而不是拋出例外把整份報告搞壞。
    """
    legs = [_leg(100)]  # 沒有任何價外報價
    rec = select_strategy(legs, SPOT, TTE, put_wall=90.0, call_wall=110.0, alert=None)
    assert "無建議" in rec.strategy_name
    assert rec.detail_lines
