#!/usr/bin/env python3
"""訊號績效審核——把每天存進 history.db 的籌碼訊號，轉成可驗證的統計。

這層跟 backtester.py 的差別：backtester.py 偏「模型元件」回測（Max Pain、
Gamma Flip）；這裡偏 Telegram bot 實戰訊號審核，回答更直接的問題：

- Call Wall 突破後，未來 1/3/5 個交易日有沒有續漲？
- Put Wall 跌破後，未來 1/3/5 個交易日有沒有續跌？
- Gamma Flip 被測試後，關卡有沒有守住？
- Pinning 高分後，價格是否真的維持窄幅？

所有統計都必須附 sample_size。這不是保證勝率，而是幫我們決定哪些 Telegram
訊號值得繼續打擾使用者，哪些應該降權或只進資料庫。
"""

from __future__ import annotations

import argparse
import logging
import statistics
from pathlib import Path

import db_manager

logger = logging.getLogger("options_gex")

DEFAULT_HORIZONS = (1, 3, 5)
GAMMA_FLIP_TOUCH_THRESHOLD_PCT = 1.5
PINNING_HIGH_SCORE_THRESHOLD = 80
PINNING_MAX_MOVE_PCT = 2.0


def _future_row(rows: list[dict], index: int, horizon: int) -> dict | None:
    future_idx = index + horizon
    if future_idx >= len(rows):
        return None
    return rows[future_idx]


def _empty_horizon_stats(horizons: tuple[int, ...]) -> dict[int, dict]:
    return {
        h: {
            "sample_size": 0,
            "success_rate_pct": None,
            "avg_return_pct": None,
            "median_return_pct": None,
        }
        for h in horizons
    }


def _summarize_events(
    events_by_horizon: dict[int, list[dict]], horizons: tuple[int, ...], *, has_success_rate: bool = True,
) -> dict[int, dict]:
    summary = _empty_horizon_stats(horizons)
    for h in horizons:
        events = events_by_horizon[h]
        if not events:
            continue
        returns = [event["return_pct"] for event in events]
        successes = sum(1 for event in events if event["success"]) if has_success_rate else 0
        summary[h] = {
            "sample_size": len(events),
            "success_rate_pct": (successes / len(events) * 100) if has_success_rate else None,
            "avg_return_pct": statistics.mean(returns),
            "median_return_pct": statistics.median(returns),
        }
    return summary


def audit_signal_performance(
    symbol: str,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    gamma_flip_touch_threshold_pct: float = GAMMA_FLIP_TOUCH_THRESHOLD_PCT,
    pinning_high_score_threshold: int = PINNING_HIGH_SCORE_THRESHOLD,
    pinning_max_move_pct: float = PINNING_MAX_MOVE_PCT,
) -> dict:
    """回傳單一標的的訊號審核統計。

    成功定義：
    - call_wall_break: 當天 spot > call_wall，未來 spot > 當天 spot。
    - put_wall_break: 當天 spot < put_wall，未來 spot < 當天 spot。
    - gamma_flip_touch: 當天 spot 距離 gamma_flip <= 門檻，未來仍在同一側。
    - pinning_high: pinning_score >= 門檻，未來絕對漲跌幅 <= pinning_max_move_pct。
    - alert_day: result.alert 非空，僅統計後續平均漲跌，不定義 success_rate。
    """
    rows = db_manager.get_recent_snapshots(symbol, limit=100_000, db_path=db_path)
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    signals = {
        "call_wall_break": {h: [] for h in horizons},
        "put_wall_break": {h: [] for h in horizons},
        "gamma_flip_touch": {h: [] for h in horizons},
        "pinning_high": {h: [] for h in horizons},
        "alert_day": {h: [] for h in horizons},
    }

    for i, row in enumerate(rows_sorted):
        spot = row.get("spot")
        if not spot:
            continue

        event_flags: dict[str, dict] = {}

        call_wall = row.get("call_wall")
        if call_wall and spot > call_wall:
            event_flags["call_wall_break"] = {"direction": "up"}

        put_wall = row.get("put_wall")
        if put_wall and spot < put_wall:
            event_flags["put_wall_break"] = {"direction": "down"}

        gamma_flip = row.get("gamma_flip")
        if gamma_flip:
            distance_pct = abs(spot - gamma_flip) / spot * 100
            if distance_pct <= gamma_flip_touch_threshold_pct:
                event_flags["gamma_flip_touch"] = {
                    "side": "above" if spot > gamma_flip else "below",
                    "level": gamma_flip,
                }

        pinning_score = row.get("pinning_score")
        if pinning_score is not None and pinning_score >= pinning_high_score_threshold:
            event_flags["pinning_high"] = {}

        if row.get("alert"):
            event_flags["alert_day"] = {}

        if not event_flags:
            continue

        for h in horizons:
            future = _future_row(rows_sorted, i, h)
            if future is None or not future.get("spot"):
                continue

            future_spot = future["spot"]
            return_pct = (future_spot - spot) / spot * 100

            if "call_wall_break" in event_flags:
                signals["call_wall_break"][h].append({
                    "date": row["date"], "future_date": future["date"],
                    "return_pct": return_pct, "success": future_spot > spot,
                })
            if "put_wall_break" in event_flags:
                signals["put_wall_break"][h].append({
                    "date": row["date"], "future_date": future["date"],
                    "return_pct": return_pct, "success": future_spot < spot,
                })
            if "gamma_flip_touch" in event_flags:
                gamma_event = event_flags["gamma_flip_touch"]
                level = gamma_event["level"]
                side = gamma_event["side"]
                held = (side == "above" and future_spot > level) or (side == "below" and future_spot < level)
                signals["gamma_flip_touch"][h].append({
                    "date": row["date"], "future_date": future["date"],
                    "return_pct": return_pct, "success": held,
                })
            if "pinning_high" in event_flags:
                signals["pinning_high"][h].append({
                    "date": row["date"], "future_date": future["date"],
                    "return_pct": return_pct,
                    "success": abs(return_pct) <= pinning_max_move_pct,
                })
            if "alert_day" in event_flags:
                signals["alert_day"][h].append({
                    "date": row["date"], "future_date": future["date"],
                    "return_pct": return_pct, "success": None,
                })

    return {
        "symbol": symbol,
        "row_count": len(rows_sorted),
        "horizons": horizons,
        "settings": {
            "gamma_flip_touch_threshold_pct": gamma_flip_touch_threshold_pct,
            "pinning_high_score_threshold": pinning_high_score_threshold,
            "pinning_max_move_pct": pinning_max_move_pct,
        },
        "signals": {
            name: _summarize_events(
                events_by_horizon, horizons, has_success_rate=(name != "alert_day"),
            )
            for name, events_by_horizon in signals.items()
        },
    }


