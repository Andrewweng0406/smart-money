#!/usr/bin/env python3
"""風險計量——把分散的籌碼指標聚合成「今天有多危險」的單一判斷。

**這個模組不預測方向。** GEX 的理論基礎是做市商對沖行為：負 Gamma 時對沖
順勢（放大波動），正 Gamma 時逆勢（壓抑波動）。那是對**波動大小**的影響
（二階矩），不是對漲跌的預測（一階矩）。拿籌碼面預測方向是用它最弱的那
一面；問「今天適不適合重倉」比問「今天會漲還跌」有把握得多。

純計算層：不做任何 I/O，所有輸入由呼叫端從 AnalysisResult 取好傳進來，
不需要抓任何新資料。

⚠️ 跟 select_strategy / compute_market_maker_pressure_score 同性質——這是
「把判斷邏輯攤開透明」的規則型工具，不是回測驗證過的最佳解。每個因子都會
在 factors 裡列出貢獻幾分與理由，使用者要能自己判斷同不同意，而不是接受
一個黑箱分數。
"""

from __future__ import annotations

import signal_tiering

# 負 Gamma 給最高權重：這是唯一有明確理論機制的因子——做市商的 delta 對沖
# 在負 Gamma 區是順勢的（跌了要賣更多、漲了要買更多），會實質放大已經發生
# 的價格變動。其餘因子都是「情境提示」，機制沒有這條硬。
NEGATIVE_GAMMA_POINTS = 30

# 0DTE 佔比高代表 gamma 集中在當日到期的合約上：到期前對沖需求劇烈變化、
# 到期後整塊 gamma 瞬間消失，前後的市場結構可能完全不同。
ZERO_DTE_SHARE_THRESHOLD_PCT = 50.0
ZERO_DTE_POINTS = 20

# 財報/FOMC 這類事件會讓籌碼面的推論失效——事件本身的資訊衝擊遠大於
# 做市商對沖流。倒數天數由 macro_calendar 判斷，這裡只看有沒有警示。
EVENT_POINTS = 25

# 做市商壓力警報（含死亡 Loop）已經是 smart_money 判定的極端情境。
ALERT_POINTS = 15

# Put IV 減 Call IV，單位是波動點。0.05 = 5 個波動點，是明顯偏斜的量級。
IV_SKEW_EXTREME = 0.05
IV_SKEW_POINTS = 10

# 正 Gamma + 高 Pinning 是被壓抑的區間環境，風險往下調。
# 刻意「只在正 Gamma 時」生效：負 Gamma 下的 Pinning 不該給人安全感，
# 對沖是順勢的，釘不住。
PINNING_DAMPENER_POINTS = -10
PINNING_DAMPENER_SCORE_THRESHOLD = 60

_LABEL_THRESHOLDS = [(25, "低"), (50, "中"), (75, "高")]
_EXTREME_LABEL = "極高"

_STAND_ASIDE_SCORE = 75


def _risk_label(score: int) -> str:
    for threshold, label in _LABEL_THRESHOLDS:
        if score < threshold:
            return label
    return _EXTREME_LABEL


def _build_posture(score: int, negative_gamma: bool) -> str:
    """回傳「這個 regime 適合哪類策略結構」——結構類型，不是倉位數字。

    刻意不輸出「投入資金的 X%」：那是投資建議，而且沒有任何回測支撐，
    寫出來會顯得比實際更有把握。
    """
    if score >= _STAND_ASIDE_SCORE:
        return "風險極高，建議觀望——多個極端條件同時成立時，籌碼面的推論最不可靠"
    if negative_gamma:
        return "負 Gamma 放大波動：偏向防守型或突圍型結構，避免裸賣任一側"
    return "正 Gamma 壓抑波動：區間環境，相對適合賣方價差/Iron Condor 類結構"


