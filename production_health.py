"""生產快照完整性判斷；只吃參數回傳結果，不做檔案或網路 I/O。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import market_calendar


_CONTEXT_COLUMNS = (
    "decision_gamma_regime", "decision_price_zone", "decision_event_regime",
    "decision_zero_dte_regime", "decision_data_regime",
)


def expected_snapshot_date(now: datetime) -> date:
    """依 NYSE session 與收盤後排程，算出現在理應已完成的最近交易日。"""
    now_et = now.astimezone(market_calendar.US_EASTERN)
    candidate = now_et.date()
    run_at = market_calendar.daily_analysis_time(candidate)
    if run_at is None or now_et < run_at:
        candidate -= timedelta(days=1)
    for _ in range(10):
        if market_calendar.is_market_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError("無法找到最近 NYSE 交易日")


def assess_symbol_health(
    symbol: str, snapshot: dict | None, expected_date: str, oi_strike_count: int,
) -> dict:
    """檢查一檔標的的快照、決策上下文與 OI 是否可供後續回測。"""
    issues: list[str] = []
    if not snapshot:
        return {
            "symbol": symbol, "healthy": False, "snapshot_date": None,
            "data_quality_score": None, "oi_strike_count": 0,
            "issues": ["從來沒有成功寫入過快照"],
        }

    snapshot_date = snapshot.get("date")
    if snapshot_date != expected_date:
        issues.append(f"缺少 {expected_date} 快照（最新 {snapshot_date or '未知'}）")
    try:
        spot = float(snapshot.get("spot"))
    except (TypeError, ValueError):
        spot = float("nan")
    if not math.isfinite(spot) or spot <= 0:
        issues.append("現貨價格無效")
    quality_score = snapshot.get("data_quality_score")
    if quality_score is None:
        issues.append("資料健康分數缺失")
    else:
        try:
            quality_score_number = float(quality_score)
        except (TypeError, ValueError):
            quality_score_number = float("nan")
        if not math.isfinite(quality_score_number) or not 0 <= quality_score_number <= 100:
            issues.append("資料健康分數無效")
    if not snapshot.get("decision_action") or not snapshot.get("decision_confidence"):
        issues.append("決策快照缺失")
    if any(not snapshot.get(column) for column in _CONTEXT_COLUMNS):
        issues.append("決策情境缺失")
    if oi_strike_count <= 0:
        issues.append("OI 快照缺失")

    return {
        "symbol": symbol, "healthy": not issues, "snapshot_date": snapshot_date,
        "data_quality_score": snapshot.get("data_quality_score"),
        "oi_strike_count": oi_strike_count, "issues": issues,
    }
