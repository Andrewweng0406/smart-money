#!/usr/bin/env python3
"""策略追蹤記分板的每日結算工作——把「到期日已到、但還沒結算」的策略建議
抓出來，用當下現貨價當結算價，算出這組策略最後是賺是賠，寫回資料庫。

跟 analyze.py 存進去（save_strategy_recommendation_if_trackable）的寫入端
是一組的兩端：寫入端在「推薦當天」記一筆，這支腳本在「到期之後」把它結算
掉——沒有這支腳本，strategy_recommendations 表只會一直堆積 resolved=0
的紀錄，永遠看不到「這個系統的策略建議實際勝率/損益到底如何」。

用結算日當天的現貨收盤價（data_fetcher.get_spot_price）當結算價的近似值，
不是選擇權到期當天交易所公告的官方結算價（AM/PM settlement）——對月選是
早上結算、對大部分股票週選是收盤結算，兩者可能有小落差，但取得官方結算價
需要額外的資料來源，這裡先用「收盤價」這個容易取得、足夠接近的近似值，
且已在 CLI 輸出/Telegram 摘要裡註明這是近似值。

用法：
    python strategy_resolver.py --symbol TSLA
    python strategy_resolver.py --symbol TSLA --notify   # 結算完發Telegram摘要
    python strategy_resolver.py --symbol TSLA --dry-run  # 只印出會結算哪些，不寫入資料庫
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import data_fetcher
import db_manager
import strategy_tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")


def resolve_pending(
    symbol: str, as_of_date: str | None = None, db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    dry_run: bool = False,
) -> list[dict]:
    """結算某標的所有「到期日已到、還沒結算」的策略建議，回傳這次結算的
    摘要列表（每筆含 strategy_name/outcome/realized_pnl，給 CLI 印出或組
    Telegram 訊息用）。找不到待結算紀錄、抓不到現貨價都不是錯誤，正常回傳
    空list——大部分日子本來就沒有剛好到期的建議。
    """
    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y-%m-%d")

    pending = [r for r in db_manager.get_pending_strategy_recommendations(as_of_date, db_path) if r["symbol"] == symbol]
    if not pending:
        return []

    try:
        settlement_spot = data_fetcher.get_spot_price(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 結算失敗，抓不到現貨價：%s", symbol, exc)
        return []

    resolved_summaries = []
    for record in pending:
        try:
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
                    db_path=db_path,
                )
            resolved_summaries.append({
                "symbol": symbol,
                "strategy_name": record["strategy_name"],
                "expiry_date": record["expiry_date"],
                "outcome": result.outcome,
                "realized_pnl": result.realized_pnl,
                "settlement_spot": settlement_spot,
            })
        except Exception as exc:  # noqa: BLE001
            # 單一筆結算失敗（例如 legs_json 格式異常）不該擋住其他筆繼續結算。
            logger.warning("%s 策略建議 id=%s 結算失敗：%s", symbol, record.get("id"), exc)

    return resolved_summaries


def build_resolution_summary_text(symbol: str, resolved: list[dict]) -> str:
    """把這次結算的結果組成一段文字，給 --notify 用。"""
    lines = [f"📊 【{symbol} 策略追蹤記分板】今日結算"]
    for item in resolved:
        emoji = "✅" if item["outcome"] == "WIN" else "❌"
        lines.append(
            f"{emoji} {item['strategy_name']}（到期 {item['expiry_date']}）："
            f"{item['outcome']}，損益 ${item['realized_pnl']:,.0f}"
            f"（結算現貨價 ${item['settlement_spot']:.2f}，近似值）"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="結算到期的策略建議，更新策略追蹤記分板")
    parser.add_argument("--symbol", required=True, help="標的代號，例如 TSLA")
    parser.add_argument("--notify", action="store_true", help="有結算到東西時發送 Telegram 摘要")
    parser.add_argument("--dry-run", action="store_true", help="只印出會結算哪些，不寫入資料庫")
    args = parser.parse_args()

    resolved = resolve_pending(args.symbol, dry_run=args.dry_run)
    if not resolved:
        logger.info("%s 沒有需要結算的策略建議", args.symbol)
        return

    for item in resolved:
        logger.info(
            "%s %s（到期%s）結算：%s，損益 $%.2f",
            item["symbol"], item["strategy_name"], item["expiry_date"], item["outcome"], item["realized_pnl"],
        )

    if args.notify and not args.dry_run:
        try:
            import telegram_notifier
            telegram_notifier.send_text_report(build_resolution_summary_text(args.symbol, resolved))
        except Exception as exc:  # noqa: BLE001
            logger.warning("結算摘要 Telegram 推播失敗：%s", exc)


if __name__ == "__main__":
    main()
