"""期權策略引擎——純計算層，不做任何 I/O（呼叫端已經抓好期權鏈資料），
方便單元測試。邏輯移植自舊專案（crypto-ai-agent/options_strategy_engine.py），
並擴充「買方突圍」與「依 GEX 狀態自動選策略」兩塊。

賣方（收租）策略家族——Bull Put Spread / Bear Call Spread / Iron Condor：
模型獲利機率依 Black-Scholes 風險中性分布及損益兩平點估算，未扣交易費用，
不是回測驗證過的統計勝率。舊規則模板只供研究比較，正式推薦另經風控篩選。

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
from scipy.stats import norm

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
    win_rate_pop: Optional[float] = None  # 風險中性模型獲利機率（0~100），未扣費用
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

    legs/net_premium/strategy_type/max_loss 這四個欄位是給「策略追蹤記分板」
    （strategy_tracker.py + db_manager 的 strategy_recommendations 表）用的
    結構化資料——「無建議」的情況（找不到合適履約價/找不到到期日）這幾個
    欄位維持 None，呼叫端看到 None 就知道這筆不用存進追蹤資料庫。
    """
    strategy_name: str
    rationale: str  # 為什麼選這個策略（依 GEX 狀態的判斷邏輯，讓使用者知道不是黑箱）
    detail_lines: list[str]
    legs: Optional[list[SpreadLegResult]] = None
    net_premium: Optional[float] = None  # credit策略=收到的權利金；debit策略=付出的權利金，一律正數
    strategy_type: Optional[Literal["credit", "debit"]] = None
    max_loss: Optional[float] = None
    expiry_date: Optional[str] = None  # select_strategy()本身不知道日期（只拿到年化到期時間），由呼叫端事後補上


def _otm_pct(spot: float, strike: float, side: SpreadSide) -> float:
    if side == "put":
        return (spot - strike) / spot
    return (strike - spot) / spot


