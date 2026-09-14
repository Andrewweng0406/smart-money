"""盤中訊號結果計算；時間對齊與方向判斷保持純函式，方便合成資料驗證。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db_manager


US_EASTERN = ZoneInfo("America/New_York")
PINNING_MAX_MOVE_PCT = 2.0

_DIRECTION = {
    "call_wall_breach": 1,
    "put_wall_breach": -1,
}

DEFAULT_HORIZONS_MINUTES = (15, 30, 60)


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
