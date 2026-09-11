#!/usr/bin/env python3
"""審核系統當時給出的決策姿態，避免只展示無法驗證的即時文字。"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import db_manager


DEFAULT_HORIZON = 1
MIN_SAMPLE_SIZE = 5

_SCORED_ACTIONS = {
    "突破觀察，等待站穩": lambda row, future: future > row["call_wall"],
    "破位風險，優先防守": lambda row, future: future < row["put_wall"],
    "區間上緣，避免追價": lambda row, future: future <= row["call_wall"],
    "區間下緣，等待止跌": lambda row, future: future >= row["put_wall"],
    "區間應對，不追方向": lambda row, future: row["put_wall"] <= future <= row["call_wall"],
}


def _episode_indices(rows: list[dict]) -> list[int]:
    """連續相同姿態只算一段，避免每日重複建議把有效樣本灌大。"""
    episodes = []
    previous_action = None
    for index, row in enumerate(rows):
        action = row.get("decision_action")
        if action and action != previous_action:
            episodes.append(index)
        previous_action = action
    return episodes


def _summarize_events(events: list[dict], min_sample_size: int) -> dict:
    matured = [event for event in events if event["outcome"] != "pending"]
    scored = [event for event in matured if event["success"] is not None]
    returns = [event["return_pct"] for event in matured]
    success_count = sum(bool(event["success"]) for event in scored)
    return {
        "sample_size": len(matured),
        "pending_count": len(events) - len(matured),
        "scored_sample_size": len(scored),
        "success_count": success_count if scored else None,
        "sufficient_sample": len(scored) >= min_sample_size,
        "success_rate_pct": success_count / len(scored) * 100 if scored else None,
        "avg_return_pct": statistics.mean(returns) if returns else None,
        "avg_abs_return_pct": statistics.mean(abs(value) for value in returns) if returns else None,
        "events": events,
    }


def audit_decision_rows(
    rows: list[dict], horizon: int = DEFAULT_HORIZON, min_sample_size: int = MIN_SAMPLE_SIZE,
) -> dict:
    """用依日期排序的每日快照評估決策；純計算，不讀檔、不打網路。"""
    if horizon < 1:
        raise ValueError("horizon 必須至少為 1")

    ordered = sorted((row for row in rows if row.get("spot")), key=lambda row: row["date"])
    decision_days = sum(bool(row.get("decision_action")) for row in ordered)
    events_by_action: dict[str, list[dict]] = {}
    recent = []

    for index in _episode_indices(ordered):
        row = ordered[index]
        action = row["decision_action"]
        future_index = index + horizon
        event = {
            "date": row["date"], "action": action,
            "confidence": row.get("decision_confidence"), "success": None,
        }
        if future_index >= len(ordered):
            event.update(outcome="pending", future_date=None, return_pct=None)
        else:
            future = ordered[future_index]
            return_pct = (future["spot"] - row["spot"]) / row["spot"] * 100
            scorer = _SCORED_ACTIONS.get(action)
            success = scorer(row, future["spot"]) if scorer else None
            event.update(
                outcome=("confirmed" if success else "invalidated") if scorer else "observed",
                success=success, future_date=future["date"], return_pct=return_pct,
            )
        events_by_action.setdefault(action, []).append(event)
        recent.append(event)

    actions = {
        action: _summarize_events(events, min_sample_size)
        for action, events in events_by_action.items()
    }
    all_events = [event for events in events_by_action.values() for event in events]
    return {
        "row_count": len(ordered), "decision_days": decision_days,
        "episode_count": len(all_events),
        "matured_episodes": sum(event["outcome"] != "pending" for event in all_events),
        "pending_episodes": sum(event["outcome"] == "pending" for event in all_events),
        "horizon": horizon, "min_sample_size": min_sample_size, "actions": actions,
        "recent": list(reversed(recent[-5:])),
    }


def audit_decision_performance(
    symbol: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    horizon: int = DEFAULT_HORIZON, min_sample_size: int = MIN_SAMPLE_SIZE,
) -> dict:
    rows = db_manager.get_recent_snapshots(symbol, limit=100_000, db_path=db_path)
    audit = audit_decision_rows(rows, horizon=horizon, min_sample_size=min_sample_size)
    audit["symbol"] = symbol
    return audit


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.1f}%"


def _abs_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def build_decision_audit_report(
    symbol: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    horizon: int = DEFAULT_HORIZON,
) -> str:
    audit = audit_decision_performance(symbol, db_path=db_path, horizon=horizon)
    lines = [
        f"🧭 【{symbol} 決策追蹤記分板】",
        f"有決策 {audit['decision_days']} 天，收斂為 {audit['episode_count']} 段；"
        f"已成熟 {audit['matured_episodes']} 段，待觀察 {audit['pending_episodes']} 段。",
        "",
    ]
    if audit["episode_count"] == 0:
        lines.append("尚無決策紀錄；從新版每日快照開始累積後才可評估。")
        return "\n".join(lines)

    for action, stats in audit["actions"].items():
        lines.append(action)
        if stats["sample_size"] == 0:
            lines.append(f"  尚待未來 {horizon} 個交易日資料")
        elif stats["scored_sample_size"] == 0:
            lines.append(
                f"  不計方向勝率｜平均絕對波動 {_abs_pct(stats['avg_abs_return_pct'])}"
                f"（{stats['sample_size']} 段）"
            )
        elif not stats["sufficient_sample"]:
            lines.append(f"  樣本不足（{stats['scored_sample_size']} 段），暫不顯示成功率")
        else:
            lines.append(
                f"  {horizon}D 成功率 {stats['success_rate_pct']:.0f}%"
                f"（{stats['success_count']}/{stats['scored_sample_size']} 段）"
                f"｜平均報酬 {_pct(stats['avg_return_pct'])}"
            )
        lines.append("")

    lines.append(
        "規則：連續相同姿態只算一段；突破／破位與區間判斷可驗證，"
        "觀望與無方向防守只記錄後續波動。"
    )
    lines.append(
        f"少於 {audit['min_sample_size']} 段不報成功率；觀望不會被算成命中，"
        "避免用沒有交易的日子美化績效。"
    )
    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="決策追蹤記分板")
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    args = parser.parse_args()
    print(build_decision_audit_report(args.symbol.upper(), horizon=args.horizon))


if __name__ == "__main__":
    main()
