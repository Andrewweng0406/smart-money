"""凍結決策當下的市場狀態，供日後按相同情境驗證；純計算。"""

from __future__ import annotations


ZERO_DTE_HIGH_THRESHOLD_PCT = 50.0
CONTEXT_FIELDS = (
    "gamma_regime", "price_zone", "event_regime", "zero_dte_regime", "data_regime",
)

_LABELS = {
    "gamma_regime": {"positive": "正 Gamma", "negative": "負 Gamma", "unknown": "Gamma 未知"},
    "price_zone": {
        "above_call_wall": "Call Wall 上方", "inside_walls": "Wall 區間內",
        "below_put_wall": "Put Wall 下方", "unknown": "Wall 位置未知",
    },
    "event_regime": {"event_risk": "事件風險", "normal": "一般交易日", "unknown": "事件狀態未知"},
    "zero_dte_regime": {"high": "高 0DTE", "normal": "一般 0DTE", "unknown": "0DTE 未知"},
    "data_regime": {"usable": "資料可用", "degraded": "資料降級", "unknown": "資料狀態未知"},
}


def classify_market_context(
    spot: float, put_wall: float, call_wall: float, gamma_flip: float | None,
    zero_dte_share_pct: float | None, event_risk: bool | None,
    data_quality: dict | None,
) -> dict[str, str]:
    """把可觀測輸入轉成穩定分類；缺值保留 unknown，不用猜測補齊。"""
    if gamma_flip is None or gamma_flip <= 0 or spot <= 0:
        gamma_regime = "unknown"
    else:
        gamma_regime = "negative" if spot < gamma_flip else "positive"

    if spot <= 0 or put_wall <= 0 or call_wall <= put_wall:
        price_zone = "unknown"
    elif spot > call_wall:
        price_zone = "above_call_wall"
    elif spot < put_wall:
        price_zone = "below_put_wall"
    else:
        price_zone = "inside_walls"

    if event_risk is None:
        event_regime = "unknown"
    else:
        event_regime = "event_risk" if event_risk else "normal"

    if zero_dte_share_pct is None:
        zero_dte_regime = "unknown"
    else:
        zero_dte_regime = (
            "high" if zero_dte_share_pct >= ZERO_DTE_HIGH_THRESHOLD_PCT else "normal"
        )

    if data_quality is None:
        data_regime = "unknown"
    else:
        data_regime = "usable" if data_quality.get("usable", False) else "degraded"

    return {
        "gamma_regime": gamma_regime,
        "price_zone": price_zone,
        "event_regime": event_regime,
        "zero_dte_regime": zero_dte_regime,
        "data_regime": data_regime,
    }


def context_key(context: dict | None) -> tuple[str, ...]:
    """固定欄位順序，避免 dict 建立順序讓同一情境被拆成不同組。"""
    context = context or {}
    return tuple(context.get(field) or "unknown" for field in CONTEXT_FIELDS)


def context_from_snapshot(row: dict) -> dict[str, str]:
    """只讀決策當時凍結的欄位；舊快照缺值時維持 unknown。"""
    return {
        field: row.get(f"decision_{field}") or "unknown"
        for field in CONTEXT_FIELDS
    }


def format_context(context: dict | None, include_data: bool = False) -> str:
    fields = CONTEXT_FIELDS if include_data else CONTEXT_FIELDS[:-1]
    context = context or {}
    return "／".join(
        _LABELS[field].get(context.get(field), _LABELS[field]["unknown"])
        for field in fields
    )
