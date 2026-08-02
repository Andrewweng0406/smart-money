"""期權策略引擎——純計算層，不做任何 I/O（呼叫端已經抓好期權鏈資料），
方便單元測試。邏輯移植自舊專案（crypto-ai-agent/options_strategy_engine.py），
並擴充「買方突圍」與「依 GEX 狀態自動選策略」兩塊。

賣方（收租）策略家族——Bull Put Spread / Bear Call Spread / Iron Condor：
理論勝率用 Delta 近似（業界標準做法，tastytrade/Option Alpha 的 Probability
of Profit 也是這樣算）：機率OTM ≈ 1 - |短腿Delta|。這只是 Black-Scholes 假設
下的理論值，不是回測驗證過的統計勝率，所以刻意回傳一個區間字串（如「70-80%」）
而不是精確到小數點的數字，避免使用者誤以為這是硬數據。

買方（付權利金）策略——Long Strangle：適用在做市商淨空 Gamma（避險行為放大
波動）的情境，此時繼續當賣方風險偏高。買方策略的風險報酬結構跟賣方完全
相反（賣方要的是「不被突破」，買方要的正是「被突破」），不適用同一套
Delta近似勝率，所以不回傳 win_rate_bucket，改回傳兩個損益兩平點。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

from data_fetcher import StrikeLegRaw
from gex_engine import black_scholes_delta

# 使用者原始規則（沿用自舊專案）：安全墊落在現價8%~15%之間——在這個範圍內選
# 「離現價最近」的履約價當短腿，同時滿足最低安全邊際跟最大化權利金這兩個目標。
MIN_OTM_PCT = 0.08
MAX_OTM_PCT = 0.15

# 買方突圍策略的價外門檻，比賣方短腿的安全墊窄很多——買方要的是「有機會被
# 突破」，履約價太遠權利金雖然便宜但幾乎不可能到，形同浪費保費。
LONG_STRANGLE_MIN_OTM_PCT = 0.03

OPTIONS_MULTIPLIER = 100  # 標準美股股票期權合約乘數（1口=100股）

SpreadSide = Literal["put", "call"]
SpreadType = Literal["put_credit", "call_credit", "iron_condor"]


@dataclass
class SpreadLegResult:
    action: Literal["SELL", "BUY"]
    option_type: Literal["PUT", "CALL"]
    strike_price: float
    reason: str


@dataclass
class SpreadResult:
    name: str
    spread_type: SpreadType
    legs: list[SpreadLegResult]
    max_profit: float
    max_loss: float
    margin_required: float
    risk_reward_ratio: str
    win_rate_bucket: str
    ai_advice: str
    win_rate_pop: Optional[float] = None  # 原始機率值（0~100，未分桶），Iron Condor組合兩腳時要用這個算聯合機率
    leg_win_rates: Optional[dict[str, str]] = None  # 只有Iron Condor會填：{"put": "70-80%", "call": "60-70%"}


@dataclass
class LongStrangleResult:
    name: str
    legs: list[SpreadLegResult]
    total_debit: float  # 總花費（權利金支出），同時也是最大虧損
    breakeven_low: float
    breakeven_high: float
    ai_advice: str


@dataclass
class StrategyRecommendation:
    """把賣方價差 / Iron Condor / 買方突圍三種不同形狀的結果，統一成呼叫端
    （analyze.py / run_watchlist.py）可以直接塞進 Markdown 的單一格式，不用
    另外判斷型別。
    """
    strategy_name: str
    rationale: str  # 為什麼選這個策略（依 GEX 狀態的判斷邏輯，讓使用者知道不是黑箱）
    detail_lines: list[str]


def _otm_pct(spot: float, strike: float, side: SpreadSide) -> float:
    if side == "put":
        return (spot - strike) / spot
    return (strike - spot) / spot


def select_credit_spread_strikes(
    legs: list[StrikeLegRaw], spot: float, side: SpreadSide,
) -> Optional[tuple[StrikeLegRaw, StrikeLegRaw]]:
    """在安全墊 [MIN_OTM_PCT, MAX_OTM_PCT] 範圍內，選「離現價最近」的履約價
    當短腿（安全邊際剛好卡在門檻、拿到的權利金最多）；長腿是短腿再往外一檔
    「實際掛牌」的履約價，保證是一個真的能下單的價差組合。找不到符合安全墊
    範圍的履約價，或短腿已經是最極端一檔時回傳 None。
    """
    sorted_legs = sorted(legs, key=lambda l: l.strike)
    candidates = [l for l in sorted_legs if MIN_OTM_PCT <= _otm_pct(spot, l.strike, side) <= MAX_OTM_PCT]
    if not candidates:
        return None

    short_leg = min(candidates, key=lambda l: _otm_pct(spot, l.strike, side))
    short_idx = sorted_legs.index(short_leg)

    if side == "put":
        if short_idx == 0:
            return None
        long_leg = sorted_legs[short_idx - 1]  # 更下方一檔，鎖定下檔風險
    else:
        if short_idx == len(sorted_legs) - 1:
            return None
        long_leg = sorted_legs[short_idx + 1]  # 更上方一檔，鎖定上檔風險

    return short_leg, long_leg


def _bucket_pct(pop_pct: float) -> str:
    """把一個0~100的機率百分比分桶成10%區間字串（見檔頭說明：故意不用精確數字）。"""
    pop_pct = max(0.0, min(100.0, pop_pct))
    lower = max(0, min(90, int(pop_pct // 10) * 10))
    upper = lower + 10
    return f"{lower}-{upper}%"


def estimate_win_rate_bucket(delta: float) -> str:
    """回傳理論勝率的10%區間字串。"""
    return _bucket_pct((1 - abs(delta)) * 100)


def build_credit_spread(
    legs: list[StrikeLegRaw], spot: float, side: SpreadSide, time_to_expiry_years: float,
) -> Optional[SpreadResult]:
    picked = select_credit_spread_strikes(legs, spot, side)
    if picked is None:
        return None
    short_leg, long_leg = picked

    if side == "put":
        short_bid, long_ask, short_iv = short_leg.put_bid, long_leg.put_ask, short_leg.put_iv
        option_type: Literal["PUT", "CALL"] = "PUT"
    else:
        short_bid, long_ask, short_iv = short_leg.call_bid, long_leg.call_ask, short_leg.call_iv
        option_type = "CALL"

    if short_bid <= 0 or long_ask <= 0:
        return None  # 無流動性（沒人報價），這種價差實際上下不了單
    credit = short_bid - long_ask
    if credit <= 0:
        return None  # 理論上價差應該收租，算出負值代表報價異常（多半是深度價外流動性太差）

    width = abs(long_leg.strike - short_leg.strike)
    max_profit = round(credit * OPTIONS_MULTIPLIER, 2)
    max_loss = round((width - credit) * OPTIONS_MULTIPLIER, 2)
    margin_required = max_loss  # 定義風險價差的標準保證金公式：保證金 = 最大虧損

    win_rate_bucket = "N/A"
    win_rate_pop: Optional[float] = None
    if short_iv > 0:
        delta = float(black_scholes_delta(
            spot=spot, strike=short_leg.strike, time_to_expiry_years=time_to_expiry_years,
            iv=short_iv, option_type=option_type.lower(),
        ))
        if not math.isnan(delta):
            win_rate_pop = (1 - abs(delta)) * 100
            win_rate_bucket = _bucket_pct(win_rate_pop)

    otm_pct = _otm_pct(spot, short_leg.strike, side) * 100
    side_label = "下方" if side == "put" else "上方"
    strategy_name = "Bull Put Spread（賣出看跌價差）" if side == "put" else "Bear Call Spread（賣出看漲價差）"
    ai_advice = (
        f"短腿履約價 ${short_leg.strike:.0f} 距現價{side_label}約{otm_pct:.1f}%，"
        f"理論勝率（Delta估算）約{win_rate_bucket}。此為賣方策略，時間流逝（Theta）對你有利，"
        f"只要到期時股價{'高於' if side == 'put' else '低於'} ${short_leg.strike:.0f} 即可拿到最大收益。"
    )

    return SpreadResult(
        name=strategy_name,
        spread_type="put_credit" if side == "put" else "call_credit",
        legs=[
            SpreadLegResult(action="SELL", option_type=option_type, strike_price=short_leg.strike,
                             reason=f"距現價{side_label}約{otm_pct:.1f}%，提供安全緩衝"),
            SpreadLegResult(action="BUY", option_type=option_type, strike_price=long_leg.strike,
                             reason="鎖定最大風險，避免極端行情"),
        ],
        max_profit=max_profit,
        max_loss=max_loss,
        margin_required=margin_required,
        risk_reward_ratio=f"1 : {max_loss / max_profit:.1f}" if max_profit > 0 else "N/A",
        win_rate_bucket=win_rate_bucket,
        win_rate_pop=win_rate_pop,
        ai_advice=ai_advice,
    )


def build_iron_condor(
    legs: list[StrikeLegRaw], spot: float, time_to_expiry_years: float,
) -> Optional[SpreadResult]:
    put_side = build_credit_spread(legs, spot, "put", time_to_expiry_years)
    call_side = build_credit_spread(legs, spot, "call", time_to_expiry_years)
    if put_side is None or call_side is None:
        return None  # 兩側都要能組出價差，任一側不成就不勉強湊一個殘缺的鐵鷹

    max_profit = round(put_side.max_profit + call_side.max_profit, 2)
    max_loss = round(max(put_side.max_loss, call_side.max_loss), 2)  # 到期只會有一側被突破，不會兩側同時虧損
    margin_required = max_loss

    put_short_strike = put_side.legs[0].strike_price
    call_short_strike = call_side.legs[0].strike_price

    # Iron Condor 要拿到 max_profit，兩腳都不能被突破，不是任一腳單獨的存活機率：
    # combined = 1 - P(跌破put) - P(突破call) = put_pop + call_pop - 100
    # （兩個突破事件互斥——到期價格不可能同時低於put履約價又高於call履約價）
    if put_side.win_rate_pop is not None and call_side.win_rate_pop is not None:
        combined_pop = max(0.0, put_side.win_rate_pop + call_side.win_rate_pop - 100.0)
        win_rate_bucket = _bucket_pct(combined_pop)
        win_rate_pop: Optional[float] = combined_pop
    else:
        win_rate_bucket = "N/A"
        win_rate_pop = None

    return SpreadResult(
        name="Iron Condor（鐵鷹策略）",
        spread_type="iron_condor",
        legs=put_side.legs + call_side.legs,
        max_profit=max_profit,
        max_loss=max_loss,
        margin_required=margin_required,
        risk_reward_ratio=f"1 : {max_loss / max_profit:.1f}" if max_profit > 0 else "N/A",
        win_rate_bucket=win_rate_bucket,
        win_rate_pop=win_rate_pop,
        leg_win_rates={"put": put_side.win_rate_bucket, "call": call_side.win_rate_bucket},
        ai_advice=(
            f"兩側各設一組價差，只要到期時股價落在 ${put_short_strike:.0f} ~ ${call_short_strike:.0f} "
            f"之間即可拿到最大收益，適合震盪整理格局；若單邊被突破，虧損以較嚴重的那一側為準（兩側不會同時虧損）。"
            f"理論勝率{win_rate_bucket}是「兩腳都不破」的聯合機率，會比任一腳單獨的存活機率低。"
        ),
    )


def build_long_strangle(
    legs: list[StrikeLegRaw], spot: float, otm_pct: float = LONG_STRANGLE_MIN_OTM_PCT,
) -> Optional[LongStrangleResult]:
    """買方突圍策略：買進價外 Call + 買進價外 Put，賭現貨會往任一方向大幅
    突破現有的 Gamma 結構——用在「大負GEX區域」：做市商淨空Gamma時避險行為
    會放大波動，此時繼續當賣方（尤其是無保護的裸賣）風險偏高，買方策略反而
    在波動放大時受益。

    選最接近現價、但仍滿足 otm_pct 門檻的 Call/Put 各一檔（越接近現價，
    Delta 越高、越容易被觸及，但權利金也越貴——這裡選「剛好滿足門檻」的
    那一檔，在「有機會被觸及」和「權利金不要太貴」之間取平衡）。
    """
    sorted_legs = sorted(legs, key=lambda l: l.strike)
    call_candidates = [l for l in sorted_legs if _otm_pct(spot, l.strike, "call") >= otm_pct and l.call_ask > 0]
    put_candidates = [l for l in sorted_legs if _otm_pct(spot, l.strike, "put") >= otm_pct and l.put_ask > 0]
    if not call_candidates or not put_candidates:
        return None  # 找不到符合門檻、且有報價（有流動性）的履約價

    call_leg = min(call_candidates, key=lambda l: l.strike)  # 最近的合格價外 Call
    put_leg = max(put_candidates, key=lambda l: l.strike)  # 最近的合格價外 Put

    debit = call_leg.call_ask + put_leg.put_ask
    if debit <= 0:
        return None

    breakeven_high = call_leg.strike + debit
    breakeven_low = put_leg.strike - debit

    return LongStrangleResult(
        name="Long Strangle（買方突圍）",
        legs=[
            SpreadLegResult(action="BUY", option_type="CALL", strike_price=call_leg.strike, reason="賭現貨向上突破"),
            SpreadLegResult(action="BUY", option_type="PUT", strike_price=put_leg.strike, reason="賭現貨向下突破"),
        ],
        total_debit=round(debit * OPTIONS_MULTIPLIER, 2),
        breakeven_low=breakeven_low,
        breakeven_high=breakeven_high,
        ai_advice=(
            f"買進 Call ${call_leg.strike:.0f} + 買進 Put ${put_leg.strike:.0f}，"
            f"總成本 ${debit * OPTIONS_MULTIPLIER:,.0f}（同時也是最大虧損）。"
            f"到期時股價需高於 ${breakeven_high:.2f} 或低於 ${breakeven_low:.2f} 才會獲利，"
            f"適合預期波動放大、方向不明確的『大負GEX區域』，不適合方向盤整的行情。"
        ),
    )


def _format_legs(legs: list[SpreadLegResult]) -> str:
    return "；".join(f"{leg.action} {leg.option_type} ${leg.strike_price:.0f}（{leg.reason}）" for leg in legs)


# 判斷「現貨與兩道牆距離接近（盤整格局）」的門檻：兩側距離百分比差距在這個
# 範圍內，視為現貨大致落在兩道牆中間，沒有明顯偏向哪一側。
WALL_BALANCE_THRESHOLD_PCT = 3.0


def select_strategy(
    legs: list[StrikeLegRaw], spot: float, time_to_expiry_years: float,
    put_wall: float, call_wall: float, alert: Optional[str],
) -> StrategyRecommendation:
    """依據當前 GEX 狀態（是否觸發大負GEX警示）與現貨對兩道牆的相對位置，
    判斷「理論上」比較適合哪一種策略——這是規則式的判斷，不是回測驗證過的
    最佳解，重點是把判斷邏輯攤開透明，讓使用者知道『為什麼』推薦這個策略，
    而不是一個黑盒子。

    規則（由上而下，符合第一條就採用）：
    1. alert 被觸發（現貨跌破 Put Wall 或處於大負GEX區域，做市商淨空Gamma、
       避險行為傾向放大波動）——此時裸賣期權風險偏高，改推薦方向不明的
       買方突圍策略，賭一個方向的大幅波動。
    2. 現貨與 Put Wall / Call Wall 兩道牆距離接近（正Gamma、沒有明顯偏向）
       ——判斷為盤整格局，推薦 Iron Condor 兩邊都收租。
    3. Put Wall 距現貨較近（下方支撐較強、上方空間較大）——推薦 Bull Put
       Spread（賣方看漲）。
    4. Call Wall 距現貨較近（上方壓力較強）——推薦 Bear Call Spread（賣方看跌）。
    """
    if alert:
        rationale = (
            "現貨處於大負GEX區域（做市商淨空Gamma，避險行為傾向放大而非抑制波動），"
            "此時裸賣期權風險偏高，改推薦方向不明的買方突圍策略。"
        )
        strangle = build_long_strangle(legs, spot)
        if strangle is None:
            return StrategyRecommendation(
                strategy_name="無建議（買方突圍）", rationale=rationale,
                detail_lines=["找不到流動性足夠的價外履約價可組成買方突圍策略。"],
            )
        return StrategyRecommendation(
            strategy_name=strangle.name, rationale=rationale,
            detail_lines=[
                f"- 動作：{_format_legs(strangle.legs)}",
                f"- 總成本（最大虧損）：${strangle.total_debit:,.0f}",
                f"- 損益兩平點：${strangle.breakeven_low:.2f} ~ ${strangle.breakeven_high:.2f}",
                f"- {strangle.ai_advice}",
            ],
        )

    put_dist_pct = (spot - put_wall) / spot * 100
    call_dist_pct = (call_wall - spot) / spot * 100

    if abs(put_dist_pct - call_dist_pct) <= WALL_BALANCE_THRESHOLD_PCT:
        rationale = (
            f"現貨與 Put Wall（距離{put_dist_pct:.1f}%）、Call Wall（距離{call_dist_pct:.1f}%）"
            "兩道牆距離接近，判斷為盤整格局，適合兩邊收租的 Iron Condor。"
        )
        result = build_iron_condor(legs, spot, time_to_expiry_years)
        strategy_label = "Iron Condor"
    elif put_dist_pct < call_dist_pct:
        rationale = (
            f"Put Wall（${put_wall:.0f}）距現貨較近（{put_dist_pct:.1f}% vs {call_dist_pct:.1f}%），"
            "判斷下方支撐較強，適合賣出看跌價差。"
        )
        result = build_credit_spread(legs, spot, "put", time_to_expiry_years)
        strategy_label = "Bull Put Spread"
    else:
        rationale = (
            f"Call Wall（${call_wall:.0f}）距現貨較近（{call_dist_pct:.1f}% vs {put_dist_pct:.1f}%），"
            "判斷上方壓力較強，適合賣出看漲價差。"
        )
        result = build_credit_spread(legs, spot, "call", time_to_expiry_years)
        strategy_label = "Bear Call Spread"

    if result is None:
        return StrategyRecommendation(
            strategy_name=f"無建議（{strategy_label}）", rationale=rationale,
            detail_lines=["安全墊範圍（現貨 8%~15% 外）內找不到有效報價，本日跳過策略建議。"],
        )

    return StrategyRecommendation(
        strategy_name=result.name, rationale=rationale,
        detail_lines=[
            f"- 動作：{_format_legs(result.legs)}",
            f"- 最大獲利：${result.max_profit:,.0f}　最大虧損：${result.max_loss:,.0f}",
            f"- 風險報酬比：{result.risk_reward_ratio}",
            f"- 理論勝率：{result.win_rate_bucket}",
            f"- {result.ai_advice}",
        ],
    )
