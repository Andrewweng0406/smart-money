"""美股期權聰明錢與做市商壓力的純計算工具。"""

from __future__ import annotations

import math
from typing import Any

from gex_engine import black_scholes_delta


DEATH_LOOP_ALERT_TEXT = (
    "⚠️ 目前訊號顯示散戶可能正集中買 Call、做市商避險賣壓偏大，值得提高警覺"
    "——這個時間點追高風險較高，建議暫緩加碼。"
)


def compute_iv_skew(
    legs: list[Any],
    spot: float,
    otm_min_pct: float = 0.05,
    otm_max_pct: float = 0.15,
) -> float | None:
    """計算指定價外區間的 Put IV 相對 Call IV 溢價。"""
    otm_call_ivs: list[float] = []
    otm_put_ivs: list[float] = []

    for leg in legs:
        if leg.strike > spot:
            call_otm_pct = (leg.strike - spot) / spot
            # IV<=0 代表這一腳的IV是被資料清洗掉的缺值（data_fetcher.py 對
            # 超出合理範圍/近到期雜訊的IV會清成0），不是「這檔真的零波動」，
            # 混進平均會把 IV Skew 拉向錯誤方向，製造出假的偏斜訊號——這是
            # 實測抓到的真bug。
            if otm_min_pct <= call_otm_pct <= otm_max_pct and leg.call_iv > 0:
                otm_call_ivs.append(leg.call_iv)
        elif leg.strike < spot:
            put_otm_pct = (spot - leg.strike) / spot
            if otm_min_pct <= put_otm_pct <= otm_max_pct and leg.put_iv > 0:
                otm_put_ivs.append(leg.put_iv)

    # 莊家做盤視角：兩側都要有樣本才可比較避險定價；缺一側就不能判斷偏斜。
    if not otm_call_ivs or not otm_put_ivs:
        return None

    average_call_iv = sum(otm_call_ivs) / len(otm_call_ivs)
    average_put_iv = sum(otm_put_ivs) / len(otm_put_ivs)

    # 正值表示下跌保護更昂貴，反映市場願意付給莊家較高的 Put 避險權利金。
    return average_put_iv - average_call_iv


def compute_put_call_ratio(legs: list[Any]) -> dict[str, float | None]:
    """計算整條期權鏈的成交量與未平倉量 Put/Call 比率。"""
    total_put_volume = sum(leg.put_volume for leg in legs)
    total_call_volume = sum(leg.call_volume for leg in legs)
    total_put_oi = sum(leg.put_oi for leg in legs)
    total_call_oi = sum(leg.call_oi for leg in legs)

    # 莊家做盤視角：成交量比率反映當日資金偏向，OI 比率則反映既有部位結構。
    # Call 端分母為零時不存在可比較基準，因此回傳 None 而不是製造失真訊號。
    volume_ratio = (
        total_put_volume / total_call_volume if total_call_volume != 0 else None
    )
    oi_ratio = total_put_oi / total_call_oi if total_call_oi != 0 else None

    return {"volume_ratio": volume_ratio, "oi_ratio": oi_ratio}


def compute_delta_adjusted_call_volume(
    legs: list[Any], spot: float, risk_free_rate: float = 0.045,
) -> float:
    """用 Delta 加權後的 Call 成交量，而不是原始張數。

    莊家做盤視角：深度價外的 Call 就算成交量很大，Delta 接近 0，實際上
    製造的方向性曝險（做市商需要對沖的股數）很小；平值/價內的 Call 即使
    張數較少，Delta 加權後的曝險反而可能更大。原始 call_volume 只看張數，
    沒辦法分辨「一堆便宜的價外樂透單」跟「真正大戶在建立實質曝險」——
    這是審查抓出的訊號品質問題之一：單靠成交量判斷死亡Loop風險太粗糙。

    legs 需要有 time_to_expiry_years 屬性（_aggregate_smart_money_legs 的
    輸出已經補上這個欄位，用跨到期日的平均到期時間近似）；沒有這個屬性、
    或 IV/成交量/到期時間任一項不是有效正值的腳位，直接跳過不計入加總
    （寧可低估也不要讓無效資料算出失真的 Delta）。
    """
    total = 0.0
    for leg in legs:
        tte = getattr(leg, "time_to_expiry_years", None)
        if not tte or tte <= 0 or leg.call_iv <= 0 or leg.call_volume <= 0:
            continue
        delta = float(black_scholes_delta(
            spot=spot, strike=leg.strike, time_to_expiry_years=tte,
            iv=leg.call_iv, option_type="call", risk_free_rate=risk_free_rate,
        ))
        if math.isnan(delta):
            continue
        total += leg.call_volume * abs(delta)
    return total


