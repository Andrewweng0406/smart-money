#!/usr/bin/env python3
"""歷史勝率與回測引擎——讀取 db_manager 累積的每日快照（history.db），
驗證 Max Pain 與 Gamma Flip 這兩個籌碼面指標的實質統計意義。

跟專案裡其他「量化建議」模組（options_strategy_engine.py）一樣的立場：
這裡的統計是規則式、透明的計算，不是機器學習模型或經過嚴謹樣本外驗證的
交易系統——重點是把「怎麼定義觸及/怎麼定義勝率」攤開講清楚，讓使用者
自己判斷這個統計對某檔標的有沒有參考價值，而不是假裝這是一個黑盒子的
「保證勝率」。樣本數（sample_size）一定會跟著每個統計數字一起回傳，
樣本太小時這些百分比本來就不該被當真。

用法：
    python backtester.py --symbol TSLA
    python backtester.py --symbol TSLA --notify   # 額外推播到 Telegram
"""

from __future__ import annotations

import argparse
import logging
import statistics
from datetime import datetime
from pathlib import Path

import db_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")

# 觸及/回踩 Gamma Flip 的定義門檻：現貨價距離當天算出的 Gamma 翻轉點在
# 這個百分比以內，視為「當天測試了這個關卡」。
GAMMA_FLIP_TOUCH_THRESHOLD_PCT = 1.5


def analyze_max_pain_deviation(symbol: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH) -> dict:
    """比較每週五實際收盤價，跟同一週「週三」（優先）或「週一」算出的 Max
    Pain 偏離幅度。

    Max Pain 理論上代表「做市商避險成本最低、因此有誘因把股價釘住」的價位；
    如果這個假設對某檔標的成立，週五實際收盤應該會傾向收斂到週初算出的
    Max Pain 附近——平均偏離度低、標準差小，代表這個訊號對這檔標的比較
    有參考價值；反之則代表這個假設在這檔標的上不太適用，不該照單全收。
    """
    rows = db_manager.get_recent_snapshots(symbol, limit=100_000, db_path=db_path)
    if not rows:
        return {"symbol": symbol, "sample_size": 0, "mean_deviation_pct": None, "stdev_deviation_pct": None, "weeks": []}

    # 依 ISO 年+週分組，才能把「同一週」的週一/週三/週五配對起來。
    by_week: dict[tuple[int, int], dict[int, dict]] = {}
    for row in rows:
        d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        iso_year, iso_week, iso_weekday = d.isocalendar()  # weekday: 1=Mon ... 5=Fri
        by_week.setdefault((iso_year, iso_week), {})[iso_weekday] = row

    deviations: list[float] = []
    week_details: list[dict] = []
    for (iso_year, iso_week), week_data in sorted(by_week.items()):
        friday = week_data.get(5)
        reference = week_data.get(3) or week_data.get(1)  # 優先用週三（比週一更接近結算、資訊更新鮮）
        if friday is None or reference is None:
            continue

        max_pain = reference["max_pain"]
        actual_close = friday["spot"]
        if not max_pain:
            continue

        deviation_pct = (actual_close - max_pain) / max_pain * 100
        deviations.append(deviation_pct)
        week_details.append({
            "iso_year": iso_year, "iso_week": iso_week,
            "reference_date": reference["date"], "max_pain": max_pain,
            "friday_date": friday["date"], "actual_close": actual_close,
            "deviation_pct": deviation_pct,
        })

    if not deviations:
        return {"symbol": symbol, "sample_size": 0, "mean_deviation_pct": None, "stdev_deviation_pct": None, "weeks": []}

    return {
        "symbol": symbol,
        "sample_size": len(deviations),
        "mean_deviation_pct": statistics.mean(deviations),
        "stdev_deviation_pct": statistics.stdev(deviations) if len(deviations) >= 2 else 0.0,
        "weeks": week_details,
    }


