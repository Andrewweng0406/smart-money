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
from datetime import datetime, timezone
from pathlib import Path

import analyze
import data_fetcher
import db_manager
import decision_engine
import risk_gauge

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
    dashboard_dir: Path | None = None, notify: bool = False, is_trading_day: bool | None = None,
) -> dict:
    """對單一標的跑完整流程，回傳一筆摘要 dict（成功或失敗都回傳，呼叫端
    不用另外 try/except——這支函式本身就是每個標的獨立失敗、互不影響的邊界）。

    is_trading_day 沒傳（None）時會自己查一次——多標的迴圈情境建議由呼叫端
    （main()）算好一次傳進來，同一次 watchlist 執行不用每檔標的各打一次
    SPY 查詢。
    """
    try:
        result = analyze.fetch_and_aggregate(symbol, max_expiries=max_expiries, risk_free_rate=risk_free_rate)
    except Exception as exc:  # noqa: BLE001
        logger.error("分析 %s 失敗，本次 watchlist 略過此標的：%s", symbol, exc)
        return {"symbol": symbol, "error": str(exc)}

    if is_trading_day is None:
        is_trading_day = data_fetcher.is_market_trading_day()

    strategy = analyze.compute_strategy_recommendation(symbol, result)

    # 同 analyze.py：只有今天真的是交易日才寫進歷史資料庫/策略追蹤，避免
    # 平日休市日排程照跑，把舊資料當新快照寫進去汙染 backtester 的統計。
    if is_trading_day:
        trading_date_str = data_fetcher.current_trading_date_str()
        try:
            db_manager.save_snapshot(result, trading_date_str)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 寫入歷史資料庫失敗：%s", symbol, exc)
        analyze.save_strategy_recommendation_if_trackable(symbol, strategy, trading_date_str)
        analyze.save_oi_snapshot_if_trading_day(symbol, result, trading_date_str)
    else:
        logger.info("今天不是美股交易日，%s 跳過歷史資料庫寫入與策略追蹤紀錄", symbol)

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

    # 風險計量是加分項：算不出來就留 None，摘要那段直接不顯示，
    # 不能讓它拖垮整份 watchlist。
    try:
        risk = risk_gauge.assess_risk(
            spot=result.spot,
            gamma_flip=result.gamma_flip,
            total_net_gex=result.zero_dte_summary["total_net_gex"],
            zero_dte_share_pct=result.zero_dte_summary["zero_dte_share_pct"],
            iv_skew=result.iv_skew,
            pinning=result.pinning,
            mm_pressure=result.mm_pressure,
            alert=result.alert,
            calendar_warnings=macro_warnings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 風險計量失敗：%s", symbol, exc)
        risk = None

    try:
        decision = decision_engine.build_decision_brief(
            spot=result.spot,
            put_wall=result.put_wall,
            call_wall=result.call_wall,
            gamma_flip=result.gamma_flip,
            total_net_gex=result.zero_dte_summary.get("total_net_gex"),
            oi_data_quality=result.oi_data_quality,
            calendar_warnings=macro_warnings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 決策摘要計算失敗：%s", symbol, exc)
        decision = None

    return {
        "symbol": symbol, "spot": result.spot, "max_pain": result.max_pain,
        "call_wall": result.call_wall, "put_wall": result.put_wall,
        "gamma_flip": result.gamma_flip, "alert": result.alert,
        "strategy_name": strategy.strategy_name if strategy else "N/A",
        "mm_pressure": result.mm_pressure, "macro_warnings": macro_warnings,
        "risk": risk, "oi_data_quality": result.oi_data_quality,
        "decision": decision,
    }


_INTRADAY_PINNING_REGIME_LABEL = {
    "PINNING": "🧲 Pinning 磁吸區間",
    "BREAKOUT": "🚀 Breakout 突破區間",
    "NEUTRAL": "🔄 Neutral 中性觀望",
}


def build_intraday_summary_line(symbol: str, result: analyze.AnalysisResult) -> str:
    """條列單一標的的盤中 GEX + Pinning 結構重點，給開盤後30分鐘（美東
    10:00）摘要用——只挑最關鍵的幾個數字，完整版留給收盤後那份 Markdown
    報告，這份純粹是「開盤後第一眼快照」。
    """
    lines = [f"◆ {symbol}　現貨 ${result.spot:.2f}"]
    lines.append(
        f"  Max Pain ${result.max_pain:.0f}　Call Wall ${result.call_wall:.0f}　"
        f"Put Wall ${result.put_wall:.0f}"
    )
    if result.pinning:
        regime_text = _INTRADAY_PINNING_REGIME_LABEL.get(
            result.pinning["regime"], result.pinning["regime"]
        )
        lines.append(f"  Pinning：{regime_text}（{result.pinning['score']}/100）")
    if result.alert:
        lines.append(f"  {result.alert}")
    return "\n".join(lines)


def build_watch_section(
    symbols: list[str], db_path: Path | str = db_manager.DEFAULT_DB_PATH,
) -> tuple[str, list[int]]:
    """組出「觀察名單」文字區塊，並回傳要標記已送的 event id。

    交付規則就是「每個摘要時間點清空待送佇列」，不需要額外的時間判斷邏輯：
    10:00 執行時佇列裡只會有前一日 16:30 之後累積的項目，16:30 執行時只會有
    當天 10:00 之後累積的項目。delivered_at 是唯一狀態，天然保證每筆只送一次。

    讀取失敗只記警告——觀察名單是加分項，不能讓它拖垮日報本身。
    """
    lines: list[str] = []
    event_ids: list[int] = []

    for symbol in symbols:
        try:
            events = db_manager.get_undelivered_watch_events(symbol, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 讀取觀察名單失敗：%s", symbol, exc)
            continue
        event_ids.extend(event["id"] for event in events)

        # 舊版每 15 分鐘把同一合約的累積 volume 新增成一列；先按穩定簽章
        # 合併，才能讓已存在 Volume 裡的歷史佇列在本版上線後立刻恢復可讀。
        grouped: dict[tuple[str, str], dict] = {}
        repeat_counts: dict[tuple[str, str], int] = {}
        for event in events:
            key = (event["kind"], event.get("signature") or str(event["id"]))
            repeat_counts[key] = repeat_counts.get(key, 0) + 1
            if key not in grouped or event["detected_at"] > grouped[key]["detected_at"]:
                grouped[key] = event

        selected = [event for event in grouped.values() if event["kind"] != "unusual_activity"]
        unusual = sorted(
            (event for event in grouped.values() if event["kind"] == "unusual_activity"),
            key=lambda event: (event.get("payload") or {}).get("ratio") or 0,
            reverse=True,
        )
        selected.extend(unusual[:5])

        for event in selected:
            text = (event.get("payload") or {}).get("text") or event["kind"]
            key = (event["kind"], event.get("signature") or str(event["id"]))
            repeated = repeat_counts[key]
            merged_note = f"；盤中 {repeated} 次掃描已合併" if repeated > 1 else ""
            lines.append(f"• {text}\n  （{event['reason']}{merged_note}）")

    if not lines:
        return "", []

    section = "👀 觀察名單（值得注意，不需立刻動作）\n\n" + "\n".join(lines)
    return section, event_ids


def run_intraday_summary(
    symbols: list[str], max_expiries: int | None, risk_free_rate: float, notify: bool = False,
) -> str:
    """開盤後30分鐘（美東 10:00）觸發的輕量盤中摘要——重新跑一次完整的
    GEX/Pinning 分析（不是重用前一天的快照，開盤後的籌碼結構才是「今天」
    真正的樣子），但刻意跳過歷史資料庫寫入、策略建議、AI評語、圖表/報告
    檔案——那些是收盤後那份「正式」每日紀錄的責任，這裡只是要一份文字
    摘要，避免跟 16:30 的每日流程重複做兩次同樣的昂貴寫入動作。
    """
    lines = [f"🕐 開盤盤中摘要 — {datetime.now():%Y-%m-%d} 10:00 ET", ""]
    for symbol in symbols:
        try:
            result = analyze.fetch_and_aggregate(symbol, max_expiries=max_expiries, risk_free_rate=risk_free_rate)
            lines.append(build_intraday_summary_line(symbol, result))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 盤中摘要分析失敗：%s", symbol, exc)
            lines.append(f"❌ {symbol}：分析失敗（{exc}）")
        lines.append("")

    text = "\n".join(lines).strip()

    watch_text, watch_ids = build_watch_section(symbols)
    if watch_text:
        text = f"{text}\n\n{watch_text}"

    if notify:
        import telegram_notifier
        telegram_notifier.send_text_report(text)
        # 推播成功才標記已送——丟例外時不標記，下個摘要時間點會自動重試。
        if watch_ids:
            db_manager.mark_events_delivered(
                watch_ids, datetime.now(timezone.utc).isoformat(), "intraday_summary",
            )
    return text


def build_watchlist_summary(summaries: list[dict]) -> str:
    """把每檔標的的摘要組成一份文字報告——這是唯一會推播到 Telegram 的內容，
    細節（AI評語、完整策略說明、圖表）留在各自的 daily_report_*.md 裡，
    避免這份總表太長。
    """
    lines = [f"📊 Watchlist 綜合評估報告 — {datetime.now():%Y-%m-%d}", ""]

    # CPI/FOMC 這類市場共同事件在每檔分析都會回傳相同文字；集中到報告頂端
    # 只顯示一次，避免三檔 watchlist 看起來像發生了三個不同事件。
    shared_warnings = list(dict.fromkeys(
        warning
        for row in summaries if "error" not in row
        for warning in (row.get("macro_warnings") or [])
    ))
    for warning in shared_warnings:
        lines.append(warning)
    if shared_warnings:
        lines.append("")

    for row in summaries:
        if "error" in row:
            lines.append(f"❌ {row['symbol']}：分析失敗（{row['error']}）")
            lines.append("")
            continue

        flip_text = f"${row['gamma_flip']:.0f}" if row["gamma_flip"] is not None else "N/A"
        lines.append(f"◆ {row['symbol']}　現貨 ${row['spot']:.2f}")
        oi_quality = row.get("oi_data_quality")
        if oi_quality and not oi_quality.get("usable", True):
            lines.append(
                f"  ⚠️ OI 資料可信度低：{oi_quality.get('reason', '資料不完整')}；"
                "本次 GEX、Wall、PCR 與異常成交只供參考"
            )
        decision = row.get("decision")
        if decision:
            lines.append(
                f"  🧭 決策：{decision['action']}（信心：{decision['confidence']}）"
            )
            lines.append(f"  {decision['summary']}")
            lines.append(f"  ↑ 向上觸發：{decision['upside_trigger']}")
            lines.append(f"  ↓ 向下風險：{decision['downside_trigger']}")
        risk = row.get("risk")
        if risk:
            lines.append(f"  🎯 風險 {risk['risk_score']}/100（{risk['risk_label']}）　{risk['regime_text']}")
            for item in risk["avoid"]:
                lines.append(f"  ⚠️ {item}")
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
        strategy_name = row["strategy_name"]
        decision_blocks_execution = bool(
            decision
            and (decision["confidence"] == "低" or "觀望" in decision["action"])
        )
        if decision_blocks_execution and strategy_name not in ("N/A",) and not strategy_name.startswith("無建議（"):
            lines.append(f"  策略：暫不執行（模型候選：{strategy_name}）")
        elif strategy_name.startswith("無建議（") and strategy_name.endswith("）"):
            candidate = strategy_name.removeprefix("無建議（").removesuffix("）")
            lines.append(f"  目前沒有可執行策略；候選方向：{candidate}（缺少合適履約價或報價）")
        elif strategy_name == "N/A":
            lines.append("  目前沒有可執行策略")
        else:
            lines.append(f"  建議策略：{strategy_name}")
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
    parser.add_argument("--dashboard-dir", default=str(analyze.DEFAULT_DASHBOARD_PATH.parent), help="HTML儀表板輸出目錄，預設專案目錄下的 dashboard/")
    parser.add_argument("--no-dashboard", action="store_true", help="跳過 HTML 儀表板產生")
    parser.add_argument(
        "--intraday-summary", action="store_true",
        help="只執行開盤後30分鐘（美東10:00）的輕量 GEX+Pinning 摘要就結束，不跑完整每日流程",
    )
    parser.add_argument(
        "--intraday-max-expiries", type=int, default=4,
        help="開盤盤中摘要每檔標的最多抓取幾個到期日（預設 4，比完整每日流程的 8 少，降低開盤尖峰時段的 API 負載）",
    )
    args = parser.parse_args()

    symbols = load_watchlist(Path(args.watchlist))

    if args.intraday_summary:
        intraday_max_expiries = None if args.intraday_max_expiries == 0 else args.intraday_max_expiries
        text = run_intraday_summary(
            symbols, max_expiries=intraday_max_expiries, risk_free_rate=args.risk_free_rate, notify=args.notify,
        )
        print(text)
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dashboard_dir = None if args.no_dashboard else Path(args.dashboard_dir)

    max_expiries = None if args.max_expiries == 0 else args.max_expiries
    is_trading_day = data_fetcher.is_market_trading_day()

    summaries = []
    for symbol in symbols:
        logger.info("開始分析 %s ...", symbol)
        summaries.append(run_one_symbol(
            symbol, output_dir=output_dir, max_expiries=max_expiries,
            risk_free_rate=args.risk_free_rate, use_ai=not args.no_ai, dashboard_dir=dashboard_dir,
            notify=args.notify, is_trading_day=is_trading_day,
        ))

    # 結算到期的策略建議（策略追蹤記分板）——這是加分項，跟其他排程步驟一樣
    # 失敗只記警告，不該讓當天的 watchlist 分析連帶失敗。過去這一步需要
    # 另外手動執行 strategy_resolver.py，排程從來沒有真的觸發過，這裡補上
    # 讓它成為每日流程的一部分。
    try:
        import strategy_resolver
        resolved = strategy_resolver.resolve_watchlist(args.watchlist)
        if resolved and args.notify:
            import telegram_notifier
            telegram_notifier.send_text_report(strategy_resolver.build_multi_symbol_summary_text(resolved))
    except Exception as exc:  # noqa: BLE001
        logger.warning("策略追蹤記分板結算失敗：%s", exc)

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

    watch_text, watch_ids = build_watch_section(symbols)
    if watch_text:
        summary_text = f"{summary_text}\n\n{watch_text}"

    date_tag = datetime.now().strftime("%Y%m%d")
    summary_path = output_dir / f"watchlist_summary_{date_tag}.md"
    summary_path.write_text(summary_text, encoding="utf-8")
    logger.info("watchlist 綜合摘要已輸出：%s", summary_path)
    print(summary_text)

    if args.notify:
        import telegram_notifier
        telegram_notifier.send_text_report(summary_text)
        # 推播成功才標記已送——丟例外時不標記，下次會重試。
        if watch_ids:
            db_manager.mark_events_delivered(
                watch_ids, datetime.now(timezone.utc).isoformat(), "daily_report",
            )

    failed = [row["symbol"] for row in summaries if "error" in row]
    if failed and len(failed) == len(symbols):
        # 全部標的都失敗才算整體失敗（可能是 Yahoo Finance 整個斷線）；
        # 部分失敗只代表某幾檔標的當天略過，watchlist 整體仍算跑完了。
        raise SystemExit(1)


if __name__ == "__main__":
    main()