def detect_unusual_activity(
    legs: list[Any],
    min_volume_oi_ratio: float = 0.5,
    min_volume: float = 100,
    top_n: int = 5,
    previous_oi_by_strike: dict[float, dict[str, float]] | None = None,
) -> list[dict[str, float | str | bool | None]]:
    """找出成交量相對 OI 明顯放大的 Call 或 Put。

    傳入 previous_oi_by_strike（前一個交易日的
    {strike: {"call_oi":.., "put_oi":..}} 快照）時，每筆異常大單會多一個
    likely_opening 欄位：今天的 OI 比前一天高，判斷為「比較可能是新開倉」
    （淨新增部位）；OI持平或下降，判斷為「比較可能是平倉/轉倉」——單靠
    當天的 volume/OI 比例沒辦法分辨這兩種情況（審查抓出的訊號品質問題：
    「大單是否為 opening trade」）。沒有提供前一天資料時，likely_opening
    一律是 None（無法判斷，不瞎猜）。
    """
    unusual: list[dict[str, float | str | bool | None]] = []

    for leg in legs:
        for side in ("call", "put"):
            volume = getattr(leg, f"{side}_volume")
            oi = getattr(leg, f"{side}_oi")

            # 莊家做盤視角：OI 為零但出現達標成交量，代表市場可能正在建立全新部位，
            # 相對既有籌碼的增幅視為無限大；一般情況則用 volume/OI 衡量新量能。
            ratio = float("inf") if oi == 0 else volume / oi
            if volume >= min_volume and ratio >= min_volume_oi_ratio:
                likely_opening = None
                if previous_oi_by_strike is not None:
                    prev_oi = previous_oi_by_strike.get(leg.strike, {}).get(f"{side}_oi", 0.0)
                    likely_opening = oi > prev_oi
                unusual.append(
                    {
                        "strike": leg.strike,
                        "side": side,
                        "volume": volume,
                        "oi": oi,
                        "ratio": ratio,
                        "likely_opening": likely_opening,
                    }
                )

    # 比例越大，當日新增量能相對舊倉越突出，越值得優先觀察是否有大戶新建倉。
    unusual.sort(key=lambda item: item["ratio"], reverse=True)
    return unusual[:top_n]


def compute_market_maker_pressure_score(
    spot: float,
    put_wall: float,
    in_negative_gamma: bool,
    legs: list[Any],
) -> dict[str, int | str | bool | None]:
    """以透明的三項分量計算 0～100 的做市商收割壓力分數。"""
    # 莊家做盤視角：負 Gamma 區內做市商會順勢追著價格避險，容易放大漲跌波動。
    gamma_score = 50.0 if in_negative_gamma else 0.0

    distance_pct = (spot - put_wall) / spot * 100
    # 莊家做盤視角：現貨逼近或跌破 Put Wall 時，大量 Put 對沖調整可能加劇賣壓。
    if distance_pct <= 2:
        put_wall_score = 20.0
    elif distance_pct >= 10:
        put_wall_score = 0.0
    else:
        put_wall_score = (10 - distance_pct) / 8 * 20

    total_call_volume = sum(leg.call_volume for leg in legs)
    total_call_oi = sum(leg.call_oi for leg in legs)
    call_volume_oi_ratio = (
        total_call_volume / total_call_oi if total_call_oi != 0 else 0.0
    )
    # 莊家做盤視角：Call 當日成交追上既有 OI，代表追價需求旺盛，做市商需增加對沖。
    call_surge_score = min(call_volume_oi_ratio, 1.0) * 30

    # 三項分數皆非負，使用加 0.5 取整以符合一般「四捨五入」而非銀行家捨入。
    score = int(gamma_score + put_wall_score + call_surge_score + 0.5)

    if score < 30:
        label = "低"
    elif score < 60:
        label = "中"
    elif score < 80:
        label = "高"
    else:
        label = "極高"

    put_call_volume_ratio = compute_put_call_ratio(legs)["volume_ratio"]
    # 莊家做盤視角：負 Gamma 時散戶集中追 Call，做市商為維持 Delta 中性可能被迫
    # 加碼賣出標的；Call 量遠勝 Put 量時，這種順勢避險賣壓容易形成死亡 Loop。
    is_death_loop_alert = (
        in_negative_gamma
        and call_volume_oi_ratio >= 0.5
        and put_call_volume_ratio is not None
        and put_call_volume_ratio < 0.7
    )

    # 額外的診斷資訊，不影響上面的分數/警示判斷本身——單靠原始張數判斷
    # 死亡Loop風險偏粗糙（審查抓出的訊號品質問題），這裡額外提供 Delta
    # 加權後的Call成交量，讓使用者自己多一個角度判斷這次的Call量能是
    # 「一堆便宜的價外樂透單」還是「真正有實質方向性曝險」，不直接改動
    # 既有的觸發邏輯（避免正在運作中的警示行為無預警跟著改變）。
    delta_adjusted_call_volume = compute_delta_adjusted_call_volume(legs, spot)

    return {
        "score": score,
        "label": label,
        "is_death_loop_alert": is_death_loop_alert,
        "alert_text": DEATH_LOOP_ALERT_TEXT if is_death_loop_alert else None,
        "delta_adjusted_call_volume": delta_adjusted_call_volume,
    }
