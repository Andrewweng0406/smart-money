#!/usr/bin/env python3
"""多標的 watchlist 分析腳本——讀 watchlist.json，把清單裡每檔標的都跑一次
analyze.py 的完整流程（GEX、Max Pain、Wall、策略建議、寫入歷史資料庫、
存個別報告與圖表），最後彙整成「一份」多標的綜合摘要推播到 Telegram
（不是每檔標的各推一次，避免每天收到一長串轟炸）。

用法：
    python run_watchlist.py
    python run_watchlist.py --watchlist my_watchlist.json --notify
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import analyze
import data_fetcher
import db_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")


def load_watchlist(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    symbols = data.get("symbols", [])
    if not symbols:
        raise RuntimeError(f"{path} 裡沒有任何 symbols")
    return symbols


def run_one_symbol(
    symbol: str, output_dir: Path, max_expiries: int | None, risk_free_rate: float, use_ai: bool,
    dashboard_dir: Path | None = None, notify: bool = False,
) -> dict:
    """對單一標的跑完整流程，回傳一筆摘要 dict（成功或失敗都回傳，呼叫端
    不用另外 try/except——這支函式本身就是每個標的獨立失敗、互不影響的邊界）。
    """
    try:
        result = analyze.fetch_and_aggregate(symbol, max_expiries=max_expiries, risk_free_rate=risk_free_rate)
    except Exception as exc:  # noqa: BLE001
        logger.error("分析 %s 失敗，本次 watchlist 略過此標的：%s", symbol, exc)
        return {"symbol": symbol, "error": str(exc)}

    try:
        db_manager.save_snapshot(result, datetime.now().strftime("%Y-%m-%d"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 寫入歷史資料庫失敗：%s", symbol, exc)

    strategy = analyze.compute_strategy_recommendation(symbol, result)
    macro_warnings = analyze.get_macro_warnings(symbol)

    if notify:
        # LINE極端警報要立刻發，不等整份 watchlist 摘要都跑完——這是比每日
        # Telegram摘要更急迫的第二通道，某幾檔標的觸發就該馬上收到，不用
        # 等清單裡其他標的也分析完。
        analyze.send_line_alert_if_extreme(result)

    ai_commentary = None
    if use_ai:
        import ai_analyst
        ai_commentary = ai_analyst.generate_commentary(
            symbol=symbol, spot=result.spot, max_pain=result.max_pain,
            call_wall=result.call_wall, put_wall=result.put_wall,
            gamma_flip=result.gamma_flip, alert=result.alert,
        )

    date_tag = datetime.now().strftime("%Y%m%d")
    chart_path = output_dir / f"gex_chart_{symbol}_{date_tag}"
    report_path = output_dir / f"daily_report_{symbol}_{date_tag}.md"

    try:
        analyze.build_chart(result, chart_path)
        analyze.build_markdown_report(
            result, report_path, ai_commentary=ai_commentary, strategy=strategy, macro_warnings=macro_warnings,
        )
    except Exception as exc:  # noqa: BLE001
        # 圖表/報告輸出失敗（例如磁碟空間、kaleido 環境問題）不影響其他標的，
        # 但這檔標的的摘要資訊還是有（result 已經算出來了），照樣加進總表。
        logger.warning("%s 圖表/報告輸出失敗：%s", symbol, exc)

    if dashboard_dir is not None:
        try:
            import dashboard_generator
            dashboard_data = analyze.build_dashboard_data(result, ai_commentary, strategy, macro_warnings)
            dashboard_generator.generate_dashboard(dashboard_data, dashboard_dir / f"{symbol}.html")
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s HTML 儀表板產生失敗：%s", symbol, exc)

    return {
        "symbol": symbol, "spot": result.spot, "max_pain": result.max_pain,
        "call_wall": result.call_wall, "put_wall": result.put_wall,
        "gamma_flip": result.gamma_flip, "alert": result.alert,
        "strategy_name": strategy.strategy_name if strategy else "N/A",
        "mm_pressure": result.mm_pressure, "macro_warnings": macro_warnings,
    }


def build_watchlist_summary(summaries: list[dict]) -> str:
    """把每檔標的的摘要組成一份文字報告——這是唯一會推播到 Telegram 的內容，
    細節（AI評語、完整策略說明、圖表）留在各自的 daily_report_*.md 裡，
    避免這份總表太長。
    """
    lines = [f"📊 Watchlist 綜合評估報告 — {datetime.now():%Y-%m-%d}", ""]
    for row in summaries:
        if "error" in row:
            lines.append(f"❌ {row['symbol']}：分析失敗（{row['error']}）")
            lines.append("")
            continue

        for warning in row.get("macro_warnings") or []:
            lines.append(f"  {warning}")

        flip_text = f"${row['gamma_flip']:.0f}" if row["gamma_flip"] is not None else "N/A"
        lines.append(f"◆ {row['symbol']}　現貨 ${row['spot']:.2f}")
        lines.append(
            f"  Max Pain ${row['max_pain']:.0f}　Call Wall ${row['call_wall']:.0f}　"
            f"Put Wall ${row['put_wall']:.0f}　Gamma翻轉點 {flip_text}"
        )
        if row["alert"]:
            lines.append(f"  {row['alert']}")
        if row.get("mm_pressure"):
            pressure = row["mm_pressure"]
            lines.append(f"  莊家收割壓力：{pressure['score']}/100（{pressure['label']}）")
            if pressure.get("is_death_loop_alert"):
                lines.append(f"  {pressure['alert_text']}")
        lines.append(f"  建議策略：{row['strategy_name']}")
        lines.append("")

    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="多標的 GEX watchlist 分析")
    parser.add_argument("--watchlist", default="watchlist.json", help="watchlist JSON 檔案路徑，預設 watchlist.json")
    parser.add_argument("--max-expiries", type=int, default=8, help="每檔標的最多抓取幾個到期日（預設 8）")
    parser.add_argument("--risk-free-rate", type=float, default=0.045, help="無風險利率，預設 0.045")
    parser.add_argument("--output-dir", default="reports", help="報告與圖表輸出目錄，預設 ./reports")
    parser.add_argument("--notify", action="store_true", help="推播多標的綜合摘要到 Telegram")
    parser.add_argument("--no-ai", action="store_true", help="跳過每檔標的的 Claude AI 綜合評語")
    parser.add_argument("--dashboard-dir", default=str(analyze.DEFAULT_DASHBOARD_PATH.parent), help="HTML儀表板輸出目錄，預設 ~/Desktop/stock.agent/dashboard")
    parser.add_argument("--no-dashboard", action="store_true", help="跳過 HTML 儀表板產生")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dashboard_dir = None if args.no_dashboard else Path(args.dashboard_dir)

    symbols = load_watchlist(Path(args.watchlist))
    max_expiries = None if args.max_expiries == 0 else args.max_expiries

    summaries = []
    for symbol in symbols:
        logger.info("開始分析 %s ...", symbol)
        summaries.append(run_one_symbol(
            symbol, output_dir=output_dir, max_expiries=max_expiries,
            risk_free_rate=args.risk_free_rate, use_ai=not args.no_ai, dashboard_dir=dashboard_dir,
            notify=args.notify,
        ))

    # index.html 這個固定路徑（給人直接打開看「目前狀態」用）永遠鏡射清單裡
    # 第一檔標的的儀表板——watchlist 本來就有多檔標的，不可能每檔都對應
    # 同一個固定檔名，用清單裡的第一檔當「主要」標的是最直覺的慣例。
    if dashboard_dir is not None and symbols and "error" not in summaries[0]:
        try:
            primary_dashboard = dashboard_dir / f"{symbols[0]}.html"
            if primary_dashboard.exists():
                shutil.copy(primary_dashboard, dashboard_dir / "index.html")
        except Exception as exc:  # noqa: BLE001
            logger.warning("複製主要標的儀表板到 index.html 失敗：%s", exc)

    summary_text = build_watchlist_summary(summaries)
    date_tag = datetime.now().strftime("%Y%m%d")
    summary_path = output_dir / f"watchlist_summary_{date_tag}.md"
    summary_path.write_text(summary_text, encoding="utf-8")
    logger.info("watchlist 綜合摘要已輸出：%s", summary_path)
    print(summary_text)

    if args.notify:
        import telegram_notifier
        telegram_notifier.send_text_report(summary_text)

    failed = [row["symbol"] for row in summaries if "error" in row]
    if failed and len(failed) == len(symbols):
        # 全部標的都失敗才算整體失敗（可能是 Yahoo Finance 整個斷線）；
        # 部分失敗只代表某幾檔標的當天略過，watchlist 整體仍算跑完了。
        raise SystemExit(1)


if __name__ == "__main__":
    main()