def select_credit_spread_strikes(
    legs: list[StrikeLegRaw], spot: float, side: SpreadSide,
    time_to_expiry_years: float | None = None,
) -> Optional[tuple[StrikeLegRaw, StrikeLegRaw]]:
    """在安全墊 [MIN_OTM_PCT, MAX_OTM_PCT] 範圍內，選「離現價最近」的履約價
    當短腿（安全邊際剛好卡在門檻、拿到的權利金最多）；長腿是短腿再往外一檔
    「實際掛牌」的履約價，保證是一個真的能下單的價差組合。找不到符合安全墊
    範圍的履約價，或短腿已經是最極端一檔時回傳 None。
    """
    if not math.isfinite(spot) or spot <= 0:
        return None
    sorted_legs = sorted(legs, key=lambda l: l.strike)
    candidates = [l for l in sorted_legs if MIN_OTM_PCT <= _otm_pct(spot, l.strike, side) <= MAX_OTM_PCT]
    if time_to_expiry_years is not None:
        candidates = [l for l in sorted_legs if _otm_pct(spot, l.strike, side) > 0
                      and 0.15 <= abs(float(black_scholes_delta(spot, l.strike, time_to_expiry_years,
                          l.put_iv if side == "put" else l.call_iv, side))) <= 0.30]
    if not candidates:
        return None

    short_leg = min(candidates, key=lambda l: _otm_pct(spot, l.strike, side))
    if time_to_expiry_years is not None:
        short_leg = min(candidates, key=lambda l: abs(abs(float(black_scholes_delta(
            spot, l.strike, time_to_expiry_years, l.put_iv if side == "put" else l.call_iv, side))) - 0.20))
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
    *, risk_based: bool = False, short_strike_floor: float | None = None,
) -> Optional[SpreadResult]:
    if short_strike_floor is not None:
        if side != "call" or not math.isfinite(short_strike_floor):
            return None
        ordered = sorted(legs, key=lambda leg: leg.strike)
        short_idx = next((i for i, leg in enumerate(ordered)
                          if leg.strike >= short_strike_floor and leg.strike > spot), None)
        picked = ((ordered[short_idx], ordered[short_idx + 1])
                  if short_idx is not None and short_idx + 1 < len(ordered) else None)
    else:
        picked = select_credit_spread_strikes(legs, spot, side, time_to_expiry_years if risk_based else None)
    if picked is None:
        return None
    short_leg, long_leg = picked
    if short_leg.expiry != long_leg.expiry:
        return None

    if side == "put":
        short_bid, long_ask, short_iv = short_leg.put_bid, long_leg.put_ask, short_leg.put_iv
        option_type: Literal["PUT", "CALL"] = "PUT"
    else:
        short_bid, long_ask, short_iv = short_leg.call_bid, long_leg.call_ask, short_leg.call_iv
        option_type = "CALL"

    if not all(math.isfinite(v) and v > 0 for v in (spot, time_to_expiry_years, short_bid, long_ask)):
        return None  # 無流動性（沒人報價），這種價差實際上下不了單
    credit = short_bid - long_ask
    if credit <= 0:
        return None  # 理論上價差應該收租，算出負值代表報價異常（多半是深度價外流動性太差）

    width = abs(long_leg.strike - short_leg.strike)
    if not math.isfinite(width) or credit >= width:
        return None
    max_profit = round(credit * OPTIONS_MULTIPLIER, 2)
    max_loss = round((width - credit) * OPTIONS_MULTIPLIER, 2)
    margin_required = max_loss  # 定義風險價差的標準保證金公式：保證金 = 最大虧損

    win_rate_bucket = "N/A"
    win_rate_pop: Optional[float] = None
    if math.isfinite(short_iv) and short_iv > 0:
        breakeven = short_leg.strike - credit if side == "put" else short_leg.strike + credit
        if breakeven <= 0:
            return None
        d2 = (math.log(spot / breakeven) + (0.045 - 0.5 * short_iv**2) * time_to_expiry_years) / (short_iv * math.sqrt(time_to_expiry_years))
        if math.isfinite(d2):
            win_rate_pop = float(norm.cdf(d2 if side == "put" else -d2)) * 100
            win_rate_bucket = _bucket_pct(win_rate_pop)

    otm_pct = _otm_pct(spot, short_leg.strike, side) * 100
    side_label = "下方" if side == "put" else "上方"
    strategy_name = "Bull Put Spread（賣出看跌價差）" if side == "put" else "Bear Call Spread（賣出看漲價差）"
    ai_advice = (
        f"短腿履約價 ${short_leg.strike:.0f} 距現價{side_label}約{otm_pct:.1f}%，"
        f"模型獲利機率（風險中性、未扣費用）約{win_rate_bucket}，不是實證勝率。"
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
    *, risk_based: bool = False,
) -> Optional[SpreadResult]:
    put_side = build_credit_spread(legs, spot, "put", time_to_expiry_years, risk_based=risk_based)
    call_side = build_credit_spread(legs, spot, "call", time_to_expiry_years, risk_based=risk_based)
    if put_side is None or call_side is None:
        return None  # 兩側都要能組出價差，任一側不成就不勉強湊一個殘缺的鐵鷹

    max_profit = round(put_side.max_profit + call_side.max_profit, 2)

    # 到期只會有一側被突破（不會兩側同時虧損）沒錯，但突破那一側的實際虧損
    # 不是「那一側單獨的 max_loss」——沒被突破的另一側會完全作廢，你會把
    # 那一側收到的權利金也整筆留下，等於額外抵掉一部分虧損。正確算法：
    # 突破較寬那一側時的虧損＝該側履約價寬度－兩側總權利金（不是只扣自己
    # 那一側的權利金）。用 max_loss+max_profit 反推每一側各自的履約價寬度
    # （因為 build_credit_spread 算 max_loss 時就是 (width-credit)*倍數，
    # max_profit 就是 credit*倍數，兩者相加剛好是 width*倍數）。
    put_width = put_side.max_loss + put_side.max_profit
    call_width = call_side.max_loss + call_side.max_profit
    max_loss = round(max(put_width, call_width) - max_profit, 2)
    if max_loss <= 0:
        return None
    margin_required = max_loss

    put_short_strike = put_side.legs[0].strike_price
    call_short_strike = call_side.legs[0].strike_price

    # A single reference volatility keeps both tails in one coherent distribution.
    valid_iv = [l for l in legs if math.isfinite(l.call_iv) and math.isfinite(l.put_iv)
                and l.call_iv > 0 and l.put_iv > 0]
    if valid_iv:
        reference = min(valid_iv, key=lambda l: abs(l.strike - spot))
        iv = (reference.call_iv + reference.put_iv) / 2
        total_credit = max_profit / OPTIONS_MULTIPLIER
        low, high = put_short_strike - total_credit, call_short_strike + total_credit
        if low <= 0:
            return None
        sigma_t = iv * math.sqrt(time_to_expiry_years)
        def cdf(k: float) -> float:
            return float(norm.cdf((math.log(k / spot) - (0.045 - .5 * iv**2) * time_to_expiry_years) / sigma_t))
        combined_pop = max(0.0, min(100.0, (cdf(high) - cdf(low)) * 100))
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
            f"模型獲利機率{win_rate_bucket}使用近現價 IV 的共同風險中性分布，未扣費用，不是實證勝率。"
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
    if not math.isfinite(spot) or spot <= 0 or not math.isfinite(otm_pct) or otm_pct <= 0:
        return None
    sorted_legs = sorted(legs, key=lambda l: l.strike)
    call_candidates = [l for l in sorted_legs if _otm_pct(spot, l.strike, "call") >= otm_pct and l.call_ask > 0]
    put_candidates = [l for l in sorted_legs if _otm_pct(spot, l.strike, "put") >= otm_pct and l.put_ask > 0]
    if not call_candidates or not put_candidates:
        return None  # 找不到符合門檻、且有報價（有流動性）的履約價

    call_leg = min(call_candidates, key=lambda l: l.strike)  # 最近的合格價外 Call
    put_leg = max(put_candidates, key=lambda l: l.strike)  # 最近的合格價外 Put
    if call_leg.expiry != put_leg.expiry:
        return None

    debit = call_leg.call_ask + put_leg.put_ask
    if not math.isfinite(debit) or debit <= 0:
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
RESISTANCE_ENTRY_BAND_PCT = 0.01  # research-only hypothesis, not optimized


