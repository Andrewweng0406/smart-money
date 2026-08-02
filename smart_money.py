"""美股期權聰明錢與做市商壓力的純計算工具。"""

from __future__ import annotations

from typing import Any


DEATH_LOOP_ALERT_TEXT = (
    "⚠️ 散戶正陷入買 Call 送權利金的死亡 Loop，做市商對沖賣壓極大，"
    "隨時有強制洗盤爆倉風險！"
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
            if otm_min_pct <= call_otm_pct <= otm_max_pct:
                otm_call_ivs.append(leg.call_iv)
        elif leg.strike < spot:
            put_otm_pct = (spot - leg.strike) / spot
            if otm_min_pct <= put_otm_pct <= otm_max_pct:
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


def detect_unusual_activity(
    legs: list[Any],
    min_volume_oi_ratio: float = 0.5,
    min_volume: float = 100,
    top_n: int = 5,
) -> list[dict[str, float | str]]:
    """找出成交量相對 OI 明顯放大的 Call 或 Put。"""
    unusual: list[dict[str, float | str]] = []

    for leg in legs:
        for side in ("call", "put"):
            volume = getattr(leg, f"{side}_volume")
            oi = getattr(leg, f"{side}_oi")

            # 莊家做盤視角：OI 為零但出現達標成交量，代表市場可能正在建立全新部位，
            # 相對既有籌碼的增幅視為無限大；一般情況則用 volume/OI 衡量新量能。
            ratio = float("inf") if oi == 0 else volume / oi
            if volume >= min_volume and ratio >= min_volume_oi_ratio:
                unusual.append(
                    {
                        "strike": leg.strike,
                        "side": side,
                        "volume": volume,
                        "oi": oi,
                        "ratio": ratio,
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

    return {
        "score": score,
        "label": label,
        "is_death_loop_alert": is_death_loop_alert,
        "alert_text": DEATH_LOOP_ALERT_TEXT if is_death_loop_alert else None,
    }
