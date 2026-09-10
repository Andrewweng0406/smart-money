#!/usr/bin/env python3
"""訊號分級政策——決定每個偵測到的訊號該走哪個通道。

這是純計算層：不做任何 I/O、不讀任何狀態。分級只看「這個訊號本身是什麼」，
不知道剛剛有沒有發過同樣的東西——持續中的狀態由 crossing 偵測跟冷卻層處理，
不是分級層的職責。在這裡讀狀態會摧毀可測性。

三個級別：
- urgent：立即推播 Telegram
- watch：不即時推播，累積後併入 10:00 摘要或 16:30 日報
- silent：只寫入資料庫，不推播
"""

from __future__ import annotations

from typing import Literal

Tier = Literal["urgent", "watch", "silent"]

KIND_CALL_WALL_BREACH = "call_wall_breach"
KIND_PUT_WALL_BREACH = "put_wall_breach"
KIND_MM_PRESSURE = "mm_pressure"
KIND_UNUSUAL_ACTIVITY = "unusual_activity"
KIND_PINNING_HIGH = "pinning_high"

# Pinning 門檻從 80 降到 70：80 分在 78 筆生產資料中從未觸發（實測最高 76），
# 是實質死碼。70 分回溯會觸發約 2.6%，稀有度合理。只放 watch——Pinning 描述
# 的是「價格可能被釘住」，本質上不急迫。
PINNING_WATCH_SCORE_THRESHOLD = 70

# 沿用 intraday_watcher 既有的偵測門檻，本模組不調整靈敏度。
UNUSUAL_ACTIVITY_WATCH_RATIO = 3.0

# 每日推播預算超額時的取捨順序，數字越小越優先保留。
_URGENT_PRIORITY = {
    KIND_PUT_WALL_BREACH: 1,
    KIND_MM_PRESSURE: 2,
    KIND_CALL_WALL_BREACH: 3,
}
_LOWEST_PRIORITY = 99


def build_regime(
    spot: float | None, gamma_flip: float | None, total_net_gex: float | None,
) -> dict:
    """組出分級要用的市場狀態。

    刻意「不用」total_net_gex 的正負號當主判斷：那個值來自前一交易日收盤
    快照，而 dealer gamma 盤中就會翻轉——那正是 gamma flip 這個概念存在的
    理由。用昨收的符號判斷今天的 regime，會在「regime 正在改變」的日子
    系統性判錯，而那恰好是唯一重要的日子；且錯誤方向不利：市場剛翻入負
    gamma、波動放大時，閘門仍會判定「正 gamma，不緊急」。

    改用 spot 相對 gamma_flip 的位置當即時代理：gamma_flip 已存在於快照、
    spot 本來就會抓，零額外 API 成本。gamma_flip 缺值時才退回 net_gex，
    並在 source 標記，讓事後稽核分得出哪些判斷走了退化路徑。
    """
    if gamma_flip is not None and spot is not None:
        return {
            "negative_gamma": spot < gamma_flip,
            "source": "gamma_flip",
            "gamma_flip": gamma_flip,
            "total_net_gex": total_net_gex,
        }
    return {
        "negative_gamma": (total_net_gex or 0) < 0,
        "source": "net_gex_fallback",
        "gamma_flip": gamma_flip,
        "total_net_gex": total_net_gex,
    }


def _regime_suffix(regime: dict) -> str:
    return "（regime 來源：net_gex 退化路徑）" if regime.get("source") == "net_gex_fallback" else ""


def classify(kind: str, payload: dict, regime: dict) -> tuple[str, str]:
    """回傳 (級別, 理由)。理由給人看，也存進 DB 供日後稽核。"""
    negative_gamma = bool(regime.get("negative_gamma"))
    suffix = _regime_suffix(regime)

    if kind == KIND_PUT_WALL_BREACH:
        # 不分 regime 一律緊急。這是風險偏好設定，不是實證發現：正 Gamma 下
        # Call Wall 突破被壓回只是錯過漲幅，Put Wall 跌破未被壓回卻是實際虧損。
        # 不可將此規則解讀成「資料顯示 Put Wall 跌破較危險」——沒有資料支持。
        return "urgent", f"Put Wall 向下穿越，採保護優先一律緊急{suffix}"

    if kind in (KIND_CALL_WALL_BREACH, KIND_MM_PRESSURE):
        label = "Call Wall 向上穿越" if kind == KIND_CALL_WALL_BREACH else "做市商賣壓警報"
        if negative_gamma:
            return "urgent", f"{label}且處於負 Gamma，波動易放大{suffix}"
        return "watch", f"{label}但處於正 Gamma，通常被壓回{suffix}"

    if kind == KIND_UNUSUAL_ACTIVITY:
        # 永遠不會是 urgent：OI 由 OCC 隔夜結算公布，盤中拿不到今日 OI，
        # 因此無法區分開倉與平倉。平倉的巨量不代表有人在建方向性部位，
        # 推成緊急警報是純噪音。這個天花板要換 OPRA 級資料源才打得開。
        ratio = payload.get("ratio", 0.0)
        if ratio >= UNUSUAL_ACTIVITY_WATCH_RATIO:
            return "watch", f"異常大單比值 {ratio:.1f}x，盤中無法判定開倉/平倉故不升級"
        return "silent", f"異常大單比值 {ratio:.1f}x 未達觀察門檻"

    if kind == KIND_PINNING_HIGH:
        score = payload.get("score", 0)
        if score >= PINNING_WATCH_SCORE_THRESHOLD:
            return "watch", f"Pinning 分數 {score} 達觀察門檻"
        return "silent", f"Pinning 分數 {score} 未達觀察門檻"

    return "silent", f"未知訊號種類 {kind}，保守落地不推播"


def urgent_priority(kind: str) -> int:
    """每日推播預算超額時的取捨順序，數字越小越優先保留。"""
    return _URGENT_PRIORITY.get(kind, _LOWEST_PRIORITY)