def near_call_wall(spot: float, call_wall: float) -> bool:
    return (math.isfinite(spot) and math.isfinite(call_wall) and spot > 0
            and 0 < call_wall - spot <= spot * RESISTANCE_ENTRY_BAND_PCT)


def _select_strategy_template(
    legs: list[StrikeLegRaw], spot: float, time_to_expiry_years: float,
    put_wall: float, call_wall: float, alert: Optional[str],
    *, risk_based: bool = False, resistance_only: bool = False,
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
    if alert and not resistance_only:
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
            legs=strangle.legs,
            # strategy_tracker.py 的 score_outcome() 是用「每股/每口」單位算損益
            # （不乘上 OPTIONS_MULTIPLIER），但 total_debit 是這裡已經乘過100的
            # 實際美元金額——兩者混用會讓結算時的權利金跟內在價值差100倍，是
            # 實測抓到的真bug。這裡除回單位一致的「每股」金額才能正確結算。
            net_premium=strangle.total_debit / OPTIONS_MULTIPLIER,
            strategy_type="debit",
            max_loss=strangle.total_debit / OPTIONS_MULTIPLIER,  # 買方策略最大虧損就是付出的權利金全額
        )

    put_dist_pct = (spot - put_wall) / spot * 100
    call_dist_pct = (call_wall - spot) / spot * 100

    if resistance_only:
        rationale = (f"現價位於 Call Wall（${call_wall:.0f}）下方 1% 內，"
                     "僅研究於阻力附近建立 Bear Call Spread。")
        result = build_credit_spread(legs, spot, "call", time_to_expiry_years,
                                     risk_based=risk_based, short_strike_floor=call_wall)
        strategy_label = "Bear Call Spread"
    elif abs(put_dist_pct - call_dist_pct) <= WALL_BALANCE_THRESHOLD_PCT:
        rationale = (
            f"現貨與 Put Wall（距離{put_dist_pct:.1f}%）、Call Wall（距離{call_dist_pct:.1f}%）"
            "兩道牆距離接近，判斷為盤整格局，適合兩邊收租的 Iron Condor。"
        )
        result = build_iron_condor(legs, spot, time_to_expiry_years, risk_based=risk_based)
        strategy_label = "Iron Condor"
    elif put_dist_pct < call_dist_pct:
        rationale = (
            f"Put Wall（${put_wall:.0f}）距現貨較近（{put_dist_pct:.1f}% vs {call_dist_pct:.1f}%），"
            "判斷下方支撐較強，適合賣出看跌價差。"
        )
        result = build_credit_spread(legs, spot, "put", time_to_expiry_years, risk_based=risk_based)
        strategy_label = "Bull Put Spread"
    else:
        rationale = (
            f"Call Wall（${call_wall:.0f}）距現貨較近（{call_dist_pct:.1f}% vs {put_dist_pct:.1f}%），"
            "判斷上方壓力較強，適合賣出看漲價差。"
        )
        result = build_credit_spread(legs, spot, "call", time_to_expiry_years, risk_based=risk_based)
        strategy_label = "Bear Call Spread"

    if result is None:
        return StrategyRecommendation(
            strategy_name=f"無建議（{strategy_label}）", rationale=rationale,
            detail_lines=["選腿範圍內找不到有效價差，本日跳過策略建議。"],
        )

    return StrategyRecommendation(
        strategy_name=result.name, rationale=rationale,
        detail_lines=[
            f"- 動作：{_format_legs(result.legs)}",
            f"- 最大獲利：${result.max_profit:,.0f}　最大虧損：${result.max_loss:,.0f}",
            f"- 風險報酬比：{result.risk_reward_ratio}",
            f"- 模型獲利機率（未扣費用）：{result.win_rate_bucket}",
            f"- {result.ai_advice}",
        ],
        legs=result.legs,
        # 同上：max_profit/max_loss 是已經乘過 OPTIONS_MULTIPLIER 的實際美元金額，
        # 除回「每股」單位才能跟 strategy_tracker.score_outcome() 的內在價值計算
        # （直接用履約價差，沒有乘100）對得上單位。
        net_premium=result.max_profit / OPTIONS_MULTIPLIER,  # 賣方策略的最大獲利＝到期兩腳都作廢時，當初收到的淨權利金
        strategy_type="credit",
        max_loss=result.max_loss / OPTIONS_MULTIPLIER,
    )


