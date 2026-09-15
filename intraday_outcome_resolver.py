"""盤中訊號結果計算；時間對齊與方向判斷保持純函式，方便合成資料驗證。"""

from __future__ import annotations

from datetime import datetime, timedelta
import statistics
from zoneinfo import ZoneInfo

import db_manager


US_EASTERN = ZoneInfo("America/New_York")
PINNING_MAX_MOVE_PCT = 2.0

_DIRECTION = {
    "call_wall_breach": 1,
    "put_wall_breach": -1,
}

DEFAULT_HORIZONS_MINUTES = (15, 30, 60)
MIN_SAMPLE_SIZE = 5

_KIND_LABELS = {
    "call_wall_breach": "Call Wall 向上穿越",
    "put_wall_breach": "Put Wall 向下穿越",
    "pinning_high": "Pinning 高分",
    "unusual_activity": "異常大單（不判多空）",
}


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate_signal_path(
    event: dict, observations: list[dict], horizon_minutes: int,
) -> dict | None:
    """在指定 horizon 已有觀測時，計算報酬、方向成敗與路徑 MFE/MAE。"""
    entry_spot = (event.get("payload") or {}).get("entry_spot")
    if not entry_spot or horizon_minutes <= 0:
        return None

    detected_et = _parse_datetime(event["detected_at"]).astimezone(US_EASTERN)
    bucket_start = detected_et.replace(
        minute=(detected_et.minute // 15) * 15, second=0, microsecond=0,
    )
    target = bucket_start + timedelta(minutes=horizon_minutes)
    usable = sorted(
        (
            (_parse_datetime(row["observed_at"]), row.get("spot"))
            for row in observations if row.get("observed_at") and row.get("spot") is not None
        ),
        key=lambda item: item[0],
    )
    future = next(((when, spot) for when, spot in usable if when >= target), None)
    if future is None:
        return None

    evaluated_at, future_spot = future
    path = [spot for when, spot in usable if bucket_start <= when <= evaluated_at]
    if not path:
        return None
    raw_returns = [(spot - entry_spot) / entry_spot * 100 for spot in path]
    return_pct = (future_spot - entry_spot) / entry_spot * 100
    kind = event.get("kind")
    direction = _DIRECTION.get(kind)

    if direction is not None:
        directed_path = [value * direction for value in raw_returns]
        success = return_pct * direction > 0
        mfe_pct = max(directed_path)
        mae_pct = min(directed_path)
    elif kind == "pinning_high":
        success = abs(return_pct) <= PINNING_MAX_MOVE_PCT
        mfe_pct = max(abs(value) for value in raw_returns)
        mae_pct = None
    else:
        success = None
        mfe_pct = max(raw_returns)
        mae_pct = min(raw_returns)

    return {
        "horizon_minutes": horizon_minutes,
        "evaluated_at": evaluated_at.isoformat(),
        "future_spot": future_spot,
        "return_pct": return_pct,
        "directional_success": success,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
    }


def resolve_available_outcomes(
    symbol: str,
    db_path=db_manager.DEFAULT_DB_PATH,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_MINUTES,
) -> int:
    """結算已有足夠後續觀測的 horizon，回傳本輪新增或更新的結果數。"""
    observations = db_manager.get_intraday_observations(symbol, limit=10_000, db_path=db_path)
    events = db_manager.get_signal_events(symbol, limit=10_000, db_path=db_path)
    existing = {
        (row["event_id"], row["horizon_minutes"])
        for row in db_manager.get_signal_outcomes(symbol, db_path=db_path)
    }
    resolved = 0
    for event in events:
        for horizon in horizons:
            if (event["id"], horizon) in existing:
                continue
            outcome = evaluate_signal_path(event, observations, horizon)
            if outcome is None:
                continue
            db_manager.save_signal_outcome(
                {**outcome, "event_id": event["id"]}, db_path=db_path,
            )
            resolved += 1
    return resolved


def _mean(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return statistics.mean(values) if values else None


def _regime_stat(rows: list[dict]) -> dict:
    directional = [row for row in rows if row.get("directional_success") is not None]
    return {
        "sample_size": len(rows),
        "success_rate_pct": (
            sum(bool(row["directional_success"]) for row in directional)
            / len(directional) * 100 if directional else None
        ),
    }


def summarize_outcomes(rows: list[dict], min_sample_size: int = MIN_SAMPLE_SIZE) -> dict:
    """依訊號與 horizon 彙總，所有百分比都保留明確樣本數。"""
    grouped: dict[str, dict[int, list[dict]]] = {}
    for row in rows:
        grouped.setdefault(row["kind"], {}).setdefault(row["horizon_minutes"], []).append(row)

    summary = {}
    for kind, horizons in grouped.items():
        summary[kind] = {}
        for horizon, samples in horizons.items():
            directional = [row for row in samples if row.get("directional_success") is not None]
            summary[kind][horizon] = {
                "sample_size": len(samples),
                "sufficient_sample": len(samples) >= min_sample_size,
                "success_rate_pct": (
                    sum(bool(row["directional_success"]) for row in directional)
                    / len(directional) * 100 if directional else None
                ),
                "avg_return_pct": _mean(samples, "return_pct"),
                "avg_mfe_pct": _mean(samples, "mfe_pct"),
                "avg_mae_pct": _mean(samples, "mae_pct"),
                "by_regime": {
                    "negative": _regime_stat([row for row in samples if row.get("negative_gamma") == 1]),
                    "positive": _regime_stat([row for row in samples if row.get("negative_gamma") == 0]),
                },
            }
    return summary


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.1f}%"


def build_intraday_outcome_report(symbol: str, db_path=db_manager.DEFAULT_DB_PATH) -> str:
    """組出 `/signals` 使用的盤中實證區塊。"""
    rows = db_manager.get_signal_outcomes(symbol, db_path=db_path)
    lines = ["📍 盤中訊號實證（15/30/60 分鐘）"]
    if not rows:
        lines.append("尚無已結算樣本，先繼續收集。")
        return "\n".join(lines)

    summary = summarize_outcomes(rows)
    for kind in _KIND_LABELS:
        if kind not in summary:
            continue
        lines.append(_KIND_LABELS[kind])
        for horizon, stat in sorted(summary[kind].items()):
            if not stat["sufficient_sample"]:
                lines.append(f"  {horizon}m：樣本不足（{stat['sample_size']} 筆）")
                continue
            rate = stat["success_rate_pct"]
            rate_text = "" if rate is None else f"｜成立率 {rate:.0f}%"
            lines.append(
                f"  {horizon}m：n={stat['sample_size']}{rate_text}"
                f"｜報酬 {_pct(stat['avg_return_pct'])}"
                f"｜MFE {_pct(stat['avg_mfe_pct'])}｜MAE {_pct(stat['avg_mae_pct'])}"
            )
            for regime, label in (("negative", "負Gamma"), ("positive", "正Gamma")):
                regime_stat = stat["by_regime"][regime]
                if regime_stat["sample_size"] >= MIN_SAMPLE_SIZE and regime_stat["success_rate_pct"] is not None:
                    lines.append(
                        f"    {label}：{regime_stat['success_rate_pct']:.0f}%"
                        f"（n={regime_stat['sample_size']}）"
                    )
    lines.append(f"少於 {MIN_SAMPLE_SIZE} 筆不顯示百分比；異常大單不判定多空成立率。")
    return "\n".join(lines)
