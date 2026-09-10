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

# 低於這個樣本數就不報百分比。n=1 的「成功率 0%」看起來像結論，實際上
# 沒有任何統計意義——寧可誠實說樣本不足，也不要給一個會被當真的數字。
MIN_SAMPLE_SIZE = 5


def _build_signal_specs(
    gamma_flip_touch_threshold_pct: float,
    pinning_high_score_threshold: int,
    pinning_max_move_pct: float,
) -> dict[str, dict]:
    """每個訊號拆成三件事：誰有資格當樣本、什麼情況算觸發、怎樣算成功。

    把「eligible」跟「trigger」分開是這個模組的核心：eligible 的全體構成
    無條件基準（=「不看訊號、每天都做」的對照組），trigger 的子集才是訊號。
    兩者相減才是訊號真正的加值，否則多頭期間任何訊號都會顯示成有效。
    """
    return {
        "call_wall_break": {
            "eligible": lambda row: bool(row.get("call_wall")),
            "trigger": lambda row: row["spot"] > row["call_wall"],
            "success": lambda row, future_spot: future_spot > row["spot"],
        },
        "put_wall_break": {
            "eligible": lambda row: bool(row.get("put_wall")),
            "trigger": lambda row: row["spot"] < row["put_wall"],
            "success": lambda row, future_spot: future_spot < row["spot"],
        },
        "gamma_flip_touch": {
            # 基準是「所有有 Gamma Flip 的日子」，訊號是「離它 1.5% 以內」。
            #
            # ⚠️ 這個基準有已知的機械性偏誤，解讀時務必留意：離關卡很遠的
            # 日子「守住」幾乎是必然的（根本碰不到），所以基準勝率天生偏高，
            # 靠近關卡的日子勝率天生偏低。因此 gamma_flip_touch 的負超額
            # 「不等於」這個訊號有害，其中有一大部分只是距離造成的。
            # 要真的隔離 gamma 的效果，需要「距離配對」的對照組（拿同樣
            # 距離的隨機價位當基準），那是後續可以再做的改良。
            # 其餘四個訊號沒有這個問題，超額可以直接解讀。
            "eligible": lambda row: bool(row.get("gamma_flip")),
            "trigger": lambda row: (
                abs(row["spot"] - row["gamma_flip"]) / row["spot"] * 100 <= gamma_flip_touch_threshold_pct
            ),
            "success": lambda row, future_spot: (
                future_spot > row["gamma_flip"] if row["spot"] > row["gamma_flip"]
                else future_spot < row["gamma_flip"]
            ),
        },
        "pinning_high": {
            "eligible": lambda row: row.get("pinning_score") is not None,
            "trigger": lambda row: row["pinning_score"] >= pinning_high_score_threshold,
            "success": lambda row, future_spot: (
                abs((future_spot - row["spot"]) / row["spot"] * 100) <= pinning_max_move_pct
            ),
        },
        "alert_day": {
            "eligible": lambda row: True,
            "trigger": lambda row: bool(row.get("alert")),
            "success": None,  # 警報日不定義方向，只看後續漲跌相對基準如何
        },
    }


def _episode_start_indices(trigger_indices: list[int]) -> list[int]:
    """把連續觸發的日子收斂成一段 episode，只保留每段的第一天。

    spot 連續 10 天待在 Call Wall 上方是「一段行情」，不是 10 個獨立事件。
    不去重的話有效樣本會被高估好幾倍，勝率的信賴區間會假性收窄——這是
    用重疊視窗做事件研究最常見的陷阱。
    """
    triggered = set(trigger_indices)
    return [i for i in trigger_indices if (i - 1) not in triggered]


def _collect_events(
    rows: list[dict], indices: list[int], horizon: int, success_fn,
) -> list[dict]:
    """把索引清單轉成「有未來報酬可算」的事件清單。"""
    events = []
    for i in indices:
        future_idx = i + horizon
        if future_idx >= len(rows):
            continue
        row = rows[i]
        future = rows[future_idx]
        future_spot = future.get("spot")
        if not future_spot:
            continue
        events.append({
            "date": row["date"],
            "future_date": future["date"],
            "return_pct": (future_spot - row["spot"]) / row["spot"] * 100,
            "success": success_fn(row, future_spot) if success_fn else None,
        })
    return events


def _empty_stats(trigger_days: int) -> dict:
    return {
        "sample_size": 0,
        "trigger_days": trigger_days,
        "sufficient_sample": False,
        "success_rate_pct": None,
        "avg_return_pct": None,
        "median_return_pct": None,
        "baseline_sample_size": 0,
        "baseline_success_rate_pct": None,
        "baseline_avg_return_pct": None,
        "excess_return_pct": None,
        "edge_pct": None,
    }