def select_strategy(
    legs: list[StrikeLegRaw], spot: float, time_to_expiry_years: float,
    put_wall: float, call_wall: float, alert: Optional[str], *,
    total_net_gex: float | None = None,
    forecast_vol: float | None = None,
    data_usable: bool = True,
    fee_per_contract: float = 0.65,
    slippage_per_share: float = 0.01,
    min_edge_on_risk: float = 0.02,
    setup: Literal["gamma_template", "resistance_defense"] = "gamma_template",
) -> StrategyRecommendation:
    """Experimental candidate filter; forecast_vol must be estimated before entry.

    GEX is a proxy, not observed dealer inventory. A lognormal zero-drift
    forecast is a screening model, not evidence of profitable trading.
    """
    def skip(reason: str) -> StrategyRecommendation:
        return StrategyRecommendation("無建議（研究風控）", reason, [reason])

    if setup not in ("gamma_template", "resistance_defense"):
        raise ValueError("Unknown research setup")
    if not data_usable or not all(math.isfinite(x) and x > 0 for x in (spot, time_to_expiry_years, put_wall, call_wall)):
        return skip("資料品質不足或價格無效，暫停策略推薦。")
    if not put_wall < spot < call_wall:
        return skip("現價未位於有效 Put Wall / Call Wall 之間，不能推論盤整或支撐壓力。")
    if setup == "resistance_defense" and not near_call_wall(spot, call_wall):
        return skip("現價未接近上方 Call Wall（阻力 1% 範圍），不建立防守價差。")
    if total_net_gex is None or not math.isfinite(total_net_gex) or total_net_gex == 0:
        return skip("缺少可判讀的 Net GEX，不能由警示文字推論 Gamma 狀態。")
    if setup == "resistance_defense" and total_net_gex < 0:
        return skip("Net GEX 為負，阻力突破風險較高，暫不賣出看漲價差。")
    if forecast_vol is None or not math.isfinite(forecast_vol) or forecast_vol <= 0:
        return skip("缺少進場前波動預測，尚不能比較期權成本與預期收益。")
    if not all(math.isfinite(x) and x >= 0 for x in (fee_per_contract, slippage_per_share, min_edge_on_risk)):
        raise ValueError("Costs and minimum edge must be finite and nonnegative")
    if not legs or len({leg.expiry for leg in legs}) != 1 or len({leg.strike for leg in legs}) != len(legs):
        return skip("期權鏈到期日混用或履約價重複。")
    # Replace the fixed percentage band with an ex-ante 15-30 delta band.
    filtered = []
    for leg in legs:
        values = (leg.strike, leg.call_iv, leg.put_iv, leg.call_bid, leg.call_ask, leg.put_bid, leg.put_ask)
        if not all(math.isfinite(x) and x >= 0 for x in values) or leg.strike <= 0:
            continue
        filtered.append(leg)
    if not filtered:
        return skip("沒有有效期權報價。")
    negative = total_net_gex < 0
    rec = _select_strategy_template(filtered, spot, time_to_expiry_years, put_wall, call_wall,
                                    "negative gamma" if negative else None, risk_based=True,
                                    resistance_only=setup == "resistance_defense")
    if rec.legs is None:
        return rec
    chain = {leg.strike: leg for leg in filtered}
    for trade_leg in rec.legs:
        if trade_leg.action == "SELL" and (
            (trade_leg.option_type == "PUT" and trade_leg.strike_price > put_wall)
            or (trade_leg.option_type == "CALL" and trade_leg.strike_price < call_wall)
        ):
            return skip("Delta 選腿位於支撐壓力保護範圍內側，跳過交易。")
        raw = chain[trade_leg.strike_price]
        side = trade_leg.option_type.lower()
        bid, ask = getattr(raw, f"{side}_bid"), getattr(raw, f"{side}_ask")
        if bid <= 0 or ask < bid or (ask - bid) / ask > 0.25:
            return skip("報價無效、交叉或買賣價差超過 25%，跳過交易。")
    # Exact expectation under the specified lognormal forecast, no random sampling.
    def intrinsic(k: float, call: bool) -> float:
        sigma_t = forecast_vol * math.sqrt(time_to_expiry_years)
        d1 = (math.log(spot / k) + 0.5 * sigma_t**2) / sigma_t
        d2 = d1 - sigma_t
        return float(spot * norm.cdf(d1) - k * norm.cdf(d2)) if call else float(k * norm.cdf(-d2) - spot * norm.cdf(-d1))
    costs = len(rec.legs) * (2 * fee_per_contract / OPTIONS_MULTIPLIER + slippage_per_share)
    expected = rec.net_premium if rec.strategy_type == "credit" else -rec.net_premium
    for trade_leg in rec.legs:
        expected += (1 if trade_leg.action == "BUY" else -1) * intrinsic(trade_leg.strike_price, trade_leg.option_type == "CALL")
    expected -= costs
    if expected <= min_edge_on_risk * (rec.max_loss + costs):
        return skip("扣除費用後模型期望收益不足；保留現金，不強制交易。")
    if setup == "resistance_defense":
        rec.rationale = (f"研究候選：現價接近 Call Wall ${call_wall:.0f}，短 Call 置於阻力或其上方；"
                         "扣費後模型期望為正，但尚未證明樣本外優勢。")
    else:
        rec.rationale = "研究候選：依 Net GEX 代理值及進場前波動預測篩選，尚未證明樣本外優勢。"
    rec.detail_lines.append(f"- 模型期望損益（扣費用）：${expected * OPTIONS_MULTIPLIER:.2f}；須經樣本外驗證。")
    return rec
