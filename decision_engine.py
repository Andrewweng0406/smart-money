"""把籌碼結構轉成保守、可驗證的決策摘要。"""

from __future__ import annotations

import signal_tiering


NEAR_LEVEL_PCT = 0.03


def _price(value: float) -> str:
    return f"${value:,.0f}"


def build_decision_brief(
    spot: float,
    put_wall: float,
    call_wall: float,
    gamma_flip: float | None,
    total_net_gex: float | None = None,
    oi_data_quality: dict | None = None,
    data_quality: dict | None = None,
    calendar_warnings: list[str] | None = None,
) -> dict[str, str | list[str]]:
    """回傳行動姿態、信心與上下觸發條件，不預測未經驗證的漲跌方向。"""
    why: list[str] = []
    regime = signal_tiering.build_regime(spot, gamma_flip, total_net_gex)
    negative_gamma = bool(regime["negative_gamma"])
    valid_levels = (
        spot > 0 and put_wall > 0 and call_wall > 0
        and put_wall < call_wall and gamma_flip is not None and gamma_flip > 0
    )

    if data_quality and not data_quality.get("usable", True):
        score = data_quality.get("score", 0)
        why.append(f"資料健康 {score}/100：{data_quality.get('reason', '資料不完整')}")
        return {
            "action": "觀望",
            "confidence": "低",
            "summary": f"期權鏈資料健康僅 {score}/100，暫不使用籌碼結構做交易判斷。",
            "upside_trigger": "等待資料健康恢復後重新分析",
            "downside_trigger": "等待資料健康恢復後重新分析",
            "why": why,
        }

    if oi_data_quality and not oi_data_quality.get("usable", True):
        why.append(f"OI 資料不可信：{oi_data_quality.get('reason', '資料不完整')}")
        return {
            "action": "觀望",
            "confidence": "低",
            "summary": "籌碼資料不完整，現在不應用 GEX 或 Wall 做交易判斷。",
            "upside_trigger": "等待 OI 恢復正常後重新分析",
            "downside_trigger": "等待 OI 恢復正常後重新分析",
            "why": why,
        }

    if calendar_warnings:
        why.append(calendar_warnings[0])
        return {
            "action": "事件前觀望",
            "confidence": "低",
            "summary": "重大事件可能壓過做市商籌碼結構，事件前不追求方向判斷。",
            "upside_trigger": f"等待事件公布後，再看價格是否站穩 Call Wall {_price(call_wall)}",
            "downside_trigger": f"等待事件公布後，再看價格是否跌破 Put Wall {_price(put_wall)}",
            "why": why,
        }

    if not valid_levels:
        why.append("Gamma Flip 或 Wall 結構缺失／順序異常")
        return {
            "action": "觀望",
            "confidence": "低",
            "summary": "關鍵價位結構不完整，無法建立可靠的觸發條件。",
            "upside_trigger": "等待下一次完整期權鏈更新",
            "downside_trigger": "等待下一次完整期權鏈更新",
            "why": why,
        }

    if negative_gamma:
        return {
            "action": "防守，等待方向確認",
            "confidence": "中",
            "summary": "負 Gamma 會放大已發生的走勢，區間內不預判方向。",
            "upside_trigger": f"站穩 Call Wall {_price(call_wall)} 才確認向上擴張",
            "downside_trigger": f"跌破 Put Wall {_price(put_wall)} 視為下行風險擴張",
            "why": ["做市商對沖順勢，假突破與快速延伸風險都較高"],
        }

    if spot > call_wall:
        action = "突破觀察，等待站穩"
        summary = "價格已越過 Call Wall，但正 Gamma 仍可能把價格拉回原區間。"
    elif spot < put_wall:
        action = "破位風險，優先防守"
        summary = "價格已跌破 Put Wall，先確認是否能收復，不把單次跌破當成反轉完成。"
    elif (call_wall - spot) / spot <= NEAR_LEVEL_PCT:
        action = "區間上緣，避免追價"
        summary = "正 Gamma 壓抑波動，價格接近區間上緣。"
    elif (spot - put_wall) / spot <= NEAR_LEVEL_PCT:
        action = "區間下緣，等待止跌"
        summary = "正 Gamma 壓抑波動，但價格接近區間下緣。"
    else:
        action = "區間應對，不追方向"
        summary = "正 Gamma 環境偏向均值回歸，價格仍在 Wall 區間內。"

    return {
        "action": action,
        "confidence": "中",
        "summary": summary,
        "upside_trigger": f"站穩 Call Wall {_price(call_wall)} 才重新評估向上突破",
        "downside_trigger": f"跌破 Gamma Flip {_price(gamma_flip)} 轉為防守；Put Wall {_price(put_wall)} 是下方風險線",
        "why": ["目前信心上限為中；訊號尚未累積足夠樣本外績效"],
    }