def _summarize(
    events: list[dict], baseline_events: list[dict], trigger_days: int, *, has_success_rate: bool,
) -> dict:
    stats = _empty_stats(trigger_days)

    if baseline_events:
        baseline_returns = [e["return_pct"] for e in baseline_events]
        stats["baseline_sample_size"] = len(baseline_events)
        stats["baseline_avg_return_pct"] = statistics.mean(baseline_returns)
        if has_success_rate:
            baseline_hits = sum(1 for e in baseline_events if e["success"])
            stats["baseline_success_rate_pct"] = baseline_hits / len(baseline_events) * 100

    if not events:
        return stats

    returns = [e["return_pct"] for e in events]
    stats["sample_size"] = len(events)
    stats["sufficient_sample"] = len(events) >= MIN_SAMPLE_SIZE
    stats["avg_return_pct"] = statistics.mean(returns)
    stats["median_return_pct"] = statistics.median(returns)

    if has_success_rate:
        stats["success_rate_pct"] = sum(1 for e in events if e["success"]) / len(events) * 100

    if stats["baseline_avg_return_pct"] is not None:
        stats["excess_return_pct"] = stats["avg_return_pct"] - stats["baseline_avg_return_pct"]
    if stats["success_rate_pct"] is not None and stats["baseline_success_rate_pct"] is not None:
        stats["edge_pct"] = stats["success_rate_pct"] - stats["baseline_success_rate_pct"]

    return stats


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
    - alert_day: result.alert 非空，僅統計後續漲跌，不定義 success_rate。

    每個訊號都會同時算出「無條件基準」——把同一套成功定義套用在所有有
    資格的日子上（不管訊號有沒有觸發）。這是判斷訊號有沒有加值的唯一方法：
    標的本身在漲的時候，任何訊號的原始報酬都會是正的，只有超額報酬
    （excess_return_pct）跟超額勝率（edge_pct）接近 0 才會揭穿它是噪音。
    """
    rows = db_manager.get_recent_snapshots(symbol, limit=100_000, db_path=db_path)
    rows_sorted = [row for row in sorted(rows, key=lambda r: r["date"]) if row.get("spot")]

    specs = _build_signal_specs(
        gamma_flip_touch_threshold_pct, pinning_high_score_threshold, pinning_max_move_pct,
    )

    signals: dict[str, dict[int, dict]] = {}
    for name, spec in specs.items():
        has_success_rate = spec["success"] is not None
        eligible_idx = [i for i, row in enumerate(rows_sorted) if spec["eligible"](row)]
        trigger_idx = [i for i in eligible_idx if spec["trigger"](rows_sorted[i])]
        episode_idx = _episode_start_indices(trigger_idx)

        signals[name] = {
            h: _summarize(
                _collect_events(rows_sorted, episode_idx, h, spec["success"]),
                _collect_events(rows_sorted, eligible_idx, h, spec["success"]),
                len(trigger_idx),
                has_success_rate=has_success_rate,
            )
            for h in horizons
        }

    return {
        "symbol": symbol,
        "row_count": len(rows_sorted),
        "horizons": horizons,
        "settings": {
            "gamma_flip_touch_threshold_pct": gamma_flip_touch_threshold_pct,
            "pinning_high_score_threshold": pinning_high_score_threshold,
            "pinning_max_move_pct": pinning_max_move_pct,
            "min_sample_size": MIN_SAMPLE_SIZE,
        },
        "signals": signals,
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
            if not stat["sufficient_sample"]:
                lines.append(
                    f"  {h}D：樣本不足（{stat['sample_size']} 段"
                    f"／觸發 {stat['trigger_days']} 天）"
                )
                continue

            if stat["success_rate_pct"] is None:
                lines.append(
                    f"  {h}D：平均 {_format_pct(stat['avg_return_pct'])}"
                    f"｜基準 {_format_pct(stat['baseline_avg_return_pct'])}"
                    f"｜超額 {_format_pct(stat['excess_return_pct'])}"
                    f"（{stat['sample_size']} 段）"
                )
            else:
                lines.append(
                    f"  {h}D：成功率 {_format_rate(stat['success_rate_pct'])}"
                    f"｜基準 {_format_rate(stat['baseline_success_rate_pct'])}"
                    f"｜超額 {_format_pct(stat['edge_pct'], 0)}"
                    f"（{stat['sample_size']} 段）"
                )
                lines.append(
                    f"       報酬 {_format_pct(stat['avg_return_pct'])}"
                    f"｜基準 {_format_pct(stat['baseline_avg_return_pct'])}"
                    f"｜超額 {_format_pct(stat['excess_return_pct'])}"
                )
        lines.append("")

    settings = audit["settings"]
    lines.append(
        "定義：Gamma Flip 距離 "
        f"{settings['gamma_flip_touch_threshold_pct']:.1f}% 內算測試；"
        f"Pinning >= {settings['pinning_high_score_threshold']} 分，"
        f"未來漲跌 <= {settings['pinning_max_move_pct']:.1f}% 算窄幅。"
    )
    lines.append(
        "「基準」= 不看訊號、每天都做的同期表現；「超額」= 訊號減基準。"
        "超額接近 0 代表這個訊號沒有加值，只是跟著標的的大方向走。"
    )
    lines.append(
        f"連續觸發的日子會收斂成一「段」（episode），少於 {settings['min_sample_size']} 段"
        "不報百分比。"
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
