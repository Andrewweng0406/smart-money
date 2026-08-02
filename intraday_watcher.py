#!/usr/bin/env python3
"""盤中即時異常監控——輕量級的單次檢查（不是長駐迴圈）。

架構決定：這支腳本每次執行只做「一次」檢查就結束，不是自己用
time.sleep(900)寫一個常駐的輪詢迴圈。原因：
1. 長駐 Python process 跑一整個交易日（美股盤前+盤中將近12小時），中途
   當機、記憶體洩漏、網路斷線都會讓監控整個停掉，而且不容易發現；
2. 每次呼叫都是獨立、無狀態的，好測試（不用 mock time.sleep），也好除錯
   （單次執行失敗只影響那一次檢查，不會拖垮後面的檢查）。
「每15分鐘執行一次」這件事交給 launchd 的 StartInterval 機制負責（見
scripts/com.andrewweng.stockgex-intraday.plist），這支腳本只負責回答
「現在這個時間點，有沒有異常」，不負責排程本身。

用法：
    python intraday_watcher.py --symbol TSLA
    python intraday_watcher.py --watchlist watchlist.json --notify
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import data_fetcher
import db_manager
import smart_money

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")

US_EASTERN = ZoneInfo("America/New_York")
PRE_MARKET_OPEN = time(4, 0)   # 盤前 04:00 ET
MARKET_CLOSE = time(16, 0)     # 收盤 16:00 ET

# 異常大單門檻——比 smart_money.py 每日報告用的門檻（volume/oi>=0.5, volume>=100）
# 嚴格很多：盤中只想抓真正巨大的單一事件，不是每天都會觸發的日常雜訊。
UNUSUAL_ACTIVITY_MIN_RATIO = 3.0
UNUSUAL_ACTIVITY_MIN_VOLUME = 3000.0

INTRADAY_ALERT_PREFIX = "🚨 盤中緊急警報"


def is_market_hours(now: datetime | None = None) -> bool:
    """判斷現在是否為美股盤前或盤中時間（週一~週五 04:00~16:00 美東時間）。

    不處理美股假日（感恩節、聖誕節等交易所公休日）——那需要一份完整的
    交易所行事曆，是後續可以再加的功能，目前假日當天執行只會白跑一次
    （抓到的資料會顯示前一個交易日收盤價，不會出現異常判定的假警報，
    只是浪費一次API呼叫，風險可控）。
    """
    now = now.astimezone(US_EASTERN) if now is not None else datetime.now(US_EASTERN)
    if now.weekday() >= 5:  # 週六=5, 週日=6
        return False
    return PRE_MARKET_OPEN <= now.time() <= MARKET_CLOSE


def check_wall_breach(symbol: str, spot: float, db_path: Path | str = db_manager.DEFAULT_DB_PATH) -> str | None:
    """拿最近一次（通常是前一交易日收盤後）算好的 Call Wall / Put Wall 跟
    即時現貨價比較——不重新計算整條期權鏈的GEX（那是「輕量級」監控的重點：
    盤中每15分鐘都要重算全部到期日的GEX成本太高），用昨天算好的牆位當
    參考關卡，現貨價格穿越就示警。
    """
    rows = db_manager.get_recent_snapshots(symbol, limit=1, db_path=db_path)
    if not rows:
        return None  # 還沒有歷史快照可以當參考牆位，優雅跳過

    latest = rows[0]
    call_wall = latest["call_wall"]
    put_wall = latest["put_wall"]

    if call_wall and spot > call_wall:
        return f"{symbol} 現貨 ${spot:.2f} 已突破 Call Wall ${call_wall:.0f}（潛在壓力位失守）"
    if put_wall and spot < put_wall:
        return f"{symbol} 現貨 ${spot:.2f} 已跌破 Put Wall ${put_wall:.0f}（潛在支撐位失守）"
    return None


def check_unusual_activity(symbol: str) -> list[dict]:
    """檢查最近到期日（含0DTE）的期權鏈，找 Volume/OI 比例與絕對成交量都
    達到「盤中巨量」門檻的合約——這比每日報告用的門檻嚴格很多倍，只想抓
    真正罕見的巨量事件。
    """
    expiries = data_fetcher.get_all_expiries(symbol, max_expiries=1)
    if not expiries:
        return []

    legs = data_fetcher.get_option_chain_legs(symbol, expiries[0])
    if not legs:
        return []

    return smart_money.detect_unusual_activity(
        legs, min_volume_oi_ratio=UNUSUAL_ACTIVITY_MIN_RATIO, min_volume=UNUSUAL_ACTIVITY_MIN_VOLUME,
    )


def run_check(symbol: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH) -> dict:
    """對單一標的做一次完整檢查，回傳結構化結果（不管有沒有觸發警示都會
    回傳，方便測試跟記錄；呼叫端自己決定要不要推播）。任何一個子檢查失敗
    都不該讓另一個檢查跟著失敗——盤中監控最忌諱因為一個小問題整個停擺。
    """
    result = {"symbol": symbol, "wall_breach": None, "unusual_activity": [], "spot": None, "error": None}

    try:
        spot = data_fetcher.get_spot_price(symbol)
        result["spot"] = spot
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 抓即時報價失敗：%s", symbol, exc)
        result["error"] = str(exc)
        return result

    try:
        result["wall_breach"] = check_wall_breach(symbol, spot, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 牆位突破檢查失敗：%s", symbol, exc)

    try:
        result["unusual_activity"] = check_unusual_activity(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 異常大單檢查失敗：%s", symbol, exc)

    return result


def build_alert_text(result: dict) -> str | None:
    """把 run_check() 的結果組成一段警示文字；沒有任何異常時回傳 None。"""
    lines = []
    if result["wall_breach"]:
        lines.append(result["wall_breach"])
    for item in result["unusual_activity"]:
        ratio_text = "∞" if item["ratio"] == float("inf") else f"{item['ratio']:.1f}x"
        lines.append(
            f"{result['symbol']} ${item['strike']:.0f} {item['side'].upper()} 出現巨量：成交量 "
            f"{item['volume']:,.0f} 張（OI的 {ratio_text}）"
        )

    if not lines:
        return None
    return f"{INTRADAY_ALERT_PREFIX}\n\n" + "\n".join(lines)


def load_symbols(symbol: str | None, watchlist_path: str | None) -> list[str]:
    if symbol:
        return [symbol]
    path = Path(watchlist_path or "watchlist.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("symbols", [])


def run_watch_cycle(symbols: list[str], notify: bool = False, force: bool = False) -> None:
    """執行一次盤中檢查週期——CLI 的 main() 跟 analyze.py 的 --watch 都呼叫
    這支函式，避免 analyze.py 要重新進入 intraday_watcher.py 自己的
    argparse（那樣 sys.argv 會混到 analyze.py 的參數，解析會出錯）。
    """
    if not force and not is_market_hours():
        logger.info("目前不是美股盤前/盤中時間，略過本次檢查")
        return

    for symbol in symbols:
        result = run_check(symbol)
        if result["error"]:
            continue

        alert_text = build_alert_text(result)
        if alert_text:
            logger.warning(alert_text)
            print(alert_text)
            if notify:
                import telegram_notifier
                telegram_notifier.send_text_report(alert_text)
        else:
            logger.info("%s 目前無異常（現貨 $%.2f）", symbol, result["spot"])


def main() -> None:
    parser = argparse.ArgumentParser(description="盤中即時異常監控（單次檢查）")
    parser.add_argument("--symbol", help="只檢查單一標的；不指定則讀 --watchlist")
    parser.add_argument("--watchlist", default="watchlist.json", help="watchlist JSON 路徑，預設 watchlist.json")
    parser.add_argument("--notify", action="store_true", help="偵測到異常時推播到 Telegram")
    parser.add_argument("--force", action="store_true", help="忽略『是否在交易時間』的檢查，強制執行（測試用）")
    args = parser.parse_args()

    symbols = load_symbols(args.symbol, args.watchlist)
    run_watch_cycle(symbols, notify=args.notify, force=args.force)


if __name__ == "__main__":
    main()