def assess_risk(
    spot: float,
    gamma_flip: float | None,
    total_net_gex: float | None,
    zero_dte_share_pct: float | None = None,
    iv_skew: float | None = None,
    pinning: dict | None = None,
    mm_pressure: dict | None = None,
    alert: str | None = None,
    calendar_warnings: list[str] | None = None,
) -> dict:
    """回傳今天的風險計量。所有輸入都來自 AnalysisResult 既有欄位。

    regime 判斷重用 signal_tiering.build_regime()——全系統只能有一個
    「什麼叫負 Gamma」的定義。這個 codebase 的獨立審查抓到過 negative_gamma
    判斷被誤用的 bug，兩套定義並存遲早再出事。
    """
    regime = signal_tiering.build_regime(spot, gamma_flip, total_net_gex)
    negative_gamma = bool(regime["negative_gamma"])

    factors: list[dict] = []
    avoid: list[str] = []

    if negative_gamma:
        factors.append({
            "name": "負 Gamma",
            "points": NEGATIVE_GAMMA_POINTS,
            "why": "做市商 delta 對沖轉為順勢，會實質放大已經發生的價格變動",
        })
        avoid.append("負 Gamma 環境下避免裸賣任一側——波動放大時裸賣的尾部風險最大")

    if zero_dte_share_pct is not None and zero_dte_share_pct >= ZERO_DTE_SHARE_THRESHOLD_PCT:
        factors.append({
            "name": f"0DTE 佔比 {zero_dte_share_pct:.0f}%",
            "points": ZERO_DTE_POINTS,
            "why": "gamma 集中在當日到期合約，到期前後的市場結構可能完全不同",
        })
        avoid.append("避免持有當日到期的部位跨越收盤")

    if calendar_warnings:
        factors.append({
            "name": "重大事件臨近",
            "points": EVENT_POINTS,
            "why": f"{calendar_warnings[0]}——事件的資訊衝擊遠大於做市商對沖流，籌碼面推論會失效",
        })
        avoid.append("避免用短天期部位跨越事件日")

    if alert:
        factors.append({
            "name": "做市商壓力警報",
            "points": ALERT_POINTS,
            "why": alert,
        })

    if iv_skew is not None and abs(iv_skew) >= IV_SKEW_EXTREME:
        points_label = f"{abs(iv_skew) * 100:.0f} 個波動點"
        if iv_skew > 0:
            factors.append({
                "name": "Put 側 IV 溢價",
                "points": IV_SKEW_POINTS,
                "why": f"Put IV 高出 Call {points_label}，市場正在為下跌尾部風險付費",
            })
            avoid.append("避免裸賣 Put——那等於承接市場正在付費規避的下跌尾部")
        else:
            factors.append({
                "name": "Call 側 IV 溢價",
                "points": IV_SKEW_POINTS,
                "why": f"Call IV 高出 Put {points_label}，市場正在為上漲軋空風險付費",
            })
            avoid.append("避免裸賣 Call——那等於承接市場正在付費規避的軋空尾部")

    pinning_score = (pinning or {}).get("score")
    if (
        not negative_gamma
        and pinning_score is not None
        and pinning_score >= PINNING_DAMPENER_SCORE_THRESHOLD
    ):
        factors.append({
            "name": f"正 Gamma 釘價（Pinning {pinning_score}）",
            "points": PINNING_DAMPENER_POINTS,
            "why": "做市商對沖逆勢，價格被拉回 Pin Strike 的傾向壓抑了波動",
        })

    raw_score = sum(factor["points"] for factor in factors)
    score = max(0, min(100, raw_score))

    return {
        "risk_score": score,
        "risk_label": _risk_label(score),
        "negative_gamma": negative_gamma,
        "regime_source": regime["source"],
        "regime_text": (
            "負 Gamma（做市商對沖順勢，放大波動）" if negative_gamma
            else "正 Gamma（做市商對沖逆勢，壓抑波動）"
        ),
        "factors": factors,
        "avoid": avoid,
        "posture": _build_posture(score, negative_gamma),
    }