def analyze_gamma_flip_winrate(
    symbol: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    horizons: tuple[int, ...] = (1, 3, 5), touch_threshold_pct: float = GAMMA_FLIP_TOUCH_THRESHOLD_PCT,
) -> dict:
    """統計「現貨價觸及/回踩 Gamma 翻轉點」之後，未來 N 個交易日這個翻轉點
    是否繼續發揮支撐/壓力作用（勝率），以及對應的平均漲跌幅。

    「觸及」定義：現貨價距離當天算出的 Gamma 翻轉點在 touch_threshold_pct
    以內。「勝率（win）」定義：觸及當下現貨價在翻轉點的哪一側（above=之上、
    below=之下），N 天後現貨價是否還停留在同一側——也就是翻轉點是否繼續
    守住「支撐」（below側沒有繼續破底）或「壓力」（above側沒有繼續突破）
    的作用。這是「這一側有沒有守住」的對稱定義，不是預測漲跌方向，也不是
    「买進賺不賺錢」的交易勝率。
    """
    rows = db_manager.get_recent_snapshots(symbol, limit=100_000, db_path=db_path)
    if not rows:
        return {"symbol": symbol, "touch_threshold_pct": touch_threshold_pct, "horizons": {}}

    # get_recent_snapshots 回傳新到舊，這裡要反過來由舊到新排序，
    # 才能用陣列索引找到「未來第N個交易日」（交易日，不是日曆天數）。
    rows_sorted = sorted(rows, key=lambda r: r["date"])

    buckets = {h: {"returns_above": [], "returns_below": [], "wins": 0, "total": 0} for h in horizons}

    for i, row in enumerate(rows_sorted):
        gamma_flip = row["gamma_flip"]
        spot = row["spot"]
        if not gamma_flip or not spot:
            continue

        distance_pct = abs(spot - gamma_flip) / spot * 100
        if distance_pct > touch_threshold_pct:
            continue  # 這天沒有觸及翻轉點，不算一次測試事件

        side = "above" if spot > gamma_flip else "below"

        for h in horizons:
            future_idx = i + h
            if future_idx >= len(rows_sorted):
                continue  # 歷史資料還沒累積到那麼多天，這個horizon暫時算不出來
            future_row = rows_sorted[future_idx]
            future_spot = future_row["spot"]

            held = (side == "above" and future_spot > gamma_flip) or (side == "below" and future_spot < gamma_flip)
            return_pct = (future_spot - spot) / spot * 100

            bucket = buckets[h]
            bucket["total"] += 1
            if held:
                bucket["wins"] += 1
            bucket["returns_above" if side == "above" else "returns_below"].append(return_pct)

    horizon_summary = {}
    for h, bucket in buckets.items():
        total = bucket["total"]
        horizon_summary[h] = {
            "sample_size": total,
            "win_rate_pct": (bucket["wins"] / total * 100) if total > 0 else None,
            "avg_return_pct_above": statistics.mean(bucket["returns_above"]) if bucket["returns_above"] else None,
            "avg_return_pct_below": statistics.mean(bucket["returns_below"]) if bucket["returns_below"] else None,
        }

    return {"symbol": symbol, "touch_threshold_pct": touch_threshold_pct, "horizons": horizon_summary}


def generate_backtest_report(symbol: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH) -> str:
    """組出一份給人看的文字回測報告，可以直接推播到 Telegram 或塞進 Markdown。"""
    max_pain_stats = analyze_max_pain_deviation(symbol, db_path=db_path)
    flip_stats = analyze_gamma_flip_winrate(symbol, db_path=db_path)

    lines = [f"📈 {symbol} 歷史籌碼模型回測報告", "", "## Max Pain 偏離度分析", ""]

    if max_pain_stats["sample_size"] == 0:
        lines.append("樣本數不足（需要至少一週完整的週一或週三 + 週五快照）。")
    else:
        lines.append(f"樣本週數：{max_pain_stats['sample_size']}")
        lines.append(f"平均偏離：{max_pain_stats['mean_deviation_pct']:+.2f}%")
        lines.append(f"標準差：{max_pain_stats['stdev_deviation_pct']:.2f}%")

    lines += ["", "## Gamma Flip 支撐/阻力勝率", ""]
    if not flip_stats["horizons"]:
        lines.append("樣本數不足（尚未累積足夠的歷史快照）。")
    else:
        for h in sorted(flip_stats["horizons"]):
            stat = flip_stats["horizons"][h]
            if stat["sample_size"] == 0:
                lines.append(f"未來{h}天：樣本數不足")
                continue
            win_text = f"{stat['win_rate_pct']:.0f}%" if stat["win_rate_pct"] is not None else "N/A"
            lines.append(f"未來{h}天：守住機率 {win_text}（樣本數 {stat['sample_size']}）")

    lines += ["", "⚠️ 免責聲明：以上統計基於本機累積的歷史快照，樣本可能很小，僅供參考，不構成投資建議。"]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="歷史籌碼模型回測統計報告")
    parser.add_argument("--symbol", default="TSLA", help="標的代號，預設 TSLA")
    parser.add_argument("--notify", action="store_true", help="額外推播報告到 Telegram")
    args = parser.parse_args()

    report_text = generate_backtest_report(args.symbol)
    print(report_text)

    if args.notify:
        import telegram_notifier
        telegram_notifier.send_text_report(report_text)


if __name__ == "__main__":
    main()
