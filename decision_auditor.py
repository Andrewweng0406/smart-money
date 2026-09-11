#!/usr/bin/env python3
"""審核系統當時給出的決策姿態，避免只展示無法驗證的即時文字。"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import db_manager


DEFAULT_HORIZON = 1
DEFAULT_HORIZONS = (1, 3, 5)
MIN_SAMPLE_SIZE = 5

_SCORED_ACTIONS = {
    "突破觀察，等待站穩": lambda row, future: future > row["call_wall"],
    "破位風險，優先防守": lambda row, future: future < row["put_wall"],
    "區間上緣，避免追價": lambda row, future: future <= row["call_wall"],
    "區間下緣，等待止跌": lambda row, future: future >= row["put_wall"],
    "區間應對，不追方向": lambda row, future: row["put_wall"] <= future <= row["call_wall"],
}


def evaluate_decision(row: dict, future_spot: float, future_date: str) -> dict:
    """用審核器唯一的規則評估一筆已成熟決策；純計算。"""
    action = row["decision_action"]
    scorer = _SCORED_ACTIONS.get(action)
    success = scorer(row, future_spot) if scorer else None
    return_pct = (future_spot - row["spot"]) / row["spot"] * 100
    return {
        "date": row["date"], "future_date": future_date, "action": action,
        "confidence": row.get("decision_confidence"),
        "outcome": ("confirmed" if success else "invalidated") if scorer else "observed",
        "success": success, "return_pct": return_pct,
    }


def evaluate_decision_path(row: dict, future_rows: list[dict]) -> dict:
    """評估決策後的完整日收盤路徑；先碰到的確認／失效條件不可被終點洗掉。"""
    if not future_rows:
        return {
            "outcome": "pending", "resolved_date": None,
            "max_upside_excursion_pct": None, "max_downside_excursion_pct": None,
        }

    action = row["decision_action"]
    entry_spot = row["spot"]
    returns = [
        (future["spot"] - entry_spot) / entry_spot * 100
        for future in future_rows
    ]
    outcome = "observed" if action not in _SCORED_ACTIONS else "unresolved"
    resolved_date = None

    for future in future_rows:
        spot = future["spot"]
        confirmed = False
        invalidated = False
        if action == "突破觀察，等待站穩":
            confirmed = spot > row["call_wall"]
            gamma_flip = row.get("gamma_flip")
            invalidated = bool(gamma_flip and spot < gamma_flip)
        elif action == "破位風險，優先防守":
            confirmed = spot < row["put_wall"]
            invalidated = spot >= row["put_wall"]
        elif action == "區間上緣，避免追價":
            invalidated = spot > row["call_wall"]
        elif action == "區間下緣，等待止跌":
            invalidated = spot < row["put_wall"]
        elif action == "區間應對，不追方向":
            invalidated = not row["put_wall"] <= spot <= row["call_wall"]

        if confirmed or invalidated:
            outcome = "confirmed_first" if confirmed else "invalidated_first"
            resolved_date = future["date"]
            break

    # 區間判斷必須整段都守住才成立；突破／破位若期限內兩條線都未碰到，
    # 應誠實保留未觸發，不能因最後一天剛好靠近某條線就補判成功。
    if outcome == "unresolved" and action.startswith("區間"):
        outcome = "confirmed_first"
        resolved_date = future_rows[-1]["date"]

    return {
        "outcome": outcome,
        "resolved_date": resolved_date,
        "max_upside_excursion_pct": max(0.0, max(returns)),
        "max_downside_excursion_pct": min(0.0, min(returns)),
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
    paths = [event["path"] for event in matured if event.get("path")]
    return {
        "sample_size": len(matured),
        "pending_count": len(events) - len(matured),
        "scored_sample_size": len(scored),
        "success_count": success_count if scored else None,
        "sufficient_sample": len(scored) >= min_sample_size,
        "success_rate_pct": success_count / len(scored) * 100 if scored else None,
        "avg_return_pct": statistics.mean(returns) if returns else None,
        "avg_abs_return_pct": statistics.mean(abs(value) for value in returns) if returns else None,
        "path_confirmed_count": sum(path["outcome"] == "confirmed_first" for path in paths),
        "path_invalidated_count": sum(path["outcome"] == "invalidated_first" for path in paths),
        "path_unresolved_count": sum(path["outcome"] == "unresolved" for path in paths),
        "avg_max_upside_excursion_pct": (
            statistics.mean(path["max_upside_excursion_pct"] for path in paths)
            if paths else None
        ),
        "avg_max_downside_excursion_pct": (
            statistics.mean(path["max_downside_excursion_pct"] for path in paths)
            if paths else None
        ),
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
            event["path"] = evaluate_decision_path(row, [])
        else:
            future = ordered[future_index]
            event = evaluate_decision(row, future["spot"], future["date"])
            event["path"] = evaluate_decision_path(
                row, ordered[index + 1:future_index + 1],
            )
        events_by_action.setdefault(action, []).append(event)
        recent.append(event)

    actions = {
        action: _summarize_events(events, min_sample_size)
        for action, events in events_by_action.items()
    }
    all_events = [event for events in events_by_action.values() for event in events]
    events_by_confidence: dict[str, list[dict]] = {}
    for event in all_events:
        confidence = event.get("confidence") or "未知"
        events_by_confidence.setdefault(confidence, []).append(event)
    confidence = {
        level: _summarize_events(events, min_sample_size)
        for level, events in events_by_confidence.items()
    }
    return {
        "row_count": len(ordered), "decision_days": decision_days,
        "episode_count": len(all_events),
        "matured_episodes": sum(event["outcome"] != "pending" for event in all_events),
        "pending_episodes": sum(event["outcome"] == "pending" for event in all_events),
        "horizon": horizon, "min_sample_size": min_sample_size, "actions": actions,
        "confidence": confidence,
        "recent": list(reversed(recent[-5:])),
    }


def audit_decision_horizons(
    rows: list[dict], horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    min_sample_size: int = MIN_SAMPLE_SIZE,
) -> dict:
    """一次評估多個交易日視窗；各期限獨立成熟，不能拿 1D 結果冒充 5D。"""
    if not horizons or any(horizon < 1 for horizon in horizons):
        raise ValueError("horizons 必須包含至少一個正整數")
    unique_horizons = tuple(dict.fromkeys(horizons))
    return {
        "horizons": {
            horizon: audit_decision_rows(
                rows, horizon=horizon, min_sample_size=min_sample_size,
            )
            for horizon in unique_horizons
        },
        "min_sample_size": min_sample_size,
    }


def build_decision_evidence(
    rows: list[dict], action: str, horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    min_sample_size: int = MIN_SAMPLE_SIZE,
) -> dict:
    """為今日姿態整理同類歷史證據；純計算，樣本不足時不顯示百分比。"""
    audit = audit_decision_horizons(
        rows, horizons=horizons, min_sample_size=min_sample_size,
    )
    stats_by_horizon = {
        horizon: horizon_audit["actions"].get(action)
        for horizon, horizon_audit in audit["horizons"].items()
    }
    primary_horizon = horizons[0]
    primary = stats_by_horizon.get(primary_horizon)
    scored_sample = primary["scored_sample_size"] if primary else 0
    observed_sample = primary["sample_size"] if primary else 0

    if primary and observed_sample and scored_sample == 0:
        parts = []
        for horizon, stats in stats_by_horizon.items():
            if stats and stats["sample_size"]:
                parts.append(
                    f"{horizon}D 平均絕對波動 {stats['avg_abs_return_pct']:.1f}%"
                    f"（{stats['sample_size']}段）"
                )
            else:
                parts.append(f"{horizon}D 待成熟")
        return {
            "sufficient_sample": False,
            "sample_size": observed_sample,
            "text": "同類姿態不計方向勝率；" + "｜".join(parts),
            "horizons": stats_by_horizon,
        }
    if scored_sample < min_sample_size:
        samples = "；".join(
            f"{horizon}D {(stats['scored_sample_size'] if stats else 0)}/{min_sample_size}段"
            for horizon, stats in stats_by_horizon.items()
        )
        return {
            "sufficient_sample": False,
            "sample_size": scored_sample,
            "text": f"同類歷史樣本不足（{samples}），暫不估計成功率",
            "horizons": stats_by_horizon,
        }

    parts = []
    for horizon, stats in stats_by_horizon.items():
        if stats and stats["sufficient_sample"]:
            parts.append(
                f"{horizon}D {stats['success_rate_pct']:.0f}%"
                f"（{stats['scored_sample_size']}段）"
            )
        elif stats:
            parts.append(f"{horizon}D 樣本不足（{stats['scored_sample_size']}段）")
    path_text = (
        f"{primary_horizon}D路徑 {primary['path_confirmed_count']}確認/"
        f"{primary['path_invalidated_count']}失效/"
        f"{primary['path_unresolved_count']}未觸發"
    )
    return {
        "sufficient_sample": True,
        "sample_size": scored_sample,
        "text": "同類決策：" + "｜".join([*parts, path_text]),
        "horizons": stats_by_horizon,
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
    horizon: int | None = None,
) -> str:
    rows = db_manager.get_recent_snapshots(symbol, limit=100_000, db_path=db_path)
    selected_horizons = (horizon,) if horizon is not None else DEFAULT_HORIZONS
    multi_audit = audit_decision_horizons(rows, horizons=selected_horizons)
    audit = multi_audit["horizons"][selected_horizons[0]]
    lines = [
        f"🧭 【{symbol} 決策追蹤記分板】",
        f"有決策 {audit['decision_days']} 天，收斂為 {audit['episode_count']} 段；"
        f"已成熟 {audit['matured_episodes']} 段，待觀察 {audit['pending_episodes']} 段。",
        "",
    ]
    if audit["episode_count"] == 0:
        lines.append("尚無決策紀錄；從新版每日快照開始累積後才可評估。")
        return "\n".join(lines)

    if horizon is None:
        lines.extend(["多期限驗證（以交易日快照計）", ""])

    for action, stats in audit["actions"].items():
        lines.append(action)
        if horizon is None:
            evidence = build_decision_evidence(
                rows, action, horizons=selected_horizons,
                min_sample_size=audit["min_sample_size"],
            )
            lines.append(f"  {evidence['text']}")
        elif stats["sample_size"] == 0:
            lines.append(f"  尚待未來 {selected_horizons[0]} 個交易日資料")
        elif stats["scored_sample_size"] == 0:
            lines.append(
                f"  不計方向勝率｜平均絕對波動 {_abs_pct(stats['avg_abs_return_pct'])}"
                f"（{stats['sample_size']} 段）"
            )
        elif not stats["sufficient_sample"]:
            lines.append(f"  樣本不足（{stats['scored_sample_size']} 段），暫不顯示成功率")
        else:
            lines.append(
                f"  {selected_horizons[0]}D 成功率 {stats['success_rate_pct']:.0f}%"
                f"（{stats['success_count']}/{stats['scored_sample_size']} 段）"
                f"｜平均報酬 {_pct(stats['avg_return_pct'])}"
            )
        if stats["scored_sample_size"]:
            lines.append(
                f"  期間路徑：先確認 {stats['path_confirmed_count']}｜"
                f"先失效 {stats['path_invalidated_count']}｜"
                f"未觸發 {stats['path_unresolved_count']}"
            )
            lines.append(
                f"  平均最大上行 {_pct(stats['avg_max_upside_excursion_pct'])}｜"
                f"平均最大下行 {_pct(stats['avg_max_downside_excursion_pct'])}"
            )
        lines.append("")

    lines.extend([f"信心校準（{selected_horizons[0]}D）", ""])
    for confidence in ("高", "中", "低", "未知"):
        stats = audit["confidence"].get(confidence)
        if not stats or stats["scored_sample_size"] == 0:
            continue
        if stats["sufficient_sample"]:
            lines.append(
                f"{confidence}：{stats['success_rate_pct']:.0f}%"
                f"（{stats['success_count']}/{stats['scored_sample_size']}段）"
            )
        else:
            lines.append(f"{confidence}：樣本不足（{stats['scored_sample_size']}段）")
    if not any(
        stats["scored_sample_size"] for stats in audit["confidence"].values()
    ):
        lines.append("尚無可計分的信心樣本")
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
    parser.add_argument("--horizon", type=int, default=None)
    args = parser.parse_args()
    print(build_decision_audit_report(args.symbol.upper(), horizon=args.horizon))


if __name__ == "__main__":
    main()