_SIGNAL_LABELS = {
    "call_wall_break": "Call Wall 突破續漲",
    "put_wall_break": "Put Wall 跌破續跌",
    "gamma_flip_touch": "Gamma Flip 關卡守住",
    "pinning_high": "Pinning 高分窄幅",
    "alert_day": "警報日後續漲跌",
}


def _format_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.{digits}f}%"


def _format_rate(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.0f}%"


def build_signal_audit_report(
    symbol: str,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> str:
    audit = audit_signal_performance(symbol, db_path=db_path, horizons=horizons)
    lines = [
        f"🧪 【{symbol} 訊號績效審核】",
        f"歷史快照：{audit['row_count']} 筆",
        "",
    ]

    if audit["row_count"] < 5:
        lines.append("樣本太少，先繼續累積資料；目前不適合解讀勝率。")
        return "\n".join(lines)

    for signal_name, label in _SIGNAL_LABELS.items():
        lines.append(label)
        stats_by_horizon = audit["signals"][signal_name]
        if all(stat["sample_size"] == 0 for stat in stats_by_horizon.values()):
            lines.append("  樣本數不足")
            lines.append("")
            continue

        for h in sorted(stats_by_horizon):
            stat = stats_by_horizon[h]
            if stat["sample_size"] == 0:
                lines.append(f"  {h}D：樣本不足")
                continue
            if signal_name == "alert_day":
                lines.append(
                    f"  {h}D：樣本 {stat['sample_size']}，平均 {_format_pct(stat['avg_return_pct'])}，"
                    f"中位數 {_format_pct(stat['median_return_pct'])}"
                )
            else:
                lines.append(
                    f"  {h}D：成功率 {_format_rate(stat['success_rate_pct'])}（樣本 {stat['sample_size']}），"
                    f"平均 {_format_pct(stat['avg_return_pct'])}"
                )
        lines.append("")

    settings = audit["settings"]
    lines.append(
        "定義：Gamma Flip 距離 "
        f"{settings['gamma_flip_touch_threshold_pct']:.1f}% 內算測試；"
        f"Pinning >= {settings['pinning_high_score_threshold']} 分，"
        f"未來漲跌 <= {settings['pinning_max_move_pct']:.1f}% 算窄幅。"
    )
    lines.append("⚠️ 樣本數小時只當作校準訊號權重，不構成投資建議。")
    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram 實戰訊號績效審核")
    parser.add_argument("--symbol", default="TSLA", help="標的代號，預設 TSLA")
    parser.add_argument("--notify", action="store_true", help="額外推播報告到 Telegram")
    args = parser.parse_args()

    report = build_signal_audit_report(args.symbol)
    print(report)

    if args.notify:
        import telegram_notifier
        telegram_notifier.send_text_report(report)


if __name__ == "__main__":
    main()
