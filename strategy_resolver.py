#!/usr/bin/env python3
"""策略追蹤記分板的每日結算工作——把「到期日已到、但還沒結算」的策略建議
抓出來，用到期日當天的收盤價當結算價，算出這組策略最後是賺是賠，寫回資料庫。

跟 analyze.py 存進去（save_strategy_recommendation_if_trackable）的寫入端
是一組的兩端：寫入端在「推薦當天」記一筆，這支腳本在「到期之後」把它結算
掉——沒有這支腳本，strategy_recommendations 表只會一直堆積 resolved=0
的紀錄，永遠看不到「這個系統的策略建議實際勝率/損益到底如何」。

用「到期日當天」的收盤價（data_fetcher.get_close_price_on_date）結算，
不是「執行當下」的最新報價——如果這支腳本沒有每天準時執行（漏跑了幾天），
用現在的價格結算幾天前到期的紀錄會有落差，尤其標的在到期日之後才大幅
波動的話，用「現在」價格結算會把根本沒發生過的損益算進記分板。抓不到
到期日當天收盤價（例如資料源缺漏）才退回用「現在」報價當近似值，並在
結果裡標記 approximate=True。這仍然不是選擇權到期當天交易所公告的官方
結算價（AM/PM settlement），但比「執行當下」的報價更接近實際情況。

預設會自動遍歷 watchlist.json 裡的所有標的（跟 run_watchlist.py 的行為
一致），不用每檔標的各自手動執行一次；也可以用 --symbol 只結算單一標的。

用法：
    python strategy_resolver.py                  # 結算 watchlist.json 裡所有標的
    python strategy_resolver.py --symbol TSLA    # 只結算單一標的
    python strategy_resolver.py --notify         # 結算完發一則合併的 Telegram 摘要
    python strategy_resolver.py --dry-run        # 只印出會結算哪些，不寫入資料庫
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import data_fetcher
import db_manager
import strategy_tracker
from options_strategy_engine import OPTIONS_MULTIPLIER

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")


def resolve_pending(
    symbol: str, as_of_date: str | None = None, db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    dry_run: bool = False,
) -> list[dict]:
    """結算某標的所有「到期日已到、還沒結算」的策略建議，回傳這次結算的
    摘要列表（每筆含 strategy_name/outcome/realized_pnl/approximate，給 CLI
    印出或組 Telegram 訊息用）。找不到待結算紀錄不是錯誤，正常回傳空
    list——大部分日子本來就沒有剛好到期的建議。
    """
    if as_of_date is None:
        as_of_date = data_fetcher.current_trading_date_str()

    pending = [r for r in db_manager.get_pending_strategy_recommendations(as_of_date, db_path) if r["symbol"] == symbol]
    if not pending:
        return []

    resolved_summaries = []
    for record in pending:
        try:
            settlement_spot = data_fetcher.get_close_price_on_date(symbol, record["expiry_date"])
            approximate = False
            if settlement_spot is None:
                # 抓不到到期日當天的歷史收盤價，退回用「現在」報價當近似值。
                settlement_spot = data_fetcher.get_spot_price(symbol)
                approximate = True

            legs = json.loads(record["legs_json"])
            result = strategy_tracker.score_outcome(
                legs=legs,
                net_premium=record["net_premium"],
                strategy_type=record["strategy_type"],
                settlement_spot=settlement_spot,
                max_loss=record["max_loss"],
            )
            if not dry_run:
                db_manager.mark_strategy_resolved(
                    recommendation_id=record["id"],
                    settlement_spot=settlement_spot,
                    outcome=result.outcome,
                    realized_pnl=result.realized_pnl,
                    max_loss_hit=result.max_loss_hit,
                    db_path=db_path,
                )
            resolved_summaries.append({
                "symbol": symbol,
                "strategy_name": record["strategy_name"],
                "expiry_date": record["expiry_date"],
                "outcome": result.outcome,
                "realized_pnl": result.realized_pnl,
                "settlement_spot": settlement_spot,
                "approximate": approximate,
            })
        except Exception as exc:  # noqa: BLE001
            # 單一筆結算失敗（例如 legs_json 格式異常、抓不到任何報價）
            # 不該擋住其他筆繼續結算。
            logger.warning("%s 策略建議 id=%s 結算失敗：%s", symbol, record.get("id"), exc)

    return resolved_summaries


def resolve_watchlist(
    watchlist_path: str | Path = "watchlist.json", as_of_date: str | None = None,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH, dry_run: bool = False,
) -> list[dict]:
    """依序結算 watchlist.json 裡每一檔標的的到期策略建議，回傳合併後的
    結算摘要列表。單一標的結算失敗（例如抓不到報價）不影響其他標的繼續
    結算，這是每日排程情境下該有的行為。
    """
    import run_watchlist  # 延遲 import 避免跟 run_watchlist.py 產生循環依賴

    symbols = run_watchlist.load_watchlist(Path(watchlist_path))
    all_resolved: list[dict] = []
    for symbol in symbols:
        try:
            all_resolved.extend(resolve_pending(symbol, as_of_date=as_of_date, db_path=db_path, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 策略結算流程失敗：%s", symbol, exc)
    return all_resolved


def build_resolution_summary_text(symbol: str, resolved: list[dict]) -> str:
    """把這次結算的結果組成一段文字，給 --notify 用。resolved 裡的
    realized_pnl 是 strategy_tracker 的「每股/每口」單位（沒有乘上
    OPTIONS_MULTIPLIER），這裡顯示給人看時要乘回去，才是使用者實際
    會拿到/損失的美元金額（1口=100股）。
    """
    lines = [f"📊 【{symbol} 策略追蹤記分板】今日結算"]
    for item in resolved:
        emoji = "✅" if item["outcome"] == "WIN" else "❌"
        display_pnl = item["realized_pnl"] * OPTIONS_MULTIPLIER
        price_note = "近似值，抓不到到期日收盤價" if item.get("approximate") else "到期日收盤價"
        lines.append(
            f"{emoji} {item['strategy_name']}（到期 {item['expiry_date']}）："
            f"{item['outcome']}，損益 ${display_pnl:,.0f}"
            f"（結算價 ${item['settlement_spot']:.2f}，{price_note}）"
        )
    return "\n".join(lines)


def build_multi_symbol_summary_text(resolved: list[dict]) -> str:
    """把跨多個標的的結算結果組成一段合併摘要，給 watchlist 排程 --notify
    用——避免每檔標的各推一次，符合 run_watchlist.py 已經建立的「一次
    彙整、單則推播」慣例。
    """
    lines = ["📊 【策略追蹤記分板】今日結算"]
    for item in resolved:
        emoji = "✅" if item["outcome"] == "WIN" else "❌"
        display_pnl = item["realized_pnl"] * OPTIONS_MULTIPLIER
        price_note = "近似值" if item.get("approximate") else "到期日收盤價"
        lines.append(
            f"{emoji} {item['symbol']} {item['strategy_name']}（到期 {item['expiry_date']}）："
            f"{item['outcome']}，損益 ${display_pnl:,.0f}（{price_note}）"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="結算到期的策略建議，更新策略追蹤記分板")
    parser.add_argument("--symbol", help="只結算單一標的；不指定則遍歷 --watchlist 裡的所有標的")
    parser.add_argument("--watchlist", default="watchlist.json", help="watchlist JSON 檔案路徑，預設 watchlist.json")
    parser.add_argument("--notify", action="store_true", help="有結算到東西時發送 Telegram 摘要")
    parser.add_argument("--dry-run", action="store_true", help="只印出會結算哪些，不寫入資料庫")
    args = parser.parse_args()

    if args.symbol:
        resolved = resolve_pending(args.symbol, dry_run=args.dry_run)
        summary_text = build_resolution_summary_text(args.symbol, resolved)
    else:
        resolved = resolve_watchlist(args.watchlist, dry_run=args.dry_run)
        summary_text = build_multi_symbol_summary_text(resolved)

    if not resolved:
        logger.info("沒有需要結算的策略建議")
        return

    for item in resolved:
        logger.info(
            "%s %s（到期%s）結算：%s，損益 $%.2f",
            item["symbol"], item["strategy_name"], item["expiry_date"], item["outcome"], item["realized_pnl"],
        )

    if args.notify and not args.dry_run:
        try:
            import telegram_notifier
            telegram_notifier.send_text_report(summary_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("結算摘要 Telegram 推播失敗：%s", exc)


if __name__ == "__main__":
    main()
